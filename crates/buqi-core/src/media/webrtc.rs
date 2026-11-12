//! 浏览器 WebRTC 视频分支。
//!
//! 它是一条**独立于 JPEG 采集/查询的管线**：采集线程仍只把原始 JPEG 放进缓存，
//! 这里在确有观看者时把帧送进 `appsrc → jpegdec → 编码器 → webrtcsink`。
//! 没有观看者时不推送帧，因此不会持续解码/转码；管线或编码器失败只影响
//! WebRTC，JPEG 图片通道继续运行。

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::time::Duration;

use anyhow::{Context, Result, bail};
use gstreamer as gst;
use gstreamer::prelude::*;
use gstreamer_app::AppSrc;
use tokio::sync::{mpsc, watch};
use tokio::task::JoinHandle;

use super::Event;
use super::config::WebrtcConfig;
use super::frame::Frame;

const BUS_POLL: Duration = Duration::from_millis(200);
/// appsrc 的字节上限；超出时丢旧帧，绝不反压采集线程。
const APPSRC_MAX_BYTES: u64 = 4 * 1024 * 1024;

/// 已启动的 WebRTC 分支。
pub(crate) struct Webrtc {
    pipeline: gst::Pipeline,
    feeder: JoinHandle<()>,
    bus_thread: Option<std::thread::JoinHandle<()>>,
    stop: Arc<AtomicBool>,
}

impl Webrtc {
    pub(crate) async fn start(
        config: &WebrtcConfig,
        frames: watch::Receiver<Option<Arc<Frame>>>,
        events: mpsc::UnboundedSender<Event>,
    ) -> Result<Self> {
        gst::init().context("初始化 GStreamer 失败")?;
        ensure_bindable(config.signalling, "WebRTC 信令地址")?;
        if let Some(web) = config.web {
            ensure_bindable(web, "WebRTC 网页地址")?;
        }

        let (pipeline, appsrc, sink) = build_pipeline(config)?;
        let (viewer_tx, viewer_rx) = watch::channel(0usize);
        let viewers = Arc::new(ViewerCount::new(viewer_tx));
        connect_viewer_signals(&sink, viewers, events.clone());

        if pipeline.set_state(gst::State::Playing).is_err() {
            let _ = pipeline.set_state(gst::State::Null);
            bail!("WebRTC 管线无法进入 PLAYING");
        }

        let failed = Arc::new(AtomicBool::new(false));
        let stop = Arc::new(AtomicBool::new(false));
        let bus_thread =
            spawn_bus_watcher(&pipeline, stop.clone(), failed.clone(), events.clone())?;
        let feeder = tokio::spawn(feed_loop(appsrc, frames, viewer_rx, failed));

        let _ = events.send(Event::WebrtcStarted {
            signalling: config.signalling.to_string(),
            web: config.web.map(|web| format!("http://{web}/")),
        });
        Ok(Self {
            pipeline,
            feeder,
            bus_thread: Some(bus_thread),
            stop,
        })
    }

    pub(crate) async fn shutdown(mut self) {
        self.feeder.abort();
        let _ = self.feeder.await;
        // 先置停止标记，避免正常关停时的 EOS/ERROR 被报告为运行期失败。
        self.stop.store(true, Ordering::Relaxed);
        let _ = self.pipeline.set_state(gst::State::Null);
        if let Some(thread) = self.bus_thread.take() {
            let _ = thread.join();
        }
    }
}

fn ensure_bindable(address: std::net::SocketAddr, what: &str) -> Result<()> {
    std::net::TcpListener::bind(address)
        .with_context(|| format!("{what} {address} 无法绑定（端口被占用或地址不可用）"))?;
    Ok(())
}

