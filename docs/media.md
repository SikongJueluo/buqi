# 相机媒体：MJPEG 采集与 Zenoh 图片通道

本文说明可选的相机媒体模块：配置、采集归属、Zenoh 协议、新鲜度与故障语义。
网关启动与监听见[网关文档](gateway.md)，环境与验证任务见[开发指南](DEVELOPMENT.md)。

> 状态：相机 MJPEG → Zenoh 原始 JPEG（单帧查询与持续观测）与可选浏览器 WebRTC
> 分支均已实现并通过本地自动测试；**WebRTC 尚未经过真实浏览器/PC2 验证**。
> 未连接实体相机、未在 PC2 或真实机器人网络验证。

## 启用与配置

媒体模块默认关闭。配置中出现 `[media]` 即启用；没有该区段时网关保持原有纯通信行为。
`[media]` 由 Buqi 在交给 Zenoh 解析前取出，因此不会成为 Zenoh 的未知字段。

```toml
listen = { endpoints = ["tcp/192.168.1.20:7447", "tcp/127.0.0.1:7447"] }

[plugins.dds]
domain = 0

[media.camera]
id = "pc2-rgb"        # 图片 key 中的相机标识，只允许 ASCII 字母、数字、- 和 _
device = "/dev/v4l/by-id/usb-Orbbec...-video-index0"
width = 1280          # 默认 1280
height = 720          # 默认 720
framerate = 30        # 默认 30

[media.observation]
rate_hz = 5.0         # 最新帧发布频率，默认 5；范围 0.1–60
max_age_ms = 500      # 缓存新鲜度上限，默认 500；范围 1–10000
```

- 首版只支持**一路** UVC 相机、固定 `image/jpeg`（MJPEG）格式；不做缩放、裁剪、
  重新编码或颜色转换。相机必须原生输出 MJPEG，否则按“格式不支持”明确报错。
- `device` 推荐使用 `/dev/v4l/by-id/...`，不要假定 `/dev/video0`；型号与序号不写进代码。
- 区段存在但缺少 `camera.id` / `camera.device`、出现未知字段或取值越界，属于配置错误，
  网关启动失败。配置非法不会降级成“媒体关闭”。
- 只有一个采集者：GStreamer `v4l2src` 直接打开设备。启动网关前必须停掉占用同一设备的
  旧程序；Buqi 不杀占用者、不重置 USB、不自动换设备。

## 采集与缓存

一个采集管线持续运行，即使没有观看者或订阅者：

```text
v4l2src（设备，MJPEG caps）→ appsink（最多 1 帧，丢旧）
                                ↓ 采集线程
                          最新帧缓存（覆盖式，不排队）→ Zenoh 发布 / 查询回复
```

`appsink` 的 `new-sample` 回调只复制 JPEG 字节并覆盖缓存，不等待网络或图片处理。
缓存不形成队列，慢读取方不会反压采集；读取方拿到的永远是当前最新帧或明确错误。

## Zenoh 协议

协议版本为 `1`，key 前缀固定：

| key | 用途 |
| --- | --- |
| `buqi/media/v1/<camera>/frame` | 持续观测，按 `rate_hz` 发布最新帧 |
| `buqi/media/v1/<camera>/snapshot` | 单帧查询，立即回复缓存或错误 |

成功的 `put` / reply：

- payload：相机原始 JPEG 字节，**不重新编码**；
- encoding：`image/jpeg`；
- attachment：JSON 元数据，与 JPEG 在同一条消息中原子绑定。

```json
{
  "version": 1,
  "camera": "pc2-rgb",
  "session": "0192f0c4d1a2b3c40001",
  "sequence": 42,
  "format": "jpeg",
  "width": 1280,
  "height": 720,
  "captured_at_unix_ms": 1760000000000
}
```

- `session` 在每次采集重启后更换；`sequence` 在本次会话内递增。
  重启后序号重新开始，但 `session` 不同，不能只看序号判断连续性。
- `captured_at_unix_ms` 是服务端**收到**采集帧时的主机墙钟，适合日志与粗略排序；
  它不是曝光时间，也未经跨机时钟同步。
- 设备曝光时间在 V4L2 路径不可用，协议不提供该字段；客户端必须按“未知”处理，
  不得用接收时间伪造。

单帧查询失败时，reply 使用 `application/json` 编码：

```json
{"version": 1, "camera": "pc2-rgb", "error": "stale", "max_age_ms": 500, "age_ms": 812}
```

- `no_frame`：本次采集会话还没有首帧；
- `stale`：缓存年龄超过 `max_age_ms`。相机停流、掉线或采集线程已停止时都归入此类；
- 服务端只回复最新有效缓存，不承诺“请求后重新曝光”，也不提供等待下一帧的语义；
- 没有有效帧时立即回复错误，不挂起查询。

持续观测使用 `CongestionControl::Drop`，网络拥塞时允许丢弃；服务端也跳过超过
`max_age_ms` 的缓存，不会把停流前的旧画面当作新帧持续发布。订阅端必须自己保留
最新帧并判断新鲜度；普通 Zenoh 订阅不会回放上一次发布。

## 客户端语义

Python SDK 提供两个入口，默认交付 JPEG 字节与元数据，不解码、不依赖 OpenCV：

```python
import buqi

with buqi.connect("tcp/192.168.1.20:7447") as session:
    frame = buqi.snapshot(session, "pc2-rgb", timeout=1.0)
    print(frame.metadata.sequence, len(frame.jpeg))

    with buqi.frames(session, "pc2-rgb") as stream:
        frame = stream.next(timeout=1.0)
```

