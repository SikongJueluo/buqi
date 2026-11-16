# ── Buqi ──

check:
    cargo check --workspace

# ── Quality Gate ──

# Full quality gate — Harness / CI entry point. Non-zero exit on failure.
quality:
    cargo fmt --all -- --check
    cargo clippy --workspace --all-targets -- -D warnings
    cargo test --workspace --all-targets

# Python SDK 连接与媒体语义测试，不需要网关进程。
test-python:
    uv run python -m pytest -q

# Python 发行物检查：wheel/sdist、sdist 重建、干净安装与回环示例。
check-python-dist:
    bash scripts/check-python-dist.sh

# 本地用 act 跑 GitHub Actions 的 Python 测试矩阵；默认全部版本后再跑 dist。
ci-act version="all":
    #!/usr/bin/env bash
    # act 的 action 缓存不能安全并发 matrix，按版本逐个调用；jj 的 @ 是 detached
    # HEAD，显式给本地事件一个分支名，避免 act 反复告警。
    set -euo pipefail

    if [[ "{{version}}" == "all" ]]; then
        versions=(3.12 3.13 3.14 3.15)
    else
        versions=("{{version}}")
    fi

    ref=(--env GITHUB_REF=refs/heads/ci-act --env GITHUB_REF_NAME=ci-act --env GITHUB_REF_TYPE=branch)

    for version in "${versions[@]}"; do
        act push -j pytest "${ref[@]}" --matrix "python:${version}"
    done

    if [[ "{{version}}" == "all" ]]; then
        act push -j dist "${ref[@]}"
    fi

# 端到端验收：真实网关进程 + 本地模拟 DDS 发布端 + Python SDK 订阅。
acceptance:
    cargo build --workspace
    uv run --group dds python sdk/python/tests/acceptance.py

# ── Dev-time fix commands (modify files — NOT for CI/Harness) ──

fix:
    cargo fmt --all
    cargo clippy --workspace --all-targets --fix --allow-dirty --allow-staged

# Watch mode rebuilds (requires cargo-watch, in devenv packages)
watch:
    cargo watch -w crates -x "check --workspace"
