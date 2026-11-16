"""多容器模拟通信验收的共享常量、事件与报告工具。

宿主编排器（``orchestrator.py``）和容器内脚本都导入本模块。这里不导入
``cyclonedds``/``unitree_sdk2py``，因此宿主机不需要 DDS 依赖也能运行编排器。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

# 测试专用 DDS domain 与 G1 状态 topic；模拟机器人只连机器人侧网络。
DOMAIN_ID = 199
TOPIC = "rt/lowstate"
RATE_HZ = 10

# 持续订阅标准（秒）。首个有效消息期限从订阅进程启动算起。
FIRST_MESSAGE_DEADLINE = 20.0
MEASURE_SECONDS = 60.0
MAX_GAP = 2.0
MIN_COVERAGE = 0.95

# 生命周期场景的期限与静默判断。
RECOVERY_DEADLINE = 30.0
SILENCE_SECONDS = 3.0

# RTPS SerializedPayload 的 4 字节表示头（little-endian CDR）。
CDR_LE_HEADER = bytes([0x00, 0x01, 0x00, 0x00])


def run_dir() -> Path:
    """本次运行的共享目录；由编排器以 ``BQ_RUN_DIR`` 传入。"""
    return Path(os.environ["BQ_RUN_DIR"])


def append(name: str, record: dict) -> None:
    """追加一行 JSON；读取端会忽略不完整的尾行。"""
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with (run_dir() / name).open("a", encoding="utf-8") as handle:
        handle.write(line)


def emit(event: str, **fields) -> None:
    """记录里程碑事件，同时打印到容器日志，便于失败时定位。"""
    record = {"event": event, "wall": time.time(), "mono": time.monotonic(), **fields}
    append("events.ndjson", record)
    print(json.dumps(record, ensure_ascii=False), flush=True)


def write_json(name: str, record: dict) -> None:
    """原子写结果文件，编排器可轮询其出现。"""
    target = run_dir() / name
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)


def read_json(name: str) -> dict:
    return json.loads((run_dir() / name).read_text(encoding="utf-8"))


def read_ndjson_at(path: Path) -> list[dict]:
    """读取 ndjson；进程被强杀时可能留下半行，丢弃不完整的尾行。"""
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def read_ndjson(name: str) -> list[dict]:
    return read_ndjson_at(run_dir() / name)
