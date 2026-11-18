"""组合式 ``Robot`` 的声明、发现与显式绑定。

应用用普通类注解声明可能存在的部件和能力：:

    class Arm(Part):
        trajectory: JointTrajectory
        motors: MotorControl | None

    class MyRobot(Robot):
        arm: Arm
        camera: CameraPart | None

必需能力缺失时绑定失败；可选能力缺失时属性为 ``None``。发现结果用
:func:`discover` 读取，绑定只接受显式的设备/部件选择，不会自动挑第一个
候选。这里只做声明解析、选择校验与能力装配，执行语义在
:mod:`buqi.control`。
"""

from __future__ import annotations

import types
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import (
    Any,
    ClassVar,
    Protocol,
    Self,
    Union,
    get_args,
    get_origin,
    get_type_hints,
    runtime_checkable,
)

from .control import (
    Camera,
    Capability,
    JointTrajectory,
    MotorControl,
    Resource,
    build_capability,
)
from .errors import BindingError, DeclarationError
from .media import CameraFrame

_NONE_TYPE = type(None)


@dataclass(frozen=True)
class PartSelector:
    """显式定位一个部件：设备 ID + 设备内部件 ID。"""

    device_id: str
    part_id: str


@dataclass(frozen=True)
class PartOffer:
    """发现结果中的一个部件候选及其能力。

    ``resources`` 中的 :class:`~buqi.Resource` 由 Adapter 创建并保持身份；
    对同一物理资源的重复发现和重复绑定必须返回同一个对象，否则互斥会失效。
    """

    device_id: str
    part_id: str
    capabilities: frozenset[type[Capability]]
    resources: Mapping[type[Capability], tuple[Resource, ...]]

    @property
    def capability_names(self) -> tuple[str, ...]:
        return tuple(sorted(kind.__name__ for kind in self.capabilities))

    @property
    def selector(self) -> PartSelector:
        return PartSelector(self.device_id, self.part_id)


@dataclass(frozen=True)
class Device:
    """一次发现的设备；``parts`` 是该设备内可选的部件。"""

    id: str
    parts: tuple[PartOffer, ...]

    def part(self, part_id: str) -> PartOffer:
        for offer in self.parts:
            if offer.part_id == part_id:
                return offer
        available = "、".join(offer.part_id for offer in self.parts) or "（无）"
        raise BindingError(f"设备 {self.id!r} 没有部件 {part_id!r}；可用部件：{available}")


@dataclass(frozen=True)
class PartSession:
    """Adapter 打开一个部件后返回的能力后端。"""

    capabilities: Mapping[type[Capability], object]


@runtime_checkable
class Adapter(Protocol):
    """设备侧 Adapter 需要实现的最小接口。

    ``discover`` 只报告候选，不建立控制；``open_part`` 打开显式选中的部件。
    两者都可能访问网络，因此都是异步的。
    """

    async def discover(self) -> Sequence[Device]: ...

    async def open_part(self, offer: PartOffer) -> PartSession: ...


@dataclass(frozen=True)
class _CapabilitySpec:
    name: str
    kind: type[Capability]
    required: bool


@dataclass(frozen=True)
class _PartSpec:
    name: str
    part_type: type[Part]
    required: bool
    capabilities: tuple[_CapabilitySpec, ...]


class Part:
    """一组能力的具名操作目标；由 ``Robot`` 字段引用。

    子类用类型注解声明能力，例如 ``trajectory: JointTrajectory``。
    必需能力缺失时绑定失败，可选的 ``X | None`` 在设备不提供时绑定为 ``None``。
    """

    _capability_specs_cache: ClassVar[tuple[_CapabilitySpec, ...] | None] = None

    @classmethod
    def _declared_capabilities(cls) -> tuple[_CapabilitySpec, ...]:
        cached = cls.__dict__.get("_capability_specs_cache")
        if cached is None:
            cached = _resolve_capability_specs(cls)
            cls._capability_specs_cache = cached
        return cached


class CameraPart(Part):
    """常见相机部件的预定义组合：一个图片能力，外加直接的快照入口。"""

    images: Camera

    def snapshot(self, *, timeout: float = 1.0) -> CameraFrame:
        """取得最近一张有效帧；语义与 :func:`buqi.snapshot` 相同。"""
        return self.images.snapshot(timeout=timeout)


