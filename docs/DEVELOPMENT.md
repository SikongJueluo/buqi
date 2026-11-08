# 开发手册

## 环境准备

系统级依赖（Rust 工具链、cmake/libclang、GStreamer、uv 等）由 devenv 提供：

```sh
direnv allow        # 或直接 devenv shell
```

- Rust：`cargo check`（首次会从源码编译 CycloneDDS，耗时较长）
- Python：`cd sdk/python && uv sync`

## 目录结构

| 目录 | 说明 |
| --- | --- |
| `crates/buqi-core` | Rust 公共核心（zenoh、zenoh-plugin-dds、gstreamer） |
| `crates/buqi-server` | 网关二进制：Zenoh Router + DDS 插件，部署在 PC2 |
| `crates/buqi-cli` | 管理 CLI（占位） |
| `sdk/python` | Python SDK（uv 管理，封装官方 eclipse-zenoh） |
| `sdk/rust` | Rust SDK（占位，接口随 Python SDK 稳定后对齐） |
| `examples` | 使用示例 |

## 架构速览

- **PC1**（机器人本体，如 Unitree G1D）：保持原样，走原生 DDS。
- **PC2**（网关机）：运行 `buqi-server`（Zenoh Router + `zenoh-plugin-dds`），
  将 DDS Topic 映射到 Zenoh Key，纯通信网关，不含机器人业务逻辑。
- **客户端**：通过 `buqi` SDK 连接 Zenoh。两层接口——原生兼容（厂商 SDK
  经 DDS 桥接直用）与统一接口（`Robot` 抽象 + 可插拔 Adapter）。

多媒体（视频监控等）走 GStreamer，不经 Zenoh，保持核心通信能力。

## 常用命令

```sh
just check     # cargo check --workspace
just quality   # fmt --check + clippy -D warnings + test
just watch     # cargo watch 增量检查
```
