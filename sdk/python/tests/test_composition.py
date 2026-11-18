"""组合式 Robot 的声明、绑定与执行语义；只使用内存模拟 Adapter。"""

from __future__ import annotations

import asyncio
from typing import assert_type, cast

import buqi
import pytest
from buqi.simulation import SimulatedAdapter, SimulatedDevice, SimulatedPart

_JOINTS = ("j1", "j2")


class ArmPart(buqi.Part):
    trajectory: buqi.JointTrajectory
    motors: buqi.MotorControl | None


class ArmRobot(buqi.Robot):
    arm: ArmPart


class CameraRobot(buqi.Robot):
    arm: ArmPart
    camera: buqi.CameraPart | None


class TwoArms(buqi.Robot):
    left: ArmPart
    right: ArmPart


def arm_device(
    device_id: str = "g1",
    part_id: str = "left_arm",
    **part_options: object,
) -> SimulatedDevice:
    options: dict[str, object] = {"joints": _JOINTS}
    options.update(part_options)
    return SimulatedDevice(device_id, (SimulatedPart(part_id, **options),))  # type: ignore[arg-type]


def camera_device(
    device_id: str = "pc2", available: bool = True
) -> SimulatedDevice:
    return SimulatedDevice(
        device_id,
        (
            SimulatedPart(
                "rgb",
                trajectory=False,
                motor=False,
                camera=True,
                camera_available=available,
            ),
        ),
    )


def trajectory(positions: tuple[float, ...] = (0.1, 0.2)) -> buqi.Trajectory:
    return buqi.Trajectory(
        joints=_JOINTS,
        waypoints=(buqi.JointWaypoint(positions, 0.1),),
    )


def test_bind_required_and_optional_parts_and_capabilities() -> None:
    async def case() -> None:
        adapter = SimulatedAdapter(arm_device(), camera_device())
        devices = await buqi.discover(adapter)

        arm_only = await CameraRobot.bind(adapter, arm=devices[0].part("left_arm"))
        assert_type(arm_only.arm.trajectory, buqi.JointTrajectory)
        assert_type(arm_only.arm.motors, buqi.MotorControl | None)
        assert_type(arm_only.camera, buqi.CameraPart | None)
        assert arm_only.camera is None
        assert arm_only.arm.motors is not None

        full = await CameraRobot.bind(
            adapter,
            arm=devices[0].part("left_arm"),
            camera=devices[1].part("rgb"),
        )
        assert full.camera is not None
        assert_type(full.camera.snapshot(), buqi.CameraFrame)
        assert full.camera.snapshot().metadata.camera == "rgb"

        with pytest.raises(buqi.BindingError, match="缺少必需部件选择"):
            await CameraRobot.bind(adapter, camera=devices[1].part("rgb"))
        with pytest.raises(buqi.BindingError, match="没有这些部件字段"):
            await ArmRobot.bind(
                adapter,
                arm=devices[0].part("left_arm"),
                camera=devices[1].part("rgb"),
            )

    asyncio.run(case())


def test_optional_capability_absent_and_required_mismatch() -> None:
    async def case() -> None:
        no_motor = SimulatedAdapter(arm_device(motor=False))
        devices = await buqi.discover(no_motor)
        robot = await ArmRobot.bind(no_motor, arm=devices[0].part("left_arm"))
        assert robot.arm.motors is None
        async with robot.arm.trajectory.control():
            pass

        no_trajectory = SimulatedAdapter(arm_device(trajectory=False))
        devices = await buqi.discover(no_trajectory)
        with pytest.raises(buqi.BindingError, match="JointTrajectory"):
            await ArmRobot.bind(no_trajectory, arm=devices[0].part("left_arm"))

    asyncio.run(case())