class Robot:
    """逻辑组合入口；一个实例可以组合来自不同 Adapter 的部件。

    字段是 ``Part`` 子类（必需）或 ``Part | None``（可选）。绑定后属性访问
    就是普通类型化属性，不需要运行时查询能力注册表。
    """

    _part_specs_cache: ClassVar[tuple[_PartSpec, ...] | None] = None

    @classmethod
    def _declared_parts(cls) -> tuple[_PartSpec, ...]:
        cached = cls.__dict__.get("_part_specs_cache")
        if cached is None:
            cached = _resolve_part_specs(cls)
            cls._part_specs_cache = cached
        return cached

    @classmethod
    async def bind(
        cls,
        adapters: Adapter | Sequence[Adapter],
        /,
        **selectors: PartOffer | PartSelector,
    ) -> Self:
        """发现候选并显式绑定；任何必需项或能力不匹配都会报错。

        ``discover`` 可以返回空结果：全可选的 ``Robot`` 会绑定为全部 ``None``；
        必需字段仍需选择，选择不存在的设备仍报错。
        """
        specs = cls._declared_parts()
        known = {spec.name for spec in specs}

        unknown = sorted(set(selectors) - known)
        if unknown:
            raise BindingError(
                f"{cls.__name__} 没有这些部件字段：{'、'.join(unknown)}；"
                f"已声明：{'、'.join(sorted(known))}"
            )
        missing = [spec.name for spec in specs if spec.required and spec.name not in selectors]
        if missing:
            raise BindingError(
                f"{cls.__name__} 缺少必需部件选择：{'、'.join(missing)}"
            )

        adapter_list = _normalize_adapters(adapters)
        devices, owners = await _discover(adapter_list)

        offers: dict[str, PartOffer] = {}
        for spec in specs:
            selected = selectors.get(spec.name)
            if selected is None:
                continue
            selector = _as_selector(selected, spec.name)
            offer = _find_offer(devices, selector)
            missing_capabilities = [
                f"{capability.name}（{capability.kind.__name__}）"
                for capability in spec.capabilities
                if capability.required and capability.kind not in offer.capabilities
            ]
            if missing_capabilities:
                available = "、".join(offer.capability_names) or "（无）"
                raise BindingError(
                    f"{spec.name} 选择的 {offer.device_id}/{offer.part_id} "
                    f"缺少必需能力：{'、'.join(missing_capabilities)}；"
                    f"该部件提供：{available}"
                )
            offers[spec.name] = offer

        opened: dict[str, PartSession] = {}
        for name, offer in offers.items():
            session = await owners[offer.device_id].open_part(offer)
            if not isinstance(session, PartSession):
                raise BindingError(
                    f"Adapter 打开 {offer.device_id}/{offer.part_id} 时未返回 PartSession"
                )
            opened[name] = session

        robot = _construct(cls)
        for spec in specs:
            offer = offers.get(spec.name)
            if offer is None:
                setattr(robot, spec.name, None)
                continue
            part = _build_part(spec, offer, opened[spec.name])
            setattr(robot, spec.name, part)
        return robot


async def discover(*adapters: Adapter) -> tuple[Device, ...]:
    """读取候选设备；不会建立控制，也不自动选择。"""
    devices, _ = await _discover(adapters)
    return devices


def _normalize_adapters(adapters: Adapter | Sequence[Adapter]) -> tuple[Adapter, ...]:
    if isinstance(adapters, Adapter):
        return (adapters,)
    normalized = tuple(adapters)
    if not normalized:
        raise BindingError("绑定至少需要一个 Adapter")
    return normalized


async def _discover(
    adapters: Sequence[Adapter],
) -> tuple[tuple[Device, ...], dict[str, Adapter]]:
    if not adapters:
        raise BindingError("绑定至少需要一个 Adapter")

    devices: list[Device] = []
    owners: dict[str, Adapter] = {}
    for adapter in adapters:
        for device in await adapter.discover():
            _validate_device(device)
            if device.id in owners:
                raise BindingError(
                    f"多个 Adapter 报告了同一个设备 ID {device.id!r}；"
                    "无法判断应由哪一个打开"
                )
            owners[device.id] = adapter
            devices.append(device)
    return tuple(devices), owners


def _validate_device(device: Device) -> None:
    if not isinstance(device, Device):
        raise BindingError(f"Adapter.discover() 返回了非 Device 对象：{device!r}")
    if not isinstance(device.id, str) or not device.id:
        raise BindingError("设备 ID 必须是非空字符串")
    if not device.parts:
        raise BindingError(f"设备 {device.id!r} 没有报告任何部件")
    seen: set[str] = set()
    for offer in device.parts:
        if not isinstance(offer, PartOffer):
            raise BindingError(f"设备 {device.id!r} 返回了非 PartOffer 对象")
        if offer.device_id != device.id:
            raise BindingError(
                f"部件 {offer.part_id!r} 声明的设备 ID {offer.device_id!r} "
                f"与所属设备 {device.id!r} 不一致"
            )
        if not isinstance(offer.part_id, str) or not offer.part_id:
            raise BindingError("部件 ID 必须是非空字符串")
        if offer.part_id in seen:
            raise BindingError(f"设备 {device.id!r} 重复报告部件 {offer.part_id!r}")
        seen.add(offer.part_id)
        _validate_offer(offer)


