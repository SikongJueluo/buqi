"""能力对象、目标数据与执行语义。

应用不直接构造能力对象：绑定 ``Robot`` 时由 Adapter 的后端构建。
这里实现所有与具体机器人无关的部分——目标校验、独占资源、等待队列、
抢占、取消和结果状态——Adapter 只负责设备侧的少量动作。
"""

from __future__ import annotations

import asyncio
import math
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, Self, cast

from .errors import (
    ControlClosed,
    ControlError,
    ControlNotEntered,
    DeclarationError,
    Disconnected,
    QueueFull,
    ResourceBusy,
    ResourceUnconfirmed,
    StopUnconfirmed,
    TargetError,
    TargetRejected,
)
from .media import CameraFrame

# 每个控制上下文最多等待的动作数。满了直接报错，不静默丢弃，也不无界积压。
_QUEUE_CAPACITY = 8


class ResourceState(Enum):
    """执行资源的状态。"""

    FREE = "free"
    HELD = "held"
    UNCONFIRMED = "unconfirmed"


class Resource:
    """某一执行资源的身份，由 Adapter 创建并在能力之间共享。

    两个 Part（或一个 Part 的两种能力）声明同一个资源对象时，控制入口互斥，
    不同 Robot 实例的重复绑定也一样。名称应在一个 Adapter 内唯一，通常带设备
    前缀。``confirm_stopped()`` 是应用在人工或外部确认设备已停止之后的状态清理，
    不是设备查询。
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._state = ResourceState.FREE
        self._owner: object | None = None

    @property
    def state(self) -> ResourceState:
        return self._state

    def confirm_stopped(self) -> None:
        """清除“未确认”状态；只在应用确认设备已停止后调用。"""
        if self._state is ResourceState.UNCONFIRMED:
            self._owner = None
            self._state = ResourceState.FREE

    def _acquire(self, owner: object) -> None:
        if self._state is ResourceState.UNCONFIRMED:
            raise ResourceUnconfirmed(
                f"资源 {self.name} 的上一次使用未确认结束；"
                "确认设备已停止后调用该资源的 confirm_stopped()"
            )
        if self._state is ResourceState.HELD:
            raise ResourceBusy(f"资源 {self.name} 已被另一个控制入口持有")
        self._state = ResourceState.HELD
        self._owner = owner

    def _release(self, owner: object) -> None:
        if self._state is not ResourceState.HELD or self._owner is not owner:
            return
        self._owner = None
        self._state = ResourceState.FREE

    def _unconfirm(self, owner: object) -> None:
        if self._owner is not owner:
            return
        self._owner = None
        self._state = ResourceState.UNCONFIRMED


def _acquire_all(resources: tuple[Resource, ...], owner: object) -> None:
    acquired: list[Resource] = []
    try:
        for resource in resources:
            resource._acquire(owner)
            acquired.append(resource)
    except BaseException:
        for resource in acquired:
            resource._release(owner)
        raise


def _release_all(resources: tuple[Resource, ...], owner: object) -> None:
    for resource in resources:
        resource._release(owner)


def _unconfirm_all(resources: tuple[Resource, ...], owner: object) -> None:
    for resource in resources:
        resource._unconfirm(owner)


def _all_owned_by(resources: tuple[Resource, ...], owner: object) -> bool:
    return all(
        resource.state is ResourceState.HELD and resource._owner is owner
        for resource in resources
    )


def _resources_state(resources: tuple[Resource, ...]) -> ResourceState:
    states = {resource.state for resource in resources}
    if ResourceState.UNCONFIRMED in states:
        return ResourceState.UNCONFIRMED
    if ResourceState.HELD in states:
        return ResourceState.HELD
    return ResourceState.FREE


def _describe(resources: tuple[Resource, ...]) -> str:
    return "、".join(resource.name for resource in resources) or "（无资源）"


class _StopAborted(Exception):
    """关闭取消处理终结了一个在途停止请求。"""


@dataclass(frozen=True)
class JointWaypoint:
    """轨迹中的一个路径点。位置按目标关节顺序排列。"""

    positions: tuple[float, ...]
    time_from_start: float


@dataclass(frozen=True)
class Trajectory:
    """一段关节轨迹。

    ``joints`` 的顺序就是每个 ``positions`` 的顺序。SDK 只校验结构，
    关节名是否属于设备、位置是否越界由 Adapter 的 ``validate`` 决定。
    """

    joints: tuple[str, ...]
    waypoints: tuple[JointWaypoint, ...]


@dataclass(frozen=True)
class MotorTarget:
    """逐周期底层电机目标，新目标覆盖旧目标。

    ``valid_for`` 是目标的有效期（秒）。执行侧必须按该期限让目标失效；
    SDK 只能把它传给 Adapter，不能跨网络保证实时截止。
    """

    positions: tuple[float, ...]
    valid_for: float = 0.2


class CommandState(Enum):
    """Adapter 执行一次动作后的结论。"""

    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CommandOutcome:
    state: CommandState
    detail: str | None = None


class ExecutionState(Enum):
    """应用可见的执行状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

    @property
    def terminal(self) -> bool:
        return self in {
            ExecutionState.SUCCEEDED,
            ExecutionState.FAILED,
            ExecutionState.CANCELLED,
            ExecutionState.UNKNOWN,
        }


