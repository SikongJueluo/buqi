"""Zenoh 回环示例：本机路由 + 发布/订阅校验，不依赖网关或硬件。

在进程内启动一个临时 Zenoh Router，用两次 ``buqi.connect`` 建立标准客户端
连接，然后发布固定载荷并校验订阅端收到的 key 与内容。用于安装发行包后的
冒烟检查，不需要 Rust 网关、DDS、相机或机器人。

运行（仓库根目录）::

    uv run python examples/loopback.py

收到并校验消息后以 0 退出；10 秒内未完成则以非零码失败。
"""

from __future__ import annotations

import json
import socket
import time

import zenoh

import buqi

KEY_EXPR = "buqi/example/loopback"
PAYLOAD = b"buqi-loopback"
TIMEOUT_SECONDS = 10.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> int:
    endpoint = f"tcp/127.0.0.1:{_free_port()}"
    router_config = zenoh.Config()
    router_config.insert_json5("mode", '"router"')
    router_config.insert_json5("listen/endpoints", json.dumps([endpoint]))
    router_config.insert_json5("scouting/multicast/enabled", "false")
    router_config.insert_json5("scouting/gossip/enabled", "false")

    with zenoh.open(router_config):
        with buqi.connect(endpoint) as subscriber_session:
            with subscriber_session.declare_subscriber(KEY_EXPR) as subscriber:
                with buqi.connect(endpoint) as publisher_session:
                    deadline = time.monotonic() + TIMEOUT_SECONDS
                    while time.monotonic() < deadline:
                        publisher_session.put(KEY_EXPR, PAYLOAD)
                        sample = subscriber.try_recv()
                        if sample is None:
                            time.sleep(0.1)
                            continue
                        if str(sample.key_expr) != KEY_EXPR:
                            raise SystemExit(f"key 不符：{sample.key_expr}")
                        received = sample.payload.to_bytes()
                        if received != PAYLOAD:
                            raise SystemExit(f"载荷不符：{received.hex()}")
                        print(f"回环成功：{sample.key_expr} -> {received.decode()}")
                        return 0

    raise SystemExit(f"{TIMEOUT_SECONDS:.0f} 秒内未收到回环消息")


if __name__ == "__main__":
    raise SystemExit(main())
