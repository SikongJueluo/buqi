"""重新订阅场景的订阅端：关闭订阅、确认静默、重建后收后续数据。

验证“关闭完成后的旧订阅不再交付新消息，重建后收后续数据”：
关闭后等待静默期，再建立新的 Session 与订阅，首条消息的 tick 必须大于关闭前
已收到的最大 tick，说明拿到的是关闭之后发布的数据，而不是历史缓存。
"""

from __future__ import annotations

import os
import time

import app_common as common
import harness

NAME = os.environ.get("BQ_APP_NAME", "resubscribe")
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
    guard.require_clean("重新订阅前的测量阶段")

    records = harness.read_ndjson("received.ndjson")
    last_tick = max(record["tick"] for record in records)
    epoch = measured["epoch"]

    closed_subscriber = receiver.subscriber
    receiver.close()
    harness.emit("subscription-closed", epoch=epoch, last_tick=last_tick)
    time.sleep(harness.SILENCE_SECONDS)

    # 关闭完成后，旧订阅不能再交付消息：取样本必须失败或返回空。
    try:
        stale = closed_subscriber.try_recv()
    except Exception as error:
        harness.emit("closed-subscription-rejects", error=type(error).__name__)
    else:
        if stale is not None:
            raise AssertionError("关闭后的旧订阅仍交付了消息")
        harness.emit("closed-subscription-silent")
    harness.emit("silence-confirmed", seconds=harness.SILENCE_SECONDS)

    receiver = guard.track(common.Receiver(ENDPOINT))
    first = common.wait_first(
        receiver,
        time.monotonic() + harness.RECOVERY_DEADLINE,
        "重建订阅后等待数据",
    )
    if first["epoch"] != epoch:
        raise AssertionError(f"重建后 epoch 变化：{epoch} -> {first['epoch']}")
    if first["tick"] <= last_tick:
        raise AssertionError(f"重建后收到旧数据：tick={first['tick']} <= {last_tick}")

    harness.emit("resubscribed", epoch=first["epoch"], tick=first["tick"])
    harness.write_json(
        "final.json",
        {
            "name": NAME,
            "phase": "resubscribed",
            "epoch": first["epoch"],
            "last_tick_before_close": last_tick,
            "first_tick_after_reopen": first["tick"],
            "invalid": guard.count,
            "errors": guard.errors,
        },
    )
    receiver.close()
    guard.require_clean("重新订阅阶段")


if __name__ == "__main__":
    main()