@dataclass(frozen=True)
class ExecutionResult:
    """一次执行的最终结果；失败不是异常。"""

    state: ExecutionState
    detail: str | None = None


class Capability:
    """能力标记基类。应用通过 ``Part`` 上的类型化属性访问具体能力。"""


class TrajectoryBackend(Protocol):
    """Adapter 提供的轨迹执行后端。

    ``run`` 在动作完成、失败、被停止或结果无法确认时返回；``stop`` 返回
    是否确认设备已停止。两者都可能被并发调用。``stop`` 用布尔值报告失败，
    连接错误或超时也应返回 ``False``，以便 SDK 保留“未确认”状态；它必须在
    有限时间内返回，不能无限等待设备。``run`` 被取消时也必须在有限时间内结束，
    不能在取消后继续执行目标；``stop`` 被取消时同样必须终止，不能吞掉取消后
    继续向设备发送停止命令。
    """

    def validate(self, target: Trajectory) -> None:
        """在改动当前动作或队列之前拒绝不可能执行的目标。"""
        ...

    async def run(self, target: Trajectory) -> CommandOutcome: ...

    async def stop(self) -> bool: ...


class MotorBackend(Protocol):
    """Adapter 提供的底层电机后端；发送即覆盖，不排队。

    ``send`` 被取消时必须在有限时间内终止，不得在取消后继续向设备提交目标；
    ``stop`` 被取消时同样必须终止，不能吞掉取消后继续发送停止命令。控制上下文
    退出被取消时会先取消并等待在途发送，再释放或确认资源。
    """

    def validate(self, target: MotorTarget) -> None: ...

    async def send(self, target: MotorTarget) -> None: ...

    async def stop(self) -> bool: ...


class CameraBackend(Protocol):
    """Adapter 提供的相机后端；查询语义与 :mod:`buqi.media` 一致。"""

    def snapshot(self, timeout: float) -> CameraFrame: ...


class Execution:
    """一次已提交动作的句柄：查询状态、取结果、取消。

    ``result()`` 只等待已经发起的执行，不会重新提交。等待方被取消时，
    执行本身继续运行。
    """

    def __init__(
        self, identifier: int, target: Trajectory, control: TrajectoryControl
    ) -> None:
        self.id = identifier
        self._target = target
        self._control = control
        self._result: asyncio.Future[ExecutionResult] = (
            asyncio.get_running_loop().create_future()
        )
        self._state = ExecutionState.QUEUED
        self._cancel_requested = False
        self._job: asyncio.Task[None] | None = None

    @property
    def state(self) -> ExecutionState:
        return self._state

    async def result(self) -> ExecutionResult:
        """等待本执行的最终结果；不启动或重发动作。"""
        return await asyncio.shield(self._result)

    async def cancel(self) -> ExecutionResult:
        """取消排队或正在执行的动作；对已结束的执行幂等。"""
        return await self._control._cancel(self)

    def _finish(self, state: ExecutionState, detail: str | None = None) -> None:
        if self._result.done():
            return
        self._state = state
        self._result.set_result(ExecutionResult(state, detail))


