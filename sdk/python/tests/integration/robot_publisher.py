"""模拟 G1 状态发布端（机器人侧容器）。

用官方宇树 SDK 的 ``ChannelPublisher`` 发布 ``unitree_hg.msg.dds_.LowState_`` 到
``rt/lowstate``，10 Hz 持续发布；只生成状态，不做动力学或运动控制。

发布记录写入 ``published.ndjson``，供编排器按同一测量窗口对齐实际发布数量。
"""

from __future__ import annotations

import time

import g1_state
import harness

from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelPublisher
from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_


def main() -> None:
    epoch = g1_state.new_epoch()
    # 官方入口失败时直接抛异常；它没有返回值，不能按布尔结果判断。
    ChannelFactoryInitialize(harness.DOMAIN_ID)

    publisher = ChannelPublisher(harness.TOPIC, LowState_)
    publisher.Init()
    harness.emit(
        "publisher-start",
        epoch=epoch,
        topic=harness.TOPIC,
        domain=harness.DOMAIN_ID,
        rate_hz=harness.RATE_HZ,
    )

    period = 1.0 / harness.RATE_HZ
    tick = 0
    next_frame = time.monotonic()
    try:
        while True:
            message = g1_state.build(epoch, tick)
            ok = publisher.Write(message)
            harness.append(
                "published.ndjson",
                {
                    "epoch": epoch,
                    "tick": tick,
                    "wall": time.time(),
                    "ok": bool(ok),
                },
            )
            tick += 1

            next_frame += period
            delay = next_frame - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                # 落后时不补发，避免用突发掩盖真实发布速率。
                next_frame = time.monotonic()
    except KeyboardInterrupt:
        pass
    finally:
        publisher.Close()
        harness.emit("publisher-stop", epoch=epoch, tick=tick)


if __name__ == "__main__":
    main()
