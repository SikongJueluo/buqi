//! 监听失败必须作为启动错误返回，不能静默改用其他端口。

use buqi_core::gateway::Gateway;

fn free_port() -> u16 {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("分配临时端口失败");
    listener.local_addr().expect("读取临时端口失败").port()
}

fn write_config(port: u16) -> std::path::PathBuf {
    let path = std::env::temp_dir().join(format!("buqi-conflict-test-{}.toml", std::process::id()));
    std::fs::write(
        &path,
        format!(
            "listen = {{ endpoints = [\"tcp/127.0.0.1:{port}\"], \
             exit_on_failure = false, timeout_ms = 500 }}\n\n\
             [plugins.dds]\ndomain = 199\nlocalhost_only = true\n"
        ),
    )
    .expect("写入测试配置失败");
    path
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn occupied_port_is_a_startup_error_despite_retry_settings() {
    let port = free_port();
    let config_path = write_config(port);
    let _first = Gateway::start(Some(&config_path))
        .await
        .expect("首次启动网关失败");

    let error = Gateway::start(Some(&config_path))
        .await
        .err()
        .expect("重复监听同一端口应失败");
    std::fs::remove_file(&config_path).ok();
    let message = format!("{error:#}");
    assert!(message.contains("监听"), "{message}");
}