class TrajectoryControl:
    """轨迹能力的控制上下文；通过 ``async with`` 获取全部执行资源。

    退出上下文时结束未完成动作并等待 Adapter 确认；取消或抢占只有在
    Adapter 确认设备停止后才会继续。
    """

    def __init__(
        self, backend: TrajectoryBackend, resources: tuple[Resource, ...]
    ) -> None:
        self._backend = backend
        self._resources = resources
        self._lock = asyncio.Lock()
        self._queue: deque[Execution] = deque()
        self._current: Execution | None = None
        self._stops: set[asyncio.Task[bool]] = set()
        self._entered = False
        self._closed = False
        self._next_id = 0

    async def __aenter__(self) -> Self:
        if self._closed:
            raise ControlClosed(
                "控制上下文已关闭，不能再次进入；请创建新的控制入口"
            )
        if self._entered:
            raise ControlError("控制上下文已经进入，不能重复进入")
        _acquire_all(self._resources, self)
        self._entered = True
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        unconfirmed = await self._close()
        if not unconfirmed:
            return
        if isinstance(exc, BaseException):
            exc.add_note(f"关闭时未能确认资源 {_describe(self._resources)} 已停止")
            return
        raise StopUnconfirmed(
            f"关闭控制上下文时未能确认资源 {_describe(self._resources)} 已停止；"
            "设备实际状态需要外部确认"
        )

    async def enqueue(self, target: Trajectory) -> Execution:
        """排队一个轨迹；空闲时立即开始，队列满时报错而不丢弃。"""
        _validate_trajectory(target)
        async with self._lock:
            self._require_open_locked()
            self._backend.validate(target)
            self._require_usable_locked()
            if len(self._queue) >= _QUEUE_CAPACITY:
                raise QueueFull(
                    f"等待队列已满（上限 {_QUEUE_CAPACITY}）；"
                    "请先取得已有执行的结果或取消动作"
                )
            execution = self._new_execution_locked(target)
            self._advance_locked()
        # 让刚创建的执行任务先进入 Adapter，句柄返回时它已真正提交。
        await asyncio.sleep(0)
        return execution

    async def run(self, target: Trajectory) -> Execution:
        """抢占：取消尚未开始的等待队列，结束当前动作后立即执行新目标。

        校验在改动任何状态之前完成。无法确认旧动作结束时不会启动新动作。
        """
        _validate_trajectory(target)

        old_job: asyncio.Task[None] | None = None
        stop_error: StopUnconfirmed | None = None
        closed_error: ControlClosed | None = None
        execution: Execution | None = None
        async with self._lock:
            self._require_open_locked()
            self._backend.validate(target)
            self._require_usable_locked()
            self._stop_queue_locked("被 run() 取代，未开始执行")
            stop_aborted = False
            current = self._current
            if current is not None and not current._state.terminal:
                current._cancel_requested = True
                old_job = current._job
                try:
                    confirmed = await self._stop_backend()
                except _StopAborted:
                    stop_aborted = True
                else:
                    if confirmed:
                        current._finish(ExecutionState.CANCELLED, "被 run() 取代")
                        self._current = None
                    else:
                        current._finish(
                            ExecutionState.UNKNOWN, "抢占前未能确认旧动作停止"
                        )
                        _unconfirm_all(self._resources, self)
                        self._current = None
                        stop_error = StopUnconfirmed(
                            "未能确认旧动作已停止，未启动新动作；"
                            f"资源 {_describe(self._resources)} 需要外部确认"
                        )
            if stop_aborted:
                closed_error = ControlClosed(
                    "控制上下文在抢占过程中已关闭；未启动新动作"
                )
            elif stop_error is None:
                # 等待 stop 期间，并发的退出取消处理可能已经把上下文关闭；
                # 不能向已关闭的上下文再加入一个永远不会启动的执行。
                if self._closed:
                    closed_error = ControlClosed(
                        "控制上下文在抢占过程中已关闭；未启动新动作"
                    )
                else:
                    execution = self._new_execution_locked(target)
                    self._advance_locked()
        if old_job is not None:
            old_job.cancel()
            await asyncio.gather(old_job, return_exceptions=True)
        if stop_error is not None:
            raise stop_error
        if closed_error is not None:
            raise closed_error
        assert execution is not None
        await asyncio.sleep(0)
        return execution

    async def _cancel(self, execution: Execution) -> ExecutionResult:
        job: asyncio.Task[None] | None = None
        async with self._lock:
            if execution._result.done():
                return execution._result.result()
            if self._closed:
                if self._current is execution:
                    execution._finish(
                        ExecutionState.UNKNOWN,
                        "控制上下文已关闭，停止结果未确认",
                    )
                else:
                    try:
                        self._queue.remove(execution)
                    except ValueError:
                        pass
                    execution._finish(
                        ExecutionState.CANCELLED, "控制上下文已关闭，未开始执行"
                    )
                return execution._result.result()
            if self._current is not execution:
                try:
                    self._queue.remove(execution)
                except ValueError:
                    pass
                execution._finish(ExecutionState.CANCELLED, "已取消：尚未开始执行")
                return execution._result.result()

            execution._cancel_requested = True
            job = execution._job
            try:
                confirmed = await self._stop_backend()
            except _StopAborted:
                execution._finish(
                    ExecutionState.UNKNOWN,
                    "控制上下文已关闭，停止结果未确认",
                )
                self._current = None
            else:
                if confirmed:
                    execution._finish(ExecutionState.CANCELLED, "已取消")
                    self._current = None
                    self._advance_locked()
                else:
                    execution._finish(
                        ExecutionState.UNKNOWN, "取消时未能确认设备已停止"
                    )
                    _unconfirm_all(self._resources, self)
                    self._stop_queue_locked("前序动作取消未获确认，未开始执行")
                    self._current = None
        if job is not None:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        return execution._result.result()

    async def _run(self, execution: Execution) -> None:
        try:
            outcome = await self._backend.run(execution._target)
        except asyncio.CancelledError:
            return
        except TargetRejected as error:
            await self._settle(
                execution, ExecutionState.FAILED, f"设备拒绝目标：{error}"
            )
            return
        except Disconnected as error:
            await self._settle(
                execution, ExecutionState.UNKNOWN, f"连接中断：{error}"
            )
            return
        except Exception as error:  # noqa: BLE001 - Adapter 异常不能留下永远等待的执行
            await self._settle(
                execution, ExecutionState.FAILED, f"执行异常：{error!r}"
            )
            return

        if outcome.state is CommandState.COMPLETED:
            await self._settle(execution, ExecutionState.SUCCEEDED, outcome.detail)
        elif outcome.state is CommandState.STOPPED:
            if execution._cancel_requested:
                await self._settle(execution, ExecutionState.CANCELLED, "已取消")
            else:
                await self._settle(
                    execution,
                    ExecutionState.FAILED,
                    outcome.detail or "设备中止了动作",
                )
        elif outcome.state is CommandState.FAILED:
            await self._settle(
                execution, ExecutionState.FAILED, outcome.detail or "执行失败"
            )
        else:
            await self._settle(
                execution, ExecutionState.UNKNOWN, outcome.detail or "执行结果未知"
            )

    async def _settle(
        self, execution: Execution, state: ExecutionState, detail: str | None
    ) -> None:
        async with self._lock:
            if execution._result.done():
                return
            execution._finish(state, detail)
            if self._current is not execution:
                return
            self._current = None
            if state is ExecutionState.UNKNOWN:
                _unconfirm_all(self._resources, self)
                self._stop_queue_locked("前序执行结果未知，未开始执行")
            elif state is ExecutionState.FAILED:
                self._stop_queue_locked("前序动作失败，未开始执行")
            self._advance_locked()

    async def _close(self) -> bool:
        job: asyncio.Task[None] | None = None
        try:
            async with self._lock:
                if self._closed:
                    return (
                        _resources_state(self._resources)
                        is ResourceState.UNCONFIRMED
                    )
                self._closed = True
                self._entered = False
                self._stop_queue_locked("控制上下文已关闭")
                current = self._current
                if current is None or current._state.terminal:
                    released = True
                else:
                    current._cancel_requested = True
                    job = current._job
                    try:
                        confirmed = await self._stop_backend()
                    except _StopAborted:
                        confirmed = False
                    if confirmed:
                        current._finish(
                            ExecutionState.CANCELLED, "控制上下文已关闭"
                        )
                        released = True
                    else:
                        current._finish(
                            ExecutionState.UNKNOWN, "关闭时未能确认设备已停止"
                        )
                        _unconfirm_all(self._resources, self)
                        released = False
                    self._current = None
            if job is not None:
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
            if released:
                _release_all(self._resources, self)
            return _resources_state(self._resources) is ResourceState.UNCONFIRMED
        except asyncio.CancelledError:
            # 退出被取消时无法再确认设备停止：收束执行，把资源转为可显式恢复的
            # UNCONFIRMED，不能留下无人管理的 HELD。
            self._closed = True
            self._entered = False
            self._stop_queue_locked("关闭被取消，未开始执行")
            current = self._current
            self._current = None
            job = None
            if current is not None:
                current._finish(
                    ExecutionState.UNKNOWN, "关闭被取消，停止结果未确认"
                )
                job = current._job
            try:
                # 先终结并发 run()/cancel() 正在等待的停止请求，再结束执行；
                # 资源恢复交接之前不能还有旧控制的 stop 在途。
                await self._abort_pending_stops()
                if job is not None:
                    job.cancel()
                    await asyncio.gather(job, return_exceptions=True)
            finally:
                _unconfirm_all(self._resources, self)
            raise

    async def _stop_backend(self) -> bool:
        task = asyncio.create_task(self._backend.stop())
        self._stops.add(task)
        try:
            return await task
        except asyncio.CancelledError:
            if not task.cancelled():
                # 等待方自身被取消：一并终结后端停止，不能留下孤儿请求。
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            raise _StopAborted from None
        finally:
            self._stops.discard(task)

    async def _abort_pending_stops(self) -> None:
        tasks = tuple(self._stops)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _new_execution_locked(self, target: Trajectory) -> Execution:
        execution = Execution(self._next_id, target, self)
        self._next_id += 1
        self._queue.append(execution)
        return execution

    def _advance_locked(self) -> None:
        if (
            self._closed
            or self._current is not None
            or not self._queue
            or not self._entered
            or not _all_owned_by(self._resources, self)
        ):
            return
        execution = self._queue.popleft()
        execution._state = ExecutionState.RUNNING
        self._current = execution
        execution._job = asyncio.create_task(self._run(execution))

    def _stop_queue_locked(self, reason: str) -> None:
        while self._queue:
            self._queue.popleft()._finish(ExecutionState.CANCELLED, reason)

    def _require_open_locked(self) -> None:
        if self._closed:
            raise ControlClosed("控制上下文已关闭")
        if not self._entered:
            raise ControlNotEntered(
                "控制上下文尚未进入；请通过 async with 获取控制后再提交动作"
            )

    def _require_usable_locked(self) -> None:
        if not _all_owned_by(self._resources, self):
            raise ControlNotEntered(
                f"控制上下文已不再持有资源 {_describe(self._resources)}；"
                "不能继续提交动作"
            )
        if _resources_state(self._resources) is ResourceState.UNCONFIRMED:
            raise ResourceUnconfirmed(
                f"资源 {_describe(self._resources)} 的上一次使用未确认结束；"
                "确认设备已停止后调用相应资源的 confirm_stopped()"
            )


