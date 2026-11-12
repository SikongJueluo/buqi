//! `[media]` 配置：唯一采集的相机与 Zenoh 观测节奏。
//!
//! 区段存在即启用媒体模块；没有 `[media]` 时网关保持原有纯通信行为。
//! 相机格式固定为 V4L2 MJPEG（`image/jpeg`），首版不提供格式、缩放或裁剪选项。

use std::net::SocketAddr;
use std::path::PathBuf;

use anyhow::{Result, ensure};
use serde::Deserialize;

const DEFAULT_WIDTH: u32 = 1280;
const DEFAULT_HEIGHT: u32 = 720;
const DEFAULT_FRAMERATE: u32 = 30;
const DEFAULT_OBSERVATION_RATE_HZ: f64 = 5.0;
const DEFAULT_MAX_AGE_MS: u64 = 500;

/// 媒体模块配置。
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MediaConfig {
    pub camera: CameraConfig,
    #[serde(default)]
    pub observation: ObservationConfig,
    /// 可选的浏览器视频分支；缺失时只提供 JPEG 图片通道。
    #[serde(default)]
    pub webrtc: Option<WebrtcConfig>,
}

impl MediaConfig {
    /// 从 TOML 的 `[media]` 表解析并校验配置。
    pub(crate) fn from_toml(value: toml::Value) -> Result<Self> {
        let config: Self = value.try_into()?;
        config.validate()?;
        Ok(config)
    }

    fn validate(&self) -> Result<()> {
        let camera = &self.camera;
        ensure!(
            is_valid_camera_id(&camera.id),
            "media.camera.id 只能包含 ASCII 字母、数字、`-` 与 `_`，长度 1–64，实际为 `{}`",
            camera.id
        );
        ensure!(
            (1..=8192).contains(&camera.width),
            "media.camera.width 必须在 1–8192 之间，实际为 {}",
            camera.width
        );
        ensure!(
            (1..=8192).contains(&camera.height),
            "media.camera.height 必须在 1–8192 之间，实际为 {}",
            camera.height
        );
        ensure!(
            (1..=120).contains(&camera.framerate),
            "media.camera.framerate 必须在 1–120 之间，实际为 {}",
            camera.framerate
        );

        let observation = &self.observation;
        ensure!(
            observation.rate_hz.is_finite() && (0.1..=60.0).contains(&observation.rate_hz),
            "media.observation.rate_hz 必须在 0.1–60 之间，实际为 {}",
            observation.rate_hz
        );
        ensure!(
            (1..=10_000).contains(&observation.max_age_ms),
            "media.observation.max_age_ms 必须在 1–10000 之间，实际为 {}",
            observation.max_age_ms
        );

        if let Some(webrtc) = &self.webrtc {
            ensure!(
                !webrtc.encoder.trim().is_empty(),
                "media.webrtc.encoder 不能为空：必须显式选择编码后端"
            );
            ensure!(
                !webrtc.signalling.ip().is_unspecified() && webrtc.signalling.port() != 0,
                "media.webrtc.signalling 必须是明确的 IP:端口，不能是 0.0.0.0/:: 或端口 0"
            );
            match (&webrtc.web, &webrtc.web_root) {
                (Some(web), Some(root)) => {
                    ensure!(
                        !web.ip().is_unspecified() && web.port() != 0,
                        "media.webrtc.web 必须是明确的 IP:端口，不能是 0.0.0.0/:: 或端口 0"
                    );
                    ensure!(
                        root.is_dir(),
                        "media.webrtc.web_root 不是目录：{}",
                        root.display()
                    );
                }
                (None, None) => {}
                _ => anyhow::bail!(
                    "media.webrtc.web 与 media.webrtc.web_root 必须同时出现或同时省略"
                ),
            }
        }
        Ok(())
    }

    pub(crate) fn max_age(&self) -> std::time::Duration {
        std::time::Duration::from_millis(self.observation.max_age_ms)
    }
}

