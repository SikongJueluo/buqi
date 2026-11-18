# 组合式 Robot 接口

本文说明 `buqi.Robot` 的声明、发现、绑定与执行语义。**当前实现是纯内存模拟切片**：
`buqi.simulation.SimulatedAdapter` 不连接网关、相机或实体机器人；真实机器人 Adapter
尚未实现。它验证的是接口形状与本地执行语义，不是实机控制保证。

应用用带类型的类声明部件，用带类型的属性声明能力：

```python
import buqi

class Arm(buqi.Part):
    trajectory: buqi.JointTrajectory
    motors: buqi.MotorControl | None

class MyRobot(buqi.Robot):
    arm: Arm
    camera: buqi.CameraPart | None
```

- 必需能力缺失时绑定失败；`X | None` 声明的能力缺失时属性为 `None`，应用用
  `is not None` 收窄。
- 用户显式选中的部件若缺少该部件的必需能力，绑定报错，不会因外层字段可选而静默变成
  `None`。
- 属性访问就是普通类型化字段，不在调用时查询能力注册表；编辑器与类型检查器看到的是
  注解里的类型。

## 发现与显式绑定

```python
devices = await buqi.discover(adapter)
for device in devices:
    for part in device.parts:
        print(device.id, part.part_id, part.capability_names)

robot = await MyRobot.bind(
    adapter,
    arm=devices[0].part("left_arm"),
    camera=devices[1].part("rgb"),
)
```

- 候选携带设备 ID、设备内部件 ID 与能力信息；`Device.part()` 返回 `PartOffer`，
  可直接作为绑定选择。
- `bind` 只接受显式选择。存在多个同能力部件时不会默认挑第一个；缺少必需字段、
  未知字段、未知设备/部件、重复设备 ID 都报 `BindingError`。
- 选择是必需的：可选部件不传选择时为 `None`，不会自动匹配。
- `discover()` 可以返回空元组：全可选的 `Robot` 会绑定为全部 `None`；必需字段
  仍必须有选择，选择不存在的设备仍报 `BindingError`。
- 一个 `Robot` 可以组合来自多个 Adapter 的部件；同一设备 ID 不能由多个 Adapter
  重复报告。

`Adapter` 只需实现两个异步方法：`discover()` 报告候选，`open_part(offer)` 打开
显式选中的部件并返回能力后端。绑定后，一次连接期间对象身份固定；设备掉线不会让属性
悄悄变成 `None` 或替换成别的设备。

## 控制上下文与执行

```python
async with robot.arm.trajectory.control() as control:
    execution = await control.enqueue(trajectory)
    result = await execution.result()
    print(result.state)
```

- `control()` 返回异步上下文管理器，独占该能力的执行资源；冲突时抛
  `ResourceBusy`，不会抢占其他入口。
- 控制入口必须先进入才能使用：未进入的对象调用 `enqueue`/`run`/`send` 抛
  `ControlNotEntered`；关闭后不能再进入或继续提交，需创建新的控制入口。
- `enqueue(target)` 追加到有界等待队列（上限 8）；队列满抛 `QueueFull`，不静默
  丢弃。空闲时立即开始执行。
- `run(target)` 抢占：先取消尚未开始的队列，再请求结束当前动作；只有 Adapter
  确认设备停止后才启动新动作，否则抛 `StopUnconfirmed`，且不启动新动作。
  目标校验在改动当前动作和队列之前完成。
- `Execution.result()` 只等待已经发起的执行结果，不重发动作；等待方被取消不会
  取消执行。`Execution.cancel()` 对已结束执行幂等。
- 结果状态是 `queued`、`running`、`succeeded`、`failed`、`cancelled` 或
  `unknown`。失败不是异常，应用据状态决定是否重试。
- 队列只保证启动顺序，不判断业务效果。下一步依赖上一步的效果时，先取得结果并在
  应用里判断，再提交下一步；不要预先排队。

队列错误只停止“这条队列”：设备报错、能力承诺未兑现或结果无法确认时，未开始的
动作被取消，控制上下文仍可继续提交。动作按承诺完成但业务效果不满意（例如抓取后没
抓住）由应用判断，SDK 不猜业务目标。

## 资源、停止确认与断连

- Adapter 为能力声明执行资源，并保持资源对象的身份；同一资源上的轨迹与底层电机
  互斥，即使来自不同 Part 或不同 Robot 绑定。独立资源可以并行。一项能力有多个
  资源时全部获取，失败则整体不占用。
- 上下文正常或异常退出都会结束未完成动作并等待 Adapter 确认。确认失败时资源进入
  `unconfirmed`：不释放、不允许新控制直接开始。应用确认设备实际状态后调用
  `resource.confirm_stopped()` 清理该状态。若退出时没有其他异常，控制上下文会抛
  `StopUnconfirmed`；若已有异常，则保留原异常并附加说明。
- 退出过程本身被取消时无法再等待确认：未完成执行结束为 `unknown`，在途电机发送
  以及并发 `run()`/`Execution.cancel()` 正在等待的停止请求都会被取消并等待终结，
  资源才转为 `unconfirmed`；这样旧控制不会在资源交接后继续发送命令。应用确认设备
  停止后同样用 `confirm_stopped()` 恢复。此时并发的 `run()` 不会向已关闭的上下文
  再加入执行，而是报 `ControlClosed`。
- 断连时正在执行的动作标记 `unknown`，未开始的动作取消，资源保持不可用；
  SDK 不会因为自身断连就宣称设备已经停止。重连不自动恢复运动、不重发动作，
  需要重新发现和绑定。
- 仲裁只覆盖当前客户端里的控制入口，不承诺跨进程、跨客户端或设备级安全排他。

```python
resource = robot.arm.trajectory.resources[0]
if resource.state is buqi.ResourceState.UNCONFIRMED:
    # 应用通过外部手段确认设备已停止之后
    resource.confirm_stopped()
```

## 持续目标与相机

- `MotorControl.control()` 返回 `MotorStream`；`send(MotorTarget(..., valid_for=))`
  发送即覆盖，不排队追赶旧目标。`valid_for` 必须由执行侧落实；模拟 Adapter 只校验
  上限，没有控制循环，不能据此推断实机截止保证。
- `CameraPart` 组合一个 `Camera` 能力并提供 `snapshot(timeout=...)`；返回
  `buqi.CameraFrame`，语义与 [`buqi.snapshot`](../sdk/python/README.md#相机图片)
  一致。真实相机接入应复用 `buqi.media`，不复制解码或协议实现。

## 当前限制

- 没有真实 Adapter、实机连接或运动验证；示例与测试全部使用模拟 Adapter。
- 轨迹与电机目标只表达关节序列和数值，未带单位、坐标系或限位元数据；首个真实
  Adapter 必须明确其单位和坐标系契约，单位换算与跨设备变换尚未实现。
- 未实现队列持久化、重连恢复、跨进程仲裁、实时截止和厂商级安全互锁。
- 兼容性以 CPython 3.12+ 和 `pyright` 基本模式为准；其他类型检查器未声明。

可运行示例见 [`examples/composed_robot.py`](../examples/composed_robot.py)：

```sh
uv run python examples/composed_robot.py
```
