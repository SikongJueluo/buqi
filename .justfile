# ── Buqi ──

check:
    cargo check --workspace

# ── Quality Gate ──

# Full quality gate — Harness / CI entry point. Non-zero exit on failure.
quality:
    cargo fmt --all -- --check
    cargo clippy --workspace --all-targets -- -D warnings
    cargo test --workspace --all-targets

# ── Dev-time fix commands (modify files — NOT for CI/Harness) ──

fix:
    cargo fmt --all
    cargo clippy --workspace --all-targets --fix --allow-dirty --allow-staged

# Watch mode rebuilds (requires cargo-watch, in devenv packages)
watch:
    cargo watch -w crates -x "check --workspace"
