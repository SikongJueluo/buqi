//! 可选媒体模块：持续采集相机 MJPEG，经 appsink 取出原始 JPEG，
//! 通过 Zenoh 发布最新帧，并回复单帧查询。
//!
//! 入口是 [`Media::start`]：它声明图片 key、启动发布/查询任务与一个采集线程。
//! 相机故障只影响媒体状态，不停止 Zenoh Router / DDS；配置语法错误仍由
//! [`crate::config`] 在启动前拒绝。

mod capture;
mod config;
mod frame;
mod protocol;
mod transport;
mod webrtc;

use std::sync::Arc;
use std::time::Duration;

use anyhow::Result;
use tokio::sync::{mpsc, watch};
use tokio::task::JoinHandle;
use zenoh::Session;

use crate::media::frame::Latest;

pub use config::{MediaConfig, VideoCodec, WebrtcConfig};
pub use frame::Metadata;

/// 媒体模块状态事件，由网关前台循环打印。
#[derive(Debug, Clone)]
pub enum Event {
    /// 采集管线已进入 PLAYING；此时还没有收到首帧。
    CaptureStarted { session: String },
    /// 本次采集会话已收到首帧。
    FirstFrame,
    /// 临时故障，退避后重试。
    Retrying {
        attempt: u32,
        delay: Duration,
        reason: String,
    },
    /// 确定性故障，媒体已停止，等待操作者修复并重启网关。
    Failed { reason: String },
    /// WebRTC 信令已启动。
    WebrtcStarted {
        signalling: String,
        web: Option<String>,
    },
    /// WebRTC 分支不可用；JPEG 图片通道不受影响。
    WebrtcUnavailable { reason: String },
    /// WebRTC 分支已停止；JPEG 图片通道不受影响。
    WebrtcFailed { reason: String },
    /// 浏览器观看者数量变化。
    WebrtcViewers { count: usize },
}

/// 已启动的媒体模块。
pub struct Media {
    capture: Option<capture::Capture>,
    webrtc: Option<webrtc::Webrtc>,
    tasks: Vec<JoinHandle<()>>,
}

impl Media {
    /// 启动图片传输与采集线程。
    ///
    /// 返回的接收端用于报告后续状态。传输任务声明失败会让启动失败；
    /// 采集管线错误只产生 [`Event`]，不影响网关其他部分。
    pub async fn start(
        config: MediaConfig,
        session: Arc<Session>,
    ) -> Result<(Self, mpsc::UnboundedReceiver<Event>)> {
        let latest = Arc::new(Latest::new());
        let (events, receiver) = mpsc::unbounded_channel();
        let (frame_tx, frame_rx) = match config.webrtc {
            Some(_) => {
                let (tx, rx) = watch::channel(None);
                (Some(tx), Some(rx))
            }
            None => (None, None),
        };
        let capture =
            capture::Capture::start(&config.camera, latest.clone(), events.clone(), frame_tx)?;
        let tasks = match transport::start(session, &config, latest).await {
            Ok(tasks) => tasks,
            Err(error) => {
                capture.shutdown();
                return Err(error);
            }
        };
        let webrtc = match (&config.webrtc, frame_rx) {
            (Some(webrtc_config), Some(frames)) => {
                match webrtc::Webrtc::start(webrtc_config, frames, events.clone()).await {
                    Ok(webrtc) => Some(webrtc),
                    Err(error) => {
                        let _ = events.send(Event::WebrtcUnavailable {
                            reason: format!("{error:#}"),
                        });
                        None
                    }
                }
            }
            _ => None,
        };
        Ok((
            Self {
                capture: Some(capture),
                webrtc,
                tasks,
            },
            receiver,
        ))
    }

    /// 停止采集并撤销 Zenoh 图片任务。
    pub async fn shutdown(mut self) {
        if let Some(webrtc) = self.webrtc.take() {
            webrtc.shutdown().await;
        }
        if let Some(capture) = self.capture.take() {
            capture.shutdown();
        }
        for task in self.tasks.drain(..) {
            task.abort();
            let _ = task.await;
        }
    }
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;
    use std::sync::Arc;
    use std::time::Duration;

    use zenoh::bytes::Encoding;

    use super::config::{CameraConfig, ObservationConfig};
    use super::frame::{Frame, Latest};
    use super::protocol;
    use super::transport;

