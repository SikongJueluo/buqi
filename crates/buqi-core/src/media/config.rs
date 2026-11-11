//! `[media]` 配置：唯一采集的相机与 Zenoh 观测节奏。
//!
//! 区段存在即启用媒体模块；没有 `[media]` 时网关保持原有纯通信行为。
//! 相机格式固定为 V4L2 MJPEG（`image/jpeg`），首版不提供格式、缩放或裁剪选项。

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
}
