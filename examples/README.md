# 示例

命令均在仓库根目录执行；先按[开发指南](../docs/DEVELOPMENT.md)准备环境并运行 `uv sync`。

当前可直接参考的例子：

- [`loopback.py`](loopback.py) —— 本机临时 Zenoh 回环，不依赖网关、DDS 或硬件，可用于安装后的冒烟检查。
- [`subscribe.py`](subscribe.py) —— 用 `buqi.connect` 连接网关并读取一条订阅消息。
- [`camera_snapshot.py`](camera_snapshot.py) —— 相机单帧查询与持续观测（需启用媒体模块）。
- [`buqi.toml`](buqi.toml) —— 网关 TOML 配置样例（监听端点与 DDS 选项）。
- [`buqi-camera.toml`](buqi-camera.toml) —— 在网关配置中加入可选 MJPEG 相机媒体。
- [`buqi-camera-webrtc.toml`](buqi-camera-webrtc.toml) —— 在同一配置中加入浏览器
  WebRTC 分支；设备与编码器需按目标机替换。

`loopback.py` 不依赖网关，可直接运行：

```sh
uv run python examples/loopback.py
```

`subscribe.py` 与 `camera_snapshot.py` 需要先以前台方式启动网关，再另开终端运行：

```sh
uv run python examples/subscribe.py
uv run python examples/camera_snapshot.py
```

规划中、尚未提供：

- `g1_control.py` —— Unitree G1 经 Zenoh 网关的状态订阅与控制
- `vla_control.py` —— VLA 模型接入示例

端到端验收脚本在 [`sdk/python/tests/acceptance.py`](../sdk/python/tests/acceptance.py)，
不属于面向使用者的示例。
