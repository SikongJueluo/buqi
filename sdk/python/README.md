# buqi Python SDK

统一机器人接口：通过 Zenoh 连接 buqi 网关（`buqi-server`，Router + DDS 插件），
以可插拔 Adapter 访问不同机器人。

```sh
uv sync          # 安装依赖（eclipse-zenoh）
uv run python -c "import buqi; print(buqi.__version__)"
```