fn build_pipeline(config: &WebrtcConfig) -> Result<(gst::Pipeline, AppSrc, gst::Element)> {
    let pipeline = gst::Pipeline::new();
    let caps = gst::Caps::builder("image/jpeg").build();
    let appsrc = AppSrc::builder()
        .caps(&caps)
        .format(gst::Format::Time)
        .is_live(true)
        .do_timestamp(true)
        .max_bytes(APPSRC_MAX_BYTES)
        .block(false)
        .build();
    if appsrc.find_property("leaky-type").is_some() {
        appsrc.set_property_from_str("leaky-type", "downstream");
    }

    let jpegdec = make("jpegdec", "需要 GStreamer jpeg 插件")?;
    let convert = make("videoconvert", "需要 GStreamer videoconvert")?;
    let encoder = gst::parse::bin_from_description(&config.encoder, true).with_context(|| {
        format!(
            "解析 media.webrtc.encoder `{}` 失败：请确认编码器已安装且是单一输入/输出的元素链",
            config.encoder
        )
    })?;
    let codec_caps = gst::Caps::builder(config.codec.caps()).build();
    let filter = make("capsfilter", "需要 GStreamer capsfilter")?;
    filter.set_property("caps", &codec_caps);

    let sink = make("webrtcsink", "需要 gst-plugin-webrtc 的 webrtcsink")?;
    require_property(&sink, "run-signalling-server")?;
    require_property(&sink, "signalling-server-host")?;
    require_property(&sink, "signalling-server-port")?;
    require_property(&sink, "video-caps")?;
    sink.set_property("run-signalling-server", true);
    sink.set_property("signalling-server-host", config.signalling.ip().to_string());
    sink.set_property("signalling-server-port", config.signalling.port() as u32);
    sink.set_property("video-caps", &codec_caps);

    if let (Some(web), Some(root)) = (config.web, &config.web_root) {
        for property in [
            "run-web-server",
            "web-server-host-addr",
            "web-server-directory",
        ] {
            require_property(&sink, property)?;
        }
        sink.set_property("run-web-server", true);
        sink.set_property("web-server-host-addr", format!("http://{web}/"));
        sink.set_property("web-server-directory", root.to_string_lossy().as_ref());
    }

    let appsrc_element: gst::Element = appsrc.clone().upcast();
    let encoder_element: gst::Element = encoder.clone().upcast();
    pipeline
        .add_many([
            &appsrc_element,
            &jpegdec,
            &convert,
            &encoder_element,
            &filter,
            &sink,
        ])
        .context("组装 WebRTC 管线失败")?;
    gst::Element::link_many([
        &appsrc_element,
        &jpegdec,
        &convert,
        &encoder_element,
        &filter,
        &sink,
    ])
    .context("连接 WebRTC 管线失败：编码器输出与所选 codec 不匹配？")?;

    Ok((pipeline, appsrc, sink))
}

fn make(name: &str, hint: &str) -> Result<gst::Element> {
    gst::ElementFactory::make(name)
        .build()
        .with_context(|| format!("创建 {name} 失败：{hint}"))
}

fn require_property(element: &gst::Element, property: &str) -> Result<()> {
    if element.find_property(property).is_none() {
        bail!("webrtcsink 缺少属性 `{property}`：当前插件版本不支持所需能力，请报告 WebRTC 不可用");
    }
    Ok(())
}

struct ViewerCount {
    count: AtomicUsize,
    tx: watch::Sender<usize>,
}

impl ViewerCount {
    fn new(tx: watch::Sender<usize>) -> Self {
        Self {
            count: AtomicUsize::new(0),
            tx,
        }
    }

    fn added(&self) -> usize {
        let count = self.count.fetch_add(1, Ordering::SeqCst) + 1;
        self.tx.send_replace(count);
        count
    }

    fn removed(&self) -> usize {
        let mut current = self.count.load(Ordering::SeqCst);
        while current > 0 {
            match self.count.compare_exchange_weak(
                current,
                current - 1,
                Ordering::SeqCst,
                Ordering::SeqCst,
            ) {
                Ok(_) => break,
                Err(observed) => current = observed,
            }
        }
        let count = self.count.load(Ordering::SeqCst);
        self.tx.send_replace(count);
        count
    }
}

