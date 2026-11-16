# 开发指南

本文说明如何修改和验证 Buqi。项目定位、总体架构与当前进度见[项目首页](../README.md)；
网关启动、监听与配置契约见[网关文档](gateway.md)。
查找专题或维护文档时，阅读[文档地图与写作约定](README.md)。

## 环境准备

需要 Nix、devenv 和 just；direnv 可选。系统级依赖由根目录的 `devenv.nix` 管理，
Rust 依赖由 Cargo 管理，Python 环境与依赖由根目录的 `pyproject.toml` / `uv.lock`
管理；Python 源码仍在 `sdk/python/src`。

在仓库根目录进入开发环境，任选一种方式：

```sh
direnv allow     # 已启用 direnv 的 shell
# 或
devenv shell
```

环境提供 Rust 工具链、cmake/libclang、GStreamer（含 `gst-plugins-base`、
`gst-plugins-good`、`gst-plugins-bad` 与 `gst-plugins-ugly`）、uv 等依赖。
nixpkgs 默认不构建 plugins-rs 的 `webrtc` 插件，`devenv.nix` 用同一份源码覆写
构建 `webrtc`/`rtp`，因此首次进入环境会额外编译该插件；随后才有 `webrtcsink`。
首次构建 DDS 桥接依赖时会编译内置 CycloneDDS，耗时可能较长。

仓库根目录同时是 Python 项目，首次使用或依赖变化后同步环境：

