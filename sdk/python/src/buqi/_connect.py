"""Buqi SDK 的最小连接入口。"""

from __future__ import annotations

import json

import zenoh

# TCP 建链上限；失败时抛错，不在后台继续尝试。
_CONNECT_TIMEOUT_MS = 3000


def connect(endpoint: str) -> zenoh.Session:
    """连接 Buqi 网关，返回官方 :class:`zenoh.Session`。

    ``endpoint`` 必须是完整的 Zenoh 端点，例如 ``tcp/192.168.1.20:7447``，
    与网关启动时打印的形式一致。SDK 不会补全传输前缀，也不会通过
    scouting 自动发现其他 Router。

    连接失败或超过等待上限时抛出 :class:`zenoh.ZError`。
    """
    config = zenoh.Config()
    config.insert_json5("mode", '"client"')
    config.insert_json5("connect/endpoints", json.dumps([endpoint]))
    config.insert_json5("scouting/multicast/enabled", "false")
    config.insert_json5("scouting/gossip/enabled", "false")
    config.insert_json5("connect/timeout_ms", str(_CONNECT_TIMEOUT_MS))
    return zenoh.open(config)
