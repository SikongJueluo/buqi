# buqi-server 网络探测与端点输出：官方资料调研

> 范围：为 Rust `buqi-server` 做“启动后显示可连接 host/port”的资料准备；只调研，不实现。
> 调研基线（2026-11-09）：锁文件中 `zenoh` / `zenoh-plugin-dds` 1.10.2；媒体依赖非本文重点。
> 标记：`[当前]` 仓库事实 · `[调研]` 一手资料 · `[建议·待确认]` 待决策 · `[未验证]` 未运行/不确定。
> 已确认范围：只列本机服务端点；面向本机与受信任局域网；不扫描设备、不做客户端自动发现。
> 除上述范围外，监听策略、输出格式与安全策略尚未确认，本文不是实现契约。
> 2026-11-10：首版网关已实现，§1 的旧事实与 §7 的建议以[网关启动与配置](gateway.md)为准。

## 0. 结论

- `[调研]` 纯 Router 的官方最小启动是 `zenoh::open(config)` 且 `mode=router`（`zenohd` 即如此），
  它不是 HTTP server。
- `[调研]` 静态嵌入 DDS 插件时，官方 `zenoh-bridge-dds` 走 `RuntimeBuilder` +
  `PluginsManager::static_plugins_only` + `declare_static_plugin` + `runtime.start()`。
- `[调研]` 公开 Session API 中，读取实际 listen 端点的文档化入口是 `session.info().locators()`
  （`unstable`）；`Runtime::get_locators*()` 也有同等能力，但属于 `internal`。端口 0 已替换为真实
  端口，wildcard 按本机接口展开。
- `[调研]` 该 API 只覆盖本机 listen，不含 connect、scouting UDP 7446 或远端节点。首版只输出本机
  端点，不做设备扫描或客户端自动发现；`scout`/gossip 仅在 §4 作概念区分。
- `[调研]` 有效 feature 是依赖图并集：`zenoh-ext` 的 `unstable` 启用 `zenoh/internal`，DDS 插件
  依赖 zenoh-ext(unstable)，所以当前清单虽未直接写 `internal`，有效 graph 仍可满足 plugin 门槛；
  不能仅据直接清单断言编译失败（未编译验证）。

## 1. 当前实现事实