def test_selection_is_explicit_and_duplicate_ids_fail() -> None:
    async def case() -> None:
        adapter = SimulatedAdapter(arm_device("g1"), arm_device("g2"))
        devices = await buqi.discover(adapter)
        assert [device.id for device in devices] == ["g1", "g2"]

        robot = await TwoArms.bind(
            adapter,
            left=devices[0].part("left_arm"),
            right=devices[1].part("left_arm"),
        )
        assert robot.left is not robot.right

        with pytest.raises(buqi.BindingError, match="没有设备"):
            await ArmRobot.bind(
                adapter, arm=buqi.PartSelector("missing", "left_arm")
            )
        with pytest.raises(buqi.BindingError, match="没有部件"):
            await ArmRobot.bind(
                adapter, arm=buqi.PartSelector("g1", "missing")
            )

        duplicate = SimulatedAdapter(
            SimulatedDevice("g1", (SimulatedPart("arm", joints=_JOINTS),))
        )
        with pytest.raises(buqi.BindingError, match="同一个设备 ID"):
            await buqi.discover(adapter, duplicate)

        repeated_part = SimulatedAdapter(
            SimulatedDevice(
                "g1",
                (
                    SimulatedPart("arm", joints=_JOINTS),
                    SimulatedPart("arm", joints=_JOINTS),
                ),
            )
        )
        with pytest.raises(buqi.BindingError, match="重复报告部件"):
            await buqi.discover(repeated_part)

    asyncio.run(case())


def test_shared_resource_conflicts_and_independent_parts() -> None:
    async def case() -> None:
        adapter = SimulatedAdapter(
            SimulatedDevice(
                "g1",
                (
                    SimulatedPart("left_arm", joints=_JOINTS),
                    SimulatedPart("right_arm", joints=_JOINTS),
                ),
            ),
            duration=5,
        )
        devices = await buqi.discover(adapter)
        robot = await TwoArms.bind(
            adapter,
            left=devices[0].part("left_arm"),
            right=devices[0].part("right_arm"),
        )

        async with robot.left.trajectory.control():
            assert robot.left.motors is not None
            with pytest.raises(buqi.ResourceBusy):
                async with robot.left.motors.control():
                    pass
            with pytest.raises(buqi.ResourceBusy):
                async with robot.left.trajectory.control():
                    pass
            async with robot.right.trajectory.control():
                pass

        async with robot.left.motors.control():
            pass

    asyncio.run(case())


def test_multi_resource_acquisition_is_all_or_nothing() -> None:
    async def case() -> None:
        adapter = SimulatedAdapter(
            SimulatedDevice(
                "g1",
                (
                    SimulatedPart("left", joints=_JOINTS, resources=("shared",)),
                    SimulatedPart(
                        "right", joints=_JOINTS, resources=("extra", "shared")
                    ),
                ),
            ),
            duration=5,
        )
        devices = await buqi.discover(adapter)
        robot = await TwoArms.bind(
            adapter,
            left=devices[0].part("left"),
            right=devices[0].part("right"),
        )
        shared = {
            resource.name.split("#", 1)[1]: resource
            for resource in robot.right.trajectory.resources
        }

        second = await TwoArms.bind(
            adapter,
            left=devices[0].part("left"),
            right=devices[0].part("right"),
        )

        async with robot.left.trajectory.control():
            with pytest.raises(buqi.ResourceBusy):
                async with robot.right.trajectory.control():
                    pass
            with pytest.raises(buqi.ResourceBusy):
                async with second.left.trajectory.control():
                    pass
            assert shared["extra"].state is buqi.ResourceState.FREE
            assert shared["shared"].state is buqi.ResourceState.HELD

        async with robot.right.trajectory.control():
            pass
        assert shared["extra"].state is buqi.ResourceState.FREE

    asyncio.run(case())


def test_enqueue_runs_in_order() -> None:
    async def case() -> None:
        adapter = SimulatedAdapter(arm_device(), duration=0.01)
        devices = await buqi.discover(adapter)
        robot = await ArmRobot.bind(adapter, arm=devices[0].part("left_arm"))
        first, second, third = trajectory((0.1, 0.2)), trajectory((0.2, 0.3)), trajectory((0.3, 0.4))

        async with robot.arm.trajectory.control() as control:
            executions = [
                await control.enqueue(first),
                await control.enqueue(second),
                await control.enqueue(third),
            ]
            results = [await execution.result() for execution in executions]

        assert [result.state for result in results] == [
            buqi.ExecutionState.SUCCEEDED
        ] * 3
        assert adapter.started == [first, second, third]

    asyncio.run(case())


def test_queue_is_bounded() -> None:
    async def case() -> None:
        adapter = SimulatedAdapter(arm_device(), duration=5)
        devices = await buqi.discover(adapter)
        robot = await ArmRobot.bind(adapter, arm=devices[0].part("left_arm"))

        async with robot.arm.trajectory.control() as control:
            running = await control.enqueue(trajectory())
            for _ in range(8):
                await control.enqueue(trajectory())
            with pytest.raises(buqi.QueueFull):
                await control.enqueue(trajectory())
            assert (await running.cancel()).state is buqi.ExecutionState.CANCELLED

    asyncio.run(case())


