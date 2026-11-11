# buqi Python SDK

通过 Zenoh 连接 Buqi 网关（`buqi-server`，Zenoh Router + DDS 插件），
以可插拔 Adapter 访问不同机器人。首版提供最小连接入口与相机图片通道，
统一 `Robot` 接口尚未实现。

```sh
uv sync          # 安装依赖（eclipse-zenoh）
uv run python -c "import buqi; print(buqi.__version__)"
```

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
  服务端接收时间。协议与新鲜度语义见[相机媒体文档](../../docs/media.md)。

## 验证

```sh
# 仓库根目录
just test-python   # 连接失败语义 + 图片协议/错误映射/最新帧行为
just acceptance    # 真实网关进程 + 模拟 DDS 发布端 + 订阅核对
```
