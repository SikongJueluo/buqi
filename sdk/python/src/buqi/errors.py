"""组合式机器人接口的异常。

异常按发生位置分组：声明与绑定发生在建立 ``Robot`` 组合时，
控制错误发生在能力的使用过程中。执行本身的结果（完成、失败、取消、未知）
不是异常，而是 :class:`buqi.ExecutionResult`。
"""

from __future__ import annotations


class RobotError(Exception):
    """本模块所有公开错误的基类。"""


class DeclarationError(RobotError):
    """``Part`` / ``Robot`` 的类型声明无法解析或不符合契约。"""


class BindingError(RobotError):
    """发现结果与声明不匹配，或显式选择无法解析。"""


class ControlError(RobotError):
    """控制上下文或执行请求被拒绝。"""


class TargetError(ControlError, ValueError):
    """目标数据本身不合法（字段缺失、长度不符、数值非有限等）。"""


class TargetRejected(ControlError):
    """Adapter 拒绝目标：设备不支持、越界或当前模式不允许。"""


class QueueFull(ControlError):
    """控制上下文的等待队列已满；没有动作被静默丢弃。"""


class ControlNotEntered(ControlError):
    """控制上下文尚未进入或已不再持有执行资源，不能提交动作。

    控制入口必须通过 ``async with`` 获取资源；没有进入的对象、已经退出后
    再次使用的对象都不能操作设备。
    """


class ControlClosed(ControlError):
    """控制上下文已经关闭，不能继续提交动作。"""


class ResourceBusy(ControlError):
    """同一执行资源被另一个控制入口持有。"""


class ResourceUnconfirmed(ControlError):
    """资源的上一次使用没有确认结束，不能直接再次获取。"""


class StopUnconfirmed(ControlError):
    """请求结束动作后，Adapter 无法确认设备已经停止。"""


class Disconnected(RobotError):
    """Adapter 与设备的连接已断开或从未建立。"""