def test_run_preempts_current_and_queued() -> None:
    async def case() -> None:
        adapter = SimulatedAdapter(arm_device(), duration=0.05)
        devices = await buqi.discover(adapter)
        robot = await ArmRobot.bind(adapter, arm=devices[0].part("left_arm"))
        first, queued, replacement = (
            trajectory((0.1, 0.2)),
            trajectory((0.2, 0.3)),
            trajectory((0.3, 0.4)),
        )

        async with robot.arm.trajectory.control() as control:
            running = await control.enqueue(first)
            waiting = await control.enqueue(queued)
            now = await control.run(replacement)
            assert (await now.result()).state is buqi.ExecutionState.SUCCEEDED
            assert (await running.result()).state is buqi.ExecutionState.CANCELLED
            assert (await waiting.result()).state is buqi.ExecutionState.CANCELLED

        assert adapter.started == [first, replacement]
        assert adapter.stops == 1

    asyncio.run(case())


def test_cancel_queued_and_running_is_idempotent() -> None:
    async def case() -> None:
        adapter = SimulatedAdapter(arm_device(), duration=0.05)
        devices = await buqi.discover(adapter)
        robot = await ArmRobot.bind(adapter, arm=devices[0].part("left_arm"))
        running, queued = trajectory((0.1, 0.2)), trajectory((0.2, 0.3))

        async with robot.arm.trajectory.control() as control:
            first = await control.enqueue(running)
            second = await control.enqueue(queued)
            assert (await second.cancel()).state is buqi.ExecutionState.CANCELLED
            assert (await second.cancel()).state is buqi.ExecutionState.CANCELLED
            assert (await first.cancel()).state is buqi.ExecutionState.CANCELLED
            assert (await first.cancel()).state is buqi.ExecutionState.CANCELLED

        assert adapter.started == [running]
        assert adapter.stops == 1

    asyncio.run(case())


def test_cancelling_result_waiter_does_not_cancel_execution() -> None:
    async def case() -> None:
        adapter = SimulatedAdapter(arm_device(), duration=0.02)
        devices = await buqi.discover(adapter)
        robot = await ArmRobot.bind(adapter, arm=devices[0].part("left_arm"))

        async with robot.arm.trajectory.control() as control:
            execution = await control.enqueue(trajectory())
            waiter = asyncio.create_task(execution.result())
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert (await execution.result()).state is buqi.ExecutionState.SUCCEEDED

    asyncio.run(case())


def test_unconfirmed_stop_keeps_resource_unavailable() -> None:
    async def case() -> None:
        adapter = SimulatedAdapter(
            arm_device(), duration=5, stop_confirmed=False
        )
        devices = await buqi.discover(adapter)
        robot = await ArmRobot.bind(adapter, arm=devices[0].part("left_arm"))
        resource = robot.arm.trajectory.resources[0]

        with pytest.raises(buqi.StopUnconfirmed):
            async with robot.arm.trajectory.control() as control:
                execution = await control.enqueue(trajectory())
                assert (await execution.cancel()).state is buqi.ExecutionState.UNKNOWN

        assert resource.state is buqi.ResourceState.UNCONFIRMED
        with pytest.raises(buqi.ResourceUnconfirmed):
            async with robot.arm.trajectory.control():
                pass

        resource.confirm_stopped()
        async with robot.arm.trajectory.control():
            pass
        assert resource.state is buqi.ResourceState.FREE

    asyncio.run(case())


def test_run_with_unconfirmed_stop_starts_nothing() -> None:
    async def case() -> None:
        adapter = SimulatedAdapter(
            arm_device(), duration=5, stop_confirmed=False
        )
        devices = await buqi.discover(adapter)
        robot = await ArmRobot.bind(adapter, arm=devices[0].part("left_arm"))
        first, replacement = trajectory((0.1, 0.2)), trajectory((0.2, 0.3))

        with pytest.raises(buqi.StopUnconfirmed):
            async with robot.arm.trajectory.control() as control:
                running = await control.enqueue(first)
                with pytest.raises(buqi.StopUnconfirmed):
                    await control.run(replacement)
                assert (await running.result()).state is buqi.ExecutionState.UNKNOWN

        assert adapter.started == [first]

    asyncio.run(case())


