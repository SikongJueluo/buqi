//! Zenoh 图片通道：持续观测发布与单帧查询回复。
//!
//! 发布只读取最新帧缓存；查询也不等待采集节奏。两者都不反压采集线程。
//! 发布使用 `CongestionControl::Drop`，跳过过期帧；订阅端仍需自行保留最新帧。

use std::sync::Arc;
use std::time::Duration;

use anyhow::Result;
use tokio::task::JoinHandle;
use zenoh::Session;
use zenoh::bytes::{Encoding, ZBytes};
use zenoh::qos::CongestionControl;

use super::config::MediaConfig;
use super::frame::{Frame, Latest, Lookup};
use super::protocol::{self, ErrorReply};

pub(super) async fn start(
    session: Arc<Session>,
    config: &MediaConfig,
    latest: Arc<Latest>,
) -> Result<Vec<JoinHandle<()>>> {
    let camera = config.camera.id.clone();
    let max_age = config.max_age();
    let snapshot = protocol::snapshot_key(&camera);

    let queryable = session
        .declare_queryable(&snapshot)
        .await
        .map_err(|error| anyhow::anyhow!("声明单帧查询入口 {snapshot} 失败：{error}"))?;

    let publisher = tokio::spawn(publish_loop(
        session.clone(),
        protocol::frame_key(&camera),
        config.observation.rate_hz,
        max_age,
        latest.clone(),
        camera.clone(),
    ));
    let query_task = tokio::spawn(query_loop(queryable, snapshot, camera, max_age, latest));
    Ok(vec![publisher, query_task])
}

async fn publish_loop(
    session: Arc<Session>,
    key: String,
    rate_hz: f64,
    max_age: Duration,
    latest: Arc<Latest>,
    camera: String,
) {
    let period = Duration::from_secs_f64(1.0 / rate_hz);
    let mut ticker = tokio::time::interval(period);
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    // 同一帧只发布一次：冻结画面不能靠重复消息刷新订阅端的新鲜度。
    let mut last: Option<(String, u64)> = None;

    loop {
        ticker.tick().await;
        let Lookup::Fresh(frame) = latest.lookup(max_age) else {
            continue;
        };
        let unchanged = last.as_ref().is_some_and(|(session, sequence)| {
            session == &frame.metadata.session && *sequence == frame.metadata.sequence
        });
        if unchanged {
            continue;
        }
        let result = session
            .put(key.clone(), ZBytes::from(frame.jpeg.clone()))
            .encoding(Encoding::IMAGE_JPEG)
            .attachment(ZBytes::from(frame.metadata_json()))
            .congestion_control(CongestionControl::Drop)
            .await;
        match result {
            Ok(()) => last = Some((frame.metadata.session.clone(), frame.metadata.sequence)),
            Err(error) => {
                if session.is_closed() {
                    break;
                }
                eprintln!("警告：发布相机 {camera} 的最新帧失败：{error}");
            }
        }
    }
}

async fn query_loop(
    queryable: zenoh::query::Queryable<zenoh::handlers::FifoChannelHandler<zenoh::query::Query>>,
    key: String,
    camera: String,
    max_age: Duration,
    latest: Arc<Latest>,
) {
    while let Ok(query) = queryable.recv_async().await {
        let result = match latest.lookup(max_age) {
            Lookup::Fresh(frame) => reply_frame(&query, &key, &frame).await,
            Lookup::Stale { age } => {
                reply_error(
                    &query,
                    &key,
                    &camera,
                    max_age,
                    protocol::STALE,
                    Some(age.as_millis() as u64),
                )
                .await
            }
            Lookup::Empty => {
                reply_error(&query, &key, &camera, max_age, protocol::NO_FRAME, None).await
            }
        };
        // 查询可能已被客户端取消；回复失败不影响后续请求。
        let _ = result;
    }
}

async fn reply_frame(query: &zenoh::query::Query, key: &str, frame: &Frame) -> zenoh::Result<()> {
    query
        .reply(key, ZBytes::from(frame.jpeg.clone()))
        .encoding(Encoding::IMAGE_JPEG)
        .attachment(ZBytes::from(frame.metadata_json()))
        .await
}

async fn reply_error(
    query: &zenoh::query::Query,
    key: &str,
    camera: &str,
    max_age: Duration,
    error: &'static str,
    age_ms: Option<u64>,
) -> zenoh::Result<()> {
    let payload = ErrorReply {
        version: protocol::PROTOCOL_VERSION,
        camera,
        error,
        max_age_ms: max_age.as_millis() as u64,
        age_ms,
    };
    let payload = serde_json::to_vec(&payload).expect("单帧错误载荷序列化不会失败");
    query
        .reply(key, ZBytes::from(payload))
        .encoding(Encoding::APPLICATION_JSON)
        .await
}