class JointTrajectory(Capability):
    """关节轨迹能力。"""

    def __init__(
        self, backend: TrajectoryBackend, resources: tuple[Resource, ...]
    ) -> None:
        self._backend = backend
        self.resources = resources

    def control(self) -> TrajectoryControl:
        return TrajectoryControl(self._backend, self.resources)


class MotorControl(Capability):
    """底层电机能力；目标覆盖式发送，不排队。"""

    def __init__(self, backend: MotorBackend, resources: tuple[Resource, ...]) -> None:
        self._backend = backend
        self.resources = resources

    def control(self) -> MotorStream:
        return MotorStream(self._backend, self.resources)


class MotorStream:
    """底层电机的控制上下文；``send`` 立即覆盖上一个目标。"""

    def __init__(self, backend: MotorBackend, resources: tuple[Resource, ...]) -> None:
        self._backend = backend
        self._resources = resources
        self._send_lock = asyncio.Lock()
        self._pending_send: asyncio.Task[None] | None = None
        self._entered = False
        self._closed = False
        self._sent = False

    async def __aenter__(self) -> Self:
        if self._closed:
            raise ControlClosed(
                "电机控制上下文已关闭，不能再次进入；请创建新的控制入口"
            )
        if self._entered:
            raise ControlError("电机控制上下文已经进入，不能重复进入")
        _acquire_all(self._resources, self)
        self._entered = True
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        unconfirmed = await self._close()
        if not unconfirmed:
            return
        if isinstance(exc, BaseException):
            exc.add_note(f"关闭时未能确认资源 {_describe(self._resources)} 已停止")
            return
        raise StopUnconfirmed(
            f"关闭电机控制时未能确认资源 {_describe(self._resources)} 已停止；"
            "设备实际状态需要外部确认"
        )

    async def send(self, target: MotorTarget) -> None:
        """发送新目标；它立即取代之前的目标，不等待之前的目标完成。"""
        _validate_motor_target(target)
        async with self._send_lock:
            if self._closed:
                raise ControlClosed("电机控制上下文已关闭")
            if not self._entered:
                raise ControlNotEntered(
                    "电机控制上下文尚未进入；请通过 async with 获取控制后再发送"
                )
            self._backend.validate(target)
            if not _all_owned_by(self._resources, self):
                raise ControlNotEntered(
                    f"电机控制上下文已不再持有资源 "
                    f"{_describe(self._resources)}；不能继续发送"
                )
            # 把后端发送单独变成一个任务，退出被取消时才能取消并等待它终结，
            # 而不是让在途发送在资源恢复后继续落命令。
            job = asyncio.create_task(self._backend.send(target))
            self._pending_send = job
            try:
                # 先标记“可能已发出”：send 被取消或异常时也要在关闭时请求停止。
                self._sent = True
                await job
            except asyncio.CancelledError:
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
                raise
            except Disconnected:
                _unconfirm_all(self._resources, self)
                raise
            finally:
                if self._pending_send is job:
                    self._pending_send = None

    async def _close(self) -> bool:
        try:
            async with self._send_lock:
                if self._closed:
                    return (
                        _resources_state(self._resources)
                        is ResourceState.UNCONFIRMED
                    )
                self._closed = True
                self._entered = False
                released = True
                if self._sent:
                    if await self._backend.stop():
                        _release_all(self._resources, self)
                    else:
                        _unconfirm_all(self._resources, self)
                        released = False
                else:
                    _release_all(self._resources, self)
                return not released
        except asyncio.CancelledError:
            # 退出被取消时先终结在途发送，再保留可显式恢复的 UNCONFIRMED：不能让
            # 旧发送在资源被确认并交给新控制之后落下。
            self._closed = True
            self._entered = False
            try:
                await self._abort_pending_send()
            finally:
                _unconfirm_all(self._resources, self)
            raise

    async def _abort_pending_send(self) -> None:
        job = self._pending_send
        if job is None:
            return
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)


