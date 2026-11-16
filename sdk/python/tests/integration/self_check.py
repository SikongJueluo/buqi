"""容器内自检：容器侧逻辑的边界条件。

这些检查依赖官方宇树消息类型，只能在已安装 SDK 的 Python 3.12 镜像里运行；
``scripts/integration-acceptance.sh`` 在跑场景前执行本文件，失败即中止验收。

覆盖：

- 非有限值（NaN）必须被逐条字段校验拒绝；
- 测量窗口末尾的静默时间必须计入最长接收间隔；
- 生命周期场景累计的解码失败（含重建订阅后的 Receiver）必须让检查失败。
"""

from __future__ import annotations

import os
import tempfile
import time

import g1_state
import app_common as common


def check_nan_rejected() -> None:
    message = g1_state.build(epoch=7, tick=3)
    for motor in message.motor_state:
        motor.q = float("nan")
    payload = bytes(message.serialize())

    try:
        g1_state.decode_and_validate(payload)
    except ValueError:
        print("PASS NaN 电机角度被拒绝")
        return
    raise AssertionError("NaN 电机角度通过了字段校验")


def check_valid_message_accepted() -> None:
    message = g1_state.build(epoch=7, tick=3)
    epoch, tick = g1_state.decode_and_validate(bytes(message.serialize()))
    assert (epoch, tick) == (7, 3), (epoch, tick)
    print("PASS 有效消息仍按约定字段解码")


def check_window_tail_counts_as_gap() -> None:
    records = [
        {"epoch": 1, "tick": index, "mono": index * 0.1, "wall": 0.0}
        for index in range(576)
    ]
    summary = common.summarize(records, window_end_mono=60.0)
    if summary["tail_gap"] < 2.4 or summary["max_gap"] < 2.4:
        raise AssertionError(f"窗口末尾静默未计入：{summary}")
    print(f"PASS 窗口末尾静默计入最长间隔（{summary['tail_gap']:.2f} 秒）")


def check_measure_tail_counts_as_gap() -> None:
    """用真实 measure 复现：窗口末尾断流必须进入统计。"""
    os.environ["BQ_RUN_DIR"] = tempfile.mkdtemp(prefix="buqi-selfcheck-")

    class ShortReceiver:
        def __init__(self) -> None:
            self.invalid = 0
            self.invalid_error = None
            self.last_valid_wall = None
            self.last_valid_mono = None
            self.started = None
            self.tick = 0

        def poll(self):
            now = time.monotonic()
            if self.started is None:
                self.started = now
            if now - self.started < 1.0:
                self.last_valid_wall = time.time()
                self.last_valid_mono = now
                self.tick += 1
                return {
                    "epoch": 1,
                    "tick": self.tick,
                    "wall": self.last_valid_wall,
                    "mono": now,
                }
            time.sleep(0.001)
            return None

    summary = common.measure(ShortReceiver(), 3.0)["stats"]
    if summary["tail_gap"] < 1.9 or summary["max_gap"] < 1.9:
        raise AssertionError(f"窗口末尾断流未计入：{summary}")
    print(f"PASS measure 将窗口末尾静默计入最长间隔（{summary['tail_gap']:.2f} 秒）")


def check_invalid_guard() -> None:
    guard = common.InvalidGuard()
    first = type("FakeReceiver", (), {"invalid": 0, "invalid_error": None})()
    second = type("FakeReceiver", (), {"invalid": 2, "invalid_error": "ValueError: nan"})()
    guard.track(first)
    guard.require_clean("阶段一")
    guard.track(second)
    if guard.count != 2:
        raise AssertionError(f"累计计数错误：{guard.count}")

    try:
        guard.require_clean("阶段二")
    except AssertionError as error:
        print(f"PASS 重建后的失败进入判定（{error}）")
        return
    raise AssertionError("累计的解码失败没有让检查失败")


def check_lifecycle_main_fails_on_invalid() -> None:
    """用真实的 measure/main 复现注入解码失败：场景必须以非零结果失败。"""
    os.environ["BQ_ENDPOINT"] = "tcp/127.0.0.1:1"
    os.environ["BQ_RUN_DIR"] = tempfile.mkdtemp(prefix="buqi-selfcheck-")
    os.environ["BQ_PRESTAGE_SECONDS"] = "0.2"

    import app_stall

    class FakeReceiver:
        def __init__(self, *args, **kwargs) -> None:
            self.invalid = 1
            self.invalid_error = "ValueError: nan 电机角度"
            self.last_valid_wall = None
            self.last_valid_mono = None
            self.sent = False

        def poll(self):
            if not self.sent:
                self.sent = True
                self.last_valid_wall = time.time()
                self.last_valid_mono = time.monotonic()
                return {
                    "epoch": 1,
                    "tick": 0,
                    "wall": self.last_valid_wall,
                    "mono": self.last_valid_mono,
                }
            time.sleep(0.001)
            return None

        def close(self) -> None:
            pass

    original = app_stall.common.Receiver
    app_stall.common.Receiver = FakeReceiver
    try:
        app_stall.main()
    except AssertionError as error:
        print(f"PASS 生命周期场景因累计解码失败而失败（{error}）")
    else:
        raise AssertionError("注入解码失败后 app_stall.main 仍正常完成")
    finally:
        app_stall.common.Receiver = original


if __name__ == "__main__":
    check_nan_rejected()
    check_valid_message_accepted()
    check_window_tail_counts_as_gap()
    check_measure_tail_counts_as_gap()
    check_invalid_guard()
    check_lifecycle_main_fails_on_invalid()
