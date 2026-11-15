# buqi Python SDK

通过 Zenoh 连接 Buqi 网关（`buqi-server`，Zenoh Router + DDS 插件），
以可插拔 Adapter 访问不同机器人。当前提供最小连接入口与相机图片通道，
统一 `Robot` 接口尚未实现。

## 安装

需要 Python 3.12 或更高版本。仓库根目录即 Python 项目，开发阶段可从 GitHub 安装：

```sh
# uv（推荐）
uv add "git+https://github.com/SikongJueluo/buqi.git" --branch main

# pip
pip install "git+https://github.com/SikongJueluo/buqi.git@main"
```

本机联调使用可编辑安装：

```sh
uv add --editable /path/to/buqi
```

默认只安装运行依赖 `eclipse-zenoh`；DDS 验收依赖与机器人厂商 SDK 不在默认依赖中。
仓库 CI 覆盖 Linux x86_64 上的 CPython 3.12–3.15，其他平台未声明已验证。

## 连接与订阅

```python
import buqi

with buqi.connect("tcp/192.168.1.20:7447") as session:
    with session.declare_subscriber("example/state") as subscriber:
        sample = subscriber.recv()
        print(sample.key_expr, sample.payload.to_bytes().hex())
```

- 端点是网关启动时打印的完整 Zenoh 端点（含 `tcp/`），SDK 不会补全前缀或自动发现。
- `connect` 返回官方 `eclipse-zenoh` `Session`；订阅、发布、payload 与资源管理沿用官方 API。
- 连接失败或超过约 3 秒抛 `zenoh.ZError`。
- 载荷是不透明的 `ZBytes`；SDK 不做 DDS/机器人消息解码。

## 相机图片

网关启用 `[media]` 后，SDK 可直接取相机原始 JPEG 与元数据（不解码、不依赖 OpenCV）：

```python
with buqi.connect("tcp/192.168.1.20:7447") as session:
    frame = buqi.snapshot(session, "pc2-rgb", timeout=1.0)
    print(frame.metadata.sequence, frame.metadata.width, len(frame.jpeg))

    with buqi.frames(session, "pc2-rgb") as stream:
        frame = stream.next(timeout=1.0)
```

- `snapshot` 返回查询到达前的最新缓存；网关没有首帧或缓存过期时分别抛
  `CameraNoFrameError` / `CameraStaleFrameError`，等待回复超时抛 `CameraTimeout`。
- `frames` 的队列只保留最新一帧；`next` 默认 1 秒无新帧即抛 `CameraTimeout`。
- `CameraFrame.jpeg` 是原始 JPEG 字节；`CameraFrame.metadata` 含会话、序号、尺寸与
  服务端接收时间。协议与新鲜度语义见[相机媒体文档](https://github.com/SikongJueluo/buqi/blob/main/docs/media.md)。

## 仓库内开发与验证

在仓库根目录运行：

```sh
uv sync
uv run python -m pytest -q   # 或 just test-python
just check-python-dist       # wheel/sdist、sdist 重建与干净安装
just acceptance              # 真实网关进程 + 模拟 DDS，需要 Rust 与显式 dds 组
```

`just acceptance` 会构建 Rust workspace 并安装 DDS 验收依赖，不连接实体机器人。
测试与示例不替代 `just acceptance` 的跨进程链路验证。

更多入口：[开发指南](https://github.com/SikongJueluo/buqi/blob/main/docs/DEVELOPMENT.md)、
[示例](https://github.com/SikongJueluo/buqi/tree/main/examples)、
[Python SDK 调研](https://github.com/SikongJueluo/buqi/blob/main/docs/python-sdk-research.md)。