def test_failure_stops_queue_without_retry() -> None:
    async def case() -> None:
        adapter = SimulatedAdapter(arm_device(fail=True), duration=0.01)
        devices = await buqi.discover(adapter)
        robot = await ArmRobot.bind(adapter, arm=devices[0].part("left_arm"))
        first, second = trajectory((0.1, 0.2)), trajectory((0.2, 0.3))

        async with robot.arm.trajectory.control() as control:
            running = await control.enqueue(first)
            waiting = await control.enqueue(second)
            assert (await running.result()).state is buqi.ExecutionState.FAILED
            assert (await waiting.result()).state is buqi.ExecutionState.CANCELLED

        assert adapter.started == [first]

    asyncio.run(case())


def test_disconnect_marks_result_unknown_and_cancels_queue() -> None:
    async def case() -> None:
        adapter = SimulatedAdapter(arm_device(), duration=5)
        devices = await buqi.discover(adapter)
        robot = await ArmRobot.bind(adapter, arm=devices[0].part("left_arm"))
        resource = robot.arm.trajectory.resources[0]
        first, second = trajectory((0.1, 0.2)), trajectory((0.2, 0.3))

        with pytest.raises(buqi.StopUnconfirmed):
            async with robot.arm.trajectory.control() as control:
                running = await control.enqueue(first)
                waiting = await control.enqueue(second)
                adapter.disconnect()
                assert (await running.result()).state is buqi.ExecutionState.UNKNOWN
                assert (await waiting.result()).state is buqi.ExecutionState.CANCELLED
                with pytest.raises(buqi.Disconnected):
                    await control.enqueue(trajectory())

        assert resource.state is buqi.ResourceState.UNCONFIRMED
        assert adapter.started == [first]

    asyncio.run(case())


def test_context_exit_cancels_running_and_queued() -> None:
    async def case() -> None:
        adapter = SimulatedAdapter(arm_device(), duration=5)
        devices = await buqi.discover(adapter)
        robot = await ArmRobot.bind(adapter, arm=devices[0].part("left_arm"))
        first, second = trajectory((0.1, 0.2)), trajectory((0.2, 0.3))

        async with robot.arm.trajectory.control() as control:
            running = await control.enqueue(first)
            waiting = await control.enqueue(second)

        assert (await running.result()).state is buqi.ExecutionState.CANCELLED
        assert (await waiting.result()).state is buqi.ExecutionState.CANCELLED
        assert adapter.stops == 1
        assert adapter.started == [first]

    asyncio.run(case())


def test_context_exit_on_body_error_still_stops_and_releases() -> None:
    async def case() -> None:
        adapter = SimulatedAdapter(arm_device(), duration=5)
        devices = await buqi.discover(adapter)
        robot = await ArmRobot.bind(adapter, arm=devices[0].part("left_arm"))

        execution: buqi.Execution | None = None
        with pytest.raises(RuntimeError, match="boom"):
            async with robot.arm.trajectory.control() as control:
                execution = await control.enqueue(trajectory())
                raise RuntimeError("boom")

        assert execution is not None
        assert (await execution.result()).state is buqi.ExecutionState.CANCELLED
        async with robot.arm.trajectory.control():
            pass

    asyncio.run(case())


def test_motor_target_overwrites_and_validates() -> None:
    async def case() -> None:
        adapter = SimulatedAdapter(arm_device(), duration=0.01)
        devices = await buqi.discover(adapter)
        robot = await ArmRobot.bind(adapter, arm=devices[0].part("left_arm"))
        assert robot.arm.motors is not None
        first = buqi.MotorTarget((0.1, 0.2), valid_for=0.1)
        second = buqi.MotorTarget((0.3, 0.4), valid_for=0.1)

        async with robot.arm.motors.control() as motor:
            await motor.send(first)
            await motor.send(second)
            with pytest.raises(buqi.TargetRejected, match="有效期"):
                await motor.send(buqi.MotorTarget((0.1, 0.2), valid_for=1.0))
            with pytest.raises(buqi.TargetRejected, match="限位"):
                await motor.send(buqi.MotorTarget((9.0, 0.0)))
            with pytest.raises(buqi.ResourceBusy):
                async with robot.arm.trajectory.control():
                    pass

        assert adapter.motor_targets == [first, second]
        assert adapter.stops == 1

    asyncio.run(case())


