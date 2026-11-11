# 摄像头适配调研：GStreamer 串流与 Zenoh 单帧

> 2026-11-11；状态：调研建议，不定义统一 Robot API、key 命名或帧协议。
> 最终实现的配置、协议与故障语义以[相机媒体](media.md)为准；本文保留调研过程与出处，
> 其中 ZMQ 过渡、WebRTC 具体元件等备选未被最终契约采用。
> 基线：Buqi 当前工作层、相邻 `~/Projects/robot` 的本地文件（change `lqmnyutnswvz`）；
> GStreamer 本机 1.28.7、Rust bindings 锁定 0.25.4；Zenoh 1.10.2。
> 下文区分代码事实、官方能力、设计建议与本次验证；没有连接实体机器人或相机。

## 结论

方向可行：**一次采集、管线内分流；视频走媒体链路，图片通过 appsink 取出后走 Zenoh**。
避免冲突的关键是唯一设备持有者，而不是“两个进程都使用 GStreamer”。
先做 RGB 最小闭环并逐步测试，不建议把相机、多媒体传输、RGB-D 一起做完才测试。

## 1. robot 项目现状

以下路径均相对 `~/Projects/robot`，是代码/历史记录，不代表当前实机服务状态。

| 路径 | 已有能力与适配含义 |
| --- | --- |
| `src/ial_g1d/v4l2_rgb.py:157–214,244–306` | OpenCV `CAP_V4L2` 打开设备，读 BGR，再 `imencode` JPEG；ZMQ 单段 `raw_jpeg` |
| `configs/orbbec_v4l2_rgb.yaml` | Gemini 336L RGB，序列号 by-id，1280×720@30、MJPG，端口 55558 |
| `configs/strive_usb_camera.yaml` | Sonix USB RGB，同一采集实现，1280×720@30 MJPG，端口 55561 |
| `src/ial_g1d/camera_server.py:76–99`、`deploy/pc2/camera.yaml` | 另一条 TeleImager 路径，头/左右腕历史配置为 55555–55557；不能只查 Orbbec 进程 |
| `src/ial_g1d/robot/camera.py:61–137` | 客户端解 JPEG、BGR→RGB、可选双目裁剪；区分等待新帧的 `read` 和带过期检查的 `latest` |
| `native/realsense_zmq/src/capture.cpp:72–127`、`configs/realsense.yaml` | librealsense 同时采 RGB/Z16 深度，读取深度 scale、内外参，再由 SDK 对齐；候选端口 55562 |
| `native/realsense_zmq/src/rgbd_packet.cpp:182–284` | `rgbd_pair_v2` 把元数据、JPEG、PNG16 组成三段消息，保留逐流时间戳/配对信息 |

`docs/ORBBEC_V4L2_RGB.md` 与 `docs/FINAL_ZMQ_DEPLOYMENT.md` 把 Orbbec RGB-only
列为当时生产路径，RGB-D/ROS2 是不同路径；README 的头/腕相机映射不能套到 Orbbec。
应使用 by-id/序列号，并重新核对现场 USB 拓扑，不能固定认为 RGB 就是 `/dev/video0`。

旧 `raw_jpeg` **没有采集端时间戳、帧号或标定**；客户端明确填为 unavailable，
见 `camera.py:97–115`。若复用它，只能补“桥接收到时间”，不能伪造原始曝光时间。
原有 `output_mode`/`stereo_side` 还影响图像几何，迁移时不能把所有宽幅图像自动当双目裁剪。

## 2. GStreamer 确实支持持续串流中取单帧

官方 [`appsink`][appsink] 提供 `pull_sample` / 带超时的 `try_pull_sample`，
返回 `GstSample`（buffer、caps 等）；不必落盘、截图播放器，也不必重开摄像头。
`pull_sample` 在 PLAYING 返回样本；超时、停止或 EOS 排空后可无样本，需结合 bus/EOS 区分原因。

[`tee`][tee] 官方要求各分支有独立 `queue`，否则一支阻塞会拖住其他分支。
[`queue`][queue] 满后默认阻塞，**仅有 queue 不代表永久隔离**；快照分支还需要有界丢旧策略。
这只隔离数据流背压，不隔离同一管线的错误、CPU 争用或整机带宽。

### 适合现有 MJPG 相机的草图

以下是设计草案，不是可直接执行的完整命令；编码器和网络协议尚未选定。

```text
一个 v4l2src（by-id，显式协商 image/jpeg 1280×720@30）
  └─ tee
      ├─ queue → jpegdec → videoconvert → 视频编码器 → 媒体传输
      └─ queue（有界、丢旧）→ appsink（最多1帧、丢旧）
                                  ↓ 独立取帧任务
                             最新帧缓存 → Zenoh 发布/查询回复
```

[`v4l2src`][v4l2] 支持 `image/jpeg`，可以在解码前分叉，直接提取相机 JPEG，
省掉旧路径的“JPEG 解码为 BGR，再编码 JPEG”。**这改变了旧 `jpeg_quality=90`
二次编码的语义**，不能声称字节或画质等价。若需裁剪、缩放、指定 JPEG 质量，
可共享解码后分流，或仅在需要变换的分支解码；不必为此再开一个设备。