- `[当前]` 网关定位是 Zenoh Router + `zenoh-plugin-dds`，不是 HTTP web server，见
  [README.md](../README.md#L21-L24)、[DEVELOPMENT.md](../docs/DEVELOPMENT.md#L53-L58)。
- `[当前]` 版本：zenoh 1.10.2（`plugins`,`unstable`）；zenoh-plugin-dds 1.10.2
  （`default-features = false`）；gstreamer 0.25。见 [Cargo.toml](../Cargo.toml#L20-L21)、
  [Cargo.lock](../Cargo.lock#L3990-L3993)。
- `[当前]` Server 仅 `println!("buqi-server")` 占位，Core 仅文档注释，无探测/启动逻辑，见
  [main.rs](../crates/buqi-server/src/main.rs#L1-L3)、[lib.rs](../crates/buqi-core/src/lib.rs#L1-L4)。

## 2. 官方启动路径

### 2.1 最小 Router：`zenoh::open`

`zenohd` 1.10.2 的启动主线是：读配置、未设 mode 则设为 Router、`zenoh::open(config)`。见
[main.rs#L91](https://github.com/eclipse-zenoh/zenoh/blob/1.10.2/zenohd/src/main.rs#L91)、
[#L119](https://github.com/eclipse-zenoh/zenoh/blob/1.10.2/zenohd/src/main.rs#L119)。

以下为伪代码（未编译），仅示意 API 调用顺序，不代表片段已通过编译。

```rust
use zenoh::config::WhatAmI;
let mut config = zenoh::Config::default();                 // 默认 mode = peer
config.set_mode(Some(WhatAmI::Router)).unwrap();
config.listen.endpoints.set(vec!["tcp/[::]:0".parse().unwrap()]).unwrap();
let session = zenoh::open(config).await?;                  // 默认路径下监听已绑定（见下）
let locators = session.info().locators().await;            // unstable
```

- `[调研]` 默认 mode 是 `peer`；Router 默认 listen `tcp/[::]:7447`，peer 默认 `tcp/[::]:0`。
  `zenoh::open` 的顺序是 `build` → 创建 Session → `runtime.start()`。默认配置
  `timeout_ms=0` + `exit_on_failure=true` 时，绑定失败会使 open 失败；若 `exit_on_failure=false`，
  open 成功不保证所有配置端点都已绑定（超时非 0 时转后台重试），locators 以实际绑定结果为准。见
  [DEFAULT_CONFIG.json5#L101-L121](https://github.com/eclipse-zenoh/zenoh/blob/1.10.2/DEFAULT_CONFIG.json5#L101-L121)、
  [orchestrator.rs#L553-L575](https://github.com/eclipse-zenoh/zenoh/blob/1.10.2/zenoh/src/net/runtime/orchestrator.rs#L553-L575)、
  [session.rs#L1461-L1478](https://github.com/eclipse-zenoh/zenoh/blob/1.10.2/zenoh/src/api/session.rs#L1461-L1478)。

### 2.2 静态嵌入 DDS 插件（官方 bridge 同款，含 feature 门槛）

官方 `zenoh-bridge-dds` 的启动代码见
[main.rs#L259-L282](https://github.com/eclipse-zenoh/zenoh-plugin-dds/blob/1.10.2/zenoh-bridge-dds/src/main.rs#L259-L282)：

以下为伪代码（未编译）；结构参考官方 bridge，不代表片段已在本仓库编译。

```rust
use zenoh::internal::{plugins::PluginsManager, runtime::RuntimeBuilder};
config.insert_json5("plugins/dds", "{}").unwrap();         // 插件启动要求该配置
let mut pm = PluginsManager::static_plugins_only();
pm.declare_static_plugin::<zenoh_plugin_dds::DDSPlugin, &str>("dds", true);
let mut runtime = RuntimeBuilder::new(config).plugins_manager(pm).build().await?;
runtime.start().await?;                                    // 绑定 listen、启动 scouting
let locators = runtime.get_locators_noloopback();          // internal 访问器
```

- `[调研]` `build()` 只创建 runtime 并启动插件，不绑定监听；`start()` 才执行
  `Orchestrator::start`。需要 Session 时，官方插件用 `zenoh::session::init(runtime.clone().into())`
  复用同一 runtime（`internal` API）；静态插件必须在 `build()` 前声明，`zenoh::open` 只会加载
  动态插件。见 [runtime/mod.rs#L805-L852](https://github.com/eclipse-zenoh/zenoh/blob/1.10.2/zenoh/src/net/runtime/mod.rs#L805-L852)、
  [session.rs#L172-L178](https://github.com/eclipse-zenoh/zenoh/blob/1.10.2/zenoh/src/api/session.rs#L172-L178)、
  [loader.rs#L50-L52](https://github.com/eclipse-zenoh/zenoh/blob/1.10.2/zenoh/src/api/loader.rs#L50-L52)。
- `[调研]` **feature 门槛**：1.10.2 的 `compile_error!` 要求 `plugins` 的有效 feature 同时含
  `unstable` 和 `internal`；`RuntimeBuilder`、`PluginsManager`、`session::init` 都在 `internal`
  模块下，见 [lib.rs#L1019-L1076](https://github.com/eclipse-zenoh/zenoh/blob/1.10.2/zenoh/src/lib.rs#L1019-L1076)。
- `[调研]` 有效 feature 是整个依赖图的并集：`zenoh-ext` 1.10.2 的 `unstable` 定义为
  `["zenoh/internal", "zenoh/unstable"]`，而 DDS 插件依赖 `zenoh-ext` 并开启 `unstable`。
  因此当前清单虽未直接写 `internal`，graph 中仍会启用；不能据此断言编译失败。见
  [zenoh-ext/Cargo.toml#L32](https://github.com/eclipse-zenoh/zenoh/blob/1.10.2/zenoh-ext/Cargo.toml#L32)、
  [zenoh-plugin-dds/Cargo.toml#L56-L58](https://github.com/eclipse-zenoh/zenoh-plugin-dds/blob/1.10.2/Cargo.toml#L56-L58)。
- `[建议·待确认]` 若 buqi 代码直接 `use zenoh::internal::...`（如 `RuntimeBuilder`），应在自己的
  zenoh 依赖显式声明 `internal`（必要时 `internal_config`），把传递激活变成直接契约，避免上游
  feature 变化时被动；这不是“已确定缺陷”，但正式构建仍需 `cargo check` 验证。

## 3. 获取实际 listen locators（端口 0）

`[调研]` 公开 Session API 中，读取 listen 端点的文档化入口是 `SessionInfo::locators()`
（`unstable`，返回 `Vec<Locator>`）；`Runtime::get_locators*()` 也有同等能力，但属 `internal`，
不算稳定公开 API。打印即 `for l in locators { println!("{l}"); }`（如 `tcp/192.168.1.20:45123`）。
见 [docs.rs](https://docs.rs/zenoh/1.10.2/zenoh/session/struct.SessionInfo.html#method.locators) 与
[info.rs#L110-L126](https://github.com/eclipse-zenoh/zenoh/blob/1.10.2/zenoh/src/api/info.rs#L110-L126)。

- `[调研]` 端口 0 与 wildcard：listener 先 `bind` 得到 `local_addr` 并回写 endpoint，所以 locator
  中的端口是实际端口；地址为 `0.0.0.0`/`[::]` 时按接口枚举地址、共用实际端口，显式 IP 则直接
  返回 endpoint。`locators()` 调 `get_locators_noloopback()`，只过滤 wildcard 展开出的 loopback，
  显式 `tcp/127.0.0.1:...` 仍返回。见
  [listener.rs#L148-L193](https://github.com/eclipse-zenoh/zenoh/blob/1.10.2/io/zenoh-link-commons/src/listener.rs#L148-L193)、
  [tcp/unicast.rs#L293-L330](https://github.com/eclipse-zenoh/zenoh/blob/1.10.2/io/zenoh-links/zenoh-link-tcp/src/unicast.rs#L293-L330)。
- `[调研+推断]` `[::]` 的展开同时包含 IPv4：`get_ipv6_ipaddrs` 依次拼非 link-local IPv6、公网 IPv4、
  link-local IPv6、私网 IPv4；`0.0.0.0` 只展开 IPv4。接口枚举在 Unix 上首次调用时缓存
  （`lazy_static` + `pnet_datalink::interfaces()`），只保留 up/running，不筛接口类型；TUN、网桥、
  Docker、VPN 地址都会进入候选。见
  [net/mod.rs#L378-L455](https://github.com/eclipse-zenoh/zenoh/blob/1.10.2/commons/zenoh-util/src/net/mod.rs#L378-L455)、
  [#L30-L34](https://github.com/eclipse-zenoh/zenoh/blob/1.10.2/commons/zenoh-util/src/net/mod.rs#L30-L34)、
  [#L148-L170](https://github.com/eclipse-zenoh/zenoh/blob/1.10.2/commons/zenoh-util/src/net/mod.rs#L148-L170)。
- `[调研]` 只覆盖 transport listeners，不含 connect 端点或 scouting 的 224.0.0.224:7446。
  `print_locators()` 会把结果写入 runtime 状态并打日志 `Zenoh can be reached at: <locator>`，见
  [orchestrator.rs#L615-L623](https://github.com/eclipse-zenoh/zenoh/blob/1.10.2/zenoh/src/net/runtime/orchestrator.rs#L615-L623)。
- `[未验证]` docs.rs 示例注释写 `["tcp/127.0.0.1:7447"]`，与 1.10.2 默认 peer listen
  `tcp/[::]:0` 不一致；只能当示例，不能当语义契约。

## 4. listen / connect / scouting / gossip

| 机制 | 方向与范围 | 1.10.2 默认 | 对输出的影响 |
| --- | --- | --- | --- |
| `listen` | 本机入站 | Router `tcp/[::]:7447`；peer `tcp/[::]:0` | `locators()` 的数据来源 |
| `connect` | 本机出站拨号 | 空 | 不出现于 `locators()` |
| scouting / gossip | UDP `224.0.0.224:7446`；已连接会话间 | 多播默认 TTL 1（不跨路由）；Router 应答但不自动连；gossip 开启 | 发现邻居、传播 locator 候选 |

- `[调研]` Router 默认不自动连其他 Router，必须配置 `connect`。scout 源是 loopback 时 Hello 给含
  loopback 的 locators，否则给 noloopback 版本；linkstate/gossip 中本节点用
  `get_locators_noloopback()`、其他节点用缓存 locators。`scout()` 返回
  `Hello { zid, whatami, locators }`。首版不依赖 scout/gossip。见
  [deployment](https://zenoh.io/docs/getting-started/deployment/)、
  [DEFAULT_CONFIG.json5#L147-L202](https://github.com/eclipse-zenoh/zenoh/blob/1.10.2/DEFAULT_CONFIG.json5#L147-L202)、
  [orchestrator.rs#L1287-L1295](https://github.com/eclipse-zenoh/zenoh/blob/1.10.2/zenoh/src/net/runtime/orchestrator.rs#L1287-L1295)、
  [network.rs#L338-L347](https://github.com/eclipse-zenoh/zenoh/blob/1.10.2/zenoh/src/net/protocol/network.rs#L338-L347)、
  [z_scout.rs](https://github.com/eclipse-zenoh/zenoh/blob/1.10.2/examples/examples/z_scout.rs)、
  [wrappers.rs#L107-L129](https://github.com/eclipse-zenoh/zenoh/blob/1.10.2/commons/zenoh-config/src/wrappers.rs#L107-L129)。

## 5. DDS 插件与两侧多网卡

- `[调研]` 插件字段见 [config.rs#L30-L58](https://github.com/eclipse-zenoh/zenoh-plugin-dds/blob/1.10.2/zenoh-plugin-dds/src/config.rs#L30-L58)；
  **没有** bind_interface/interface，且启用 deny_unknown_fields（未知键会被拒绝）。
- `[调研]` localhost_only=true 会向 CYCLONEDDS_URI 前置 `<NetworkInterface address="127.0.0.1"/>`
  的 XML，默认来自 ROS_LOCALHOST_ONLY=1；否则用 `dds_create_participant(domain, null, null)`
  交给 CycloneDDS。见 [lib.rs#L228-L262](https://github.com/eclipse-zenoh/zenoh-plugin-dds/blob/1.10.2/zenoh-plugin-dds/src/lib.rs#L228-L262)、
  [config.rs#L49-L50](https://github.com/eclipse-zenoh/zenoh-plugin-dds/blob/1.10.2/zenoh-plugin-dds/src/config.rs#L49-L50)。
- `[调研]` CycloneDDS 当前官方文档说明默认选择首选接口，多网卡可列多个 `<NetworkInterface>`；
  IPv4 默认，IPv6 用 udp6/tcp6 且不混用。CYCLONEDDS_URI 可指向文件或内联 XML。
  以下是滚动文档，未核对本项目 cyclors 所带 CycloneDDS 的具体版本，配置仍需版本复核与验证。见
  [CycloneDDS 网络接口](https://cyclonedds.io/docs/cyclonedds/latest/config/network_interfaces.html)、
  [CycloneDDS 配置](https://cyclonedds.io/docs/cyclonedds/latest/config/index.html)。
- `[调研]` Zenoh 侧 `#iface=<name>`（Linux 调 bind_device，其他平台带警告空操作）或 `#bind=<addr>`
  指定本地源地址，二者不能同用；`#iface=` 也把 wildcard 的 locator 展开限制到该接口。两侧无
  统一开关：`#iface=` 不影响 CycloneDDS，`CYCLONEDDS_URI` 不影响 Zenoh listener。见
  [DEFAULT_CONFIG.json5#L23-L34](https://github.com/eclipse-zenoh/zenoh/blob/1.10.2/DEFAULT_CONFIG.json5#L23-L34)、
  [tcp.rs#L136-L146](https://github.com/eclipse-zenoh/zenoh/blob/1.10.2/io/zenoh-link-commons/src/tcp.rs#L136-L146)、
  [net/mod.rs#L465-L495](https://github.com/eclipse-zenoh/zenoh/blob/1.10.2/commons/zenoh-util/src/net/mod.rs#L465-L495)。

## 6. 端点输出约束与不可保证项

- `[调研+推断]` `locators()` 是“本机已绑定地址”，不是可达性探测；防火墙、NAT、VPN/TUN、Docker
  网桥都可能让 locator 从另一台主机不可达。NAT 下私网 locator 不能直接给外网使用，Zenoh 默认
  不提供 NAT 穿透或中继保证。
- `[调研]` multicast scouting 默认 TTL=1，不会跨路由转发；通常限同一链路/子网，L2 只是常见
  环境而非严格等价。VPN/TUN 或跨子网通常看不到，见
  [DEFAULT_CONFIG.json5#L155-L161](https://github.com/eclipse-zenoh/zenoh/blob/1.10.2/DEFAULT_CONFIG.json5#L155-L161)。
- `[调研]` `[::]` 的 IPv4 locator 依赖 OS 双栈；Zenoh 不显式设 IPV6_V6ONLY，Linux 默认
  bindv6only=0（[man7](https://man7.org/linux/man-pages/man2/IPV6_V6ONLY.2const.html)）。其他 OS/改过
  sysctl 不能假定成立；link-local IPv6 会进入候选，但不带 zone id，远端如何使用不确定。
- `[调研]` 没有“扫描所有 host/port”的 API：本笔记范围只输出本机 listener；任意 TCP 服务、
  非 Zenoh 主机不可见。不要命名为“所有可连接 host”，也不要承诺未验证的可达性。

## 7. 建议待确认（历史记录，已被实现取代）

> 首版取固定 7447、显式逐地址监听并排除虚拟接口，不生成 CycloneDDS 网卡覆盖；本节仅供追溯。

- `[建议·待确认]` 输出模型：只列本机 listen locators；不做 scout/设备发现，也不做客户端自动发现。
  不要合并成“可连接列表”；展示保留 Locator 原串并解析 host/port，IPv6 加方括号。
- `[建议·待确认]` listen/过滤：用 `tcp/[::]:0` 或 `tcp/0.0.0.0:0` 让内核选端口再读 `locators()`；
  固定端口用 7447。是否排除 link-local、Docker、TUN/VPN，去重/排序规则，需产品层决定。
- `[建议·待确认]` 若直接使用 `zenoh::internal`（如 `RuntimeBuilder`）：有效 graph 可能已由
  `zenoh-ext` 传递启用 `internal`，但应在自身 zenoh 依赖显式声明它（必要时 `internal_config`），
  作为直接依赖契约；`internal` 非稳定 API，升级需重验。或改 cdylib + `zenoh::open` 动态加载
  （`zenohd` 路径），代价是部署带 `.so`。
- `[建议·待确认]` DDS 侧固定机器人网卡时，显式设置 `CYCLONEDDS_URI` 的
  `<Interfaces><NetworkInterface .../></Interfaces>`；`localhost_only` 只用于同机场景。

## 8. 未验证

- `[未验证]` 未运行 `cargo check`/`build`；有效 feature 经 `zenoh-ext` 传递满足 plugin 门槛的
  判断、代码片段的可编译性、后台 listener retry 下 open 返回与实际绑定端点的差异，均未验证。
- `[未验证]` 未观察 `locators()` 在本机的实际输出（mihomo TUN、Docker 网桥、多网卡顺序），未验证
  端口 0、`[::]` 双栈、link-local、`#iface=` 的权限/非 Linux 行为，以及 `zenoh-plugin-dds` 与
  GStreamer/系统依赖的编译兼容性。
- `[未验证]` 未验证任何远端可达性、防火墙/NAT/VPN 穿透或真实机器人网络。调研代理未执行 VCS 操作、
  编译、服务启动、网络扫描或机器人通信。
