"""G1 ``unitree_hg.msg.dds_.LowState_`` 的确定性生成规则与校验规则。

模拟端只用官方宇树 SDK 的 ChannelPublisher 发布状态，不做动力学或运动控制。
为了让订阅端能区分“解码正确”“丢包”“重启前后数据”，约定字段按固定规则生成：

- ``tick``：本次发布进程内从 0 开始，每帧 +1；
- ``reserve[0]``：本次发布进程的 epoch，取进程启动时的 Unix 秒数，用来区分重启前后；
- ``imu_state.rpy``：``[tick, epoch % 65536, 0.5]``；
- ``motor_state[j].q``：``j + tick * 0.001``；
- 其余字段保持官方默认构造函数的值。

订阅端逐条检查这些字段；任何一条不符都算解码错误，而不是丢包统计。
"""

from __future__ import annotations

import math
import time

from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowState_
from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_

from harness import CDR_LE_HEADER

# float32 能精确表示 epoch 的低 16 位；高位放在 uint32 的 reserve[0] 中。
_EPOCH_MODULUS = 1 << 16

_RPY_LAST = 0.5
_MOTOR_SCALE = 0.001

# float32 在 q 的数量级上约 1e-6 的绝对误差；留出两个数量级余量。
_Q_TOLERANCE = 2e-4


def new_epoch() -> int:
    """每次发布进程启动取新 epoch；同一次运行内的链路使用同一 epoch。"""
    return int(time.time()) % (1 << 32)


def epoch_modulus(epoch: int) -> float:
    return float(epoch % _EPOCH_MODULUS)


def build(epoch: int, tick: int) -> LowState_:
    """按约定规则构造一帧 G1 状态。"""
    message = unitree_hg_msg_dds__LowState_()
    message.tick = tick
    message.reserve[0] = epoch

    message.imu_state.rpy[0] = float(tick)
    message.imu_state.rpy[1] = epoch_modulus(epoch)
    message.imu_state.rpy[2] = _RPY_LAST

    for index, motor in enumerate(message.motor_state):
        motor.q = index + tick * _MOTOR_SCALE

    return message


def decode(payload: bytes) -> LowState_:
    """从 Zenoh 载荷解码官方消息类型。

    zenoh-plugin-dds 转发的是完整 RTPS SerializedPayload，前面有 4 字节表示头；
    这里先校验表示头，再交给官方 ``IdlStruct.deserialize``，不手工替换成简化类型。
    """
    if payload[:4] != CDR_LE_HEADER:
        raise ValueError(f"不是 little-endian CDR 载荷：{payload[:4].hex()}")
    # cyclonedds 的 IdlStruct 序列化/反序列化都带这 4 字节表示头，不额外剥离。
    return LowState_.deserialize(payload)


def _require_exact(value: float, expected: float, field: str) -> None:
    """精确比较约定字段；NaN/Inf 必须显式拒绝，不能靠比较结果碰巧为真。"""
    if not math.isfinite(value) or value != expected:
        raise ValueError(f"{field}={value} 与期望 {expected} 不符")


def _require_close(value: float, expected: float, field: str) -> None:
    """容差比较约定字段；非有限值与超差同样算无效。"""
    if not math.isfinite(value) or abs(value - expected) > _Q_TOLERANCE:
        raise ValueError(f"{field}={value} 与期望 {expected} 不符")


def validate(message: LowState_) -> tuple[int, int]:
    """校验约定字段，返回 ``(epoch, tick)``；不符时抛 ``ValueError``。"""
    epoch = int(message.reserve[0])
    tick = int(message.tick)

    _require_exact(message.imu_state.rpy[0], float(tick), "rpy[0]")
    _require_exact(message.imu_state.rpy[1], epoch_modulus(epoch), "rpy[1]")
    _require_exact(message.imu_state.rpy[2], _RPY_LAST, "rpy[2]")

    for index, motor in enumerate(message.motor_state):
        _require_close(
            motor.q,
            index + tick * _MOTOR_SCALE,
            f"motor_state[{index}].q",
        )

    return epoch, tick


def decode_and_validate(payload: bytes) -> tuple[int, int]:
    return validate(decode(payload))
