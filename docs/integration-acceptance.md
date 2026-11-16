# 多容器模拟通信验收

本文说明 `just test-integration`（也包含在 `just test` 中）验证什么、如何判定、产物在哪，
以及它不能证明什么。它用官方宇树 SDK 发布 G1 状态，经 DDS → 网关 → Zenoh 到 Buqi Python SDK，
不是实机测试，也不承诺硬实时或绝对零丢失。

## 拓扑

```text
机器人侧容器 ──robot-net(10.203.17.0/24)── 网关容器 ──app-net(10.203.18.0/24)── 应用容器 ×N
```

- 机器人侧容器只接机器人侧网络，应用容器只接应用侧网络，网关同时接两边；
  没有绕过网关的转发路径，也没有使用 host 网络。
- 两个网络是独立子网；网关在应用侧固定 `10.203.18.2`，`podman/docker start` 重启后
  端点不变，应用才能按原端点重连。
- DDS 发现只依赖机器人侧 CycloneDDS 默认的多播自动发现，不配置静态对端 IP。
- 应用侧使用显式 Zenoh 端点 `tcp/10.203.18.2:7447`，关闭 scouting。

## 镜像

| 镜像 | 内容 | 构建方式 |
| --- | --- | --- |
| `buqi-gateway` | 本次 `cargo build` 的 `buqi` 二进制及 Nix 运行库闭包 | `containers/gateway/image.nix`，nixpkgs 取自 `devenv.lock`；镜像内是完整 `/nix/store` 闭包，不是把 Nix ELF 复制进普通 Linux 镜像 |
| `buqi-python-test` | Python 3.12、源码构建的 CycloneDDS 0.10.2、固定提交的官方宇树 SDK、本次构建的 Buqi wheel | `containers/python/Containerfile` |

宇树 SDK 固定为提交 `814556d15970dd2ecf1c9984e845ca02ab07e206`，其 `cyclonedds==0.10.2`
没有 CPython 3.12 wheel，因此在镜像内从 CycloneDDS 0.10.2 源码编译；Buqi 不因此降低
Python 3.12 下限。接入编排之前先在 Python 3.12 容器里单独验证了安装、导入、`LowState_`
构造、序列化/反序列化与原生 DDS 收发；镜像构建末尾再做一次导入检查。

应用侧脚本以只读方式挂载仓库中的 `sdk/python/tests/integration`，镜像里安装的是本次构建的
Buqi wheel；因此脚本改动不需要重建镜像，但 wheel 或依赖变化会改变镜像 tag。
镜像 tag 由内容哈希决定，构建缓存与镜像保留，只清理容器与网络。

镜像构建后先跑 `self_check.py`（容器内、依赖官方消息类型）：NaN 电机角度必须被逐条校验拒绝，
窗口末尾静默必须计入最长间隔，注入解码失败时生命周期场景必须以非零结果失败。任一项失败即中止，
不进入场景执行。

## 消息与生成规则

模拟端用官方 `ChannelPublisher` 发布 `unitree_hg.msg.dds_.LowState_` 到 `rt/lowstate`
（DDS domain 199，10 Hz）。为区分解码错误、丢包与重启前后数据，约定字段按固定规则生成：

| 字段 | 规则 |
| --- | --- |
| `tick` | 本次发布进程内从 0 开始，每帧 +1 |
| `reserve[0]` | 本次发布进程的 epoch（启动时的 Unix 秒数），重启后变化 |
| `imu_state.rpy` | `[tick, epoch % 65536, 0.5]` |
| `motor_state[j].q` | `j + tick * 0.001` |
| 其余字段 | 官方默认构造函数的值 |

订阅端用官方 `IdlStruct.deserialize` 解码完整载荷（含 4 字节小端 CDR 表示头），
并逐条校验上表字段。任何一条不符都计入 `invalid`，不当作丢包放过。

## 判定标准

持续订阅（两个独立订阅者，同一 60 秒窗口）：

- 进程启动后 20 秒内收到首条有效消息；
- 不同序号覆盖**同一窗口实际发布数量**的 ≥ 95%（发布数量取自发布端
  `published.ndjson`，不假设恰好 600 条）；窗口内实际发布少于 300 条判失败；
- 相邻有效消息接收间隔 ≤ 2 秒（单调时钟）；
- 重复、乱序、逐条校验失败计数都为 0。

生命周期场景：

| 场景 | 判定 |
| --- | --- |
| 关闭一个订阅者 | 被关闭进程退出码 0；另一个订阅者计数继续增长且保持运行 |
| 晚加入 | 20 秒内收到首条有效消息，收到的是加入之后的后续数据，覆盖率与逐条校验同样成立 |
| 重新订阅 | 关闭后旧订阅取样本失败或为空；静默 3 秒；重建订阅后首条消息的 `tick` 大于关闭前的最大值 |
| 发布端重启 | 停止后可判定断流（最后有效接收超过 2 秒）；重启后 30 秒内收到新 epoch 数据 |
| 网关重启 | 应用显式关闭旧 Session，按固定间隔重连，重启后 30 秒内恢复接收；发布端数据身份不变 |

断流判断由验收程序按最后有效接收时间实现；本轮没有新增 SDK 自动重连或断流事件。

## 产物与失败处理

每次运行的唯一标识同时用于容器名、网络名与目录名。产物在
`target/acceptance/runs/<run-id>/`：

- `report.json`：镜像、运行时版本、阈值、每个场景的检查项与统计、清理结果；
- `logs/<容器名>.log`：网关、发布端与各应用的完整输出；
- 各端目录下的 `events.ndjson`、`published.ndjson`、`received.ndjson`、
  `heartbeat.ndjson`、`measured.json`、`final.json`。

脚本打印报告绝对路径。独立场景失败后其余场景继续；网关或发布端启动失败时，
依赖它们的场景标为 `NOT RUN` 并说明依赖。成功、失败和 `Ctrl+C` 都会停止并删除本次
容器与网络；清理失败同样让最终退出码非零。

## 命令与 CI

```sh
just test-integration   # 只跑多容器验收
just test               # Rust 测试、Python 测试、发行物检查、本地回环验收、多容器验收
```

需要 Linux x86_64、Nix、uv 与 Podman（`CONTAINER_RUNTIME=docker` 可切换到 Docker）。
devenv 会为本地 act 把 `DOCKER_HOST` 固定成 Podman 的 socket；Docker 路径下脚本会清除这个
指向 Podman 的值，并在开始前执行 `docker info` / `podman info`，确认选中的 daemon 真的可用。
`.github/workflows/integration.yml` 是独立的集成作业，用 Docker 与 devenv 运行同一脚本；
脚本本身已在本地用真实 Docker daemon（29.9.3，dind）跑通镜像构建与全部场景，
但该工作流尚未在 GitHub 托管 runner 上实际执行，不能据此认为 CI 已通过。

## 限制

- 不连接实体机器人、相机，也不发送运动命令；DDS 接口选择沿用 CycloneDDS 默认行为，
  没有覆盖真实机器人网络、多播配置差异或网卡切换。
- 不验证 DDS QoS 的硬实时保证、TLS/ACL、跨主机时间同步，也不把单次运行结果扩大为
  长期稳定性结论。
- 应用侧用官方消息类型解码是必过项；只收到字节不算通过。
