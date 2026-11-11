"""相机图片通道的协议语义测试。

这些测试用同一个独立的 Zenoh peer Session 声明查询/发布端，不需要网关进程，
也不连接实体相机。它们验证 SDK 的载荷解析、错误映射与“只保留最新帧”的行为。
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest
import zenoh

import buqi

CAMERA = "pc2-rgb"
SNAPSHOT_KEY = f"buqi/media/v1/{CAMERA}/snapshot"
FRAME_KEY = f"buqi/media/v1/{CAMERA}/frame"
JPEG = b"\xff\xd8payload\xff\xd9"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def routed_sessions():
    """网关的 router 会话与一个把查询默认期限设得很短的 client。"""
    endpoint = f"tcp/127.0.0.1:{_free_port()}"
    router_config = zenoh.Config()
    router_config.insert_json5("mode", '"router"')
    router_config.insert_json5("listen/endpoints", json.dumps([endpoint]))
    router_config.insert_json5("scouting/multicast/enabled", "false")
    router_config.insert_json5("scouting/gossip/enabled", "false")
    router = zenoh.open(router_config)

    client_config = zenoh.Config()
    client_config.insert_json5("mode", '"client"')
    client_config.insert_json5("connect/endpoints", json.dumps([endpoint]))
    client_config.insert_json5("connect/timeout_ms", "3000")
    client_config.insert_json5("scouting/multicast/enabled", "false")
    client_config.insert_json5("scouting/gossip/enabled", "false")
    client_config.insert_json5("queries_default_timeout", "50")
    client = zenoh.open(client_config)
    try:
        deadline = time.monotonic() + 3.0
        while not client.info.routers_zid() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert client.info.routers_zid(), "client 未连接 router"
        yield router, client
    finally:
        client.close()
        router.close()


@pytest.fixture
def session():
    config = zenoh.Config()
    config.insert_json5("mode", '"peer"')
    config.insert_json5("listen/endpoints", "[]")
    config.insert_json5("scouting/multicast/enabled", "false")
    config.insert_json5("scouting/gossip/enabled", "false")
    session = zenoh.open(config)
    try:
        yield session
    finally:
        session.close()


def metadata(sequence: int = 7, camera: str = CAMERA) -> dict:
    return {
        "version": 1,
        "camera": camera,
        "session": "session-1",
        "sequence": sequence,
        "format": "jpeg",
        "width": 1280,
        "height": 720,
        "captured_at_unix_ms": 1_700_000_000_000,
    }


def test_snapshot_returns_jpeg_and_metadata(session):
    def reply(query):
        query.reply(
            SNAPSHOT_KEY,
            JPEG,
            encoding="image/jpeg",
            attachment=json.dumps(metadata()),
        )

    queryable = session.declare_queryable(SNAPSHOT_KEY, reply)
    try:
        frame = buqi.snapshot(session, CAMERA, timeout=1.0)
    finally:
        queryable.undeclare()

    assert frame.jpeg == JPEG
    assert frame.metadata.camera == CAMERA
    assert frame.metadata.session == "session-1"
    assert frame.metadata.sequence == 7
    assert frame.metadata.width == 1280
    assert frame.metadata.height == 720
    assert frame.received_at <= time.monotonic()


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("no_frame", buqi.CameraNoFrameError),
        ("stale", buqi.CameraStaleFrameError),
    ],
)
def test_snapshot_maps_server_errors(session, code, expected):
    payload = {"version": 1, "camera": CAMERA, "error": code, "max_age_ms": 500}
    if code == "stale":
        payload["age_ms"] = 812

    def reply(query):
        query.reply(
            SNAPSHOT_KEY,
            json.dumps(payload),
            encoding="application/json",
        )

    queryable = session.declare_queryable(SNAPSHOT_KEY, reply)
    try:
        with pytest.raises(expected):
            buqi.snapshot(session, CAMERA, timeout=1.0)
    finally:
        queryable.undeclare()


def test_snapshot_times_out_without_reply(session):
    with pytest.raises(buqi.CameraTimeout):
        buqi.snapshot(session, CAMERA, timeout=0.2)


def test_snapshot_deadline_maps_held_query_to_timeout(session):
    held = []
    queryable = session.declare_queryable(SNAPSHOT_KEY, held.append)
    try:
        started = time.monotonic()
        with pytest.raises(buqi.CameraTimeout):
            buqi.snapshot(session, CAMERA, timeout=0.2)
        elapsed = time.monotonic() - started
        assert 0.15 <= elapsed < 1.0, elapsed
    finally:
        queryable.undeclare()


def test_snapshot_deadline_survives_short_session_default(routed_sessions):
    router, client = routed_sessions
    held = []
    queryable = router.declare_queryable(SNAPSHOT_KEY, held.append)
    try:
        started = time.monotonic()
        with pytest.raises(buqi.CameraTimeout):
            buqi.snapshot(client, CAMERA, timeout=0.3)
        elapsed = time.monotonic() - started
        assert 0.25 <= elapsed < 1.0, elapsed
    finally:
        queryable.undeclare()


def test_snapshot_maps_synthetic_timeout_to_camera_timeout(
    routed_sessions, monkeypatch
):
    router, client = routed_sessions
    # 模拟底层查询期限早于 SDK deadline：只有映射合成超时才能仍是 CameraTimeout。
    monkeypatch.setattr("buqi.media._QUERY_TIMEOUT_MARGIN", -0.2)
    held = []
    queryable = router.declare_queryable(SNAPSHOT_KEY, held.append)
    try:
        with pytest.raises(buqi.CameraTimeout):
            buqi.snapshot(client, CAMERA, timeout=0.3)
    finally:
        queryable.undeclare()


def test_snapshot_accepts_reply_within_deadline_despite_short_default(routed_sessions):
    router, client = routed_sessions
    timers = []

    def hold_and_reply(query):
        timer = threading.Timer(
            0.15,
            lambda: query.reply(
                SNAPSHOT_KEY,
                JPEG,
                encoding="image/jpeg",
                attachment=json.dumps(metadata(9)),
            ),
        )
        timers.append(timer)
        timer.start()

    queryable = router.declare_queryable(SNAPSHOT_KEY, hold_and_reply)
    try:
        frame = buqi.snapshot(client, CAMERA, timeout=0.3)
        assert frame.metadata.sequence == 9
        assert frame.jpeg == JPEG
    finally:
        for timer in timers:
            timer.join(timeout=1.0)
        queryable.undeclare()


def test_snapshot_preserves_non_timeout_query_errors(session):
    def reply(query):
        query.reply_err(b"boom")

    queryable = session.declare_queryable(SNAPSHOT_KEY, reply)
    try:
        with pytest.raises(buqi.CameraError) as info:
            buqi.snapshot(session, CAMERA, timeout=1.0)
        assert not isinstance(info.value, buqi.CameraTimeout), info.value
    finally:
        queryable.undeclare()


def test_snapshot_rejects_missing_attachment(session):
    def reply(query):
        query.reply(SNAPSHOT_KEY, JPEG, encoding="image/jpeg")

    queryable = session.declare_queryable(SNAPSHOT_KEY, reply)
    try:
        with pytest.raises(buqi.CameraProtocolError):
            buqi.snapshot(session, CAMERA, timeout=1.0)
    finally:
        queryable.undeclare()


def test_frames_keep_only_the_latest_and_expire(session):
    publisher = session.declare_publisher(FRAME_KEY)
    stream = buqi.frames(session, CAMERA)
    try:
        for sequence, jpeg in [(1, b"\xff\xd8one\xff\xd9"), (2, b"\xff\xd8two\xff\xd9")]:
            publisher.put(
                jpeg,
                encoding="image/jpeg",
                attachment=json.dumps(metadata(sequence)),
            )
        time.sleep(0.2)

        frame = stream.next(timeout=1.0)
        assert frame.metadata.sequence == 2, "慢消费者应追到最新帧"
        assert frame.jpeg == b"\xff\xd8two\xff\xd9"

        with pytest.raises(buqi.CameraTimeout):
            stream.next(timeout=0.2)

        publisher.put(
            b"\xff\xd8old\xff\xd9",
            encoding="image/jpeg",
            attachment=json.dumps(metadata(3)),
        )
        time.sleep(0.3)
        with pytest.raises(buqi.CameraTimeout):
            stream.next(timeout=0.1)
    finally:
        stream.close()
        publisher.undeclare()


def test_duplicate_frames_do_not_extend_observation_deadline(session):
    publisher = session.declare_publisher(FRAME_KEY)
    stream = buqi.frames(session, CAMERA)
    duplicate = json.dumps(metadata(7))
    try:
        publisher.put(JPEG, encoding="image/jpeg", attachment=duplicate)
        assert stream.next(timeout=1.0).metadata.sequence == 7

        stop = threading.Event()

        def republish_duplicate():
            while not stop.is_set():
                publisher.put(JPEG, encoding="image/jpeg", attachment=duplicate)
                time.sleep(0.05)

        thread = threading.Thread(target=republish_duplicate, daemon=True)
        thread.start()
        started = time.monotonic()
        try:
            with pytest.raises(buqi.CameraTimeout):
                stream.next(timeout=0.3)
        finally:
            stop.set()
            thread.join(timeout=1.0)
        assert time.monotonic() - started >= 0.25, "重复帧不应刷新观测期限"

        publisher.put(
            b"\xff\xd8new\xff\xd9",
            encoding="image/jpeg",
            attachment=json.dumps(metadata(8)),
        )
        assert stream.next(timeout=1.0).metadata.sequence == 8
    finally:
        stream.close()
        publisher.undeclare()


def test_invalid_camera_id_is_rejected(session):
    with pytest.raises(ValueError):
        buqi.snapshot(session, "pc2/rgb", timeout=0.2)
    with pytest.raises(ValueError):
        buqi.frames(session, "pc2*")