def _validate_offer(offer: PartOffer) -> None:
    if not offer.capabilities:
        raise BindingError(
            f"{offer.device_id}/{offer.part_id} 没有报告任何能力"
        )
    for kind in offer.capabilities:
        if not (isinstance(kind, type) and issubclass(kind, Capability)):
            raise BindingError(
                f"{offer.device_id}/{offer.part_id} 报告了非能力类型 {kind!r}"
            )
    extra = set(offer.resources) - set(offer.capabilities)
    if extra:
        raise BindingError(
            f"{offer.device_id}/{offer.part_id} 为未提供的能力声明了资源：{extra!r}"
        )
    for kind, resources in offer.resources.items():
        if not isinstance(resources, tuple) or any(
            not isinstance(resource, Resource) or not resource.name
            for resource in resources
        ):
            raise BindingError(
                f"{offer.device_id}/{offer.part_id} 的资源必须是带名称的 Resource 元组"
            )
        if len(set(resources)) != len(resources):
            raise BindingError(
                f"{offer.device_id}/{offer.part_id} 重复声明了同一个资源"
            )
    for controllable in (JointTrajectory, MotorControl):
        if controllable in offer.capabilities and not offer.resources.get(controllable):
            raise BindingError(
                f"{offer.device_id}/{offer.part_id} 提供 {controllable.__name__} "
                "但没有声明执行资源；无法与其他控制入口互斥"
            )


def _find_offer(devices: Sequence[Device], selector: PartSelector) -> PartOffer:
    for device in devices:
        if device.id == selector.device_id:
            return device.part(selector.part_id)
    available = "、".join(device.id for device in devices) or "（无）"
    raise BindingError(
        f"没有设备 {selector.device_id!r}；已发现：{available}"
    )


def _as_selector(value: PartOffer | PartSelector, field: str) -> PartSelector:
    if isinstance(value, PartOffer):
        return value.selector
    if isinstance(value, PartSelector):
        return value
    raise BindingError(
        f"部件字段 {field} 的选择必须是 PartOffer 或 PartSelector，实际为 {value!r}"
    )


def _build_part(
    spec: _PartSpec, offer: PartOffer, session: PartSession
) -> Part:
    part = _construct(spec.part_type)
    for capability in spec.capabilities:
        if capability.kind not in offer.capabilities:
            setattr(part, capability.name, None)
            continue
        backend = session.capabilities.get(capability.kind)
        if backend is None:
            raise BindingError(
                f"Adapter 打开 {offer.device_id}/{offer.part_id} 后没有提供 "
                f"{capability.kind.__name__} 的后端"
            )
        try:
            built = build_capability(
                capability.kind,
                backend,
                offer.resources.get(capability.kind, ()),
            )
        except DeclarationError as error:
            raise BindingError(str(error)) from error
        setattr(part, capability.name, built)
    return part


def _construct[T](cls: type[T]) -> T:
    try:
        return cls()
    except TypeError as error:
        raise BindingError(f"{cls.__name__} 必须能无参数构造：{error}") from error


def _resolve_capability_specs(cls: type[Part]) -> tuple[_CapabilitySpec, ...]:
    items = _annotation_items(cls)
    if not items:
        raise DeclarationError(f"部件类 {cls.__name__} 没有声明任何能力")
    specs: list[_CapabilitySpec] = []
    for name, hint in items:
        kind, optional = _unwrap_optional(cls, name, hint)
        if not (isinstance(kind, type) and issubclass(kind, Capability)):
            raise DeclarationError(
                f"部件类 {cls.__name__} 的字段 {name} 必须是 Capability 子类，"
                f"实际注解为 {hint!r}"
            )
        specs.append(_CapabilitySpec(name, kind, required=not optional))
    return tuple(specs)


def _resolve_part_specs(cls: type[Robot]) -> tuple[_PartSpec, ...]:
    items = _annotation_items(cls)
    if not items:
        raise DeclarationError(f"Robot 类 {cls.__name__} 没有声明任何部件字段")
    specs: list[_PartSpec] = []
    for name, hint in items:
        part_type, optional = _unwrap_optional(cls, name, hint)
        if not (isinstance(part_type, type) and issubclass(part_type, Part)):
            raise DeclarationError(
                f"Robot 类 {cls.__name__} 的字段 {name} 必须是 Part 子类，"
                f"实际注解为 {hint!r}"
            )
        specs.append(
            _PartSpec(
                name,
                part_type,
                required=not optional,
                capabilities=part_type._declared_capabilities(),
            )
        )
    return tuple(specs)


def _annotation_items(cls: type) -> list[tuple[str, Any]]:
    try:
        hints = get_type_hints(cls)
    except (NameError, TypeError) as error:
        raise DeclarationError(f"无法解析 {cls.__name__} 的类型注解：{error}") from error

    items: list[tuple[str, Any]] = []
    for name, hint in hints.items():
        if name.startswith("_"):
            continue
        if get_origin(hint) is ClassVar:
            continue
        items.append((name, hint))
    return items


def _unwrap_optional(
    cls: type, name: str, hint: Any
) -> tuple[Any, bool]:
    origin = get_origin(hint)
    if origin is Union or origin is types.UnionType:
        args = get_args(hint)
        concrete = tuple(argument for argument in args if argument is not _NONE_TYPE)
        if len(concrete) != 1 or len(concrete) == len(args):
            raise DeclarationError(
                f"{cls.__name__}.{name} 只支持 X 或 X | None，实际为 {hint!r}"
            )
        return concrete[0], True
    return hint, False


__all__ = [
    "Adapter",
    "CameraPart",
    "Device",
    "Part",
    "PartOffer",
    "PartSelector",
    "PartSession",
    "Robot",
    "discover",
]
