#!/usr/bin/env bash
# 构建并验证 Python 发行物：wheel、sdist、从 sdist 重建 wheel、干净安装与回环示例。
# 只使用官方 PyPI 与临时目录，不借用仓库 .venv、开发组或源码路径。
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root"

python=${PYTHON:-3.13}
out=target/python-dist
index=https://pypi.org/simple

rm -rf "$out"
mkdir -p "$out"

uv build --default-index "$index" --out-dir "$out"
wheel=$(find "$out" -maxdepth 1 -name 'buqi-*.whl' -print -quit)
sdist=$(find "$out" -maxdepth 1 -name 'buqi-*.tar.gz' -print -quit)
if [[ -z "$wheel" || -z "$sdist" ]]; then
    echo "缺少 wheel 或 sdist：$out" >&2
    exit 1
fi
echo "PASS 构建 $(basename "$wheel") 与 $(basename "$sdist")"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# 解包 sdist 并在脱离仓库的目录里重建 wheel。
tar -xzf "$sdist" -C "$work"
srcdir=$(find "$work" -mindepth 1 -maxdepth 1 -type d -name 'buqi-*' -print -quit)
if [[ -z "$srcdir" ]]; then
    echo "sdist 解包后缺少项目目录" >&2
    exit 1
fi
uv build --default-index "$index" --wheel --out-dir "$work/rebuilt" "$srcdir"
rebuilt=$(find "$work/rebuilt" -maxdepth 1 -name 'buqi-*.whl' -print -quit)
if [[ -z "$rebuilt" ]]; then
    echo "未能从 sdist 重建 wheel" >&2
    exit 1
fi
echo "PASS 从 sdist 重建 $(basename "$rebuilt")"

# 干净环境安装标准 wheel；不安装开发组，也不引用仓库源码。
uv venv --python "$python" "$work/venv"
py="$work/venv/bin/python"
uv pip install --python "$py" --default-index "$index" "$wheel"

"$py" - "$wheel" "$sdist" "$rebuilt" <<'PY'
import pathlib
import sys
import tarfile
import zipfile

import buqi

wheel, sdist, rebuilt = map(pathlib.Path, sys.argv[1:])
version = buqi.__version__

assert callable(buqi.connect)
assert callable(buqi.snapshot)
assert callable(buqi.frames)
assert pathlib.Path(buqi.__file__).with_name("py.typed").is_file(), "安装后缺少 py.typed"

with zipfile.ZipFile(wheel) as archive:
    names = set(archive.namelist())
assert "buqi/__init__.py" in names
assert "buqi/py.typed" in names
assert not any(name.startswith("crates/") for name in names)
assert any(
    name.startswith("buqi-") and name.endswith(".dist-info/licenses/LICENSE")
    for name in names
), f"wheel 缺少许可证：{sorted(names)}"

with zipfile.ZipFile(rebuilt) as archive:
    rebuilt_names = set(archive.namelist())
assert "buqi/py.typed" in rebuilt_names
assert "buqi/__init__.py" in rebuilt_names

with tarfile.open(sdist) as archive:
    sdist_names = set(archive.getnames())
prefix = f"buqi-{version}/"
required = [
    "pyproject.toml",
    "LICENSE",
    "sdk/python/README.md",
    "sdk/python/src/buqi/__init__.py",
    "sdk/python/src/buqi/py.typed",
    "sdk/python/tests/test_connect.py",
    "sdk/python/tests/test_media.py",
    "examples/loopback.py",
]
missing = [name for name in required if prefix + name not in sdist_names]
assert not missing, f"sdist 缺少：{missing}"
assert not any("/crates/" in name for name in sdist_names)
assert not any("/.cache/" in name or "/.venv/" in name for name in sdist_names)
print(f"PASS wheel/sdist 内容与版本 {version}")
PY

# 复制示例到临时目录，确保只依赖已安装的 site-packages。
cp examples/loopback.py "$work/loopback.py"
(
    cd "$work"
    timeout 60 "$py" -I loopback.py
)
echo "PASS 干净安装后导入并完成回环示例"