def test_camera_snapshot_and_missing_frame() -> None:
    async def case() -> None:
        adapter = SimulatedAdapter(
            arm_device(), camera_device(available=True), camera_device("pc3", available=False)
        )
        devices = await buqi.discover(adapter)
        robot = await CameraRobot.bind(
            adapter,
            arm=devices[0].part("left_arm"),
            camera=devices[1].part("rgb"),
        )
        assert robot.camera is not None
        frame = robot.camera.snapshot(timeout=0.5)
        assert frame.metadata.sequence == 1
        assert frame.jpeg.startswith(b"\xff\xd8")

        broken = await CameraRobot.bind(
            adapter,
            arm=devices[0].part("left_arm"),
            camera=devices[2].part("rgb"),
        )
        assert broken.camera is not None
        with pytest.raises(buqi.CameraNoFrameError):
            broken.camera.snapshot()

    asyncio.run(case())


def test_declaration_errors_are_explicit() -> None:
    class WrongPart(buqi.Part):
        trajectory: int

    class WrongRobot(buqi.Robot):
        arm: int

    class EmptyPart(buqi.Part):
        pass

    class EmptyPartRobot(buqi.Robot):
        arm: EmptyPart

    class EmptyRobot(buqi.Robot):
        pass

    async def case() -> None:
        adapter = SimulatedAdapter(arm_device())
        offer = (await buqi.discover(adapter))[0].part("left_arm")

        with pytest.raises(buqi.DeclarationError):
            await WrongRobot.bind(adapter, arm=offer)
        with pytest.raises(buqi.DeclarationError):
            await EmptyPartRobot.bind(adapter, arm=offer)
        with pytest.raises(buqi.DeclarationError):
            await EmptyRobot.bind(adapter, arm=offer)
        assert issubclass(WrongPart, buqi.Part)

    asyncio.run(case())


def test_target_validation_leaves_state_untouched() -> None:
    async def case() -> None:
        adapter = SimulatedAdapter(arm_device(), duration=0.02)
        devices = await buqi.discover(adapter)
        robot = await ArmRobot.bind(adapter, arm=devices[0].part("left_arm"))
        first, queued = trajectory((0.1, 0.2)), trajectory((0.2, 0.3))

        async with robot.arm.trajectory.control() as control:
            running = await control.enqueue(first)
            waiting = await control.enqueue(queued)
            with pytest.raises(buqi.TargetRejected, match="关节"):
                await control.run(
                    buqi.Trajectory(
                        joints=("bad",),
                        waypoints=(buqi.JointWaypoint((0.1,), 0.1),),
                    )
                )
            with pytest.raises(buqi.TargetError, match="路径点"):
                await control.run(
                    buqi.Trajectory(
                        joints=_JOINTS,
                        waypoints=(buqi.JointWaypoint((0.1,), 0.1),),
                    )
                )
            assert (await running.result()).state is buqi.ExecutionState.SUCCEEDED
            assert (await waiting.result()).state is buqi.ExecutionState.SUCCEEDED

        assert adapter.started == [first, queued]
        assert adapter.stops == 0

    asyncio.run(case())


class OptionalCameraRobot(buqi.Robot):
    camera: buqi.CameraPart | None


class _MotorGate:
    """包装电机后端，用 Event 暂停 send/stop，检查在途调用与关闭的协调。"""

    def __init__(
        self, inner: buqi.MotorBackend, *, gate_stop: bool = False
    ) -> None:
        self.inner = inner
        self.sending = asyncio.Event()
        self.release = asyncio.Event()
        self.stopping = asyncio.Event()
        self.stop_release = asyncio.Event()
        self._gate_stop = gate_stop

    def validate(self, target: buqi.MotorTarget) -> None:
        self.inner.validate(target)

    async def send(self, target: buqi.MotorTarget) -> None:
        self.sending.set()
        await self.release.wait()
        await self.inner.send(target)

    async def stop(self) -> bool:
        if not self._gate_stop:
            return await self.inner.stop()
        self.stopping.set()
        await self.stop_release.wait()
        return True


