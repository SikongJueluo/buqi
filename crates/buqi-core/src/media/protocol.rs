//! Zenoh 图片通道的 key、编码与错误载荷。
//!
//! 成功回复的 payload 是相机原始 JPEG 字节，附件是 [`crate::media::Metadata`]
//! 的 JSON；JPEG 与元数据在同一条 reply/put 中绑定。失败回复使用
//! `application/json`，客户端以编码区分两者。

use serde::Serialize;

pub const PROTOCOL_VERSION: u8 = 1;

pub const NO_FRAME: &str = "no_frame";
pub const STALE: &str = "stale";

/// 持续观测 key：按配置频率发布最新帧。
pub fn frame_key(camera: &str) -> String {
    format!("buqi/media/v{PROTOCOL_VERSION}/{camera}/frame")
}

/// 单帧查询 key：读取服务端最新有效缓存。
pub fn snapshot_key(camera: &str) -> String {
    format!("buqi/media/v{PROTOCOL_VERSION}/{camera}/snapshot")
}

/// 单帧查询的失败回复；不携带 JPEG。
#[derive(Debug, Serialize)]
pub struct ErrorReply<'a> {
    pub version: u8,
    pub camera: &'a str,
    pub error: &'static str,
    pub max_age_ms: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub age_ms: Option<u64>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keys_are_stable_and_camera_scoped() {
        assert_eq!(frame_key("pc2-rgb"), "buqi/media/v1/pc2-rgb/frame");
        assert_eq!(snapshot_key("pc2-rgb"), "buqi/media/v1/pc2-rgb/snapshot");
    }

    #[test]
    fn stale_error_serializes_age_but_no_frame_does_not() {
        let stale = ErrorReply {
            version: PROTOCOL_VERSION,
            camera: "pc2-rgb",
            error: STALE,
            max_age_ms: 500,
            age_ms: Some(812),
        };
        assert_eq!(
            serde_json::to_value(&stale).unwrap(),
            serde_json::json!({
                "version": 1,
                "camera": "pc2-rgb",
                "error": "stale",
                "max_age_ms": 500,
                "age_ms": 812,
            })
        );

        let empty = ErrorReply {
            version: PROTOCOL_VERSION,
            camera: "pc2-rgb",
            error: NO_FRAME,
            max_age_ms: 500,
            age_ms: None,
        };
        assert_eq!(
            serde_json::to_value(&empty).unwrap(),
            serde_json::json!({
                "version": 1,
                "camera": "pc2-rgb",
                "error": "no_frame",
                "max_age_ms": 500,
            })
        );
    }
}
