# buqi Python SDK

通过 Zenoh 连接 Buqi 网关（`buqi-server`，Zenoh Router + DDS 插件），
以可插拔 Adapter 访问不同机器人。首版只提供最小连接入口，统一 `Robot` 接口尚未实现。

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

## 验证

```sh
# 仓库根目录
just test-python   # 连接失败语义
just acceptance    # 真实网关进程 + 模拟 DDS 发布端 + 订阅核对
```