class _TrajectoryGate:
    """包装轨迹后端，让 stop 停在 Event 上，模拟退出时确认很慢。"""

    def __init__(self, inner: buqi.TrajectoryBackend) -> None:
        self.inner = inner
        self.stopping = asyncio.Event()
        self.release = asyncio.Event()

    def validate(self, target: buqi.Trajectory) -> None:
        self.inner.validate(target)

    async def run(self, target: buqi.Trajectory) -> buqi.CommandOutcome:
        return await self.inner.run(target)

    async def stop(self) -> bool:
        self.stopping.set()
        try:
            await asyncio.wait_for(self.release.wait(), timeout=2)
        except TimeoutError:
            pass
        return await self.inner.stop()


class _GatedAdapter:
    """公开 Adapter 契约的包装器；只替换后端，不改变发现结果。"""

    def __init__(
        self, inner: SimulatedAdapter, *, gate_motor_stop: bool = False
    ) -> None:
        self.inner = inner
        self._gate_motor_stop = gate_motor_stop
        self.motor: _MotorGate | None = None
        self.trajectory: _TrajectoryGate | None = None

    async def discover(self) -> tuple[buqi.Device, ...]:
        return await self.inner.discover()

    async def open_part(self, offer: buqi.PartOffer) -> buqi.PartSession:
        session = await self.inner.open_part(offer)
        capabilities: dict[type[buqi.Capability], object] = dict(
            session.capabilities
        )
        motor = capabilities.get(buqi.MotorControl)
        if motor is not None:
            self.motor = _MotorGate(
                cast(buqi.MotorBackend, motor), gate_stop=self._gate_motor_stop
            )
            capabilities[buqi.MotorControl] = self.motor
        trajectory = capabilities.get(buqi.JointTrajectory)
        if trajectory is not None:
            self.trajectory = _TrajectoryGate(
                cast(buqi.TrajectoryBackend, trajectory)
            )
            capabilities[buqi.JointTrajectory] = self.trajectory
        return buqi.PartSession(capabilities=capabilities)


def test_control_requires_entry_and_ownership() -> None:
    async def case() -> None:
        adapter = SimulatedAdapter(arm_device(), duration=0.01)
        devices = await buqi.discover(adapter)
        robot = await ArmRobot.bind(adapter, arm=devices[0].part("left_arm"))
        resource = robot.arm.trajectory.resources[0]

        unentered = robot.arm.trajectory.control()
        with pytest.raises(buqi.ControlNotEntered):
            await unentered.enqueue(trajectory())
        assert adapter.started == []

        assert robot.arm.motors is not None
        unentered_motor = robot.arm.motors.control()
        with pytest.raises(buqi.ControlNotEntered):
            await unentered_motor.send(buqi.MotorTarget((0.1, 0.2)))
        assert adapter.motor_targets == []

        async with robot.arm.trajectory.control() as control:
            with pytest.raises(buqi.ControlError, match="重复进入"):
                async with control:
                    pass
            other = robot.arm.trajectory.control()
            with pytest.raises(buqi.ControlNotEntered):
                await other.enqueue(trajectory())
            assert adapter.started == []

        assert resource.state is buqi.ResourceState.FREE
        with pytest.raises(buqi.ControlClosed):
            async with control:
                pass
        with pytest.raises(buqi.ControlClosed):
            await control.enqueue(trajectory())

        async with robot.arm.trajectory.control():
            pass

    asyncio.run(case())


def test_motor_close_waits_for_inflight_send() -> None:
    async def case() -> None:
        inner = SimulatedAdapter(arm_device(), duration=0.01)
        adapter = _GatedAdapter(inner)
        devices = await buqi.discover(adapter)
        robot = await ArmRobot.bind(adapter, arm=devices[0].part("left_arm"))
        assert robot.arm.motors is not None
        assert adapter.motor is not None
        resource = robot.arm.motors.resources[0]
        target = buqi.MotorTarget((0.1, 0.2), valid_for=0.1)

        control = robot.arm.motors.control()
        await control.__aenter__()
        sender = asyncio.create_task(control.send(target))
        await adapter.motor.sending.wait()

        exiter = asyncio.create_task(control.__aexit__(None, None, None))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert resource.state is buqi.ResourceState.HELD
        assert inner.stops == 0

        adapter.motor.release.set()
        await sender
        await exiter

        assert inner.motor_targets == [target]
        assert inner.stops == 1
        assert resource.state is buqi.ResourceState.FREE

    asyncio.run(case())


