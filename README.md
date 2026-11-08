# 不器

不器取自《论语·为政》，原文为“君子不器”，意思是人不应该被单一角色所限。

因此我将此作为一个通用机器人框架的命名，期待他能够兼容多个机器人框架，让大家能够更容易使用各类机器人。

## 技术栈

Zenoh 作为核心通信协议，兼容DDS、MQTT、ZMQ等边缘设备的通信协议。统一了机器人上各种通信协议，方便使用。

GStreamer 作为多媒体串流框架，保证了机器人对于多媒体视频监控的需求。避免过度使用 Zenoh，保证了核心通信能力。

## 架构

### 通信架构

<!-- Wait for agent to draw  -->

## 开发

详细请看[开发手册](./docs/DEVELOPMENT.md)。

### 开发环境

使用 nix + devenv 搭建系统级环境，cargo 与 uv 分别管理 rust 与 python 环境。

```sh
# 启动系统环境
devenv allow
# OR
direnv allow

# Rust 环境
cargo check

# Python 环境
uv sync
```

### 目录说明

- crates: rust代码，包括 server 与 core
- sdk: python / rust sdk，方便用户直接调用
- examples: 相关示例，方便调用

## LICENSE

Apache 2.0 or later
