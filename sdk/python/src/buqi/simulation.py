"""纯内存模拟 Adapter，用于示例与测试，不连接任何设备。

它实现 :class:`buqi.Adapter` 契约：设备与部件由配置描述，能力后端在
内存中运行，轨迹在 ``duration`` 秒后“完成”。这验证组合、绑定和执行语义，
不代表真实机器人、真实网络或实时性。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .composition import Device, PartOffer, PartSession
from .control import (
    Camera,
    Capability,
    CommandOutcome,
    CommandState,
    JointTrajectory,
    JointWaypoint,
    MotorControl,
    MotorTarget,
    Resource,
    Trajectory,
)
from .errors import BindingError, Disconnected, TargetRejected
from .media import CameraFrame, CameraNoFrameError, FrameMetadata

_JPEG = b"\xff\xd8\xff\xe0buqi-simulated-jpeg\xff\xd9"


@dataclass(frozen=True)
class SimulatedPart:
    """一个模拟部件的配置。

    ``trajectory`` 与 ``motor`` 默认共享 ``<part_id>/joints`` 资源，因此两种
    控制互斥；``resources`` 可覆盖资源名，用于模拟跨部件共享；``fail`` 让轨迹
    在开始执行后失败，用于验证队列停止。
    """

    part_id: str
    joints: tuple[str, ...] = ()
    trajectory: bool = True
    motor: bool = True
    camera: bool = False
    camera_available: bool = True
    lower: float = -3.0
    upper: float = 3.0
    fail: bool = False
    resources: tuple[str, ...] = ()


@dataclass(frozen=True)
class SimulatedDevice:
    device_id: str
    parts: tuple[SimulatedPart, ...]


class SimulatedAdapter:
    """内存 Adapter；``disconnect()`` 模拟连接中断。"""

    def __init__(
        self,
        *devices: SimulatedDevice,
        duration: float = 0.01,
        stop_confirmed: bool = True,
        max_valid_for: float = 0.5,
    ) -> None:
        if duration < 0:
            raise ValueError("duration 不能为负")
        if max_valid_for <= 0:
            raise ValueError("max_valid_for 必须为正数")
        self._devices = devices
        self.duration = duration
        self.stop_confirmed = stop_confirmed
        self.max_valid_for = max_valid_for
        self.started: list[Trajectory] = []
        self.motor_targets: list[MotorTarget] = []
        self.stops = 0
        self._disconnected = False
        self._resources: dict[str, Resource] = {}
        self._backends: list[
            _TrajectoryBackend | _MotorBackend | _CameraBackend
        ] = []

    @property
    def disconnected(self) -> bool:
        return self._disconnected

    async def discover(self) -> tuple[Device, ...]:
        self._require_connected()
        return tuple(self._describe(device) for device in self._devices)

    async def open_part(self, offer: PartOffer) -> PartSession:
        self._require_connected()
        _, part = self._configuration(offer.device_id, offer.part_id)
        capabilities: dict[type[Capability], object] = {}
        if part.trajectory:
            backend = _TrajectoryBackend(self, part)
            self._backends.append(backend)
            capabilities[JointTrajectory] = backend
        if part.motor:
            backend = _MotorBackend(self, part)
            self._backends.append(backend)
            capabilities[MotorControl] = backend
        if part.camera:
            backend = _CameraBackend(self, part)
            self._backends.append(backend)
            capabilities[Camera] = backend
        return PartSession(capabilities=capabilities)

    def disconnect(self) -> None:
        """模拟断连：唤醒所有执行并标记为结果未知。"""
        self._disconnected = True
        for backend in self._backends:
            backend._disconnect()

    def _require_connected(self) -> None:
        if self._disconnected:
            raise Disconnected("模拟设备已断连；重新连接需要重新发现和绑定")

    def _resource(self, device_id: str, name: str) -> Resource:
        key = f"{device_id}#{name}"
        resource = self._resources.get(key)
        if resource is None:
            resource = Resource(key)
            self._resources[key] = resource
        return resource

    def _configuration(
        self, device_id: str, part_id: str
    ) -> tuple[SimulatedDevice, SimulatedPart]:
        for device in self._devices:
            if device.device_id != device_id:
                continue
            for part in device.parts:
                if part.part_id == part_id:
                    return device, part
        raise BindingError(f"模拟 Adapter 没有 {device_id}/{part_id}")

    def _describe(self, device: SimulatedDevice) -> Device:
        if not device.device_id:
            raise ValueError("模拟设备 ID 不能为空")
        if not device.parts:
            raise ValueError(f"模拟设备 {device.device_id} 没有部件")
        offers = []
        for part in device.parts:
            capabilities: set[type[Capability]] = set()
            resources: dict[type[Capability], tuple[Resource, ...]] = {}
            resource_names = part.resources or (f"{part.part_id}/joints",)
            part_resources = tuple(
                self._resource(device.device_id, name) for name in resource_names
            )
            if part.trajectory:
                capabilities.add(JointTrajectory)
                resources[JointTrajectory] = part_resources
            if part.motor:
                capabilities.add(MotorControl)
                resources[MotorControl] = part_resources
            if part.camera:
                capabilities.add(Camera)
            if (part.trajectory or part.motor) and not part.joints:
                raise ValueError(
                    f"模拟部件 {device.device_id}/{part.part_id} 需要 joints"
                )
            offers.append(
                PartOffer(
                    device_id=device.device_id,
                    part_id=part.part_id,
                    capabilities=frozenset(capabilities),
                    resources=resources,
                )
            )
        return Device(id=device.device_id, parts=tuple(offers))


class _TrajectoryBackend:
    def __init__(self, adapter: SimulatedAdapter, part: SimulatedPart) -> None:
        self._adapter = adapter
        self._part = part
        self._finished = asyncio.Event()

    def validate(self, target: Trajectory) -> None:
        self._adapter._require_connected()
        if target.joints != self._part.joints:
            raise TargetRejected(
                f"{self._part.part_id} 的关节为 {self._part.joints!r}，"
                f"目标是 {target.joints!r}"
            )
        _check_limits(target.waypoints, self._part)

    async def run(self, target: Trajectory) -> CommandOutcome:
        self._adapter.started.append(target)
        self._finished.clear()
        try:
            await asyncio.wait_for(self._finished.wait(), timeout=self._adapter.duration)
        except TimeoutError:
            if self._adapter.disconnected:
                return CommandOutcome(CommandState.UNKNOWN, "连接中断")
            if self._part.fail:
                return CommandOutcome(CommandState.FAILED, "模拟执行失败")
            return CommandOutcome(CommandState.COMPLETED)
        if self._adapter.disconnected:
            return CommandOutcome(CommandState.UNKNOWN, "连接中断")
        return CommandOutcome(CommandState.STOPPED, "设备侧停止")

    async def stop(self) -> bool:
        if self._adapter.disconnected or not self._adapter.stop_confirmed:
            return False
        self._adapter.stops += 1
        self._finished.set()
        return True

    def _disconnect(self) -> None:
        self._finished.set()


class _MotorBackend:
    def __init__(self, adapter: SimulatedAdapter, part: SimulatedPart) -> None:
        self._adapter = adapter
        self._part = part
    def validate(self, target: MotorTarget) -> None:
        self._adapter._require_connected()
        if len(target.positions) != len(self._part.joints):
            raise TargetRejected(
                f"{self._part.part_id} 有 {len(self._part.joints)} 个关节，"
                f"目标是 {len(target.positions)} 个位置"
            )
        for position in target.positions:
            if not self._part.lower <= position <= self._part.upper:
                raise TargetRejected(
                    f"位置 {position} 超出模拟限位 "
                    f"[{self._part.lower}, {self._part.upper}]"
                )
        if target.valid_for > self._adapter.max_valid_for:
            raise TargetRejected(
                f"有效期 {target.valid_for}s 超过模拟控制器的 "
                f"{self._adapter.max_valid_for}s"
            )

    async def send(self, target: MotorTarget) -> None:
        self._adapter._require_connected()
        self._adapter.motor_targets.append(target)

    async def stop(self) -> bool:
        if self._adapter.disconnected or not self._adapter.stop_confirmed:
            return False
        self._adapter.stops += 1
        return True

    def _disconnect(self) -> None:
        return


class _CameraBackend:
    def __init__(self, adapter: SimulatedAdapter, part: SimulatedPart) -> None:
        self._adapter = adapter
        self._part = part
        self._sequence = 0

    def snapshot(self, timeout: float) -> CameraFrame:
        self._adapter._require_connected()
        if not self._part.camera_available:
            raise CameraNoFrameError(f"模拟相机 {self._part.part_id} 没有首帧")
        self._sequence += 1
        return CameraFrame(
            jpeg=_JPEG,
            metadata=FrameMetadata(
                version=1,
                camera=self._part.part_id,
                session="simulated",
                sequence=self._sequence,
                format="jpeg",
                width=2,
                height=2,
                captured_at_unix_ms=int(time.time() * 1000),
            ),
            received_at=time.monotonic(),
        )

    def _disconnect(self) -> None:
        return


def _check_limits(
    waypoints: tuple[JointWaypoint, ...], part: SimulatedPart
) -> None:
    for waypoint in waypoints:
        for position in waypoint.positions:
            if not part.lower <= position <= part.upper:
                raise TargetRejected(
                    f"位置 {position} 超出 {part.part_id} 的模拟限位 "
                    f"[{part.lower}, {part.upper}]"
                )


__all__ = ["SimulatedAdapter", "SimulatedDevice", "SimulatedPart"]
