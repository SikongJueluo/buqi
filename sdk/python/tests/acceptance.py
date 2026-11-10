#!/usr/bin/env python3
"""Buqi 首版端到端验收（本机、隔离）。

启动真实网关进程（Zenoh Router + DDS 插件），依次验证：

1. 网关输出可直接连接的 ``tcp/...`` 端点；
2. 两个 Zenoh 客户端经 Router 转发固定载荷；
3. 本地模拟 DDS 发布端的消息经插件桥接到 Zenoh，载荷完整；
4. 不可达或缺少传输前缀的端点快速失败；
5. Ctrl+C 以退出码 0 停止网关。

DDS 只使用测试 domain 199，并把两端限制在 127.0.0.1，不连接实体机器人。
用法（仓库根目录）::

    cargo build --workspace
    cd sdk/python && uv run python tests/acceptance.py

或直接运行 ``just acceptance``。
"""

from __future__ import annotations

import os
import queue
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

import zenoh

import buqi
from dds_state import EXPECTED_PAYLOAD, TOPIC

REPO_ROOT = Path(__file__).resolve().parents[3]
GATEWAY = REPO_ROOT / "target" / "debug" / "buqi"
PUBLISHER = Path(__file__).with_name("dds_publisher.py")

LOOPBACK_CYCLONEDDS_URI = (
    "<CycloneDDS><Domain><General><Interfaces>"
    '<NetworkInterface address="127.0.0.1"/>'
    "</Interfaces></General></Domain></CycloneDDS>"
)

ROUTER_KEY = "buqi/acceptance/router"
ROUTER_PAYLOAD = bytes([0x00, 0x01, 0x02, 0xFF])


class LinePump:
    """持续读取子进程输出，并支持带超时等待某一行。"""

    def __init__(self, stream) -> None:
        self.lines: list[str] = []
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread = threading.Thread(target=self._pump, args=(stream,), daemon=True)
        self._thread.start()

    def _pump(self, stream) -> None:
        for line in stream:
            line = line.rstrip("\n")
            self.lines.append(line)
            self._queue.put(line)
        self._queue.put(None)

    def _failure(self, description: str) -> AssertionError:
        return AssertionError(
            f"等待{description}失败；已输出：\n" + "\n".join(self.lines)
        )

    def wait_for(
        self, predicate: Callable[[str], bool], timeout: float, description: str
    ) -> str:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._failure(description)
            try:
                line = self._queue.get(timeout=remaining)
            except queue.Empty:
                raise self._failure(description) from None
            if line is None:
                raise self._failure(description)
            if predicate(line):
                return line


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def router_roundtrip(locator: str) -> None:
    with buqi.connect(locator) as subscriber_session:
        with subscriber_session.declare_subscriber(ROUTER_KEY) as subscriber:
            with buqi.connect(locator) as publisher_session:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    publisher_session.put(ROUTER_KEY, ROUTER_PAYLOAD)
                    sample = subscriber.try_recv()
                    if sample is not None:
                        if str(sample.key_expr) != ROUTER_KEY:
                            raise AssertionError(f"意外的 key expression：{sample.key_expr}")
                        if sample.payload.to_bytes() != ROUTER_PAYLOAD:
                            raise AssertionError(
                                f"载荷不符：{sample.payload.to_bytes().hex()}"
                            )
                        return
                    time.sleep(0.1)
    raise AssertionError("10 秒内未通过 Router 收到消息")


def dds_roundtrip(locator: str) -> None:
    publisher = subprocess.Popen(
        [sys.executable, str(PUBLISHER)],
        cwd=PUBLISHER.parent,
        env=dict(os.environ, CYCLONEDDS_URI=LOOPBACK_CYCLONEDDS_URI),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        with buqi.connect(locator) as session:
            with session.declare_subscriber(TOPIC) as subscriber:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    sample = subscriber.try_recv()
                    if sample is not None:
                        payload = sample.payload.to_bytes()
                        if payload != EXPECTED_PAYLOAD:
                            raise AssertionError(
                                "DDS 桥接载荷不符："
                                f"期望 {EXPECTED_PAYLOAD.hex()}，实际 {payload.hex()}"
                            )
                        return
                    time.sleep(0.1)
        raise AssertionError("20 秒内未收到 DDS 桥接消息")
    finally:
        publisher.terminate()
        try:
            publisher.wait(timeout=10)
        except subprocess.TimeoutExpired:
            publisher.kill()
            publisher.wait(timeout=5)


def unreachable_endpoint_fails() -> None:
    for endpoint in ("tcp/127.0.0.1:1", "127.0.0.1:1"):
        started = time.monotonic()
        try:
            buqi.connect(endpoint)
        except zenoh.ZError:
            pass
        else:
            raise AssertionError(f"连接 {endpoint} 应失败，而不是返回 Session")
        elapsed = time.monotonic() - started
        if elapsed > 10:
            raise AssertionError(f"连接 {endpoint} 失败用时 {elapsed:.1f} 秒")


def main() -> int:
    if not GATEWAY.exists():
        print(
            f"缺少网关二进制 {GATEWAY}，请先在仓库根目录运行 cargo build --workspace",
            file=sys.stderr,
        )
        return 2

    port = unused_port()
    with tempfile.TemporaryDirectory(prefix="buqi-acceptance-") as directory:
        config = Path(directory) / "buqi.toml"
        config.write_text(
            f'listen = {{ endpoints = ["tcp/127.0.0.1:{port}"] }}\n\n'
            "[plugins.dds]\n"
            "domain = 199\n"
            "localhost_only = true\n",
            encoding="utf-8",
        )

        process = subprocess.Popen(
            [str(GATEWAY), "server", "--config", str(config)],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        output = LinePump(process.stdout)

        try:
            locator = output.wait_for(
                lambda line: line.strip().startswith("tcp/"), 20, "网关输出监听端点"
            ).strip()
            if locator != f"tcp/127.0.0.1:{port}":
                raise AssertionError(f"监听端点不符合预期：{locator}")
            print(f"PASS 网关在前台启动并报告端点 {locator}")

            dds_line = output.wait_for(
                lambda line: "DDS 插件已启动" in line or "警告：DDS 插件不可用" in line,
                15,
                "DDS 启动观察结果",
            )
            if "警告" in dds_line:
                raise AssertionError(dds_line)
            print("PASS DDS 主任务通过初步启动检查")

            router_roundtrip(locator)
            print(f"PASS 两个客户端经 Router 转发固定载荷 {ROUTER_PAYLOAD.hex()}")

            dds_roundtrip(locator)
            print(f"PASS DDS → Zenoh 桥接完整载荷 {EXPECTED_PAYLOAD.hex()}")

            unreachable_endpoint_fails()
            print("PASS 不可达/无传输前缀的端点快速失败")

            process.send_signal(signal.SIGINT)
            if process.wait(timeout=15) != 0:
                raise AssertionError(f"网关退出码为 {process.returncode}")
            output.wait_for(
                lambda line: "网关已停止。" in line, 5, "关停输出"
            )
            if any("警告：DDS 插件不可用" in line for line in output.lines):
                raise AssertionError("DDS 插件在验收期间失效")
            print("PASS Ctrl+C 以退出码 0 停止网关")
        except AssertionError:
            print("\n网关输出：", file=sys.stderr)
            print("\n".join(output.lines), file=sys.stderr)
            raise
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    print("验收通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
