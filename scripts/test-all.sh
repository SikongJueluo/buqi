#!/usr/bin/env bash
# 统一验收入口（``just test``）。
#
# 依次运行 Rust 构建与测试、Python 测试、发行物检查、本地回环验收与多容器验收。
# 独立检查即使前面的失败也继续执行；依赖未通过的检查标为 NOT RUN 并说明依赖。
# 任一 FAIL、NOT RUN 或缺少运行条件，最终返回非零。
set -uo pipefail

REPO_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$REPO_ROOT"

declare -A status
declare -A dependency=(
  [rust-tests]=rust-build
  [acceptance]=rust-build
  [integration]=rust-build
)
declare -A command=(
  [rust-build]=rust_build
  [rust-tests]=rust_tests
  [python-tests]=python_tests
  [python-dist]=python_dist
  [acceptance]=acceptance
  [integration]=integration
)
steps=(rust-build rust-tests python-tests python-dist acceptance integration)

rust_build() {
  cargo build --workspace
}

rust_tests() {
  cargo test --workspace --all-targets
}

python_tests() {
  uv run python -m pytest -q
}

python_dist() {
  bash scripts/check-python-dist.sh
}

acceptance() {
  uv run --group dds python sdk/python/tests/acceptance.py
}

integration() {
  bash scripts/integration-acceptance.sh
}

for step in "${steps[@]}"; do
  dependency_name=${dependency[$step]:-}
  if [[ -n $dependency_name && ${status[$dependency_name]:-} != PASS ]]; then
    status[$step]="NOT RUN"
    printf 'NOT RUN %-14s 依赖 %s 未通过\n' "$step" "$dependency_name"
    continue
  fi
  printf '\n=== %s\n' "$step"
  if "${command[$step]}"; then
    status[$step]=PASS
  else
    status[$step]=FAIL
  fi
done

printf '\n—— 汇总\n'
failed=0
for step in "${steps[@]}"; do
  printf '%-14s %s\n' "$step" "${status[$step]}"
  if [[ ${status[$step]} != PASS ]]; then
    failed=1
  fi
done

if ((failed)); then
  echo "存在失败或未运行的检查。" >&2
  exit 1
fi
echo "全部检查通过。"