    fn config(id: &str, rate_hz: f64, max_age_ms: u64) -> super::MediaConfig {
        super::MediaConfig {
            camera: CameraConfig {
                id: id.to_owned(),
                device: PathBuf::from("/dev/null"),
                width: 1280,
                height: 720,
                framerate: 30,
            },
            observation: ObservationConfig {
                rate_hz,
                max_age_ms,
            },
            webrtc: None,
        }
    }

    async fn test_session() -> zenoh::Session {
        let mut config = zenoh::Config::default();
        config.insert_json5("mode", "\"peer\"").unwrap();
        config.insert_json5("listen/endpoints", "[]").unwrap();
        config
            .insert_json5("scouting/multicast/enabled", "false")
            .unwrap();
        config
            .insert_json5("scouting/gossip/enabled", "false")
            .unwrap();
        zenoh::open(config).await.expect("创建测试 Session 失败")
    }

    fn frame(sequence: u64, jpeg: &[u8]) -> Arc<Frame> {
        Arc::new(Frame::new(
            "pc2-rgb",
            "session-1",
            sequence,
            1280,
            720,
            jpeg.to_vec(),
        ))
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn snapshot_returns_jpeg_with_metadata_below_publish_rate() {
        let session = Arc::new(test_session().await);
        let latest = Arc::new(Latest::new());
        let media = config("pc2-rgb", 0.1, 500);
        let tasks = transport::start(session.clone(), &media, latest.clone())
            .await
            .expect("启动图片传输失败");

        let jpeg = vec![0xff, 0xd8, 0x01, 0x02, 0xff, 0xd9];
        let published = frame(7, &jpeg);
        latest.publish(published.clone());

        let key = protocol::snapshot_key("pc2-rgb");
        let replies = session.get(&key).await.expect("查询失败");
        let reply = tokio::time::timeout(Duration::from_secs(2), replies.recv_async())
            .await
            .expect("查询超时")
            .expect("没有回复");
        let sample = reply.result().expect("单帧查询不应返回错误");

        assert_eq!(sample.encoding(), &Encoding::IMAGE_JPEG);
        assert_eq!(sample.payload().to_bytes().as_ref(), jpeg.as_slice());
        assert_eq!(
            sample.attachment().expect("应带元数据").to_bytes().as_ref(),
            published.metadata_json().as_bytes()
        );

        for task in tasks {
            task.abort();
        }
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn snapshot_reports_no_frame_and_stale_explicitly() {
        let session = Arc::new(test_session().await);
        let latest = Arc::new(Latest::new());
        let media = config("pc2-rgb", 0.1, 10);
        let tasks = transport::start(session.clone(), &media, latest.clone())
            .await
            .expect("启动图片传输失败");

        let key = protocol::snapshot_key("pc2-rgb");
        let replies = session.get(&key).await.expect("查询失败");
        let reply = tokio::time::timeout(Duration::from_secs(2), replies.recv_async())
            .await
            .expect("查询超时")
            .expect("没有回复");
        let sample = reply.result().expect("失败回复应是有效载荷");
        assert_eq!(sample.encoding(), &Encoding::APPLICATION_JSON);
        let error: serde_json::Value =
            serde_json::from_slice(&sample.payload().to_bytes()).expect("错误载荷应是 JSON");
        assert_eq!(error["error"], protocol::NO_FRAME);
        assert_eq!(error["max_age_ms"], 10);

        latest.publish(frame(1, &[0xff, 0xd8, 0xff, 0xd9]));
        tokio::time::sleep(Duration::from_millis(30)).await;

        let replies = session.get(&key).await.expect("查询失败");
        let reply = tokio::time::timeout(Duration::from_secs(2), replies.recv_async())
            .await
            .expect("查询超时")
            .expect("没有回复");
        let sample = reply.result().expect("失败回复应是有效载荷");
        let error: serde_json::Value =
            serde_json::from_slice(&sample.payload().to_bytes()).expect("错误载荷应是 JSON");
        assert_eq!(error["error"], protocol::STALE);
        assert!(error["age_ms"].as_u64().unwrap() >= 30, "{error}");

        for task in tasks {
            task.abort();
        }
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn unchanged_frame_is_not_republished() {
        let session = Arc::new(test_session().await);
        let latest = Arc::new(Latest::new());
        let media = config("pc2-rgb", 50.0, 500);
        let tasks = transport::start(session.clone(), &media, latest.clone())
            .await
            .expect("启动图片传输失败");

        let subscriber = session
            .declare_subscriber(protocol::frame_key("pc2-rgb"))
            .await
            .expect("声明订阅失败");

        let jpeg = vec![0xff, 0xd8, 0x01, 0xff, 0xd9];
        latest.publish(frame(7, &jpeg));
        let first = tokio::time::timeout(Duration::from_secs(2), subscriber.recv_async())
            .await
            .expect("等待首帧超时")
            .expect("订阅失败");
        let metadata: serde_json::Value =
            serde_json::from_slice(&first.attachment().expect("应带元数据").to_bytes())
                .expect("元数据应是 JSON");
        assert_eq!(metadata["sequence"], 7);

        // 采集缓存没有更新时，即使发布频率很高也不能重复发送同一帧。
        assert!(
            tokio::time::timeout(Duration::from_millis(300), subscriber.recv_async())
                .await
                .is_err(),
            "冻结帧被重复发布"
        );

        latest.publish(frame(8, &jpeg));
        let second = tokio::time::timeout(Duration::from_secs(2), subscriber.recv_async())
            .await
            .expect("等待更新帧超时")
            .expect("订阅失败");
        let metadata: serde_json::Value =
            serde_json::from_slice(&second.attachment().expect("应带元数据").to_bytes())
                .expect("元数据应是 JSON");
        assert_eq!(metadata["sequence"], 8);

        for task in tasks {
            task.abort();
        }
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn subscription_receives_only_latest_frames() {
        let session = Arc::new(test_session().await);
        let latest = Arc::new(Latest::new());
        let media = config("pc2-rgb", 50.0, 500);
        let tasks = transport::start(session.clone(), &media, latest.clone())
            .await
            .expect("启动图片传输失败");

        let subscriber = session
            .declare_subscriber(protocol::frame_key("pc2-rgb"))
            .await
            .expect("声明订阅失败");

        let first_jpeg = vec![0xff, 0xd8, 0x0a, 0xff, 0xd9];
        latest.publish(frame(1, &first_jpeg));
        let sample = tokio::time::timeout(Duration::from_secs(2), subscriber.recv_async())
            .await
            .expect("等待首帧超时")
            .expect("订阅失败");
        assert_eq!(sample.payload().to_bytes().as_ref(), first_jpeg.as_slice());
        assert_eq!(sample.encoding(), &Encoding::IMAGE_JPEG);
        let metadata: serde_json::Value =
            serde_json::from_slice(&sample.attachment().expect("应带元数据").to_bytes())
                .expect("元数据应是 JSON");
        assert_eq!(metadata["sequence"], 1);

        latest.publish(frame(9, &[0xff, 0xd8, 0x0b, 0xff, 0xd9]));
        let sample = tokio::time::timeout(Duration::from_secs(2), subscriber.recv_async())
            .await
            .expect("等待更新帧超时")
            .expect("订阅失败");
        let metadata: serde_json::Value =
            serde_json::from_slice(&sample.attachment().expect("应带元数据").to_bytes())
                .expect("元数据应是 JSON");
        assert!(metadata["sequence"].as_u64().unwrap() >= 9, "{metadata}");

        for task in tasks {
            task.abort();
        }
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn media_keeps_serving_explicit_errors_when_capture_fails() {
        let session = Arc::new(test_session().await);
        let (media, mut events) = super::Media::start(config("pc2-rgb", 0.1, 500), session.clone())
            .await
            .expect("媒体传输应能启动");

        // 设备不存在/不可用时，采集层必须先报告状态，而不是让启动挂住。
        let first = tokio::time::timeout(Duration::from_secs(5), events.recv())
            .await
            .expect("5 秒内没有媒体状态")
            .expect("媒体状态通道不应关闭");
        assert!(
            matches!(
                first,
                super::Event::CaptureStarted { .. }
                    | super::Event::Failed { .. }
                    | super::Event::Retrying { .. }
            ),
            "意外的首个媒体状态：{first:?}"
        );

        // 即使采集不可用，图片 key 仍以明确错误回复，而不是超时。
        let replies = session
            .get(protocol::snapshot_key("pc2-rgb"))
            .await
            .expect("单帧查询失败");
        let reply = tokio::time::timeout(Duration::from_secs(2), replies.recv_async())
            .await
            .expect("查询超时：采集失败不应让查询挂起")
            .expect("没有回复");
        let sample = reply.result().expect("应是明确的错误回复");
        let error: serde_json::Value =
            serde_json::from_slice(&sample.payload().to_bytes()).expect("错误载荷应是 JSON");
        assert_eq!(error["error"], protocol::NO_FRAME);

        media.shutdown().await;
    }
}