class Camera(Capability):
    """相机能力；查询语义与 :func:`buqi.snapshot` 相同。"""

    def __init__(self, backend: CameraBackend) -> None:
        self._backend = backend

    def snapshot(self, *, timeout: float = 1.0) -> CameraFrame:
        """取得最近一张有效帧；不会解码 JPEG。"""
        _validate_timeout(timeout)
        return self._backend.snapshot(timeout)


def build_capability(
    kind: type[Capability],
    backend: object,
    resources: tuple[Resource, ...],
) -> Capability:
    """按声明的能力类型构造能力对象；由绑定流程调用。"""
    if kind is JointTrajectory:
        if not resources:
            raise DeclarationError("轨迹能力缺少执行资源")
        return JointTrajectory(cast(TrajectoryBackend, backend), resources)
    if kind is MotorControl:
        if not resources:
            raise DeclarationError("电机能力缺少执行资源")
        return MotorControl(cast(MotorBackend, backend), resources)
    if kind is Camera:
        return Camera(cast(CameraBackend, backend))
    raise DeclarationError(f"未注册的能力类型：{kind!r}")


def _validate_trajectory(target: Trajectory) -> None:
    if not isinstance(target, Trajectory):
        raise TargetError("target 必须是 Trajectory")
    if not target.joints:
        raise TargetError("轨迹至少需要一个关节")
    if len(set(target.joints)) != len(target.joints):
        raise TargetError("轨迹中的关节名不能重复")
    if any(not joint for joint in target.joints):
        raise TargetError("关节名不能为空")
    if not target.waypoints:
        raise TargetError("轨迹至少需要一个路径点")

    previous = -1.0
    for index, waypoint in enumerate(target.waypoints):
        if len(waypoint.positions) != len(target.joints):
            raise TargetError(
                f"第 {index} 个路径点有 {len(waypoint.positions)} 个位置，"
                f"但轨迹声明了 {len(target.joints)} 个关节"
            )
        for position in waypoint.positions:
            _require_finite(position, f"第 {index} 个路径点的位置")
        _require_finite(waypoint.time_from_start, f"第 {index} 个路径点的时间")
        if waypoint.time_from_start < 0:
            raise TargetError("路径点时间不能为负")
        if waypoint.time_from_start <= previous:
            raise TargetError("路径点时间必须严格递增")
        previous = waypoint.time_from_start


def _validate_motor_target(target: MotorTarget) -> None:
    if not isinstance(target, MotorTarget):
        raise TargetError("target 必须是 MotorTarget")
    if not target.positions:
        raise TargetError("电机目标至少需要一个位置")
    for position in target.positions:
        _require_finite(position, "电机目标位置")
    _require_finite(target.valid_for, "电机目标有效期")
    if target.valid_for <= 0:
        raise TargetError("电机目标有效期必须为正数秒")


def _require_finite(value: object, description: str) -> None:
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TargetError(f"{description} 必须是有限数值，实际为 {value!r}")


def _validate_timeout(timeout: float) -> None:
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise TargetError("timeout 必须是正数秒")


__all__ = [
    "Camera",
    "CameraBackend",
    "Capability",
    "CommandOutcome",
    "CommandState",
    "Execution",
    "ExecutionResult",
    "ExecutionState",
    "JointTrajectory",
    "JointWaypoint",
    "MotorBackend",
    "MotorControl",
    "MotorStream",
    "MotorTarget",
    "Resource",
    "ResourceState",
    "Trajectory",
    "TrajectoryBackend",
    "TrajectoryControl",
]
