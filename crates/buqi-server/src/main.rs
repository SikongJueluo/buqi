//! `buqi-server` —— 网关程序入口。
//!
//! 前台启动 Zenoh Router 与 DDS 插件，直到 Ctrl+C；参数与行为与
//! `buqi server` 相同，供只需要网关二进制的部署使用。

use std::path::PathBuf;
use std::process::ExitCode;

use clap::Parser;

/// 不器网关（Zenoh Router + DDS 插件）。
#[derive(Debug, Parser)]
#[command(
    name = "buqi-server",
    version,
    about = "不器网关：Zenoh Router + DDS 桥接"
)]
struct Args {
    /// TOML 配置路径；省略时自动选择本机私有 IPv4 与回环地址
    #[arg(short, long, value_name = "PATH")]
    config: Option<PathBuf>,
}

#[tokio::main]
async fn main() -> ExitCode {
    let Args { config } = Args::parse();
    match buqi_core::gateway::run(config.as_deref()).await {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("错误：{error:#}");
            ExitCode::FAILURE
        }
    }
}
