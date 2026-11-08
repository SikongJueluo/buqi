//! buqi —— Rust SDK。
//!
//! 统一机器人接口：通过 Zenoh 访问网关（buqi-server，Router + DDS 插件）
//! 暴露的机器人能力，内部以可插拔 Adapter 对接不同机器人。接口设计先由
//! Python SDK（sdk/python）验证，稳定后在此对齐。