- `snapshot` 的 `timeout` 是等待网关回复的上限，默认 1 秒。SDK 显式覆盖底层
  查询 timeout 并关闭 Zenoh 的 `LATEST` 合并，因此 Session 的
  `queries_default_timeout` 不会提前截断仍在本次期限内的回复；底层合成的
  `Timeout` 错误也映射为 `CameraTimeout`。
  网关无首帧或缓存过期时分别抛 `CameraNoFrameError` / `CameraStaleFrameError`。
- `frames` 的客户端队列只保留最新一帧：消费者变慢时丢旧帧，不积压历史。
  `next` 默认 1 秒无新帧即抛 `CameraTimeout`；队列里只有超过时限的旧帧时也按过期处理。
- `CameraFrame.received_at` 是客户端收到帧时的 `time.monotonic()`，用于本地新鲜度判断。
- 图像与关节/机器人状态没有严格时间同步，协议不宣称原子 observation。

## 故障、恢复与状态

- 采集错误分为两类：缺插件、caps/格式不支持、运行期 `not-negotiated` 等
  **确定性错误**立即停止并报告；设备掉线、占用、读失败、EOS 等**临时错误**
  以 1 秒起、上限 30 秒的退避重试，直到恢复或网关退出。
  连续稳定采集 10 秒后才重置退避。
- 只重试配置中的设备，不换设备、不改规格。
- 相机故障不停止 Zenoh Router 与 DDS；媒体状态由前台输出单独报告。
  确定性命中后图片 key 仍可查询，会明确回复 `no_frame` 或 `stale`，而不是伪装成在线。
- WebRTC 分支是独立管线，失败不影响 JPEG 观测；媒体与网关状态分别报告。
- `Ctrl+C` 时停止采集（管线置 NULL）、撤销图片任务，再关闭 Zenoh Session。

## 浏览器视频与 WebRTC

浏览器无法直接协商 MJPEG，因此该分支解 JPEG 再编码为 H.264/VP8/VP9。它是与
采集/查询独立的第二条管线：

```text
缓存最新帧 ──(仅当观看者 > 0)──> appsrc ! jpegdec ! videoconvert
                                  ! <encoder 片段> ! capsfilter(codec) ! webrtcsink
```

```toml
[media.webrtc]
# 必须显式选择编码器；不自动回退软件后端。可写多元素链，例如 Jetson 的
# "nvvidconv ! video/x-raw(memory:NVMM),format=NV12 ! nvv4l2h264enc"。
encoder = "x264enc tune=zerolatency speed-preset=ultrafast"
codec = "h264"                 # h264 / vp8 / vp9
signalling = "127.0.0.1:8443"  # 必须显式绑定，禁止 0.0.0.0/:: 或端口 0

# 可选静态网页；两者必须同时出现。web_root 指向 gstwebrtc-api 的 dist 目录。
# web = "127.0.0.1:8080"
# web_root = "/path/to/gstwebrtc-api/dist"
```

- 没有观看者时只保留信令服务器，不向 appsrc 推送帧，因此不持续解码/转码；
  第一个观看者连接时才发送当前最新帧。
- 编码器、插件或 caps 不可用时，WebRTC 报告不可用，JPEG 图片通道继续运行；
  不静默切换软件编码器，也不重启为其他后端。
- 信令与网页地址必须显式写成本机或指定 LAN 地址；端口在启动时预检查。
- 内置网页服务只是静态文件服务器，页面需要部署方提供（自建 gstwebrtc-api dist
  或等价页面）。不提供公网/TURN/认证；WebRTC 的 DTLS-SRTP 不等于访问控制。
- 本机开发环境由 `devenv.nix` 覆写 nixpkgs 的 `gst-plugins-rs` 构建
  `webrtc`/`rtp` 插件（默认包排除了 webrtc），并声明 `gst-plugins-bad`
  （webrtcbin）与 `gst-plugins-ugly`（x264enc，仅作显式开发后端）。
- PC2 的 GStreamer 版本与硬件编码器（如 `nvv4l2h264enc`）仍未核实；
  必须按目标机实际插件选择 encoder 片段，不能照抄开发机配置。

## 验证范围

已自动验证（`just quality` / `just test-python`）：

- 配置默认值、未知字段、越界值、相机 id 注入与旧配置兼容；
- 合成 `videotestsrc → jpegenc` 管线经 appsink 产出真实 JPEG，元数据尺寸/会话/序号正确；
- Zenoh 回环中 JPEG 字节逐字节一致、attachment 元数据一致；
- 单帧查询不受 5 Hz 发布节奏限制，`no_frame` / `stale` 错误明确；
- 订阅收到最新帧、慢消费者只保留最新一帧；同一 `(session, sequence)` 不重复发布，
  重复消息不刷新客户端观测期限；
- Python 端查询持有不回复时在 deadline 映射为 `CameraTimeout`，而不是普通错误；
  即使 Session 的 `queries_default_timeout` 更短也不会提前截断，SDK 期限内到达的
  延迟回复仍成功；底层合成的 `Timeout` 同样映射为 `CameraTimeout`；
- 运行期 caps 协商失败（GStreamer `not-negotiated`）按确定性错误停止；
- WebRTC 信令服务器起监听、静态网页目录可访问、无观看者不推送帧、
  观看者连接/断开驱动推送、未知编码器报告 WebRTC 不可用。

未验证：真实浏览器 WebRTC 会话（SDP/ICE/RTP 与页面）、实体 V4L2 相机、
MJPEG caps 协商、USB 掉线恢复、PC2 部署与资源占用、PC2 硬件编码器、
跨机网络与真实 VLA 负载。首次实机测试须另行获得维护者授权，
并确认旧相机进程未占用同一设备。
