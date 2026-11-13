# Python SDK 现状、分发与宇树接入调研

本文回答 SDK 已做到哪里、能否作为库分发，以及如何配合宇树（Unitree）SDK 接收模型动作。
调研时间：2026-11-13。区分**当前事实**、**建议**与**待验证**；不定义新的 `Robot` Interface，
不作为实机操作指南。模块归属仍以[开发指南](DEVELOPMENT.md#模块职责与边界)为准。

## 结论

- **已经能打包，也已经能通过 uv 从 GitHub 安装**；无需为此拆仓库或先发布 PyPI。
- 当前是“通信 + 图片”SDK，不是“模型动作 → 机器人控制”SDK；首个机器人 Adapter 尚未实现。
- 原生 Unitree SDK 绑定 CycloneDDS，不能直接接收 `buqi.connect()` 返回的 Zenoh Session。
- Unitree 的旧版 CycloneDDS 与本仓库的模拟 DDS 开发依赖冲突，**不代表两个发行包不能共用**。
- 建议先补齐分发保障，再在独立消费项目验证 Unitree，最后以明确的动作契约实现首个 Adapter。

## 1. 当前实现与验证范围

| 能力 | 本地事实与依据 |
| --- | --- |
| 包结构 | [`pyproject.toml`](../sdk/python/pyproject.toml)：Hatchling、`src/buqi`、包名 `buqi`、版本 `0.1.0`、Python `>=3.10` |
| 连接 | [`connect`](../sdk/python/src/buqi/_connect.py)：显式 Zenoh 端点、关闭自动发现、约 3 秒连接期限，返回原生 Session |
| 收发 | 沿用官方 Session；机器人 DDS 载荷仍为不透明字节，没有机器人消息解码 |
| 图片 | [`media.py`](../sdk/python/src/buqi/media.py)：`snapshot`、`frames`、JPEG/元数据、明确错误与只保留最新帧的队列 |
| 控制 | 无机器人 Adapter、模型动作转换、单位/坐标转换、运动执行或控制权管理；见[首页](../README.md#当前状态) |
| 依赖 | runtime 仅 `eclipse-zenoh>=1.10.2,<2`；`cyclonedds>=11.1.0` 与 pytest 属于开发组，不进入 wheel 的运行依赖 |
| DDS 验收 | [`tests/dds_publisher.py`](../sdk/python/tests/dds_publisher.py) 使用 CycloneDDS；[`acceptance.py`](../sdk/python/tests/acceptance.py) 只验证本地模拟 DDS → Zenoh 等链路，不是 Unitree 控制验收 |

本次在 Linux x86_64、Python 3.13.12、uv 0.13.4、eclipse-zenoh 1.10.2 下执行：

| 检查 | 结果 |
| --- | --- |
| `just test-python`（离线、冻结锁文件） | **15 passed**；只使用进程内/回环 Zenoh，无机器人 |
| `uv build --offline --no-sources … sdk/python` | 成功生成 wheel 与 sdist；首次构建曾因无 Hatchling 缓存失败，获取构建依赖后离线复验通过 |
| wheel 安装到新 venv，再用 `python -I` 导入 | 成功；实际从 `site-packages` 加载，`connect` / `snapshot` / `frames` 可用 |
| 独立消费项目从 GitHub `main` 安装并导入 | 成功，安装来源记录包含 `subdirectory=sdk/python` |

日志与发行物保存在本机忽略目录 `target/sdk-research/`：`local-checks.log`、`git-install.log`、
`external-checks.md`、`dist/`；它们不是需要提交的源码。
**未执行**跨进程 `just acceptance`、Unitree 安装/控制、实体相机或机器人验证；没有发布或推送。
这里只验证了一个 Python/Zenoh 组合，不等于声明的全部版本和平台都已验证。

## 2. uv 从 GitHub 使用：现在可行

以下命令在**使用 Buqi 的应用项目**中运行，不是在 Buqi 仓库根目录运行。[U1][U2]

```sh
# 跟踪开发分支；子项目路径必须写入 URL
uv add "git+https://github.com/SikongJueluo/buqi.git#subdirectory=sdk/python" --branch main

# 主动更新到该分支的新提交；普通 uv sync 会优先保留已锁定提交
uv sync --upgrade-package buqi

# 固定版本：将 <git-commit-sha> 换成已推送提交的实际 SHA
uv add "git+https://github.com/SikongJueluo/buqi.git#subdirectory=sdk/python" --rev <git-commit-sha>
```

uv 0.13.4 的 `uv add` 没有 `--git` / `--subdirectory` 标志；使用 URL 前缀和 fragment。
它会写入普通项目依赖 `buqi` 以及以下 uv 源配置，`uv.lock` 再固定实际提交：

```toml
[tool.uv.sources]
buqi = { git = "https://github.com/SikongJueluo/buqi.git", subdirectory = "sdk/python", branch = "main" }
```

**本次远端与本地不同**：安装实测解析到远端 `main` 的 Git SHA
`7f301b1f5f818a3e790c5afe307a9006db1fd948`，导入后仅有 `connect`，没有 `snapshot` / `frames`；
本地工作区已有这些相机能力。这里的 SHA 是 uv 安装证据，不是 jj 操作引用。
Git 安装只能取得已推送内容；本地和远端都标 `0.1.0`，不能只用 `__version__` 判断能力。

不想每次推送时，在应用项目使用本地源码：[U1][U2]

```sh
# 把路径换成这台机器的实际路径；这是 editable，SDK 源码修改可直接生效
uv add --editable /home/sikongjueluo/Projects/buqi/sdk/python
```

推荐流程：**本机改代码用 editable → 跨机协作用 Git 分支 → 部署固定 Git SHA/tag → 稳定后 PyPI**。
有 SSH 权限时 URL 可换成 `git+ssh://git@github.com/SikongJueluo/buqi.git#subdirectory=sdk/python`；
SSH 形式未实测，本次验证的是 HTTPS。Git 引用不受本机 jj 的未推送工作层影响。

## 3. 成为可发布库还应补什么

当前发行物为 `buqi-0.1.0-py3-none-any.whl` 与 `buqi-0.1.0.tar.gz`，无需添加 `setup.py`。
Buqi wheel 自身没有二进制扩展，但依赖 eclipse-zenoh 的平台支持，不能据此承诺全平台可用。
**可构建不等于已完成公开发布准备**。以下均为建议，不是本次已实现项：

1. 补齐发行物中的许可证正文：仓库根目录有 [`LICENSE`](../LICENSE)，但本次 wheel/sdist
   都没有收录；只有 `License-Expression: Apache-2.0`。源码包必须自包含，不要只引用包根之外的文件。
2. 补 `project.urls`，把 SDK README 的仓库相对文档链接改为适合包索引阅读的链接；
   如对外提供内联类型，加入 `py.typed`；不必为此引入新打包后端。
3. 消除 manifest 与 `buqi.__version__` 两处手写版本的漂移风险；明确 Python SDK 的发版规则。
4. 自动验证测试、`uv build --no-sources`、wheel 安装导入、sdist 重建及最低支持版本；
   将普通 SDK 验证与需要 CycloneDDS 的模拟验收分开。[U3]
5. 首发前核对 PyPI 名称、凭据/Trusted Publishing 和 TestPyPI 流程；本次未检查名称可注册性、未上传。

**不要把 Git-only 的 Unitree 直接依赖写进将发布到 PyPI 的 Buqi 元数据。**
`tool.uv.sources` 只对 uv 生效，不会替下游声明一个 Git 依赖；而显式 `pkg @ git+…` 依赖会被
PyPI/Warehouse 拒绝。[U1][P1] 让应用显式选择厂商 SDK 来源，是当前更简单的做法。

## 4. Unitree 集成：两个需要先处理的问题

### 4.1 DDS 与 Zenoh 不是可互换的 Session

核对官方 `unitree_sdk2_python` 的提交 `814556d15970dd2ecf1c9984e845ca02ab07e206`：[T1–T4]

- 包名 `unitree_sdk2py`，版本 `1.0.1`；运行依赖固定 `cyclonedds==0.10.2`，另含 numpy、opencv-python。
- `ChannelFactoryInitialize(id, networkInterface)` 创建 CycloneDDS Domain/Participant，
  publisher/subscriber 使用 DataWriter/DataReader；没有传入 Zenoh Session 的 Interface。
- G1 高层运动使用 **`LocoClient`**，不是 Go2 等机型的 `SportClient`。
  G1 高层请求/响应使用 DDS `rt/api/sport/request` 与 `rt/api/sport/response` topic，
  按请求 id 匹配应答；不是可以直接替换成 Zenoh query 的 ROS 服务。[T3]

在不改变既有项目职责的前提下，有两条路径（**设计候选**）：

- **复用原生 SDK**：应用 Adapter 调用 Unitree；应用侧保留 DDS 环境，并按已确认部署方式配置
  DDS ↔ Zenoh 桥，经现有网关到机器人。需补齐[首页原生兼容路径](../README.md#两种使用路径)
  已实测但未记录的拓扑。这不意味着把厂商 SDK 部署到网关或机器人上。
- **Zenoh 原生 Adapter**：在客户端实现 Unitree 消息 CDR 编解码与请求/应答协议。
  这会增加协议维护成本，不是简单传入 Session；必须验证 IDL、类型名、QoS、topic/key 映射与双向链路。

用户希望继续配合厂商 SDK，建议先复现第一条路径；不要在网关新增机器人业务或 monkey-patch
厂商的全局 ChannelFactory。桥接模拟消息成功不证明 G1 控制、RPC 或时延已验证。

### 4.2 CycloneDDS 版本冲突，但 Buqi 运行依赖并未冲突

本地 `dev` 要求 `>=11.1.0`，Unitree 要求 `==0.10.2`，无交集。
临时项目执行 `uv add --no-sync` 实测解析失败；只是解析试验，没有实际安装 Unitree。
uv 通常联合解析各 dependency group/extra，因此单纯移动到另一个默认未安装的组并不能解决。[U4]

**独立应用项目同时依赖 Buqi 发行包与 Unitree**，不会继承 Buqi 仓库的 dev 组，
因此可避开这个特定冲突。建议先这么验证；本仓库模拟 DDS 验收保留自己的环境。
暂不为此拆发行包、覆盖厂商依赖或宣称新版 CycloneDDS 与 Unitree 兼容。

PyPI 的 CycloneDDS `0.10.2` wheel 只提供 CPython 3.7–3.10，没有 3.11+ 或 Linux aarch64 wheel；
当前 Python 3.13 环境不能直接依赖预编译包，需验证源码构建、CycloneDDS C 库与
`CYCLONEDDS_HOME` 等安装条件。[T1][C1] 可先在独立 Python 3.10 环境验证依赖，
但这仍不等于运行支持保证；系统依赖继续通过 Nix/devenv 声明管理。

## 5. 从模型输出到执行：建议的最小闭环

这里仅描述职责，不预设统一 `Robot` Interface：

```text
应用读取模型输出 → 明确的结构化动作 → 参数/状态/有效期检查 → 客户端 Unitree Adapter
                                                    ↓
                                 厂商调用 → 通信链路 → 机器人原有控制器
                                                    ↑
                                         状态/应答 → 执行结果回到应用
```

- **模型相关解析留在应用**：不要让核心 SDK 依赖某个模型提供商、提示词或输出文本格式。
- **Adapter 收拢机器人差异**：单位、坐标系、动作到厂商调用的映射，以及应答、超时、资源生命周期。
  动作必须说明目标、适用状态、参数范围/单位、持续时间/有效期与失败含义；模型文本不直接获得执行权。
- **执行前的检查不能代替本地安全保护**：过期动作应拒绝，控制权、断线行为、停止方式需明确，
  实机运动仍须授权；不能把“发送成功”当作“运动完成”。

**建议顺序与完成判据：**

1. **分发基线**：完成 §3、保留 Git 安装示例；干净环境可从 wheel/sdist/Git 安装并导入。
2. **厂商依赖与只读链路**：独立项目固定 Unitree 提交，先做录制状态/模拟 DDS 测试，
   再经授权核对真实 G1 状态、网卡/domain、桥接拓扑；不先发送运动命令。
3. **一个动作闭环**：明确模型输出契约，先验证动作转换与 dry-run，再授权验证一个动作及停止/超时路径。
   若目标是导航，优先考虑高层速度命令；若模型输出关节轨迹，应单独设计低层执行器。
4. **按真实用例提炼 Interface**：首个机型闭环后再讨论多机器人共性，不提前堆通用插件/执行框架。

G1 官方低层例程含 29 电机、`control_dt_=0.002`（500 Hz）、Kp/Kd、控制模式、CRC 与状态回读。[T4]
这是特定例程，不是所有 G1/模型的统一契约，也不是广域 Zenoh 链路的实时保证。
关节模型输出还需专门确认关节顺序、坐标、频率、插值/限幅和低层控制部署，不能直接当 LowCmd 发送。

进入实现前最关键的待确认项：**首个目标是否 G1；模型输出是高层速度/动作、末端位姿，还是关节轨迹？**
另外需要维护者提供原生 SDK 经桥接实测的拓扑与版本；公开来源不能替代这一现场事实。

## 来源

本地事实来自 §1 链接的 manifest、源码、测试及本次命令输出；以下为一手外部来源：

- [U1] uv 0.13.4：[Git/路径依赖与 sources](https://github.com/astral-sh/uv/blob/7456b60/docs/concepts/projects/dependencies.md)
- [U2] uv：[同步、editable 与 Git 升级](https://docs.astral.sh/uv/concepts/projects/sync/)
- [U3] uv：[打包与发布](https://github.com/astral-sh/uv/blob/7456b60/docs/guides/package.md)
- [U4] uv：[联合解析与冲突声明](https://github.com/astral-sh/uv/blob/7456b60/docs/concepts/resolution.md)
- [T1] Unitree：[安装说明](https://github.com/unitreerobotics/unitree_sdk2_python/blob/814556d15970dd2ecf1c9984e845ca02ab07e206/README.md) · [依赖 manifest](https://github.com/unitreerobotics/unitree_sdk2_python/blob/814556d15970dd2ecf1c9984e845ca02ab07e206/setup.py)
- [T2] Unitree：[DDS Channel 实现](https://github.com/unitreerobotics/unitree_sdk2_python/blob/814556d15970dd2ecf1c9984e845ca02ab07e206/unitree_sdk2py/core/channel.py)
- [T3] Unitree：[G1 LocoClient](https://github.com/unitreerobotics/unitree_sdk2_python/blob/814556d15970dd2ecf1c9984e845ca02ab07e206/unitree_sdk2py/g1/loco/g1_loco_client.py) · [RPC topic](https://github.com/unitreerobotics/unitree_sdk2_python/blob/814556d15970dd2ecf1c9984e845ca02ab07e206/unitree_sdk2py/core/channel_name.py) · [应答匹配](https://github.com/unitreerobotics/unitree_sdk2_python/blob/814556d15970dd2ecf1c9984e845ca02ab07e206/unitree_sdk2py/rpc/client_stub.py)
- [T4] Unitree：[G1 低层例程](https://github.com/unitreerobotics/unitree_sdk2_python/blob/814556d15970dd2ecf1c9984e845ca02ab07e206/example/g1/low_level/g1_low_level_example.py)
- [C1] PyPI：[CycloneDDS 0.10.2 文件元数据](https://pypi.org/pypi/cyclonedds/0.10.2/json)
- [P1] PyPA：[Direct references](https://packaging.python.org/en/latest/specifications/version-specifiers/#direct-references) · [Warehouse 拒绝实现](https://github.com/pypi/warehouse/blob/23706dbb9ae242823fc2ec92cb5e3f71f7fd3fb3/warehouse/forklift/metadata.py)
- 类型标记依据：[PEP 561](https://peps.python.org/pep-0561/)。
本次读取的 uv 文档 tag `0.13.4` 对应提交 `7456b60bc6bc348e13e06e3691bf08e78d3aef54`。
未核实 PyPI 名称保留、全部硬件版本的兼容性与实机故障行为；上述路线是建议，不是已交付能力。