/// 唯一采集的相机。
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CameraConfig {
    /// 图片 key 中使用的相机标识，例如 `pc2-rgb`。
    pub id: String,
    /// V4L2 设备，推荐 `/dev/v4l/by-id/...`；不写死型号或序号。
    pub device: PathBuf,
    #[serde(default = "default_width")]
    pub width: u32,
    #[serde(default = "default_height")]
    pub height: u32,
    #[serde(default = "default_framerate")]
    pub framerate: u32,
}

/// 浏览器视频分支。编码器必须显式选择，失败时报告 WebRTC 不可用。
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WebrtcConfig {
    /// GStreamer 元素片段，例如 `x264enc tune=zerolatency speed-preset=ultrafast`。
    /// 片段应输出 `codec` 对应的编码格式；不做软件后端自动回退。
    pub encoder: String,
    pub codec: VideoCodec,
    /// webrtcsink 内置信令服务器的绑定地址。
    pub signalling: SocketAddr,
    /// 可选的静态网页服务地址；与 `web_root` 成对出现。
    #[serde(default)]
    pub web: Option<SocketAddr>,
    #[serde(default)]
    pub web_root: Option<PathBuf>,
}

/// 浏览器 WebRTC 可协商的视频编码。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum VideoCodec {
    H264,
    Vp8,
    Vp9,
}

impl VideoCodec {
    pub(crate) fn caps(self) -> &'static str {
        match self {
            Self::H264 => "video/x-h264",
            Self::Vp8 => "video/x-vp8",
            Self::Vp9 => "video/x-vp9",
        }
    }
}

/// 持续观测与新鲜度上限。
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ObservationConfig {
    /// 最新帧的 Zenoh 发布频率。单帧查询不等待这个节奏。
    #[serde(default = "default_rate_hz")]
    pub rate_hz: f64,
    /// 服务端缓存超过该年龄即拒绝回复；发布也跳过过期帧。
    #[serde(default = "default_max_age_ms")]
    pub max_age_ms: u64,
}

impl Default for ObservationConfig {
    fn default() -> Self {
        Self {
            rate_hz: DEFAULT_OBSERVATION_RATE_HZ,
            max_age_ms: DEFAULT_MAX_AGE_MS,
        }
    }
}

fn default_width() -> u32 {
    DEFAULT_WIDTH
}

fn default_height() -> u32 {
    DEFAULT_HEIGHT
}

fn default_framerate() -> u32 {
    DEFAULT_FRAMERATE
}

fn default_rate_hz() -> f64 {
    DEFAULT_OBSERVATION_RATE_HZ
}

fn default_max_age_ms() -> u64 {
    DEFAULT_MAX_AGE_MS
}

