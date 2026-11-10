# 网关启动、监听与配置

本文说明首版 `buqi server` 的启动契约：监听哪些地址、如何配置、DDS 插件如何启动与降级、
客户端如何连接。定位与总体架构见[项目首页](../README.md)，环境与验证任务见[开发指南](DEVELOPMENT.md)。

> 当前只支持 Linux + IPv4。监听地址的选择与绑定不是访问控制，也不代表远端可达。

## 启动方式

`buqi`（`buqi-cli` 包）与 `buqi-server` 都以前台进程运行，直到 `Ctrl+C`：

```sh
buqi server                    # 自动选择监听地址
buqi server --config buqi.toml # 使用 TOML 配置
buqi-server --config buqi.toml # 只部署网关二进制时等价
```

启动后打印一次实际监听结果，每行是一个可直接交给 Zenoh 的完整端点：

```text
Buqi 网关已启动（Zenoh Router + DDS 插件桥接）
客户端连接端点（可直接用于 Zenoh）：
  tcp/192.168.1.20:7447
  tcp/127.0.0.1:7447
```

输出只说明本机已监听，不保证某个客户端可达；网卡地址变化后需要重启网关。
`Ctrl+C` 会停止 DDS 任务、关闭 Session 并退出。

## 自动选择监听地址

配置中没有可用的 `listen.endpoints` 时：

- 选择已启用并在运行（`IFF_UP` 且 `IFF_RUNNING`）、由真实设备承载的普通有线/Wi-Fi 接口；
- 只取这些接口上的 RFC1918 私有 IPv4（`10/8`、`172.16/12`、`192.168/16`）；
- 符合条件的地址全部监听，不只看默认路由；
- 始终额外监听 `127.0.0.1`；
- 固定端口 **7447**；端口被占用或绑定失败会报错退出，不会自动换端口。

VPN/TUN、容器网桥、veth、bridge、VLAN 等不自动选择，需要时用配置文件显式列出。
判断依据是 Linux sysfs：接口存在 `device` 链接，且地址为私有 IPv4；不根据接口名前缀猜测。

没有符合条件的局域网地址属于正常情况：只监听 `127.0.0.1` 并提示本机范围，仍正常启动。
接口枚举失败、配置非法、绑定失败则作为启动错误返回，不会伪装成“没有局域网”。

选择结果是一次启动快照，不扫描网段、不探测远端、不持续监控或重绑接口。

## TOML 配置

`--config` 读取原生 Zenoh TOML 配置格式（与 `zenohd` 的字段同名），Buqi 在其上施加启动契约。
未提供 `--config` 时不会自动搜索配置文件。

```toml
# 显式监听列表，完整替换自动选择（不再自动追加 127.0.0.1）。
listen = { endpoints = ["tcp/192.168.1.20:7447", "tcp/127.0.0.1:7447"] }

[plugins.dds]
domain = 0      # 插件默认 0；设置了有效的 ROS_DOMAIN_ID 时用该值
# 其余字段沿用 zenoh-plugin-dds：scope、allow、deny、localhost_only 等
```

- `listen.endpoints` 写字符串数组。非空数组完整替换自动选择；省略或空数组表示自动选择。
  Zenoh 原生的按模式写法（`{ router = [...] }`）不参与选择，会按“未显式配置”处理。
- 端点必须包含传输前缀，例如 `tcp/192.168.1.20:7447`；裸 `host:port` 会被拒绝。
- `mode` 固定为 `router`，scouting（多播与 gossip）被关闭：客户端使用显式端点连接，
  不依赖自动发现。
- `plugins` 只接受 `dds`；插件由网关自己启动并监督，不能被 Zenoh 的插件加载器再启动一次。
- DDS 始终启用，不提供关闭开关；Domain、topic 过滤等语义沿用插件本身。

完整示例见 [examples/buqi.toml](../examples/buqi.toml)。

## DDS 插件

网关在同一个进程、同一个 Zenoh runtime 中启动 `zenoh-plugin-dds`。DDS 的网卡选择与发现交给
CycloneDDS 默认行为，不由 Buqi 生成网卡覆盖配置，也不要求机器人侧改 DDS 端口或 IP。
Zenoh 侧自动选择或只监听回环，不会把 DDS 一起限制到 `127.0.0.1`；两侧的接口选择相互独立。

启动后有一个约 2 秒的观察窗口：

- 窗口内未观察到失败：打印“DDS 插件已启动”。这**不是** ready 信号，不表示 DDS 已完成发现、
  机器人已连接或数据已互通。
- 主任务提前返回、返回错误或 panic：打印警告，说明 DDS 不可用，**Zenoh Router 继续运行**；
  不自动重启。修复原因后需要重启网关。
- 正常 `Ctrl+C` 关停不会误报为启动失败。

插件没有公开的 ready/清理协议，因此首版只承诺对“被监督的 Rust 主任务”可观察：

- 底层 C/FFI 回调、CycloneDDS 自身线程中的错误不会表现为该任务退出；
- 不承诺捕获 `panic=abort`、SIGSEGV 或依赖内部的所有失败；
- 关停时取消任务，DDS 原生资源由进程退出释放，不承诺可安全重新初始化。

## 客户端连接

Python SDK 的首版入口是 `buqi.connect(endpoint)`：

```python
import buqi

with buqi.connect("tcp/192.168.1.20:7447") as session:
    with session.declare_subscriber("example/state") as subscriber:
        sample = subscriber.recv()
        print(sample.key_expr, sample.payload.to_bytes().hex())
```

- 返回官方 `eclipse-zenoh` 的 `Session`，订阅、收发与资源管理沿用官方 API；
- 固定 client 模式，只连接传入的端点，关闭 scouting，不会误连其他 Router；
- 端点必须完整（含 `tcp/`），连接失败或超过约 3 秒抛 `zenoh.ZError`；
- 载荷是不透明的 `ZBytes`，SDK 不做 DDS/机器人消息解码。

## 安全边界

首版面向隔离、受信任网络中的开发使用：

- Zenoh 端点是明文 TCP，未启用认证、TLS 或 ACL；能连到端口的人即可收发。
- DDS 插件默认双向桥接，`allow`/`deny` 默认不过滤 topic。
- 自动选择监听地址只是减少无关接口暴露，不是“识别受信任网络”，也不能替代防火墙。

## 验证范围

`just acceptance` 用真实网关进程、两个 Zenoh 客户端与本地模拟 DDS 发布端验证：
监听端点输出、Router 转发、DDS 桥接的完整 CDR 载荷、连接失败与 `Ctrl+C` 退出。

该脚本把 DDS 限制在 `127.0.0.1`、使用测试 domain 199，不连接实体机器人；
同机多进程限制在回环也避免开发机的多播环回差异影响结论。
它验证的是桥接机制，不等价于跨主机发现与真实机器人网络的可达性。
自动选择、无局域网、接口枚举失败由单元测试与固定快照覆盖，未在每次验收中重复扫描本机网络。
