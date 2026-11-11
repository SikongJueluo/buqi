//! 采集帧、元数据与“只保留最新帧”的有界缓存。
//!
//! 缓存由采集线程在收到样本时覆盖；读取方只拿到 `Arc`，不会反压采集。

use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::Serialize;

use super::protocol::PROTOCOL_VERSION;

/// 帧时间来自服务端收到采集帧时的主机时钟；不是曝光时间，也未经跨机同步。
#[derive(Debug, Serialize)]
pub struct Metadata {
    pub version: u8,
    pub camera: String,
    pub session: String,
    pub sequence: u64,
    pub format: &'static str,
    pub width: u32,
    pub height: u32,
    pub captured_at_unix_ms: u64,
}

/// 一帧相机原始 JPEG 与同一条消息中携带的元数据。
#[derive(Debug)]
pub struct Frame {
    pub metadata: Metadata,
    pub jpeg: Vec<u8>,
    pub received: Instant,
}

impl Frame {
    pub fn new(
        camera: &str,
        session: &str,
        sequence: u64,
        width: u32,
        height: u32,
        jpeg: Vec<u8>,
    ) -> Self {
        let metadata = Metadata {
            version: PROTOCOL_VERSION,
            camera: camera.to_owned(),
            session: session.to_owned(),
            sequence,
            format: "jpeg",
            width,
            height,
            captured_at_unix_ms: unix_ms(),
        };
        Self {
            metadata,
            jpeg,
            received: Instant::now(),
        }
    }

    /// 与 JPEG 同一条消息发送的元数据 JSON。
    pub fn metadata_json(&self) -> String {
        serde_json::to_string(&self.metadata).expect("帧元数据序列化不会失败")
    }
}

/// 最新帧缓存的一次读取结果。
#[derive(Debug)]
pub enum Lookup {
    Fresh(Arc<Frame>),
    /// 有缓存，但年龄超过上限；停流后的画面也属于这一类。
    Stale {
        age: Duration,
    },
    /// 本次采集会话尚未产生首帧。
    Empty,
}

/// 只保留最新帧的缓存；写入覆盖旧值，不形成队列。
#[derive(Debug, Default)]
pub struct Latest {
    slot: Mutex<Option<Arc<Frame>>>,
}

impl Latest {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn publish(&self, frame: Arc<Frame>) {
        *self.slot.lock().expect("最新帧缓存互斥锁被毒化") = Some(frame);
    }

    pub fn lookup(&self, max_age: Duration) -> Lookup {
        let slot = self.slot.lock().expect("最新帧缓存互斥锁被毒化");
        let Some(frame) = slot.as_ref() else {
            return Lookup::Empty;
        };
        let age = frame.received.elapsed();
        if age <= max_age {
            Lookup::Fresh(frame.clone())
        } else {
            Lookup::Stale { age }
        }
    }
}

fn unix_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |elapsed| elapsed.as_millis() as u64)
}

#[cfg(test)]
mod tests {
    use std::thread::sleep;

    use super::*;

    fn frame(sequence: u64) -> Arc<Frame> {
        Arc::new(Frame::new(
            "pc2-rgb",
            "s1",
            sequence,
            1280,
            720,
            vec![0xff, 0xd8],
        ))
    }

    #[test]
    fn empty_cache_is_not_fresh() {
        assert!(matches!(
            Latest::new().lookup(Duration::from_secs(1)),
            Lookup::Empty
        ));
    }

    #[test]
    fn newest_frame_replaces_old_one() {
        let latest = Latest::new();
        latest.publish(frame(1));
        latest.publish(frame(2));

        let Lookup::Fresh(frame) = latest.lookup(Duration::from_secs(1)) else {
            panic!("应返回最新帧");
        };
        assert_eq!(frame.metadata.sequence, 2);
    }

    #[test]
    fn frame_older_than_limit_is_stale() {
        let latest = Latest::new();
        latest.publish(frame(1));
        sleep(Duration::from_millis(20));

        let Lookup::Stale { age } = latest.lookup(Duration::from_millis(10)) else {
            panic!("超龄缓存应判为过期");
        };
        assert!(age >= Duration::from_millis(20), "{age:?}");
    }

    #[test]
    fn metadata_carries_session_sequence_and_dimensions() {
        let frame = frame(7);
        let metadata: serde_json::Value =
            serde_json::from_str(&frame.metadata_json()).expect("元数据应是 JSON");

        assert_eq!(
            metadata,
            serde_json::json!({
                "version": 1,
                "camera": "pc2-rgb",
                "session": "s1",
                "sequence": 7,
                "format": "jpeg",
                "width": 1280,
                "height": 720,
                "captured_at_unix_ms": frame.metadata.captured_at_unix_ms,
            })
        );
        assert!(
            !metadata
                .as_object()
                .unwrap()
                .contains_key("exposure_time_ns")
        );
    }
}
