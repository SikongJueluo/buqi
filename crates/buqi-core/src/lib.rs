//! buqi-core —— 网关与各模块共享的核心库。
//!
//! 首版提供前台网关的启动逻辑：TOML 配置解析、Linux IPv4 监听地址选择、
//! Zenoh Router 启动，以及对 `zenoh-plugin-dds` 主任务的监督。
//! 媒体处理（GStreamer）仍是后续工作。

mod config;
pub mod gateway;
mod net;