fn connect_viewer_signals(
    sink: &gst::Element,
    viewers: Arc<ViewerCount>,
    events: mpsc::UnboundedSender<Event>,
) {
    let added_viewers = viewers.clone();
    let added_events = events.clone();
    sink.connect("consumer-added", false, move |_| {
        let count = added_viewers.added();
        let _ = added_events.send(Event::WebrtcViewers { count });
        None
    });

    let removed_viewers = viewers;
    let removed_events = events;
    sink.connect("consumer-removed", false, move |_| {
        let count = removed_viewers.removed();
        let _ = removed_events.send(Event::WebrtcViewers { count });
        None
    });
}

fn spawn_bus_watcher(
    pipeline: &gst::Pipeline,
    stop: Arc<AtomicBool>,
    failed: Arc<AtomicBool>,
    events: mpsc::UnboundedSender<Event>,
) -> Result<std::thread::JoinHandle<()>> {
    let bus = pipeline.bus().context("WebRTC 管线没有 bus")?;
    std::thread::Builder::new()
        .name("buqi-webrtc-bus".to_owned())
        .spawn(move || {
            loop {
                if stop.load(Ordering::Relaxed) {
                    break;
                }
                let Some(message) = bus.timed_pop_filtered(
                    gst::ClockTime::from_mseconds(BUS_POLL.as_millis() as u64),
                    &[gst::MessageType::Error, gst::MessageType::Eos],
                ) else {
                    continue;
                };
                let reason = match message.view() {
                    gst::MessageView::Error(error) => {
                        let glib_error = error.error();
                        let debug = error.debug();
                        describe_error(&glib_error, debug.as_ref().map(|debug| debug.as_str()))
                    }
                    gst::MessageView::Eos(_) => "WebRTC 管线已结束".to_owned(),
                    _ => continue,
                };
                failed.store(true, Ordering::Relaxed);
                let _ = events.send(Event::WebrtcFailed { reason });
                break;
            }
        })
        .context("创建 WebRTC 总线监听线程失败")
}

fn describe_error(error: &gst::glib::Error, debug: Option<&str>) -> String {
    match debug.filter(|debug| !debug.is_empty()) {
        Some(debug) => format!("{error}（{debug}）"),
        None => error.to_string(),
    }
}

