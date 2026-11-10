# 开发指南

本文说明如何修改和验证 Buqi。项目定位、总体架构与当前进度见[项目首页](../README.md)；
网关启动、监听与配置契约见[网关文档](gateway.md)。
查找专题或维护文档时，阅读[文档地图与写作约定](README.md)。

## 环境准备

需要 Nix、devenv 和 just；direnv 可选。系统级依赖由根目录的 `devenv.nix` 管理，
Rust 依赖由 Cargo 管理，Python 环境与依赖由 `sdk/python` 下的 uv 项目管理。

在仓库根目录进入开发环境，任选一种方式：

```sh
direnv allow     # 已启用 direnv 的 shell
# 或
devenv shell
```

环境提供 Rust 工具链、cmake/libclang、GStreamer、uv 等依赖。
首次构建 DDS 桥接依赖时会编译内置 CycloneDDS，耗时可能较长。

Python SDK 在独立项目目录中初始化：

```sh
cd sdk/python
uv sync
uv run python -c "import buqi; print(buqi.__version__)"
```

这里仅验证包可导入，不会建立机器人连接。后续 `just` 任务均在仓库根目录运行。

## 常用任务

[`.justfile`](../.justfile) 是命令配方的来源；此处说明选择时机、覆盖范围和副作用。

| 任务 | 使用场景 | 范围与注意事项 |
| --- | --- | --- |
| `just check` | 开发中的快速编译检查 | 检查 Rust workspace，不运行测试 |
| `just quality` | Rust 改动完成后的检查 | 格式检查、Clippy（警告视为错误）、Rust 测试；不含 Python 与端到端验收 |
| `just test-python` | 修改 Python SDK 后 | 在 `sdk/python` 运行 pytest；覆盖 `buqi.connect` 的失败语义，不需要网关 |
| `just acceptance` | 跨进程闭环验收 | 构建 workspace，启动真实网关进程与本地模拟 DDS 发布端；DDS 限制在回环与测试 domain 199 |
| `just fix` | 本地自动修正 | 格式化并执行 Clippy 自动修复，会修改源码，完成后检查差异 |
| `just watch` | 开发中持续检查 | 监听 `crates/`，触发 Rust workspace 编译检查 |

- `quality` 不包含 doctest、Python、实机通信或功能验收；各步骤失败会中止任务。
- `acceptance` 会临时占用本机 TCP 端口并启动子进程；不访问外部网络，也不连接实体机器人。
- `check`、`quality` 会产生构建缓存；依赖配置变化时，Cargo 也可能更新锁文件。
- `fix` 用于本地修正，不作为只读检查或 CI 入口；修正后仍需运行检查。
- `watch` 只监听 `crates/`；修改 `sdk/rust`、Python 或根配置后需手动验证。

## 模块职责与边界

以下是代码归属与目标边界，不表示所有功能已经实现。

| 模块 | 职责 | 边界 |
| --- | --- | --- |
| `crates/buqi-core` | 网关配置解析、监听地址选择、Router/DDS 启动与监督，以及后续媒体处理 | 收纳实际复用的逻辑，供 Server/CLI 共用；不把 binary crate 当库 |
| `crates/buqi-server` | 网关程序入口，前台运行到 `Ctrl+C` | 薄入口，启动逻辑在 Core；不承载厂商 SDK 或机器人业务 |
| `crates/buqi-cli` | 维护者命令入口，当前只有 `buqi server` | 负责参数与交互，复用 Core；不做后台服务管理或诊断命令 |
| `sdk/python` | Python 客户端接口及机器人 Adapter | `buqi.connect` 封装官方 `eclipse-zenoh` 并返回原生 Session；机器人适配留在客户端 |
| `sdk/rust` | Rust 客户端接口 | 在 Python 接口稳定后对齐能力与语义，不要求调用形式完全相同 |
| `examples` | 面向使用者的最小调用示例与配置样例 | 消费 SDK 的公开接口，不承载 SDK 实现 |

放置代码时按职责判断：

- 网关启动、配置解析与 DDS 监督放在 Core，由 Server/CLI 入口共用；不要依赖 `buqi-server` 二进制。
- 某种机器人的消息解释与接口适配放在 SDK/Adapter，避免进入网关。
- SDK 通过通信协议访问网关，不把 Server 可执行程序作为库依赖。
- 只有实际复用需求出现时再提取模块；新增依赖以对应 manifest 为准。

## 验证

### Rust

开发中使用 `just check` 快速反馈，完成 Rust 改动后运行 `just quality`。
现有测试覆盖接口快照选择、配置契约、DDS 任务结局分类与降级、Router 两客户端转发、
端口占用导致的启动失败，以及 CLI 的错误退出。编译与测试成功不代表 DDS 已与真实机器人互通。

### Python

`just test-python` 只验证连接失败语义与参数要求，不证明完整订阅链路。
`just acceptance` 覆盖网关进程启动、Router 转发、模拟 DDS 桥接的完整 CDR 载荷、
不可达端点失败与 `Ctrl+C` 退出。

验收脚本把 DDS 限制在 `127.0.0.1` 并使用测试 domain 199，属于隔离环境；
真实机器人网络、CycloneDDS 默认网卡发现、TLS/ACL 与媒体链路不在自动验收范围内。

### 联调与实机

仓库内已有本地回环验收，但没有可复现的实体机器人与真实网关联调步骤。
记录验证结果时，区分本地检查、模拟联调与实机结果，并注明未验证的部分。
后续部署步骤应记录网关和客户端的桥接方式、网络条件及适用机器人。

涉及实体机器人运动时，先获得维护者明确确认，核对本地安全保护与停止方式，
再执行约定的验证；普通构建、导入或测试流程不默认连接实机或发送运动命令。