def test_trajectory_close_cancellation_leaves_recoverable_state() -> None:
    async def case() -> None:
        inner = SimulatedAdapter(arm_device(), duration=5)
        adapter = _GatedAdapter(inner)
        devices = await buqi.discover(adapter)
        robot = await ArmRobot.bind(adapter, arm=devices[0].part("left_arm"))
        assert adapter.trajectory is not None
        resource = robot.arm.trajectory.resources[0]

        control = robot.arm.trajectory.control()
        await control.__aenter__()
        execution = await control.enqueue(trajectory())

        exiter = asyncio.create_task(control.__aexit__(None, None, None))
        await adapter.trajectory.stopping.wait()
        exiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await exiter

        assert (await execution.result()).state is buqi.ExecutionState.UNKNOWN
        assert resource.state is buqi.ResourceState.UNCONFIRMED
        with pytest.raises(buqi.ResourceUnconfirmed):
            async with robot.arm.trajectory.control():
                pass

        resource.confirm_stopped()
        async with robot.arm.trajectory.control():
            pass
        assert resource.state is buqi.ResourceState.FREE

    asyncio.run(case())


def test_motor_close_cancellation_leaves_recoverable_state() -> None:
    async def case() -> None:
        inner = SimulatedAdapter(arm_device(), duration=0.01)
        adapter = _GatedAdapter(inner, gate_motor_stop=True)
        devices = await buqi.discover(adapter)
        robot = await ArmRobot.bind(adapter, arm=devices[0].part("left_arm"))
        assert robot.arm.motors is not None
        assert adapter.motor is not None
        resource = robot.arm.motors.resources[0]

        control = robot.arm.motors.control()
        await control.__aenter__()
        adapter.motor.release.set()
        await control.send(buqi.MotorTarget((0.1, 0.2), valid_for=0.1))

        exiter = asyncio.create_task(control.__aexit__(None, None, None))
        await adapter.motor.stopping.wait()
        exiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await exiter

        assert resource.state is buqi.ResourceState.UNCONFIRMED
        resource.confirm_stopped()
        async with robot.arm.motors.control():
            pass
        assert resource.state is buqi.ResourceState.FREE

    asyncio.run(case())


def test_empty_discovery_allows_all_optional_robot() -> None:
    async def case() -> None:
        adapter = SimulatedAdapter()

        assert await buqi.discover(adapter) == ()
        robot = await OptionalCameraRobot.bind(adapter)
        assert robot.camera is None

        with pytest.raises(buqi.BindingError, match="没有设备"):
            await OptionalCameraRobot.bind(
                adapter, camera=buqi.PartSelector("missing", "rgb")
            )
        with pytest.raises(buqi.BindingError, match="缺少必需部件选择"):
            await ArmRobot.bind(adapter)

    asyncio.run(case())


def test_cancelled_motor_exit_terminates_inflight_send() -> None:
    async def case() -> None:
        inner = SimulatedAdapter(arm_device(), duration=5)
        adapter = _GatedAdapter(inner)
        devices = await buqi.discover(adapter)
        robot = await ArmRobot.bind(adapter, arm=devices[0].part("left_arm"))
        assert robot.arm.motors is not None
        assert adapter.motor is not None
        resource = robot.arm.motors.resources[0]
        target = buqi.MotorTarget((0.1, 0.2), valid_for=0.1)

        control = robot.arm.motors.control()
        await control.__aenter__()
        sender = asyncio.create_task(control.send(target))
        await adapter.motor.sending.wait()

        exiter = asyncio.create_task(control.__aexit__(None, None, None))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        exiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await exiter
        with pytest.raises(asyncio.CancelledError):
            await sender

        assert resource.state is buqi.ResourceState.UNCONFIRMED
        resource.confirm_stopped()

        # 资源恢复并交给轨迹控制之后，放行旧发送也不能再落命令。
        async with robot.arm.trajectory.control():
            adapter.motor.release.set()
            await asyncio.sleep(0.01)
            assert inner.motor_targets == []
            assert inner.stops == 0

    asyncio.run(case())


