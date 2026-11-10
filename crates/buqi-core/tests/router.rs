//! 网关 Router 行为：显式监听端点，两个客户端经路由器收发固定载荷。
//!
//! 测试只使用本机回环地址与测试 DDS domain，不连接实体机器人。

use std::time::{Duration, Instant};

use buqi_core::gateway::Gateway;

const KEY: &str = "buqi/acceptance/router";
const PAYLOAD: [u8; 4] = [0x00, 0x01, 0x02, 0xff];

fn write_config(port: u16) -> std::path::PathBuf {
    let path = std::env::temp_dir().join(format!("buqi-router-test-{}.toml", std::process::id()));
    std::fs::write(
        &path,
        format!(
            "listen = {{ endpoints = [\"tcp/127.0.0.1:{port}\"] }}\n\n\
             [plugins.dds]\ndomain = 199\nlocalhost_only = true\n"
        ),
    )
    .expect("写入测试配置失败");
    path
}

fn client_config(locator: &str) -> zenoh::Config {
    let mut config = zenoh::Config::default();
    config.insert_json5("mode", "\"client\"").unwrap();
    config
        .insert_json5(
            "connect/endpoints",
            &serde_json::json!([locator]).to_string(),
        )
        .unwrap();
    config
        .insert_json5("scouting/multicast/enabled", "false")
        .unwrap();
    config.insert_json5("connect/timeout_ms", "3000").unwrap();
    config
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn two_client_sessions_exchange_a_known_payload() {
    let config_path = write_config(0);
    let gateway = Gateway::start(Some(&config_path))
        .await
        .expect("启动网关失败");
    std::fs::remove_file(&config_path).ok();

    let locator = gateway
        .locators()
        .into_iter()
        .next()
        .expect("网关应报告监听端点");
    assert!(
        locator.starts_with("tcp/127.0.0.1:"),
        "意外的监听端点：{locator}"
    );

    let subscriber_session = zenoh::open(client_config(&locator))
        .await
        .expect("订阅端连接网关失败");
    let subscriber = subscriber_session
        .declare_subscriber(KEY)
        .await
        .expect("声明订阅失败");
    let publisher_session = zenoh::open(client_config(&locator))
        .await
        .expect("发布端连接网关失败");

    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        publisher_session
            .put(KEY, PAYLOAD.to_vec())
            .await
            .expect("发布失败");
        if let Ok(Ok(sample)) =
            tokio::time::timeout(Duration::from_millis(200), subscriber.recv_async()).await
        {
            assert_eq!(sample.payload().to_bytes().as_ref(), PAYLOAD);
            break;
        }
        assert!(Instant::now() < deadline, "10 秒内未通过网关收到消息");
    }
}
