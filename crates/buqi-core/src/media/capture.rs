//! V4L2 MJPEG 采集：一个设备只由一个管线持有。
//!
//! 采集线程在 `new-sample` 回调里只做一次拷贝并覆盖最新帧缓存，不等待 Zenoh、
//! 网络或图片处理。管线错误按确定性/临时性分类：缺插件、格式不支持等立即停止并
//! 报告；设备掉线、占用等以有上限的退避重试，每次成功启动都换新的采集会话 id。

use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::thread::JoinHandle;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result, anyhow};
use gstreamer as gst;
use gstreamer::prelude::*;
use gstreamer_app::{AppSink, AppSinkCallbacks};
use tokio::sync::mpsc;

use super::Event;
use super::config::CameraConfig;
use super::frame::{Frame, Latest};

const BACKOFF_START: Duration = Duration::from_secs(1);
const BACKOFF_MAX: Duration = Duration::from_secs(30);
/// 连续稳定采集这么久后才重置退避，避免设备反复“打开即断”时紧密重试。
const STABLE_SESSION: Duration = Duration::from_secs(10);
const BUS_POLL: Duration = Duration::from_millis(200);

/// 一个相机采集任务。
pub(crate) struct Capture {
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}

impl Capture {
    pub(crate) fn start(
        camera: &CameraConfig,
        latest: Arc<Latest>,
        events: mpsc::UnboundedSender<Event>,
    ) -> Result<Self> {
        let stop = Arc::new(AtomicBool::new(false));
        let camera = camera.clone();
        let thread_stop = stop.clone();
        let thread = std::thread::Builder::new()
            .name(format!("buqi-capture-{}", camera.id))
            .spawn(move || run(&camera, latest, &events, &thread_stop))
            .context("创建相机采集线程失败")?;
        Ok(Self {
            stop,
            thread: Some(thread),
        })
    }

