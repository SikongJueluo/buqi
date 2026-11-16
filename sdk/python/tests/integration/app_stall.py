"""发布端重启场景的订阅端：判定断流，然后等新一轮（新 epoch）数据。

断流判断由本进程按“最后一条有效消息距今是否超过 MAX_GAP”实现；本轮不新增
SDK 自动重连或断流事件。发布端重新运行后 epoch 变化，用来区分重启前后数据。
"""

from __future__ import annotations

import os
import time

import app_common as common
import harness

NAME = os.environ.get("BQ_APP_NAME", "stall")
ENDPOINT = os.environ["BQ_ENDPOINT"]
PRESTAGE_SECONDS = float(os.environ.get("BQ_PRESTAGE_SECONDS", "10"))


def main() -> None:
    harness.emit("app-start", name=NAME, endpoint=ENDPOINT)

    guard = common.InvalidGuard()
    receiver = guard.track(common.Receiver(ENDPOINT))
    measured = common.measure(receiver, PRESTAGE_SECONDS)
    measured.update({"name": NAME, "phase": "measured"})
    harness.write_json("measured.json", measured)
    harness.emit("measure-done", name=NAME, received=measured["stats"]["received"])
    guard.require_clean("发布端重启前的测量阶段")

    epoch = measured["epoch"]
    harness.emit("awaiting-stall", epoch=epoch)
    stalled = common.stall_watch(
        receiver,
        harness.MAX_GAP,
        time.monotonic() + 60,
        "等待发布端停止后的断流",
    )
    harness.emit("stall-detected", gap=stalled["gap"])

    recovered = common.wait_epoch(
        receiver,
        epoch,
        time.monotonic() + 60,
        "等待发布端重新运行后的新数据",
    )
    harness.emit(
        "epoch-recovered",
        epoch=recovered["epoch"],
        tick=recovered["tick"],
        wall=recovered["wall"],
    )
    harness.write_json(
        "final.json",
        {
            "name": NAME,
            "phase": "recovered",
            "old_epoch": epoch,
            "new_epoch": recovered["epoch"],
            "stall_gap": stalled["gap"],
            "recovered_wall": recovered["wall"],
            "invalid": guard.count,
            "errors": guard.errors,
        },
    )
    receiver.close()
    guard.require_clean("发布端重启场景")


if __name__ == "__main__":
    main()