def test_cancelled_trajectory_exit_rejects_concurrent_run() -> None:
    async def case() -> None:
        inner = SimulatedAdapter(arm_device(), duration=5)
        adapter = _GatedAdapter(inner)
        devices = await buqi.discover(adapter)
        robot = await ArmRobot.bind(adapter, arm=devices[0].part("left_arm"))
        assert adapter.trajectory is not None
        resource = robot.arm.trajectory.resources[0]
        first, replacement = trajectory((0.1, 0.2)), trajectory((0.2, 0.3))

        control = robot.arm.trajectory.control()
        await control.__aenter__()
        running = await control.enqueue(first)

        runner = asyncio.create_task(control.run(replacement))
        await adapter.trajectory.stopping.wait()

        exiter = asyncio.create_task(control.__aexit__(None, None, None))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        exiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await exiter

        adapter.trajectory.release.set()
        with pytest.raises(buqi.ControlClosed):
            await runner

        assert (await running.result()).state is buqi.ExecutionState.UNKNOWN
        assert inner.started == [first]
        assert resource.state is buqi.ResourceState.UNCONFIRMED
        resource.confirm_stopped()
        async with robot.arm.trajectory.control():
            pass

    asyncio.run(case())


def test_cancelled_exit_aborts_inflight_run_stop() -> None:
    async def case() -> None:
        inner = SimulatedAdapter(arm_device(), duration=5)
        adapter = _GatedAdapter(inner)
        devices = await buqi.discover(adapter)
        robot = await ArmRobot.bind(adapter, arm=devices[0].part("left_arm"))
        assert adapter.trajectory is not None
        resource = robot.arm.trajectory.resources[0]
        first, replacement = trajectory((0.1, 0.2)), trajectory((0.2, 0.3))

        control = robot.arm.trajectory.control()
        await control.__aenter__()
        running = await control.enqueue(first)
        await asyncio.sleep(0)

        runner = asyncio.create_task(control.run(replacement))
        await adapter.trajectory.stopping.wait()
        exiter = asyncio.create_task(control.__aexit__(None, None, None))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        exiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await exiter

        assert (await running.result()).state is buqi.ExecutionState.UNKNOWN
        assert inner.stops == 0

        assert resource.state is buqi.ResourceState.UNCONFIRMED
        resource.confirm_stopped()

        async with robot.arm.trajectory.control() as fresh:
            second = await fresh.enqueue(trajectory((0.1, 0.2)))
            # 放行旧 stop：它已被退出取消终结，不能停止这个新动作。
            adapter.trajectory.release.set()
            await asyncio.sleep(0.02)
            assert second.state is buqi.ExecutionState.RUNNING
            assert inner.stops == 0
        assert (await second.result()).state is buqi.ExecutionState.CANCELLED

        with pytest.raises(buqi.ControlClosed):
            await runner

    asyncio.run(case())


def test_cancelled_exit_aborts_inflight_cancel_stop() -> None:
    async def case() -> None:
        inner = SimulatedAdapter(arm_device(), duration=5)
        adapter = _GatedAdapter(inner)
        devices = await buqi.discover(adapter)
        robot = await ArmRobot.bind(adapter, arm=devices[0].part("left_arm"))
        assert adapter.trajectory is not None
        resource = robot.arm.trajectory.resources[0]
        first = trajectory((0.1, 0.2))

        control = robot.arm.trajectory.control()
        await control.__aenter__()
        running = await control.enqueue(first)
        await asyncio.sleep(0)

        canceller = asyncio.create_task(running.cancel())
        await adapter.trajectory.stopping.wait()
        exiter = asyncio.create_task(control.__aexit__(None, None, None))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        exiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await exiter

        assert (await running.result()).state is buqi.ExecutionState.UNKNOWN
        assert inner.stops == 0

        assert resource.state is buqi.ResourceState.UNCONFIRMED
        resource.confirm_stopped()

        async with robot.arm.trajectory.control() as fresh:
            second = await fresh.enqueue(trajectory((0.1, 0.2)))
            # 放行旧 stop：它已被退出取消终结，不能停止这个新动作。
            adapter.trajectory.release.set()
            await asyncio.sleep(0.02)
            assert second.state is buqi.ExecutionState.RUNNING
            assert inner.stops == 0
        assert (await second.result()).state is buqi.ExecutionState.CANCELLED
        assert (await canceller).state is buqi.ExecutionState.UNKNOWN

    asyncio.run(case())