async fn feed_loop(
    appsrc: AppSrc,
    mut frames: watch::Receiver<Option<Arc<Frame>>>,
    mut viewers: watch::Receiver<usize>,
    failed: Arc<AtomicBool>,
) {
    let mut last: Option<(String, u64)> = None;
    loop {
        if failed.load(Ordering::Relaxed) {
            break;
        }
        if *viewers.borrow() > 0 {
            let frame = { frames.borrow().clone() };
            if let Some(frame) = frame {
                let identity = (frame.metadata.session.clone(), frame.metadata.sequence);
                if last.as_ref() != Some(&identity) {
                    let buffer = gst::Buffer::from_slice(frame.jpeg.clone());
                    match appsrc.push_buffer(buffer) {
                        Ok(_) => last = Some(identity),
                        // 管线正在停止：结束推送，避免继续访问失效的 appsrc。
                        Err(gst::FlowError::Flushing | gst::FlowError::Eos) => break,
                        // appsrc 队列满等临时问题：丢弃本帧追下一帧；真正失败由
                        // 总线线程报告，不能因编码器暂时落后就停掉整个分支。
                        Err(_) => last = Some(identity),
                    }
                }
            }
        }
        tokio::select! {
            changed = frames.changed() => {
                if changed.is_err() {
                    break;
                }
            }
            changed = viewers.changed() => {
                if changed.is_err() {
                    break;
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;
    use std::time::Instant;

    fn free_port() -> u16 {
        TcpListener::bind("127.0.0.1:0")
            .expect("绑定临时端口失败")
            .local_addr()
            .expect("读取临时端口失败")
            .port()
    }

    fn config(encoder: &str) -> WebrtcConfig {
        WebrtcConfig {
            encoder: encoder.to_owned(),
            codec: super::super::config::VideoCodec::H264,
            signalling: format!("127.0.0.1:{}", free_port())
                .parse()
                .expect("测试信令地址无效"),
            web: None,
            web_root: None,
        }
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn signalling_server_starts_without_a_viewer() {
        gst::init().expect("初始化 GStreamer 失败");
        if gst::ElementFactory::find("webrtcsink").is_none() {
            panic!("测试需要 webrtcsink：请使用仓库 devenv（会构建 webrtc 插件）");
        }
        if gst::ElementFactory::find("x264enc").is_none() {
            panic!("测试需要 x264enc：请使用仓库 devenv");
        }

        let config = config("x264enc tune=zerolatency speed-preset=ultrafast");
        let signalling = config.signalling;
        let (_frame_tx, frame_rx) = watch::channel(None);
        let (events, mut event_rx) = mpsc::unbounded_channel();
        let webrtc = Webrtc::start(&config, frame_rx, events)
            .await
            .expect("WebRTC 管线应可启动");

        let deadline = Instant::now() + Duration::from_secs(5);
        let mut connected = false;
        while Instant::now() < deadline {
            if std::net::TcpStream::connect(signalling).is_ok() {
                connected = true;
                break;
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        assert!(connected, "信令服务器未在 {signalling} 上监听");

        let first = tokio::time::timeout(Duration::from_secs(1), event_rx.recv())
            .await
            .expect("启动后应有 WebRTC 状态事件")
            .expect("事件通道不应关闭");
        assert!(
            matches!(first, Event::WebrtcStarted { .. }),
            "意外的首个事件：{first:?}"
        );

        webrtc.shutdown().await;
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn web_server_serves_the_configured_directory() {
        use std::io::{Read, Write};

        gst::init().expect("初始化 GStreamer 失败");
        let root = std::env::temp_dir().join(format!(
            "buqi-webrtc-web-{}-{}",
            std::process::id(),
            free_port()
        ));
        std::fs::create_dir_all(&root).expect("创建测试网页目录失败");
        std::fs::write(root.join("index.html"), "<html>buqi-webrtc-ok</html>")
            .expect("写入测试网页失败");

        let mut config = config("x264enc tune=zerolatency speed-preset=ultrafast");
        let web: std::net::SocketAddr = format!("127.0.0.1:{}", free_port())
            .parse()
            .expect("测试网页地址无效");
        config.web = Some(web);
        config.web_root = Some(root.clone());
        let (_frame_tx, frame_rx) = watch::channel(None);
        let (events, _event_rx) = mpsc::unbounded_channel();
        let webrtc = Webrtc::start(&config, frame_rx, events)
            .await
            .expect("带网页服务的 WebRTC 管线应可启动");

        let deadline = Instant::now() + Duration::from_secs(5);
        let mut response = String::new();
        while Instant::now() < deadline {
            if let Ok(mut stream) = std::net::TcpStream::connect(web) {
                stream
                    .write_all(
                        b"GET / HTTP/1.1
Host: localhost
Connection: close

",
                    )
                    .expect("发送测试请求失败");
                stream
                    .read_to_string(&mut response)
                    .expect("读取测试响应失败");
                break;
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        assert!(response.contains("200"), "实际响应：{response:?}");
        assert!(
            response.contains("buqi-webrtc-ok"),
            "实际响应：{response:?}"
        );

        webrtc.shutdown().await;
        std::fs::remove_dir_all(root).ok();
    }

    #[tokio::test]
    async fn missing_encoder_reports_unavailable() {
        gst::init().expect("初始化 GStreamer 失败");
        let config = config("definitely-not-a-real-gst-element");
        let (_frame_tx, frame_rx) = watch::channel(None);
        let (events, _event_rx) = mpsc::unbounded_channel();
        let error = Webrtc::start(&config, frame_rx, events)
            .await
            .err()
            .expect("未知编码器应让 WebRTC 启动失败");
        assert!(
            format!("{error:#}").contains("media.webrtc.encoder"),
            "{error:#}"
        );
    }

    #[test]
    fn viewer_count_saturates_and_notifies() {
        let (tx, mut rx) = watch::channel(0usize);
        let viewers = ViewerCount::new(tx);
        assert_eq!(viewers.added(), 1);
        assert_eq!(*rx.borrow_and_update(), 1);
        assert_eq!(viewers.added(), 2);
        assert_eq!(*rx.borrow_and_update(), 2);
        assert_eq!(viewers.removed(), 1);
        assert_eq!(*rx.borrow_and_update(), 1);
        assert_eq!(viewers.removed(), 0);
        assert_eq!(viewers.removed(), 0);
        assert_eq!(*rx.borrow_and_update(), 0);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn feeder_pushes_only_while_a_viewer_is_connected() {
        gst::init().expect("初始化 GStreamer 失败");
        let caps = gst::Caps::builder("image/jpeg").build();
        let appsrc = AppSrc::builder()
            .caps(&caps)
            .format(gst::Format::Time)
            .is_live(true)
            .do_timestamp(true)
            .max_bytes(1024 * 1024)
            .block(false)
            .build();
        let pipeline = gst::Pipeline::new();
        let sink = gst::ElementFactory::make("fakesink")
            .property("sync", false)
            .build()
            .expect("测试需要 fakesink");
        let appsrc_element: gst::Element = appsrc.clone().upcast();
        pipeline
            .add_many([&appsrc_element, &sink])
            .expect("组装测试管线失败");
        gst::Element::link_many([&appsrc_element, &sink]).expect("链接测试管线失败");

        let pushed = Arc::new(AtomicUsize::new(0));
        let probe_count = pushed.clone();
        appsrc
            .static_pad("src")
            .expect("appsrc 没有 src pad")
            .add_probe(gst::PadProbeType::BUFFER, move |_, _| {
                probe_count.fetch_add(1, Ordering::SeqCst);
                gst::PadProbeReturn::Ok
            });
        pipeline
            .set_state(gst::State::Playing)
            .expect("测试管线无法进入 PLAYING");

        let (frame_tx, frame_rx) = watch::channel(None);
        let (viewer_tx, viewer_rx) = watch::channel(0usize);
        let feeder = tokio::spawn(feed_loop(
            appsrc,
            frame_rx,
            viewer_rx,
            Arc::new(AtomicBool::new(false)),
        ));

        frame_tx.send_replace(Some(test_frame(1)));
        tokio::time::sleep(Duration::from_millis(50)).await;
        assert_eq!(pushed.load(Ordering::SeqCst), 0, "无观看者时不应推送帧");

        viewer_tx.send_replace(1);
        tokio::time::sleep(Duration::from_millis(100)).await;
        assert!(
            pushed.load(Ordering::SeqCst) >= 1,
            "观看者连接后应收到最新帧"
        );

        viewer_tx.send_replace(0);
        tokio::time::sleep(Duration::from_millis(50)).await;
        let before = pushed.load(Ordering::SeqCst);
        frame_tx.send_replace(Some(test_frame(2)));
        tokio::time::sleep(Duration::from_millis(50)).await;
        assert_eq!(
            pushed.load(Ordering::SeqCst),
            before,
            "观看者断开后不应继续推送"
        );

        feeder.abort();
        let _ = pipeline.set_state(gst::State::Null);
    }

    fn test_frame(sequence: u64) -> Arc<Frame> {
        Arc::new(Frame::new(
            "pc2-rgb",
            "session-1",
            sequence,
            320,
            240,
            vec![0xff, 0xd8, 0x01, 0xff, 0xd9],
        ))
    }
}