raw 源可把取出的帧按需编码 JPEG；不要为了偶尔取帧，先把每帧 JPEG 编码再丢弃。
[`videorate`][videorate] 适合固定低频抽样，不是按请求触发快门；可复制帧，需注意帧号。

### 单帧语义与队列

- 建议首版返回**最近已采集帧**，带序号、采集/接收时间及其时钟域，并检查最大帧龄。
  `max-buffers=1` 限的是数量，不是时间；停流后唯一缓存也可能已经很旧。
- 若需要“等待更新帧”，记录当前采集序号并等待更新、设置 deadline；请求时直接拉一次
  appsink 仍可能拿到请求前缓存。PTS 门槛需处理管线重启与时钟映射，不能保证请求后才曝光。
- `appsink max-buffers=1 sync=false wait-on-eos=false`，GStreamer 1.28+ 配
  `leaky-type=downstream`；旧版本使用 `drop=true`，该属性从 1.28 废弃。[appsink]
- 分支 queue 可设 `max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream`；
  queue 的属性是 `leaky`，与 appsink 的 `leaky-type` 不同。[queue]
- `new-sample` 在 streaming thread 触发。回调只做快速交接，或用专门线程拉取；
  **不要在回调中等待 Zenoh、图片处理或网络**。应用侧通道也必须有界并有丢弃策略。[appsink]
- 原始帧需按 caps、stride/plane 和实际内存类型读取，不能只假定 `width×height×3`。
  跨任务传递时管理 sample 生命周期，及时释放设备 buffer；JPEG 字节与 raw 布局不同。

不要使用两个常见替代办法：`pull_preroll` 是 PAUSED/seek 预滚样本，不是持续快照；
在共享 source 上设 `num-buffers=1` 会让 source 发 EOS、两支一起结束。
参见 [appsink][appsink]、[live source][live]、[GstBaseSrc][basesrc]。
V4L2 在分配采集 buffer 后有唯一 filehandle owner，其他采集可得 EBUSY；
内核也建议由用户态代理共享，而不是依赖多进程同时采集。[V4L2 设备所有权][ownership]

## 3. Zenoh 图片通道怎么组织

Zenoh [`ZBytes`][zbytes] 能直接传 JPEG/PNG 字节，`encoding` 是格式标记，不负责图像解码。
以下是选择建议，不是已确认 Interface：

| 使用方式 | 建议机制 | 注意点 |
| --- | --- | --- |
| 偶尔请求一张 | `get` → 媒体模块的 queryable → 回复最新有效缓存 | 明确无首帧、过期、停流、超时；queryable 不会自动建立缓存 |
| VLA 连续取图 | 指定频率 pub/sub，客户端保留最新一帧 | 不把每次推理都变成往返查询；普通订阅不自动回放上次发布 |

[`Queryable`][queryable] / 官方 [请求回复示例][query-example] 支持前一种模式，
缓存逻辑仍由媒体模块实现；不是在 DDS 插件里添加机器人业务方法。
JPEG 与元数据放在**同一次 reply/put**：同一 payload 封装，或 payload + attachment，
不要分别发到两个 key 后假定能配对。格式/版本、尺寸、相机 id、序号、时钟域、帧龄语义需先约定。
Zenoh [`Sample`][sample] 的 timestamp 不能直接当曝光时间。

实时图像建议在应用发送任务中选择可丢帧策略，并分别约束生产端、传输和客户端队列。
[`CongestionControl::Drop`][qos] 队列满时允许丢消息，`Block` 会等待；
**Drop 不等于自动保留最新帧**，也不保证图像与控制消息在共享网络中互不影响。

`robot/docs/FINAL_ZMQ_DEPLOYMENT.md` 历史记录 JPEG 均值 133,757 B，30 Hz 约 30 Mbps，
不是本次实测。按同样均值，5 Hz 也约 5.35 Mbps（十进制、未计协议开销）；
避免为了偶尔截图把全帧率 JPEG 另发一遍。视频码率、快照频率应联合预算。

## 4. 部署与模块归属：先选唯一采集者

