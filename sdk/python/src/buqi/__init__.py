"""buqi —— 机器人统一接口 SDK。

通过 Zenoh 连接网关（buqi-server：Zenoh Router + DDS 插件）访问机器人能力，
内部以可插拔 Adapter 对接不同机器人（如 Unitree G1 经 DDS 桥接）。

首版提供最小连接入口 :func:`buqi.connect`，返回官方 ``eclipse-zenoh``
Session，以及相机图片通道 :func:`buqi.snapshot` / :func:`buqi.frames`。
图片默认交付未解码的 JPEG 字节；``Robot`` 统一接口尚未实现。
"""

from ._connect import connect
from .media import (
    CameraError,
    CameraFrame,
    CameraNoFrameError,
    CameraProtocolError,
    CameraStaleFrameError,
    CameraTimeout,
    FrameMetadata,
    FrameStream,
    frames,
    snapshot,
)

__version__ = "0.1.0"

__all__ = [
    "connect",
    "snapshot",
    "frames",
    "CameraFrame",
    "FrameMetadata",
    "FrameStream",
    "CameraError",
    "CameraTimeout",
    "CameraNoFrameError",
    "CameraStaleFrameError",
    "CameraProtocolError",
]
