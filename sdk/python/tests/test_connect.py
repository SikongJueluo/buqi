"""``buqi.connect`` 的失败语义；不需要运行中的网关。"""

import time

import pytest
import zenoh

import buqi


def test_unreachable_endpoint_raises() -> None:
    started = time.monotonic()
    with pytest.raises(zenoh.ZError):
        buqi.connect("tcp/127.0.0.1:1")
    assert time.monotonic() - started < 10, "连接失败应在有限时间内返回"


def test_endpoint_without_transport_prefix_raises() -> None:
    with pytest.raises(zenoh.ZError):
        buqi.connect("127.0.0.1:7447")
