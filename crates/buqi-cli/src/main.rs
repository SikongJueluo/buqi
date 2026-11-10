//! `buqi` —— 维护者的网关命令行入口。
//!
//! 首版只有一个子命令：前台启动网关，直到 Ctrl+C。

use std::path::PathBuf;
use std::process::ExitCode;

use clap::{Parser, Subcommand};

/// 不器机器人的通信网关。
#[derive(Debug, Parser)]
#[command(name = "buqi", version, about = "不器网关：Zenoh Router + DDS 桥接")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// 前台启动网关（Zenoh Router + DDS 插件）
    Server {
        /// TOML 配置路径；省略时自动选择本机私有 IPv4 与回环地址
        #[arg(short, long, value_name = "PATH")]
        config: Option<PathBuf>,
    },
}

#[tokio::main]
async fn main() -> ExitCode {
    let Cli { command } = Cli::parse();
    let result = match command {
        Command::Server { config } => buqi_core::gateway::run(config.as_deref()).await,
    };
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("错误：{error:#}");
            ExitCode::FAILURE
        }
    }
}
