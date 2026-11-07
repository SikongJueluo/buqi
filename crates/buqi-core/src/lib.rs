//! buqi-core —— 各模块共享的核心库。
//!
//! 目前引入 zenoh 与 zenoh-plugin-dds（DDS 桥接，静态嵌入模式）、gstreamer
//!（媒体管线），后续在此封装桥接配置、会话管理与媒体处理。
