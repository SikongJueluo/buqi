"""持续订阅验收的订阅端：测量固定窗口，然后保持订阅以验证生命周期。

输出：

- ``received.ndjson``：测量窗口内每条有效消息（epoch/tick/wall/mono）；
- ``measured.json``：窗口边界、逐条校验结果与缺失/重复/乱序/最长间隔统计；
- ``heartbeat.ndjson``：测量结束后的每秒计数，供编排器验证其他订阅者不受影响。
"""

from __future__ import annotations

import os
import signal
import time

import app_common as common
import harness

NAME = os.environ.get("BQ_APP_NAME", "steady")
ENDPOINT = os.environ["BQ_ENDPOINT"]
MEASURE_SECONDS = float(os.environ.get("BQ_MEASURE_SECONDS", str(harness.MEASURE_SECONDS)))
LINGER_SECONDS = float(os.environ.get("BQ_LINGER_SECONDS", "60"))

_stop = False


def _request_stop(signum, frame) -> None:
    del signum, frame
    global _stop
    _stop = True


def main() -> None:
    started = time.time()
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    harness.emit("app-start", name=NAME, endpoint=ENDPOINT)

    guard = common.InvalidGuard()
    receiver = guard.track(common.Receiver(ENDPOINT))
    measured = common.measure(receiver, MEASURE_SECONDS)
    measured.update({"name": NAME, "app_start_wall": started, "phase": "measured"})
    harness.write_json("measured.json", measured)
    guard.require_clean("持续订阅测量阶段")
    harness.emit(
        "measure-done",
        name=NAME,
        received=measured["stats"]["received"],
        max_gap=measured["stats"]["max_gap"],
        invalid=measured["invalid"],
    )

    total = measured["stats"]["received"]
    deadline = time.monotonic() + LINGER_SECONDS
    last_heartbeat = 0.0
    while not _stop and time.monotonic() < deadline:
        record = receiver.poll()
        if record is not None:
            total += 1
            harness.append("received.ndjson", record)

        now = time.monotonic()
        if now - last_heartbeat >= 1.0:
            last_heartbeat = now
            harness.append(
                "heartbeat.ndjson",
                {
                    "wall": time.time(),
                    "mono": now,
                    "received_total": total,
                    "last_valid_wall": receiver.last_valid_wall,
                },
            )
        time.sleep(0.002)

    harness.write_json(
        "final.json",
        {
            "name": NAME,
            "phase": "stopped",
            "received_total": total,
            "invalid": guard.count,
            "stopped_wall": time.time(),
        },
    )
    harness.emit("app-stop", name=NAME, received_total=total, invalid=guard.count)
    receiver.close()
    guard.require_clean("持续订阅场景")


if __name__ == "__main__":
    main()
