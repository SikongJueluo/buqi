# ── Buqi ──

check:
    cargo check --workspace

# ── Quality Gate ──

# Full quality gate — Harness / CI entry point. Non-zero exit on failure.
quality:
    cargo fmt --all -- --check
    cargo clippy --workspace --all-targets -- -D warnings
    cargo test --workspace --all-targets

# Python SDK 连接语义测试，不需要网关进程。
test-python:
    cd sdk/python && uv run python -m pytest -q

# 端到端验收：真实网关进程 + 本地模拟 DDS 发布端 + Python SDK 订阅。
acceptance:
    cargo build --workspace
    cd sdk/python && uv run python tests/acceptance.py

# ── Dev-time fix commands (modify files — NOT for CI/Harness) ──

fix:
    cargo fmt --all
    cargo clippy --workspace --all-targets --fix --allow-dirty --allow-staged

# Watch mode rebuilds (requires cargo-watch, in devenv packages)
watch:
    cargo watch -w crates -x "check --workspace"
