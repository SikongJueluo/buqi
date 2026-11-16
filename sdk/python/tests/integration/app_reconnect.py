"""网关重启场景的订阅端：显式关闭旧 Session，重建连接与订阅并恢复接收。

网关停止后本进程按最后一条有效消息判定断流，显式关闭旧 Session，然后按固定
间隔重试 :func:`buqi.connect`；重建成功后重新订阅，并在期限内收到后续数据。
本轮不新增 SDK 自动重连机制，重连逻辑属于验收程序。
"""

from __future__ import annotations

import os
import time

import app_common as common
import buqi
import harness

NAME = os.environ.get("BQ_APP_NAME", "reconnect")
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
    guard.require_clean("网关重启前的测量阶段")

    epoch = measured["epoch"]
    harness.emit("awaiting-gateway-stop", epoch=epoch)
    lost = common.stall_watch(
        receiver,
        harness.MAX_GAP,
        time.monotonic() + 60,
        "等待网关停止后的断流",
    )
    harness.emit("connection-lost", gap=lost["gap"])

    try:
        receiver.close()
    except Exception as error:  # 会话已死时关闭可能失败；记录而不掩盖
        harness.emit("session-close-error", error=f"{type(error).__name__}: {error}")
    harness.emit("session-closed")

    deadline = time.monotonic() + 60
    session = None
    attempts = 0
    while session is None:
        attempts += 1
        try:
            session = buqi.connect(ENDPOINT)
        except Exception:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"网关重启后重连失败（尝试 {attempts} 次）") from None
            time.sleep(1.0)
    harness.emit("reconnected", attempts=attempts)

    receiver = guard.track(common.Receiver(session=session))
    first = common.wait_first(
        receiver,
        time.monotonic() + harness.RECOVERY_DEADLINE,
        "网关重启后等待数据",
    )
    if first["epoch"] != epoch:
        raise AssertionError(f"网关重启引入了新的发布 epoch：{epoch} -> {first['epoch']}")
    harness.emit("recovered", epoch=first["epoch"], tick=first["tick"], wall=first["wall"])
    harness.write_json(
        "final.json",
        {
            "name": NAME,
            "phase": "recovered",
            "epoch": first["epoch"],
            "tick": first["tick"],
            "recovered_wall": first["wall"],
            "invalid": guard.count,
            "errors": guard.errors,
        },
    )
    receiver.close()
    guard.require_clean("网关重启场景")


if __name__ == "__main__":
    main()