```sh
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
| `just test-python` | 修改 Python SDK 后 | 在仓库根目录运行 pytest；覆盖 `buqi.connect` 的失败语义与媒体行为，不需要网关 |
| `just check-python-dist` | 修改打包配置或发布前 | 构建 wheel/sdist、从 sdist 重建、干净安装并运行回环示例；产物留在 `target/python-dist` |
| `just ci-act [版本]` | 本地复现 GitHub Actions 的 Python 作业 | 需要 `act` 与 Docker/Podman；默认逐版本串行跑 3.12–3.15，再跑 `dist` 作业；传版本号只跑对应矩阵项 |
| `just acceptance` | 跨进程闭环验收 | 构建 workspace 并安装 `dds` 依赖组，启动真实网关进程与本地模拟 DDS 发布端；DDS 限制在回环与测试 domain 199 |
| `just test-integration` | 验证 DDS → 网关 → Zenoh → SDK 的完整模拟链路 | 构建网关与 Python 镜像，在两个隔离网络里用官方宇树 SDK 发布 G1 状态并跑持续订阅与生命周期场景；需要 Nix 与 Podman/Docker，镜像保留、容器与网络清理 |
| `just test` | 提交前的统一验收 | 依次运行 Rust 测试、Python 测试、发行物检查、本地回环验收与多容器验收；独立检查互不阻塞，依赖未通过的标为 NOT RUN，任一失败返回非零 |
| `just fix` | 本地自动修正 | 格式化并执行 Clippy 自动修复，会修改源码，完成后检查差异 |
| `just watch` | 开发中持续检查 | 监听 `crates/`，触发 Rust workspace 编译检查 |

- `quality` 不包含 doctest、Python、实机通信或功能验收；各步骤失败会中止任务。
- `check-python-dist` 使用临时虚拟环境与官方 PyPI，不借用仓库 `.venv` 或开发组；
  只验证 Linux x86_64 上的实际结果，不代表其他平台。
- `ci-act` 在本地容器中执行 workflow；act 的 action 缓存不能安全并发 matrix，配方逐版本调用。
  Podman 用户需先设置 `DOCKER_HOST`。它不能替代 GitHub 托管 runner、权限与缓存环境的真实验证。
- `acceptance` 会临时占用本机 TCP 端口并启动子进程；不访问外部网络，也不连接实体机器人。
- `test-integration` 首次构建需要下载基础镜像、从源码编译 CycloneDDS 0.10.2，并可能编译
  Nix 闭包；每次都创建唯一命名的容器与网络，结束时清理，镜像与构建缓存保留。
  判定标准、场景与产物见[多容器模拟通信验收](integration-acceptance.md)。
- `check`、`quality` 会产生构建缓存；依赖配置变化时，Cargo 也可能更新锁文件。
- `fix` 用于本地修正，不作为只读检查或 CI 入口；修正后仍需运行检查。
- `watch` 只监听 `crates/`；修改 `sdk/rust`、Python 或根配置后需手动验证。

## 模块职责与边界

以下是代码归属与目标边界，不表示所有功能已经实现。

| 模块 | 职责 | 边界 |
| --- | --- | --- |
| `crates/buqi-core` | 网关配置、监听地址、Router/DDS 监督与可选媒体模块 | 收纳实际复用的逻辑，供 Server/CLI 共用；不把 binary crate 当库 |
| `crates/buqi-server` | 网关程序入口，前台运行到 `Ctrl+C` | 薄入口，启动逻辑在 Core；不承载厂商 SDK 或机器人业务 |
| `crates/buqi-cli` | 维护者命令入口，当前只有 `buqi server` | 负责参数与交互，复用 Core；不做后台服务管理或诊断命令 |
| `sdk/python` | Python 客户端接口及机器人 Adapter；根 `pyproject.toml` / `uv.lock` 是发行与依赖配置 | `buqi.connect` 封装官方 `eclipse-zenoh` 并返回原生 Session；机器人适配留在客户端 |
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
现有测试覆盖接口快照选择、配置契约（含覆盖 `exit_on_failure`/`timeout_ms` 的敌意配置）、
DDS 观察窗口内外的失败与 panic、Router 两客户端转发、端口占用导致的启动失败，
以及 CLI 的错误退出。媒体测试用合成 `videotestsrc → jpegenc` 管线验证真实 JPEG
经 appsink 的采集路径，并用进程内 Zenoh 回环验证单帧/观测的 key、元数据、原样 JPEG、
`no_frame`/`stale`、冻结帧不重复发布、查询持有不回复映射为超时、Session 默认
查询期限短于 SDK 期限时仍按 SDK deadline 交付，以及运行期 `not-negotiated`
按确定性错误终止。WebRTC 测试验证信令端口监听、静态网页目录、观看者门控推送与
未知编码器报告不可用；它们需要 `devenv` 构建的 `webrtcsink` 与 `x264enc`。
这些测试都不连接实体相机或真实浏览器。编译与测试成功不代表 DDS 已与真实机器人
互通，也不代表 V4L2 caps 协商或 USB 掉线恢复已验证。

### Python

`just test-python` 验证连接失败语义、图片元数据解析、错误映射、单帧回复与
“只保留最新帧”的观测队列；测试用进程内 Zenoh peer 充当服务端，不需要网关进程，
也不证明真实网关的完整媒体链路。
`just check-python-dist` 额外构建 wheel 与 sdist，从 sdist 重建 wheel，在干净虚拟环境
安装标准 wheel，验证 `py.typed`、许可证与包元数据，并运行 `examples/loopback.py`。
它不使用仓库 `.venv`、开发组或源码路径；产物在 `target/python-dist`，临时环境随即清理。
`just acceptance` 覆盖网关进程启动、Router 转发、模拟 DDS 桥接的完整 CDR 载荷、
不可达端点失败与 `Ctrl+C` 退出；它显式安装 `dds` 依赖组，默认开发环境不包含 DDS。
`just test-integration` 进一步覆盖跨容器的 DDS 自动发现：官方宇树 SDK 以 10 Hz 发布
G1 `LowState_`，应用侧用 Buqi SDK 订阅并用官方消息类型逐条解码校验，测量 60 秒持续订阅、
两个订阅者互不影响、晚加入、重新订阅、发布端重启与网关重启后的恢复。它不连接实体机器人，
也不代表真实机器人网络或 QoS 的硬实时保证；细节见[多容器模拟通信验收](integration-acceptance.md)。

Python 发行要求 CPython 3.12 或更高；`.github/workflows/python.yml` 在 Linux x86_64
覆盖 3.12–3.15 的锁文件安装与 pytest，并单独执行发行物检查。其他平台未声明已验证。
`.github/workflows/integration.yml` 是独立的多容器集成作业（Linux x86_64、Python 3.12），
用同一镜像构建方式运行 `scripts/integration-acceptance.sh`；它尚未在托管 runner 上实际执行。

`just acceptance` 把 DDS 限制在 `127.0.0.1` 并使用测试 domain 199，属于隔离环境；
多容器验收使用 CycloneDDS 默认接口发现，但仍未覆盖真实机器人网络、网卡切换、
TLS/ACL 与媒体链路。

### 联调与实机

仓库内已有本地回环验收，但没有可复现的实体机器人与真实网关联调步骤。
媒体模块的实机检查（V4L2 设备占用、MJPEG caps、掉线恢复、PC2 编码器）以及
真实浏览器 WebRTC 会话（SDP/ICE/RTP、网页）同样待授权后执行。
记录验证结果时，区分本地检查、模拟联调与实机结果，并注明未验证的部分。
后续部署步骤应记录网关和客户端的桥接方式、网络条件及适用机器人。

涉及实体机器人运动时，先获得维护者明确确认，核对本地安全保护与停止方式，
再执行约定的验证；普通构建、导入或测试流程不默认连接实机或发送运动命令。