fn is_valid_camera_id(id: &str) -> bool {
    (1..=64).contains(&id.len())
        && id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(toml_text: &str) -> Result<MediaConfig> {
        let value: toml::Value = toml::from_str(toml_text).expect("测试 TOML 无效");
        MediaConfig::from_toml(value)
    }

    #[test]
    fn camera_defaults_are_720p30_and_5hz() {
        let config = parse(
            r#"
            [camera]
            id = "pc2-rgb"
            device = "/dev/v4l/by-id/usb-camera-video-index0"
            "#,
        )
        .expect("默认值应有效");

        assert_eq!(config.camera.width, 1280);
        assert_eq!(config.camera.height, 720);
        assert_eq!(config.camera.framerate, 30);
        assert_eq!(config.observation.rate_hz, 5.0);
        assert_eq!(config.observation.max_age_ms, 500);
    }

    #[test]
    fn explicit_observation_settings_are_used() {
        let config = parse(
            r#"
            [camera]
            id = "pc2-rgb"
            device = "/dev/video0"
            width = 640
            height = 480
            framerate = 15

            [observation]
            rate_hz = 2.5
            max_age_ms = 1200
            "#,
        )
        .expect("显式配置应有效");

        assert_eq!(config.camera.width, 640);
        assert_eq!(config.observation.rate_hz, 2.5);
        assert_eq!(config.max_age(), std::time::Duration::from_millis(1200));
    }

    #[test]
    fn unknown_keys_are_rejected() {
        let error = parse(
            r#"
            [camera]
            id = "pc2-rgb"
            device = "/dev/video0"
            exposure = 100
            "#,
        )
        .expect_err("未知字段应被拒绝");
        assert!(format!("{error}").contains("unknown field"), "{error}");
    }

    #[test]
    fn camera_id_cannot_escape_the_key_expression() {
        for id in ["", "pc2/rgb", "pc2 rgb", "pc2*", "a".repeat(65).as_str()] {
            let error = parse(&format!(
                r#"
                [camera]
                id = "{id}"
                device = "/dev/video0"
                "#
            ))
            .expect_err("非法相机 id 应被拒绝");
            assert!(format!("{error}").contains("media.camera.id"), "{error}");
        }
    }

    #[test]
    fn out_of_range_observation_rate_is_rejected() {
        for rate in ["0", "-1", "61", "nan"] {
            let error = parse(&format!(
                r#"
                [camera]
                id = "pc2-rgb"
                device = "/dev/video0"

                [observation]
                rate_hz = {rate}
                "#
            ))
            .expect_err("越界频率应被拒绝");
            assert!(format!("{error}").contains("rate_hz"), "{error}");
        }
    }

    #[test]
    fn zero_max_age_is_rejected() {
        let error = parse(
            r#"
            [camera]
            id = "pc2-rgb"
            device = "/dev/video0"

            [observation]
            max_age_ms = 0
            "#,
        )
        .expect_err("零新鲜度上限应被拒绝");
        assert!(format!("{error}").contains("max_age_ms"), "{error}");
    }

    #[test]
    fn media_section_must_be_a_table() {
        let value: toml::Value = toml::from_str("media = 1").unwrap();
        assert!(MediaConfig::from_toml(value).is_err());
    }

    #[test]
    fn webrtc_section_is_optional_and_validated() {
        let config = parse(
            r#"
            [camera]
            id = "pc2-rgb"
            device = "/dev/video0"

            [webrtc]
            encoder = "x264enc tune=zerolatency"
            codec = "h264"
            signalling = "127.0.0.1:8443"
            "#,
        )
        .expect("基本 WebRTC 配置应有效");
        let webrtc = config.webrtc.expect("应解析出 WebRTC 配置");
        assert_eq!(webrtc.codec, VideoCodec::H264);
        assert_eq!(webrtc.signalling.to_string(), "127.0.0.1:8443");
        assert!(webrtc.web.is_none());

        let error = parse(
            r#"
            [camera]
            id = "pc2-rgb"
            device = "/dev/video0"

            [webrtc]
            encoder = "x264enc"
            codec = "h264"
            signalling = "0.0.0.0:8443"
            "#,
        )
        .expect_err("信令不能默认暴露所有网卡");
        assert!(format!("{error}").contains("signalling"), "{error}");

        let error = parse(
            r#"
            [camera]
            id = "pc2-rgb"
            device = "/dev/video0"

            [webrtc]
            encoder = "x264enc"
            codec = "h264"
            signalling = "127.0.0.1:8443"
            web = "127.0.0.1:8080"
            "#,
        )
        .expect_err("web 缺少 web_root 应被拒绝");
        assert!(format!("{error}").contains("web_root"), "{error}");

        let error = parse(
            r#"
            [camera]
            id = "pc2-rgb"
            device = "/dev/video0"

            [webrtc]
            encoder = ""
            codec = "h264"
            signalling = "127.0.0.1:8443"
            "#,
        )
        .expect_err("空编码器应被拒绝");
        assert!(format!("{error}").contains("encoder"), "{error}");
    }
}
