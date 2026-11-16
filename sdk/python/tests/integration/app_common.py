"""订阅端公共逻辑：连接、逐条解码校验与接收统计。"""

from __future__ import annotations

import time

import buqi

import g1_state
import harness


class Receiver:
    """一个 Buqi（官方 Zenoh Session）订阅者，逐条解码官方 G1 消息。"""

    def __init__(self, endpoint: str | None = None, session=None) -> None:
        self.session = session if session is not None else buqi.connect(endpoint)
        self.subscriber = self.session.declare_subscriber(harness.TOPIC)
        self.invalid = 0
        self.invalid_error: str | None = None
        self.last_valid_wall: float | None = None
        self.last_valid_mono: float | None = None

    def poll(self) -> dict | None:
        """取一条有效消息；没有新样本或解码失败时返回 ``None``。"""
        sample = self.subscriber.try_recv()
        if sample is None:
            return None
        try:
            epoch, tick = g1_state.decode_and_validate(sample.payload.to_bytes())
        except Exception as error:  # 解码/校验失败必须计数，不能当成丢包
            self.invalid += 1
            if self.invalid_error is None:
                self.invalid_error = f"{type(error).__name__}: {error}"
            return None
        self.last_valid_wall = time.time()
        self.last_valid_mono = time.monotonic()
        return {
            "epoch": epoch,
            "tick": tick,
            "wall": self.last_valid_wall,
            "mono": self.last_valid_mono,
        }

    def close(self) -> None:
        self.subscriber.undeclare()
        self.session.close()


def wait_first(receiver: Receiver, deadline: float, description: str) -> dict:
    """在 ``deadline``（monotonic 秒）前等到首条有效消息。"""
    while True:
        record = receiver.poll()
        if record is not None:
            return record
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{description}：未在期限内收到首条有效消息")
        time.sleep(0.005)


def measure(receiver: Receiver, seconds: float, received_name: str = "received.ndjson") -> dict:
    """从首条有效消息起测量固定窗口，返回窗口记录与统计。

    间隔与期限用单调时钟；``wall`` 只用于和其他容器按同一主机时钟对齐窗口。
    """
    first = wait_first(
        receiver,
        time.monotonic() + harness.FIRST_MESSAGE_DEADLINE,
        "等待首条有效消息",
    )
    window_start_mono = first["mono"]
    window_start_wall = first["wall"]
    window_end_mono = window_start_mono + seconds
    window_end_wall = window_start_wall + seconds

    records = [first]
    harness.append(received_name, first)
    while True:
        now = time.monotonic()
        if now >= window_end_mono:
            break
        record = receiver.poll()
        if record is not None:
            records.append(record)
            harness.append(received_name, record)
        else:
            time.sleep(0.002)

    summary = summarize(records, window_end_mono)
    return {
        "window_start_wall": window_start_wall,
        "window_end_wall": window_end_wall,
        "window_seconds": seconds,
        "first_valid_wall": first["wall"],
        "first_valid_tick": first["tick"],
        "epoch": first["epoch"],
        "invalid": receiver.invalid,
        "invalid_error": receiver.invalid_error,
        "stats": summary,
    }


def summarize(records: list[dict], window_end_mono: float | None = None) -> dict:
    """按到达顺序统计缺失、重复、乱序与最长相邻间隔。

    给出 ``window_end_mono`` 时，最后一条有效消息到窗口结束的静默时间也计入
    ``max_gap``；否则窗口末尾断流会被遗漏。
    """
    seen: set[tuple[int, int]] = set()
    highest: dict[int, int] = {}
    duplicates = 0
    out_of_order = 0
    missing = 0
    max_gap = 0.0
    previous_mono = None

    for record in records:
        key = (record["epoch"], record["tick"])
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)

        top = highest.get(record["epoch"])
        if top is None:
            highest[record["epoch"]] = record["tick"]
        elif record["tick"] <= top:
            out_of_order += 1
        else:
            missing += record["tick"] - top - 1
            highest[record["epoch"]] = record["tick"]

        if previous_mono is not None:
            max_gap = max(max_gap, record["mono"] - previous_mono)
        previous_mono = record["mono"]

    tail_gap = None
    if window_end_mono is not None:
        tail_gap = max(0.0, window_end_mono - records[-1]["mono"])
        max_gap = max(max_gap, tail_gap)

    return {
        "received": len(records),
        "duplicates": duplicates,
        "out_of_order": out_of_order,
        "missing": missing,
        "max_gap": max_gap,
        "tail_gap": tail_gap,
        "epochs": sorted(highest),
    }


class InvalidGuard:
    """累计一次运行中所有 Receiver 的解码/字段校验失败。

    生命周期场景会重建 Session 与订阅，只看最后一个 Receiver 会漏掉先前的失败；
    各阶段结束时和写报告前都调用 :meth:`require_clean`。
    """

    def __init__(self) -> None:
        self.receivers: list[Receiver] = []

    def track(self, receiver: Receiver) -> Receiver:
        self.receivers.append(receiver)
        return receiver

    @property
    def count(self) -> int:
        return sum(receiver.invalid for receiver in self.receivers)

    @property
    def errors(self) -> list[str]:
        return [
            receiver.invalid_error
            for receiver in self.receivers
            if receiver.invalid_error is not None
        ]

    def require_clean(self, stage: str) -> None:
        if self.count:
            raise AssertionError(
                f"{stage}：{self.count} 条载荷解码/字段校验失败，例：{self.errors[0]}"
            )


def stall_watch(receiver: Receiver, limit: float, deadline: float, description: str) -> dict:
    """等到连续 ``limit`` 秒没有有效消息，返回断流事件。"""
    while True:
        record = receiver.poll()
        if record is not None:
            continue
        last = receiver.last_valid_mono
        if last is not None and time.monotonic() - last > limit:
            return {"wall": time.time(), "gap": time.monotonic() - last}
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{description}：{limit} 秒内未出现断流")


def wait_epoch(
    receiver: Receiver, current: int, deadline: float, description: str
) -> dict:
    """等到与 ``current`` 不同的新 epoch 消息（发布端重启）。"""
    while True:
        record = receiver.poll()
        if record is not None and record["epoch"] != current:
            return record
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{description}：未收到新 epoch 的数据")
        time.sleep(0.005)
