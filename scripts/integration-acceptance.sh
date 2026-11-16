#!/usr/bin/env bash
# 构建镜像并运行多容器模拟通信验收。
#
# 依赖：Linux x86_64、Nix（构建网关镜像）、容器运行时（默认 Podman，可用
# CONTAINER_RUNTIME=docker 指定）、uv。镜像与构建缓存保留；容器与网络按每次运行的
# 唯一标识创建，并由编排器在成功、失败或中断后清理。报告位置由编排器打印。
set -euo pipefail

REPO_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$REPO_ROOT"

runtime=${CONTAINER_RUNTIME:-podman}
runtime_name=${runtime##*/}
for tool in "$runtime" nix uv; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "缺少 $tool；多容器验收不能运行，也不会被跳过。" >&2
    exit 2
  fi
done

# devenv 为本地 act 固定设置 Podman 的 DOCKER_HOST；Docker 路径必须回到 Docker 自己的
# daemon，否则 Docker CLI 会去连不存在的 Podman socket。
if [[ $runtime_name == docker* && ${DOCKER_HOST:-} == *podman* ]]; then
  unset DOCKER_HOST
fi

# 先确认选中的运行时真的能连上 daemon，而不是等到第一个容器命令才失败。
if ! "$runtime" info >/dev/null 2>&1; then
  echo "容器运行时 $runtime 不可用（daemon 未运行或 DOCKER_HOST 指向错误）；验收不能运行。" >&2
  exit 2
fi

image_present() {
  # Docker 会把本地镜像显示成 localhost/<name>；两种写法都查一次。
  "$runtime" image inspect "$1" >/dev/null 2>&1 ||
    "$runtime" image inspect "localhost/$1" >/dev/null 2>&1
}

# 解析成本地实际存在的镜像名；Docker 对带 localhost/ 前缀的镜像不会回退到远端拉取。
resolve_image() {
  if "$runtime" image inspect "$1" >/dev/null 2>&1; then
    printf '%s' "$1"
  elif "$runtime" image inspect "localhost/$1" >/dev/null 2>&1; then
    printf 'localhost/%s' "$1"
  else
    echo "镜像 $1 在 $runtime 中不可用。" >&2
    return 1
  fi
}

artifact_dir=target/acceptance
context_dir=$artifact_dir/build-context
rm -rf -- "$context_dir"
mkdir -p -- "$context_dir"
uv build --wheel --out-dir "$context_dir"
wheel=$(find "$context_dir" -maxdepth 1 -name 'buqi-*.whl' -print -quit)
if [[ -z $wheel ]]; then
  echo "未构建出 buqi wheel。" >&2
  exit 2
fi

# 宿主代理指向回环地址时容器内不可达；只在这种情况关闭构建期的代理注入，
# 不改动用户的代理或网络配置。Podman 用 --http-proxy，Docker 用空 build-arg。
proxy_args=()
case "${http_proxy:-}${HTTP_PROXY:-}" in
  *127.0.0.1*)
    if [[ $runtime_name == docker* ]]; then
      proxy_args+=(--build-arg HTTP_PROXY= --build-arg HTTPS_PROXY=)
      proxy_args+=(--build-arg http_proxy= --build-arg https_proxy=)
    else
      proxy_args+=(--http-proxy=false)
    fi
    ;;
esac

python_tag=$(sha256sum containers/python/Containerfile "$wheel" | sha256sum | cut -c1-12)
python_image="buqi-python-test:${python_tag}"
if ! image_present "$python_image"; then
  echo "构建 Python 测试镜像 $python_image"
  "$runtime" build "${proxy_args[@]}" \
    -f containers/python/Containerfile -t "$python_image" "$context_dir"
fi
python_image=$(resolve_image "$python_image")

echo "容器内自检 $python_image"
"$runtime" run --rm \
  --volume "$PWD/sdk/python/tests/integration:/app:ro" \
  "$python_image" python /app/self_check.py

cargo build --workspace

gateway_tag=$(sha256sum target/debug/buqi containers/gateway/image.nix devenv.lock | sha256sum | cut -c1-12)
gateway_image="buqi-gateway:${gateway_tag}"
if ! image_present "$gateway_image"; then
  echo "构建网关镜像 $gateway_image"
  nix build --impure --file containers/gateway/image.nix --out-link "$artifact_dir/gateway-image"
  "$runtime" load -i "$artifact_dir/gateway-image" >/dev/null
  "$runtime" tag buqi-gateway:local "$gateway_image"
fi
gateway_image=$(resolve_image "$gateway_image")

exec uv run --quiet python sdk/python/tests/integration/orchestrator.py \
  --runtime "$runtime" \
  --gateway-image "$gateway_image" \
  --python-image "$python_image"