依据 [Buqi 开发指南](DEVELOPMENT.md#模块职责与边界)，通用媒体处理可归 Core，
厂商解释/双目选择等机器人语义留在 SDK/Adapter；网关不导入 Unitree/Orbbec/RealSense SDK。

- **少改现有系统**：保留 robot 采集者，从它已有 ZMQ JPEG 订阅字节，送入
  [`appsrc`][appsrc] 后分流。不会抢设备，但仍依赖 ZMQ 转接，不能补出缺失采集元数据。
  若仅接入现有视频流，也可在接收端解码后取帧，但快照依赖视频链路且可能经历有损编码。
- **原生媒体路径**：在能访问 USB 设备的 PC2，由 GStreamer 替换对应旧采集者，
  一源分流视频/Zenoh。PC1 不必变；需要维护者先确认旧服务停用及旧 ZMQ 消费者兼容需求。
  **本次只建议，不部署、不停服务。** 网关若不在相机所在主机，则不能直接用本地 v4l2src。

首版限定一路 RGB。RealSense/Orbbec RGB-D 保留 SDK/已有采集流程，颜色可另接 appsrc，
深度需无损保留、配对、scale、内外参及时钟信息；普通 H.264/JPEG 不是深度传输方案。
GStreamer 的 [raw video caps][rawcaps] 描述像素布局，不自动提供这些 RGB-D 语义。

视频协议仍待选择：可信局域网原生客户端可先验证 H.264 + RTP/UDP；浏览器观看通常考虑 WebRTC，
还需信令/ICE 等，不是仅替换一个 sink。编码器必须在 PC2 实测，不能把开发机 AMD 能力当目标能力。

## 5. 本机依赖与实际验证

代码事实：`crates/buqi-core/src/lib.rs` 明确媒体仍未实现；Cargo 当前只有 `gstreamer`，
没有 `gstreamer-app`/`gstreamer-video`。devenv 只声明 GStreamer 与 plugins-base。
如使用 typed AppSink/AppSrc，可加入同系列 gstreamer-app；处理 raw 布局时再按需加入 video。

本次 `gst-inspect-1.0 --version` / 元件检查：系统 **1.28.7**；
有 tee、queue、appsink、appsrc、videotestsrc、videoconvert、videorate；
缺 v4l2src、jpegenc/jpegdec、rtph264pay、udpsink、x264enc、openh264enc、webrtcbin。
后续应在 `devenv.nix` 声明所选插件，不临时安装；V4L2/JPEG/RTP/UDP 常见元件属于 Good，
编码/WebRTC 按选定方案补充。PC2 插件、系统版本和 ABI **未检查**，不能照抄 1.28 属性。

已运行以下**无设备、无网络**管线，约 3 秒后退出 0；appsink 完全不消费也正常 EOS：

```sh
timeout 10s gst-launch-1.0 -q \
  videotestsrc is-live=true num-buffers=90 ! \
  video/x-raw,format=RGB,width=320,height=240,framerate=30/1 ! tee name=t \
  t. ! queue ! fakesink sync=false \
  t. ! queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! \
  appsink max-buffers=1 leaky-type=downstream sync=false wait-on-eos=false
```

另以临时 C 程序调用 `gst_app_sink_try_pull_sample`，每 300 ms 取一次，共 5 帧；
PTS 为 267.64/567.64/867.64/1167.64/1467.64 ms，RGB buffer 每帧 230,400 B，
另一 fakesink 的 handoff 计数 **90/90**，EOS 正常，退出 0；编译 `-Wall -Wextra -Werror` 通过。
临时源码未留入仓库；本段是观察记录，不是持久自动化测试。
**没有验证 JPEG、视频编解码/传输、Zenoh 图片端到端、PC2 或实机，也未改源码/依赖。**

## 6. 建议的实施顺序与待确认项

1. 先确认首个相机、采集所在主机、保留 ZMQ 还是替换旧采集者、视频客户端类型。
2. 用测试图完成“媒体输出 + appsink 最新帧 + Zenoh 回复”，立即验证图片能解码与元数据一致。
3. 增加慢消费者、无首帧、缓存过期、停流、断网、退出释放测试；检查内存/队列有界，
   查询不挂住视频，也不会把旧图当作新图。高频图像测试时同时观察模拟状态链路时延。
4. 获维护者确认后，现场只启一个设备 owner；核对 by-id/caps、颜色、裁剪、帧率和编码器。
5. 对实际链路测 CPU、码率、掉帧、p95 帧龄与稳定性；未证明时钟同步时不声称跨机单向延迟。

[appsink]: https://gstreamer.freedesktop.org/documentation/app/appsink.html
[tee]: https://gstreamer.freedesktop.org/documentation/coreelements/tee.html
[queue]: https://gstreamer.freedesktop.org/documentation/coreelements/queue.html
[v4l2]: https://gstreamer.freedesktop.org/documentation/video4linux2/v4l2src.html
[videorate]: https://gstreamer.freedesktop.org/documentation/videorate/index.html
[live]: https://gstreamer.freedesktop.org/documentation/additional/design/live-source.html
[basesrc]: https://gstreamer.freedesktop.org/documentation/base/gstbasesrc.html
[ownership]: https://www.kernel.org/doc/html/latest/userspace-api/media/v4l/open.html#multiple-opens
[zbytes]: https://docs.rs/zenoh/1.10.2/zenoh/bytes/struct.ZBytes.html
[queryable]: https://docs.rs/zenoh/1.10.2/zenoh/query/struct.Queryable.html
[query-example]: https://github.com/eclipse-zenoh/zenoh/blob/1.10.2/examples/examples/z_queryable.rs
[sample]: https://docs.rs/zenoh/1.10.2/zenoh/sample/struct.Sample.html
[qos]: https://docs.rs/zenoh/1.10.2/zenoh/qos/enum.CongestionControl.html
[appsrc]: https://gstreamer.freedesktop.org/documentation/app/appsrc.html
[rawcaps]: https://gstreamer.freedesktop.org/documentation/additional/design/mediatype-video-raw.html