    pub(crate) fn shutdown(mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

fn run(
    camera: &CameraConfig,
    latest: Arc<Latest>,
    events: &mpsc::UnboundedSender<Event>,
    stop: &AtomicBool,
) {
    if let Err(error) = gst::init() {
        let _ = events.send(Event::Failed {
            reason: format!("初始化 GStreamer 失败：{error}"),
        });
        return;
    }

    let mut backoff = BACKOFF_START;
    let mut attempt = 0;
    while !stop.load(Ordering::Relaxed) {
        let session = new_session_id();
        let (end, produced, streamed) = run_session(
            camera,
            Source::Device(&camera.device),
            latest.clone(),
            events,
            stop,
            &session,
        );
        match end {
            SessionEnd::Stopped => break,
            SessionEnd::Failed {
                deterministic: true,
                reason,
            } => {
                let _ = events.send(Event::Failed { reason });
                break;
            }
            SessionEnd::Failed {
                deterministic: false,
                reason,
            } => {
                if produced > 0 && streamed >= STABLE_SESSION {
                    backoff = BACKOFF_START;
                    attempt = 0;
                }
                attempt += 1;
                let _ = events.send(Event::Retrying {
                    attempt,
                    delay: backoff,
                    reason,
                });
                if !sleep_unless_stopped(backoff, stop) {
                    break;
                }
                backoff = (backoff * 2).min(BACKOFF_MAX);
            }
        }
    }
}

enum SessionEnd {
    Stopped,
    Failed { deterministic: bool, reason: String },
}

/// 采集源；测试用合成图，正式运行只接受配置中的设备。
#[derive(Clone, Copy)]
enum Source<'a> {
    Device(&'a Path),
    #[cfg(test)]
    TestPattern,
}

fn run_session(
    camera: &CameraConfig,
    source: Source<'_>,
    latest: Arc<Latest>,
    events: &mpsc::UnboundedSender<Event>,
    stop: &AtomicBool,
    session: &str,
) -> (SessionEnd, u64, Duration) {
    let started = Instant::now();
    let (pipeline, appsink) = match build_pipeline(camera, source) {
        Ok(parts) => parts,
        Err(error) => {
            return (
                SessionEnd::Failed {
                    deterministic: true,
                    reason: format!("{error:#}"),
                },
                0,
                started.elapsed(),
            );
        }
    };

    let produced = Arc::new(AtomicU64::new(0));
    let first_frame = Arc::new(AtomicBool::new(false));
    install_callbacks(
        &appsink,
        latest,
        events,
        camera.id.clone(),
        session.to_owned(),
        produced.clone(),
        first_frame,
    );

    let bus = match pipeline.bus() {
        Some(bus) => bus,
        None => {
            let _ = pipeline.set_state(gst::State::Null);
            return (
                SessionEnd::Failed {
                    deterministic: true,
                    reason: "采集管线没有 bus".to_owned(),
                },
                0,
                started.elapsed(),
            );
        }
    };

    if pipeline.set_state(gst::State::Playing).is_err() {
        let _ = pipeline.set_state(gst::State::Null);
        return (
            SessionEnd::Failed {
                deterministic: false,
                reason: "采集管线无法进入 PLAYING".to_owned(),
            },
            0,
            started.elapsed(),
        );
    }
    let _ = events.send(Event::CaptureStarted {
        session: session.to_owned(),
    });

    let end = loop {
        if stop.load(Ordering::Relaxed) {
            break SessionEnd::Stopped;
        }
        let Some(message) = bus.timed_pop_filtered(
            gst::ClockTime::from_mseconds(BUS_POLL.as_millis() as u64),
            &[gst::MessageType::Error, gst::MessageType::Eos],
        ) else {
            continue;
        };
        match message.view() {
            gst::MessageView::Error(error) => {
                let glib_error = error.error();
                let debug = error.debug();
                let debug = debug.as_ref().map(|debug| debug.as_str());
                break SessionEnd::Failed {
                    deterministic: is_deterministic(&glib_error, debug),
                    reason: describe_error(&glib_error, debug),
                };
            }
            gst::MessageView::Eos(_) => {
                break SessionEnd::Failed {
                    deterministic: false,
                    reason: "采集流已结束（设备可能断开）".to_owned(),
                };
            }
            _ => {}
        }
    };

    let _ = pipeline.set_state(gst::State::Null);
    (end, produced.load(Ordering::Relaxed), started.elapsed())
}

fn install_callbacks(
    appsink: &AppSink,
    latest: Arc<Latest>,
    events: &mpsc::UnboundedSender<Event>,
    camera: String,
    session: String,
    produced: Arc<AtomicU64>,
    first_frame: Arc<AtomicBool>,
) {
    let events = events.clone();
    let callbacks = AppSinkCallbacks::builder()
        .new_sample(move |appsink| {
            let sample = appsink.pull_sample().map_err(|_| gst::FlowError::Eos)?;
            let buffer = sample.buffer().ok_or(gst::FlowError::Error)?;
            let caps = sample.caps().ok_or(gst::FlowError::Error)?;
            let structure = caps.structure(0).ok_or(gst::FlowError::Error)?;
            let (Ok(width), Ok(height)) = (
                structure.get::<i32>("width"),
                structure.get::<i32>("height"),
            ) else {
                return Err(gst::FlowError::Error);
            };
            let map = buffer.map_readable().map_err(|_| gst::FlowError::Error)?;
            let sequence = produced.fetch_add(1, Ordering::Relaxed);
            let frame = Frame::new(
                &camera,
                &session,
                sequence,
                width as u32,
                height as u32,
                map.as_slice().to_vec(),
            );
            latest.publish(Arc::new(frame));
            if !first_frame.swap(true, Ordering::Relaxed) {
                let _ = events.send(Event::FirstFrame);
            }
            Ok(gst::FlowSuccess::Ok)
        })
        .build();
    appsink.set_callbacks(callbacks);
}

fn build_pipeline(camera: &CameraConfig, source: Source<'_>) -> Result<(gst::Pipeline, AppSink)> {
    let pipeline = gst::Pipeline::new();
    let source_element = match source {
        Source::Device(device) => {
            let element = gst::ElementFactory::make("v4l2src")
                .build()
                .context("创建 v4l2src 失败：需要 GStreamer video4linux2 插件")?;
            element.set_property("device", device.to_string_lossy().as_ref());
            element
        }
        #[cfg(test)]
        Source::TestPattern => gst::ElementFactory::make("videotestsrc")
            .property("is-live", true)
            .build()
            .context("创建 videotestsrc 失败：测试需要 GStreamer videotestsrc")?,
    };

    let caps = gst::Caps::builder("image/jpeg")
        .field("width", camera.width as i32)
        .field("height", camera.height as i32)
        .field("framerate", gst::Fraction::new(camera.framerate as i32, 1))
        .build();
    let capsfilter = gst::ElementFactory::make("capsfilter")
        .property("caps", &caps)
        .build()
        .context("创建 capsfilter 失败")?;

    let appsink = gst::ElementFactory::make("appsink")
        .build()
        .context("创建 appsink 失败：需要 GStreamer app 插件")?
        .downcast::<AppSink>()
        .map_err(|_| anyhow!("appsink 元件类型不符"))?;
    appsink.set_property("max-buffers", 1u32);
    appsink.set_property("sync", false);
    appsink.set_property("wait-on-eos", false);
    if gst::version() >= (1, 28, 0, 0) {
        appsink.set_property_from_str("leaky-type", "downstream");
    } else {
        // 1.28 起 `drop` 废弃，改为 `leaky-type`；旧版只有 `drop`。
        appsink.set_property("drop", true);
    }

    let appsink_element = appsink.clone().upcast::<gst::Element>();
    #[cfg(test)]
    let encoder = match source {
        Source::TestPattern => Some(
            gst::ElementFactory::make("jpegenc")
                .build()
                .context("创建 jpegenc 失败：测试需要 GStreamer plugins-good")?,
        ),
        _ => None,
    };

    match source {
        Source::Device(_) => {
            pipeline.add_many([&source_element, &capsfilter, &appsink_element])?;
            gst::Element::link_many([&source_element, &capsfilter, &appsink_element])?;
        }
        #[cfg(test)]
        Source::TestPattern => {
            let encoder = encoder.expect("测试源需要 jpegenc");
            pipeline.add_many([&source_element, &encoder, &capsfilter, &appsink_element])?;
            gst::Element::link_many([&source_element, &encoder, &capsfilter, &appsink_element])?;
        }
    }

    Ok((pipeline, appsink))
}

fn is_deterministic(error: &gst::glib::Error, debug: Option<&str>) -> bool {
    if let Some(core) = error.kind::<gst::CoreError>() {
        return matches!(
            core,
            gst::CoreError::MissingPlugin | gst::CoreError::Negotiation
        );
    }
    if let Some(resource) = error.kind::<gst::ResourceError>() {
        return matches!(resource, gst::ResourceError::Settings);
    }
    if let Some(stream) = error.kind::<gst::StreamError>() {
        return match stream {
            gst::StreamError::Format => true,
            // basesrc/v4l2src 在运行期发现设备 caps 与目标 caps 无交集时，
            // 发的是 StreamError::Failed + “reason not-negotiated”，不是 Format。
            gst::StreamError::Failed => {
                error.message().contains("not-negotiated")
                    || debug.is_some_and(|debug| debug.contains("not-negotiated"))
            }
            _ => false,
        };
    }
    false
}

fn describe_error(error: &gst::glib::Error, debug: Option<&str>) -> String {
    match debug.filter(|debug| !debug.is_empty()) {
        Some(debug) => format!("{error}（{debug}）"),
        None => error.to_string(),
    }
}

fn new_session_id() -> String {
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |elapsed| elapsed.as_nanos() as u64);
    format!(
        "{nanos:016x}{:04x}",
        COUNTER.fetch_add(1, Ordering::Relaxed) as u16
    )
}

fn sleep_unless_stopped(duration: Duration, stop: &AtomicBool) -> bool {
    let deadline = Instant::now() + duration;
    while Instant::now() < deadline {
        if stop.load(Ordering::Relaxed) {
            return false;
        }
        std::thread::sleep(Duration::from_millis(50).min(deadline - Instant::now()));
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deterministic_errors_are_classified() {
        let missing = gst::glib::Error::new(gst::CoreError::MissingPlugin, "缺少 v4l2src");
        let caps = gst::glib::Error::new(gst::ResourceError::Settings, "不支持的格式");
        let busy = gst::glib::Error::new(gst::ResourceError::Busy, "设备被占用");
        let read = gst::glib::Error::new(gst::ResourceError::Read, "设备读失败");
        let negotiated = gst::glib::Error::new(
            gst::StreamError::Failed,
            "streaming stopped, reason not-negotiated (-4)",
        );
        let failed = gst::glib::Error::new(gst::StreamError::Failed, "其他流错误");
        assert!(is_deterministic(&missing, None));
        assert!(is_deterministic(&caps, None));
        assert!(!is_deterministic(&busy, None));
        assert!(!is_deterministic(&read, None));
        assert!(is_deterministic(&negotiated, None));
        assert!(is_deterministic(
            &failed,
            Some("streaming stopped, reason not-negotiated (-4)")
        ));
        assert!(!is_deterministic(&failed, Some("其他调试信息")));
        assert!(!is_deterministic(&failed, None));
    }

    #[test]
    fn runtime_negotiation_failure_is_classified_as_deterministic() {
        use gstreamer_app::AppSrc;

        gst::init().expect("初始化 GStreamer 失败");
        let pipeline = gst::Pipeline::new();
        // appsrc 在运行期才固定 RGB caps，模拟 v4l2src 打开设备后才确定格式；
        // 下游 capsfilter 要求 I420，协商必须失败，而不是在 link 阶段被拒绝。
        let appsrc = AppSrc::builder()
            .format(gst::Format::Time)
            .is_live(true)
            .build();
        let filter_caps = gst::Caps::builder("video/x-raw")
            .field("format", "I420")
            .field("width", 640i32)
            .field("height", 480i32)
            .build();
        let filter = gst::ElementFactory::make("capsfilter")
            .property("caps", &filter_caps)
            .build()
            .expect("测试需要 capsfilter");
        let sink = gst::ElementFactory::make("fakesink")
            .build()
            .expect("测试需要 fakesink");
        pipeline
            .add_many([appsrc.upcast_ref(), &filter, &sink])
            .expect("添加测试元件失败");
        gst::Element::link_many([appsrc.upcast_ref(), &filter, &sink]).expect("链接测试元件失败");
        let _ = pipeline.set_state(gst::State::Playing);

        let input_caps = gst::Caps::builder("video/x-raw")
            .field("format", "RGB")
            .field("width", 320i32)
            .field("height", 240i32)
            .build();
        appsrc.set_caps(Some(&input_caps));
        let mut buffer = gst::Buffer::with_size(320 * 240 * 3).expect("创建测试 buffer 失败");
        buffer
            .get_mut()
            .expect("buffer 可写")
            .set_pts(gst::ClockTime::ZERO);
        let _ = appsrc.push_buffer(buffer);

        let bus = pipeline.bus().expect("测试管线没有 bus");
        let message = bus
            .timed_pop_filtered(gst::ClockTime::from_seconds(5), &[gst::MessageType::Error])
            .expect("5 秒内未观察到运行期协商错误");
        let (error, debug) = match message.view() {
            gst::MessageView::Error(error) => (error.error(), error.debug()),
            other => panic!("期望 Error 消息，实际为 {other:?}"),
        };
        let _ = pipeline.set_state(gst::State::Null);

        let debug = debug.as_ref().map(|debug| debug.as_str());
        assert_eq!(
            error.kind::<gst::StreamError>(),
            Some(gst::StreamError::Failed),
            "实际错误：{error}，debug={debug:?}"
        );
        assert!(
            debug.is_some_and(|debug| debug.contains("not-negotiated")),
            "实际 debug={debug:?}"
        );
        assert!(
            is_deterministic(&error, debug),
            "运行期 caps 协商失败应按确定性错误处理"
        );
    }

    #[test]
    fn synthetic_jpeg_flows_through_the_capture_pipeline() {
        use crate::media::frame::Lookup;
        gst::init().expect("初始化 GStreamer 失败");
        let camera = CameraConfig {
            id: "test".to_owned(),
            device: std::path::PathBuf::from("/dev/null"),
            width: 320,
            height: 240,
            framerate: 30,
        };
        let latest = Arc::new(Latest::new());
        let stop = Arc::new(AtomicBool::new(false));
        let (events, _event_rx) = mpsc::unbounded_channel();
        let thread_latest = latest.clone();
        let thread_stop = stop.clone();
        let handle = std::thread::spawn(move || {
            run_session(
                &camera,
                Source::TestPattern,
                thread_latest,
                &events,
                &thread_stop,
                "s-test",
            )
        });

        let deadline = Instant::now() + Duration::from_secs(5);
        let frame = loop {
            if let Lookup::Fresh(frame) = latest.lookup(Duration::from_secs(1)) {
                break frame;
            }
            assert!(Instant::now() < deadline, "5 秒内未收到测试帧");
            std::thread::sleep(Duration::from_millis(20));
        };
        stop.store(true, Ordering::Relaxed);

        assert_eq!(frame.metadata.width, 320);
        assert_eq!(frame.metadata.height, 240);
        assert_eq!(frame.metadata.session, "s-test");
        assert!(frame.jpeg.starts_with(&[0xff, 0xd8]), "不是 JPEG 字节");
        assert!(frame.jpeg.ends_with(&[0xff, 0xd9]), "JPEG 未以 EOI 结束");

        let (end, produced, _) = handle.join().expect("采集线程 panic");
        assert!(matches!(end, SessionEnd::Stopped), "停止信号应正常结束");
        assert!(produced >= 1);
    }
}
