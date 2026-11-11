//! buqi-core —— 网关与各模块共享的核心库。
//!
//! 提供前台网关的启动逻辑：TOML 配置解析、Linux IPv4 监听地址选择、
//! Zenoh Router 启动、`zenoh-plugin-dds` 主任务监督，以及可选的相机媒体模块
//! （GStreamer 采集 MJPEG，Zenoh 发布原始 JPEG 与单帧查询）。

mod config;
pub mod gateway;
pub mod media;
mod net;
