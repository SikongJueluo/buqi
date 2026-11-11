"""相机图片通道：单帧查询与持续观测。

服务端（``buqi server`` 的媒体模块）把相机原始 JPEG 直接放进 Zenoh，
不重新编码。这里默认交付 JPEG 字节与元数据，不解码、也不依赖 OpenCV。

key 与元数据格式由网关的媒体协议定义；客户端凭 ``encoding`` 区分 JPEG 与
JSON 错误回复。
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from dataclasses import dataclass

import zenoh

_KEY_PREFIX = "buqi/media/v1"
_JPEG_ENCODING = "image/jpeg"
_JSON_ENCODING = "application/json"

_CAMERA_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_DEFAULT_TIMEOUT = 1.0
# 底层 Zenoh query 期限比 SDK deadline 稍长，避免 Session 的
# queries_default_timeout 或查询启动开销把仍在 SDK 期限内的回复提前截断。
_QUERY_TIMEOUT_MARGIN = 0.1


class CameraError(Exception):
    """图片通道错误。"""


class CameraTimeout(CameraError):
    """在给定时限内没有收到回复或新帧。"""


class CameraNoFrameError(CameraError):
    """服务端本次采集会话尚未产生首帧。"""


class CameraStaleFrameError(CameraError):
    """服务端缓存超过新鲜度上限；相机停流或掉线时也会出现。"""


class CameraProtocolError(CameraError):
    """回复的编码、载荷或元数据不符合协议。"""


@dataclass(frozen=True)
class FrameMetadata:
    """与 JPEG 同一条消息到达的帧元数据。

    ``captured_at_unix_ms`` 是服务端收到采集帧时的主机墙钟，不是曝光时间，
    也未经跨机同步。设备曝光时间不可用时协议不提供该字段。
    """

    version: int
    camera: str
    session: str
    sequence: int
    format: str
    width: int
    height: int
    captured_at_unix_ms: int


@dataclass(frozen=True)
class CameraFrame:
    """一帧未解码的相机 JPEG。"""

    jpeg: bytes
    metadata: FrameMetadata
    received_at: float
    """客户端收到该帧时的 ``time.monotonic()``。"""


def snapshot(
    session: zenoh.Session, camera: str, *, timeout: float = _DEFAULT_TIMEOUT
) -> CameraFrame:
    """查询服务端最新的有效帧。

    返回的是请求到达前最近一次采集的缓存，不保证在请求后重新曝光；
    服务端没有首帧或缓存过期时立即返回明确错误。``timeout`` 是等待网关
    回复的上限，不是帧年龄。

    Raises:
        CameraTimeout: 在 ``timeout`` 秒内没有收到任何回复。
        CameraNoFrameError: 服务端还没有首帧。
        CameraStaleFrameError: 服务端缓存已超过其新鲜度上限。
        CameraProtocolError: 回复不符合协议。
    """
    _validate_camera(camera)
    _validate_timeout(timeout)

    key = _snapshot_key(camera)
    replies: queue.Queue[zenoh.Reply] = queue.Queue(maxsize=1)

    def on_reply(reply: zenoh.Reply) -> None:
        try:
            replies.put_nowait(reply)
        except queue.Full:
            pass

    # 显式设置底层 query 期限：否则它受 Session 的 queries_default_timeout
    # 约束，可能早于本次调用的 deadline。SDK 仍以自己的 deadline 为准。
    token = zenoh.CancellationToken()
    session.get(
        key,
        on_reply,
        timeout=timeout + _QUERY_TIMEOUT_MARGIN,
        # 单帧只取第一个回复；NONE 让回复到达即交付，而不是被 Latest
        # 合并策略压到查询结束时才放行。
        consolidation=zenoh.ConsolidationMode.NONE,
        cancellation_token=token,
    )
    try:
        reply = replies.get(timeout=timeout)
    except queue.Empty:
        token.cancel()
        raise CameraTimeout(
            f"{timeout:.2f} 秒内没有收到 {camera} 的单帧回复；"
            "请确认网关已启用媒体且相机在线"
        ) from None
    token.cancel()

    if reply.err is not None:
        if _is_synthetic_timeout(reply.err):
            raise CameraTimeout(
                f"{timeout:.2f} 秒内没有收到 {camera} 的单帧回复；"
                "请确认网关已启用媒体且相机在线"
            )
        raise CameraError(
            f"网关查询 {camera} 失败：{reply.err.payload.to_bytes()!r}"
        )

    sample = reply.ok
    if sample is None:
        raise CameraProtocolError(f"{camera} 的回复既不是数据也不是错误")

    if sample.encoding == _JPEG_ENCODING:
        return _frame_from_sample(sample, camera, received_at=time.monotonic())
    if sample.encoding == _JSON_ENCODING:
        raise _server_error(sample.payload.to_bytes(), camera)
    raise CameraProtocolError(
        f"{camera} 的回复编码为 {sample.encoding}，期望 {_JPEG_ENCODING} 或 {_JSON_ENCODING}"
    )


def frames(session: zenoh.Session, camera: str) -> FrameStream:
    """订阅持续观测帧；配合 ``with`` 或显式 ``close()`` 使用。

    服务端按配置频率发布最新帧，客户端队列只保留最新一帧：消费者变慢时
    丢弃旧帧而不是积压。``FrameStream.next()`` 默认 1 秒没有新帧即报过期。
    """
    _validate_camera(camera)
    return FrameStream(session, camera)


class FrameStream:
    """只保留最新帧的观测订阅。"""

    def __init__(self, session: zenoh.Session, camera: str) -> None:
        self._camera = camera
        self._queue: queue.Queue[tuple[float, zenoh.Sample]] = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._closed = False
        self._last_seen: tuple[str, int] | None = None
        self._last_delivered: tuple[str, int] | None = None
        self._subscriber = session.declare_subscriber(
            _frame_key(camera), self._on_sample
        )

    def _on_sample(self, sample: zenoh.Sample) -> None:
        received_at = time.monotonic()
        identity = _identity_from_sample(sample)
        with self._lock:
            # 同一 (session, sequence) 的重复消息不是新采集：既不覆盖队列，
            # 也不刷新已有帧的接收时间。
            if identity is not None and identity == self._last_seen:
                return
            if identity is not None:
                self._last_seen = identity
            try:
                self._queue.put_nowait((received_at, sample))
            except queue.Full:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass
                self._queue.put_nowait((received_at, sample))

    def next(self, timeout: float = _DEFAULT_TIMEOUT) -> CameraFrame:
        """等待并返回最新帧。

        若队列中的帧已超过 ``timeout`` 秒仍没有更新，同样按过期处理，
        不会把暂停消费前收到的旧画面当成新观测。
        """
        _validate_timeout(timeout)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CameraTimeout(
                    f"{timeout:.2f} 秒内没有 {self._camera} 的新帧；"
                    "相机可能停流，或观测发布已停止"
                )
            try:
                received_at, sample = self._queue.get(timeout=remaining)
            except queue.Empty:
                raise CameraTimeout(
                    f"{timeout:.2f} 秒内没有 {self._camera} 的新帧；"
                    "相机可能停流，或观测发布已停止"
                ) from None
            if sample.encoding != _JPEG_ENCODING:
                raise CameraProtocolError(
                    f"{self._camera} 的观测帧编码为 {sample.encoding}，期望 {_JPEG_ENCODING}"
                )
            frame = _frame_from_sample(sample, self._camera, received_at)
            identity = (frame.metadata.session, frame.metadata.sequence)
            if identity == self._last_delivered:
                # 重复帧不构成“新观测”，继续等到本次调用的截止时间。
                continue
            if time.monotonic() - received_at > timeout:
                # 只有旧帧可用：继续等到截止时间，避免把过期画面交付给调用方。
                continue
            self._last_delivered = identity
            return frame

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._subscriber.undeclare()

    def __enter__(self) -> FrameStream:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _frame_key(camera: str) -> str:
    return f"{_KEY_PREFIX}/{camera}/frame"


def _snapshot_key(camera: str) -> str:
    return f"{_KEY_PREFIX}/{camera}/snapshot"


def _validate_camera(camera: str) -> None:
    if not _CAMERA_ID.fullmatch(camera):
        raise ValueError(
            "camera 只能包含 ASCII 字母、数字、`-` 与 `_`，长度 1–64"
        )


def _validate_timeout(timeout: float) -> None:
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("timeout 必须是正数秒")


def _is_synthetic_timeout(error: zenoh.ReplyError) -> bool:
    """Zenoh 在底层 query 到期时会向回调合成 ReplyError("Timeout")。"""
    return error.payload.to_bytes() == b"Timeout" and str(error.encoding).startswith(
        "zenoh/"
    )


def _identity_from_sample(sample: zenoh.Sample) -> tuple[str, int] | None:
    """尽力提取帧身份；附件损坏时返回 None，让 next() 报协议错误。"""
    attachment = sample.attachment
    if attachment is None:
        return None
    try:
        document = json.loads(attachment.to_bytes())
        return str(document["session"]), int(document["sequence"])
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def _frame_from_sample(
    sample: zenoh.Sample, camera: str, received_at: float
) -> CameraFrame:
    attachment = sample.attachment
    if attachment is None:
        raise CameraProtocolError(f"{camera} 的帧缺少元数据附件")
    metadata = _metadata_from_json(attachment.to_bytes(), camera)
    return CameraFrame(
        jpeg=sample.payload.to_bytes(),
        metadata=metadata,
        received_at=received_at,
    )


def _metadata_from_json(raw: bytes, camera: str) -> FrameMetadata:
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CameraProtocolError(f"{camera} 的帧元数据不是合法 JSON：{error}") from error

    try:
        metadata = FrameMetadata(
            version=int(document["version"]),
            camera=str(document["camera"]),
            session=str(document["session"]),
            sequence=int(document["sequence"]),
            format=str(document["format"]),
            width=int(document["width"]),
            height=int(document["height"]),
            captured_at_unix_ms=int(document["captured_at_unix_ms"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise CameraProtocolError(f"{camera} 的帧元数据字段不完整：{error}") from error

    if metadata.version != 1:
        raise CameraProtocolError(
            f"{camera} 的帧协议版本为 {metadata.version}，此 SDK 只支持 1"
        )
    if metadata.camera != camera:
        raise CameraProtocolError(
            f"帧元数据相机为 {metadata.camera!r}，与请求的 {camera!r} 不一致"
        )
    if metadata.format != "jpeg":
        raise CameraProtocolError(f"{camera} 的帧格式为 {metadata.format!r}，期望 'jpeg'")
    if metadata.width <= 0 or metadata.height <= 0:
        raise CameraProtocolError(f"{camera} 的帧尺寸无效：{metadata.width}x{metadata.height}")
    if not metadata.session or metadata.sequence < 0:
        raise CameraProtocolError(f"{camera} 的会话或序号无效")
    return metadata


def _server_error(raw: bytes, camera: str) -> CameraError:
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return CameraProtocolError(f"{camera} 的错误回复不是合法 JSON：{error}")

    code = document.get("error")
    if code == "no_frame":
        return CameraNoFrameError(f"网关还没有 {camera} 的首帧")
    if code == "stale":
        age = document.get("age_ms")
        limit = document.get("max_age_ms")
        return CameraStaleFrameError(
            f"{camera} 的缓存已过期（年龄 {age} ms，上限 {limit} ms）"
        )
    return CameraProtocolError(f"{camera} 的未知错误回复：{document!r}")
