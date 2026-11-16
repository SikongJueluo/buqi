#!/usr/bin/env python3
"""Buqi 多容器模拟通信验收。

拓扑（两个隔离的容器网络，模拟机器人只接机器人侧、应用只接应用侧）：

    机器人侧容器  --robot-net--  网关容器（Zenoh Router + DDS 插件）  --app-net--  应用容器

机器人侧用官方宇树 SDK 的 ``ChannelPublisher`` 以 10 Hz 发布 G1
``unitree_hg.msg.dds_.LowState_`` 到 ``rt/lowstate``；应用侧用本次构建的 Buqi SDK
连接网关、订阅 Zenoh 键，并用官方消息类型解码与逐条校验。DDS 发现只依赖机器人侧
的自动发现（CycloneDDS 多播），不配置静态对端。

场景：持续订阅（两个独立订阅者）、关闭一个不影响另一个、晚加入、重新订阅、
发布端重启断流恢复、网关重启后显式重连。

用法（仓库根目录，通常经 ``just test-integration`` 调用）：:

    bash scripts/integration-acceptance.sh

脚本负责构建镜像并调用本文件；本文件只使用容器运行时与本次运行的产物目录。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
INTEGRATION_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(INTEGRATION_DIR))

import harness  # noqa: E402  只取常量，不触发 DDS 导入

# 两个隔离网络使用固定子网；网关在应用侧用固定地址，重启后客户端仍能连到同一端点。
ROBOT_SUBNET = "10.203.17.0/24"
APP_SUBNET = "10.203.18.0/24"
GATEWAY_APP_IP = "10.203.18.2"

# 测量窗口内至少应发布的消息数（名义 10 Hz 的一半），避免“覆盖率”建立在个位数样本上。
MIN_PUBLISHED = 300


class ScenarioFailed(Exception):
    pass


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class Harness:
    def __init__(self, args: argparse.Namespace) -> None:
        self.runtime = args.runtime
        self.gateway_image = args.gateway_image
        self.python_image = args.python_image

        stamp = time.strftime("%Y%m%dT%H%M%S")
        self.run_id = f"{stamp}-{uuid.uuid4().hex[:6]}"
        self.prefix = f"bq-it-{self.run_id}"
        self.run_dir = REPO_ROOT / "target" / "acceptance" / "runs" / self.run_id
        self.log_dir = self.run_dir / "logs"

        self.net_robot = f"{self.prefix}-robot"
        self.net_app = f"{self.prefix}-app"
        self.gateway = f"{self.prefix}-gw"
        self.publisher = f"{self.prefix}-robot"
        self.endpoint: str | None = None

        self.containers: list[str] = []
        self.report: dict = {
            "run_id": self.run_id,
            "started_wall": time.time(),
            "runtime": self.runtime,
            "criteria": {
                "domain": harness.DOMAIN_ID,
                "topic": harness.TOPIC,
                "rate_hz": harness.RATE_HZ,
                "measure_seconds": harness.MEASURE_SECONDS,
                "max_gap": harness.MAX_GAP,
                "min_coverage": harness.MIN_COVERAGE,
                "recovery_deadline": harness.RECOVERY_DEADLINE,
                "first_message_deadline": harness.FIRST_MESSAGE_DEADLINE,
                "min_published": MIN_PUBLISHED,
            },
            "images": {},
            "scenarios": [],
            "cleanup": None,
        }
        self.results: dict[str, str] = {}
        self.setup_error: str | None = None

    # ── 进程与容器 ────────────────────────────────────────────────

    def run(
        self, *args: str, check: bool = True, timeout: float = 120.0
    ) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(
                [self.runtime, *args],
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise ScenarioFailed(
                f"{self.runtime} {' '.join(args)} 超时（{timeout:.0f} 秒）"
            ) from None
        if check and result.returncode != 0:
            raise ScenarioFailed(
                f"{self.runtime} {' '.join(args)} 失败（{result.returncode}）："
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        return result

    def image_info(self, image: str) -> dict:
        result = self.run("image", "inspect", image, "--format", "{{.Id}} {{.Size}}")
        return {"image": image, "inspect": result.stdout.strip()}

    def container_logs(self, name: str) -> str:
        return self.run("logs", name, check=False).stdout

    def save_logs(self, name: str) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        text = self.container_logs(name)
        (self.log_dir / f"{name}.log").write_text(text, encoding="utf-8")

    def inspect(self, name: str, template: str) -> str:
        return self.run("inspect", "--format", template, name).stdout.strip()

    def is_running(self, name: str) -> bool | None:
        """``True``/``False`` 明确表示容器状态；查询失败返回 ``None``，不能当作已退出。"""
        if name not in self.containers:
            return False
        result = self.run(
            "inspect", "--format", "{{.State.Running}}", name, check=False
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() == "true"

    def start_container(
        self,
        name: str,
        image: str,
        networks: list[str],
        env: dict[str, str],
        command: list[str],
        mounts: list[tuple[Path, str]],
    ) -> None:
        args = ["run", "-d", "--name", name, "--label", f"buqi-it={self.run_id}"]
        for network in networks:
            args += ["--network", network]
        for key, value in env.items():
            args += ["--env", f"{key}={value}"]
        for source, target in mounts:
            args += ["--volume", f"{source}:{target}"]
        args += [image, *command]
        self.run(*args)
        self.containers.append(name)

    def stop_container(self, name: str, timeout: float = 15.0) -> None:
        self.run("stop", "-t", str(int(timeout)), name, check=False)

    def start_existing(self, name: str) -> None:
        self.run("start", name)

    def state_summary(self, name: str) -> str:
        """失败信息里带上容器状态，便于区分真实退出与状态查询瞬时失败。"""
        result = self.run(
            "inspect",
            "--format",
            "{{.State.Status}} running={{.State.Running}} exit={{.State.ExitCode}}",
            name,
            check=False,
        )
        return (result.stdout.strip() or result.stderr.strip()) or "状态未知"

    def exit_code(self, name: str) -> int | None:
        result = self.run(
            "inspect", "--format", "{{.State.ExitCode}}", name, check=False
        )
        if result.returncode != 0:
            return None
        return int(result.stdout.strip())

    # ── 等待与事件 ────────────────────────────────────────────────

    def wait_for(self, description: str, predicate, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            if predicate():
                return
            if time.monotonic() >= deadline:
                raise ScenarioFailed(f"等待{description}超时（{timeout:.0f} 秒）")
            time.sleep(0.1)

    def events(self, name: str) -> list[dict]:
        return harness.read_ndjson_at(self.run_dir / name / "events.ndjson")

    def wait_event(self, name: str, event: str, timeout: float, count: int = 1) -> dict:
        box: dict = {}

        def found() -> bool:
            matches = [record for record in self.events(name) if record["event"] == event]
            if len(matches) >= count:
                box["record"] = matches[count - 1]
                return True
            return False

        self.wait_for(f"{name} 事件 {event}", found, timeout)
        return box["record"]

    def wait_file(self, name: str, filename: str, timeout: float) -> dict:
        """等待结果文件；容器提前退出时快速失败并带上容器状态与日志尾部。"""
        path = self.run_dir / name / filename
        deadline = time.monotonic() + timeout
        while not path.exists():
            # 容器状态查询可能瞬时滞后或失败；连续两次观察到退出且结果文件仍缺失才判失败。
            if self.is_running(name) is False:
                time.sleep(0.5)
                if not path.exists() and self.is_running(name) is False:
                    raise ScenarioFailed(
                        f"{name} 在写出 {filename} 前退出（{self.state_summary(name)}）："
                        f"{self.log_tail(name)}"
                    )
                continue
            if time.monotonic() >= deadline:
                raise ScenarioFailed(f"等待{name}/{filename}超时（{timeout:.0f} 秒）")
            time.sleep(0.1)
        return read_json(path)

    def log_tail(self, name: str, lines: int = 6) -> str:
        return "\n".join(self.container_logs(name).strip().splitlines()[-lines:])

    # ── 检查记录 ──────────────────────────────────────────────────

    def check(self, entry: dict, name: str, ok: bool, detail: str = "") -> None:
        entry["checks"].append({"name": name, "ok": bool(ok), "detail": detail})

    # ── 网关与发布端 ──────────────────────────────────────────────

    def create_networks(self) -> None:
        self.run(
            "network", "create",
            "--label", f"buqi-it={self.run_id}",
            "--subnet", ROBOT_SUBNET,
            self.net_robot,
        )
        self.run(
            "network", "create",
            "--label", f"buqi-it={self.run_id}",
            "--subnet", APP_SUBNET,
            self.net_app,
        )

    def start_gateway(self) -> None:
        config_dir = self.run_dir / "gateway"
        config_dir.mkdir(parents=True, exist_ok=True)
        config = (
            'listen = { endpoints = ["tcp/0.0.0.0:7447"] }\n\n'
            "[plugins.dds]\n"
            f"domain = {harness.DOMAIN_ID}\n"
            "localhost_only = false\n"
        )
        (config_dir / "buqi.toml").write_text(config, encoding="utf-8")
        self.report["gateway_config"] = config
        self.start_container(
            self.gateway,
            self.gateway_image,
            [self.net_robot],
            {},
            ["server", "--config", "/etc/buqi/buqi.toml"],
            [(config_dir, "/etc/buqi")],
        )
        # 固定地址必须显式连接；容器重启后沿用同一地址，应用才能按原端点重连。
        self.run("network", "connect", "--ip", GATEWAY_APP_IP, self.net_app, self.gateway)
        self.wait_for(
            "网关报告监听端点",
            lambda: "客户端连接端点" in self.container_logs(self.gateway),
            30,
        )
        ip = self.inspect(
            self.gateway,
            f'{{{{(index .NetworkSettings.Networks "{self.net_app}").IPAddress}}}}',
        )
        if ip != GATEWAY_APP_IP:
            raise ScenarioFailed(f"网关应用侧地址不符合预期：{ip}")
        self.endpoint = f"tcp/{ip}:7447"

    def start_publisher(self) -> dict:
        run_path = self.run_dir / "publisher"
        run_path.mkdir(parents=True, exist_ok=True)
        count = len(
            [record for record in self.events("publisher") if record["event"] == "publisher-start"]
        )
        if count == 0:
            self.start_container(
                self.publisher,
                self.python_image,
                [self.net_robot],
                {"BQ_RUN_DIR": "/run/buqi-test"},
                ["python", "/app/robot_publisher.py"],
                [(INTEGRATION_DIR, "/app:ro"), (run_path, "/run/buqi-test")],
            )
        else:
            self.start_existing(self.publisher)
        return self.wait_event("publisher", "publisher-start", 60, count=count + 1)

    def start_app(self, name: str, script: str, extra_env: dict[str, str] | None = None) -> None:
        run_path = self.run_dir / name
        run_path.mkdir(parents=True, exist_ok=True)
        env = {
            "BQ_RUN_DIR": "/run/buqi-test",
            "BQ_APP_NAME": name,
            "BQ_ENDPOINT": self.endpoint or "",
        }
        env.update(extra_env or {})
        self.start_container(
            name,
            self.python_image,
            [self.net_app],
            env,
            ["python", f"/app/{script}"],
            [(INTEGRATION_DIR, "/app:ro"), (run_path, "/run/buqi-test")],
        )

    # ── 覆盖率 ────────────────────────────────────────────────────

    def coverage(self, name: str, measured: dict) -> tuple[float, int, int]:
        start = measured["window_start_wall"]
        end = measured["window_end_wall"]
        published = {
            (record["epoch"], record["tick"])
            for record in harness.read_ndjson_at(self.run_dir / "publisher" / "published.ndjson")
            if record["ok"] and start <= record["wall"] < end
        }
        received = {
            (record["epoch"], record["tick"])
            for record in harness.read_ndjson_at(self.run_dir / name / "received.ndjson")
            if start <= record["wall"] < end
        }
        if not published:
            return 0.0, 0, len(received)
        return len(published & received) / len(published), len(published), len(received)

    # ── 场景 ──────────────────────────────────────────────────────

    def scenario_sustained(self, entry: dict) -> None:
        apps = ("steady-a", "steady-b")
        for name in apps:
            self.start_app(name, "app_steady.py", {"BQ_LINGER_SECONDS": "120"})

        measured = {}
        for name in apps:
            measured[name] = self.wait_file(name, "measured.json", 180)

        for name in apps:
            result = measured[name]
            stats = result["stats"]
            latency = result["first_valid_wall"] - result["app_start_wall"]
            coverage, published, received = self.coverage(name, result)
            self.check(
                entry,
                f"{name} 20 秒内收到首条有效消息",
                latency <= harness.FIRST_MESSAGE_DEADLINE,
                f"{latency:.2f} 秒",
            )
            self.check(
                entry,
                f"{name} 逐条解码与约定字段校验",
                result["invalid"] == 0,
                f"invalid={result['invalid']} {result['invalid_error'] or ''}".strip(),
            )
            self.check(
                entry,
                f"{name} 无重复与乱序",
                stats["duplicates"] == 0 and stats["out_of_order"] == 0,
                f"duplicates={stats['duplicates']} out_of_order={stats['out_of_order']}",
            )
            self.check(
                entry,
                f"{name} 相邻有效消息间隔 <= {harness.MAX_GAP} 秒",
                stats["max_gap"] <= harness.MAX_GAP,
                f"max_gap={stats['max_gap']:.3f} 秒",
            )
            self.check(
                entry,
                f"{name} 覆盖同一窗口实际发布数量的 {harness.MIN_COVERAGE:.0%}",
                coverage >= harness.MIN_COVERAGE,
                f"coverage={coverage:.3f} published={published} received={received}",
            )
            self.check(
                entry,
                f"{name} 测量窗口样本量下限 {MIN_PUBLISHED}",
                published >= MIN_PUBLISHED,
                f"published={published}",
            )
            entry["notes"].append(
                {
                    "app": name,
                    "stats": stats,
                    "coverage": round(coverage, 4),
                    "published": published,
                    "received": received,
                }
            )

    def scenario_stop_one(self, entry: dict) -> None:
        keeper, stopped = "steady-b", "steady-a"
        self.wait_for("存活订阅者的心跳", lambda: self.last_heartbeat(keeper) is not None, 15)
        before = self.last_heartbeat(keeper)
        self.stop_container(stopped)
        code = self.exit_code(stopped)
        self.check(entry, "被关闭的订阅者以退出码 0 结束", code == 0, f"exit={code}")

        target = before["received_total"] + 5
        self.wait_for(
            "存活的订阅者继续收到消息",
            lambda: (self.last_heartbeat(keeper) or {}).get("received_total", 0) >= target,
            20,
        )
        after = self.last_heartbeat(keeper)
        self.check(
            entry,
            "关闭一个订阅者后另一个继续接收",
            after["received_total"] > before["received_total"],
            f"{before['received_total']} -> {after['received_total']}",
        )
        self.check(
            entry,
            "存活的订阅者保持运行",
            self.is_running(keeper) is True,
            f"running={self.is_running(keeper)}",
        )

    def last_heartbeat(self, name: str) -> dict | None:
        records = harness.read_ndjson_at(self.run_dir / name / "heartbeat.ndjson")
        return records[-1] if records else None

    def scenario_late_join(self, entry: dict) -> None:
        name = "late-join"
        self.start_app(name, "app_steady.py", {"BQ_MEASURE_SECONDS": "20", "BQ_LINGER_SECONDS": "0"})
        result = self.wait_file(name, "measured.json", 120)
        stats = result["stats"]
        latency = result["first_valid_wall"] - result["app_start_wall"]
        coverage, published, received = self.coverage(name, result)
        self.check(
            entry,
            "晚加入的订阅者 20 秒内收到首条有效消息",
            latency <= harness.FIRST_MESSAGE_DEADLINE,
            f"{latency:.2f} 秒",
        )
        self.check(
            entry,
            "晚加入的订阅者收到后续数据",
            coverage >= harness.MIN_COVERAGE and published > 0,
            f"coverage={coverage:.3f} published={published} received={received}",
        )
        self.check(
            entry,
            "晚加入的订阅者逐条校验通过",
            result["invalid"] == 0 and stats["max_gap"] <= harness.MAX_GAP,
            f"invalid={result['invalid']} max_gap={stats['max_gap']:.3f}",
        )
        published_at = {
            (record["epoch"], record["tick"]): record["wall"]
            for record in harness.read_ndjson_at(
                self.run_dir / "publisher" / "published.ndjson"
            )
        }
        joined = [
            (record["epoch"], record["tick"])
            for record in harness.read_ndjson_at(self.run_dir / name / "received.ndjson")
            if result["window_start_wall"] <= record["wall"] < result["window_end_wall"]
        ]
        history = [
            key
            for key in joined
            if published_at.get(key, 0.0) < result["app_start_wall"] - 0.5
        ]
        self.check(
            entry,
            "晚加入不补历史数据",
            not history,
            f"早于加入的消息 {len(history)} 条",
        )
        entry["notes"].append(
            {
                "stats": stats,
                "coverage": round(coverage, 4),
                "published": published,
                "received": received,
            }
        )
        self.stop_container("steady-b")
        self.stop_container(name)
        self.check(
            entry,
            "关闭后的两个订阅进程以退出码 0 结束",
            self.exit_code("steady-b") == 0 and self.exit_code(name) == 0,
            f"steady-b={self.exit_code('steady-b')} {name}={self.exit_code(name)}",
        )

    def scenario_resubscribe(self, entry: dict) -> None:
        name = "resubscribe"
        self.start_app(name, "app_resubscribe.py")
        result = self.wait_file(name, "final.json", 150)
        events = [record["event"] for record in self.events(name)]
        self.check(entry, "订阅者关闭旧订阅", "subscription-closed" in events)
        self.check(
            entry,
            f"关闭后静默 {harness.SILENCE_SECONDS:.0f} 秒",
            "silence-confirmed" in events,
        )
        self.check(
            entry,
            "关闭后的旧订阅不再交付消息",
            "closed-subscription-rejects" in events or "closed-subscription-silent" in events,
        )
        self.check(entry, "重建订阅后收到数据", "resubscribed" in events)
        self.check(
            entry,
            "重建后收到的是关闭之后发布的数据",
            result["first_tick_after_reopen"] > result["last_tick_before_close"],
            f"last={result['last_tick_before_close']} first={result['first_tick_after_reopen']}",
        )
        self.check(
            entry,
            "重新订阅场景无解码/字段校验失败",
            result["invalid"] == 0,
            f"invalid={result['invalid']} {result['errors']}",
        )
        self.check(
            entry,
            "重新订阅场景以退出码 0 结束",
            self.exit_code(name) == 0,
            f"exit={self.exit_code(name)}",
        )
        self.stop_container(name)

    def scenario_publisher_restart(self, entry: dict) -> None:
        name = "publisher-restart"
        self.start_app(name, "app_stall.py")
        self.wait_file(name, "measured.json", 120)
        self.wait_event(name, "measure-done", 30)
        self.wait_event(name, "awaiting-stall", 30)

        self.stop_container(self.publisher)
        self.wait_event(name, "stall-detected", 60)
        started = self.start_publisher()
        self.check(
            entry,
            "模拟发布端停止后可判定断流",
            True,
            f"publisher 已停止并重启，新 epoch={started['epoch']}",
        )

        recovered = self.wait_event(name, "epoch-recovered", 90)
        result = self.wait_file(name, "final.json", 30)
        latency = recovered["wall"] - started["wall"]
        self.check(
            entry,
            "重启前后数据可区分",
            result["new_epoch"] != result["old_epoch"],
            f"{result['old_epoch']} -> {result['new_epoch']}",
        )
        self.check(
            entry,
            "发布端重启后 30 秒内收到新一轮数据",
            latency <= harness.RECOVERY_DEADLINE,
            f"{latency:.2f} 秒",
        )
        self.check(
            entry,
            "发布端重启场景无解码/字段校验失败",
            result["invalid"] == 0,
            f"invalid={result['invalid']} {result['errors']}",
        )
        self.check(
            entry,
            "发布端重启场景以退出码 0 结束",
            self.exit_code(name) == 0,
            f"exit={self.exit_code(name)}",
        )
        self.stop_container(name)

    def scenario_gateway_restart(self, entry: dict) -> None:
        name = "gateway-restart"
        self.start_app(name, "app_reconnect.py")
        self.wait_file(name, "measured.json", 120)
        self.wait_event(name, "measure-done", 30)
        self.wait_event(name, "awaiting-gateway-stop", 30)

        self.stop_container(self.gateway)
        self.wait_event(name, "connection-lost", 60)
        started_wall = time.time()
        self.start_existing(self.gateway)
        self.wait_for(
            "网关重新报告监听端点",
            lambda: "客户端连接端点" in self.container_logs(self.gateway)[-4000:],
            30,
        )

        recovered = self.wait_event(name, "recovered", 90)
        result = self.wait_file(name, "final.json", 30)
        latency = recovered["wall"] - started_wall
        self.check(
            entry,
            "网关重启后应用显式重连并重新订阅",
            "reconnected" in [record["event"] for record in self.events(name)],
        )
        self.check(
            entry,
            "网关重启后 30 秒内恢复接收",
            latency <= harness.RECOVERY_DEADLINE,
            f"{latency:.2f} 秒",
        )
        self.check(
            entry,
            "网关重启不改变发布端数据身份",
            result["epoch"] == recovered["epoch"],
            f"epoch={result['epoch']}",
        )
        self.check(
            entry,
            "网关重启场景无解码/字段校验失败",
            result["invalid"] == 0,
            f"invalid={result['invalid']} {result['errors']}",
        )
        self.check(
            entry,
            "网关重启场景以退出码 0 结束",
            self.exit_code(name) == 0,
            f"exit={self.exit_code(name)}",
        )
        self.stop_container(name)

    # ── 运行与清理 ────────────────────────────────────────────────

    def setup(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.report["images"] = {
            "gateway": self.image_info(self.gateway_image),
            "python": self.image_info(self.python_image),
        }
        self.report["runtime_version"] = self.run("version", check=False).stdout.strip()
        self.create_networks()
        self.start_gateway()
        self.start_publisher()
        self.report["endpoint"] = self.endpoint

    def run_scenario(self, name: str, function) -> str:
        entry = {"name": name, "status": "FAIL", "checks": [], "notes": []}
        self.report["scenarios"].append(entry)
        started = time.monotonic()
        print(f"\n—— 场景：{name}", flush=True)
        try:
            function(entry)
        except Exception as error:  # 记录后继续跑其余场景
            entry["error"] = f"{type(error).__name__}: {error}"
            traceback.print_exc()
        entry["seconds"] = round(time.monotonic() - started, 1)

        if "error" not in entry and all(check["ok"] for check in entry["checks"]):
            entry["status"] = "PASS"
        for check in entry["checks"]:
            print(
                f"  {'PASS' if check['ok'] else 'FAIL'} {check['name']}"
                + (f"（{check['detail']}）" if check["detail"] else ""),
                flush=True,
            )
        if "error" in entry:
            print(f"  FAIL {entry['error']}", flush=True)
        print(f"{entry['status']} {name}（{entry['seconds']} 秒）", flush=True)
        self.results[name] = entry["status"]
        return entry["status"]

    def mark_not_run(self, name: str, reason: str) -> None:
        entry = {"name": name, "status": "NOT RUN", "reason": reason, "checks": []}
        self.report["scenarios"].append(entry)
        self.results[name] = "NOT RUN"
        print(f"NOT RUN {name}（{reason}）", flush=True)

    def cleanup(self) -> None:
        print("\n—— 清理", flush=True)
        errors = []
        for name in self.containers:
            try:
                self.save_logs(name)
            except Exception as error:
                errors.append(f"保存 {name} 日志失败：{error}")
        for name in self.containers:
            try:
                result = self.run("rm", "-f", name, check=False)
            except Exception as error:
                errors.append(f"删除容器 {name} 失败：{error}")
                continue
            if result.returncode != 0:
                errors.append(f"删除容器 {name} 失败：{result.stderr.strip()}")
        for network in (self.net_robot, self.net_app):
            try:
                result = self.run("network", "rm", "-f", network, check=False)
            except Exception as error:
                errors.append(f"删除网络 {network} 失败：{error}")
                continue
            if result.returncode != 0:
                errors.append(f"删除网络 {network} 失败：{result.stderr.strip()}")
        self.report["cleanup"] = {"ok": not errors, "errors": errors}
        if errors:
            for error in errors:
                print(f"FAIL {error}", flush=True)
        else:
            print("PASS 已删除本次容器与网络（镜像与构建缓存保留）", flush=True)

    def write_report(self) -> Path:
        path = self.run_dir / "report.json"
        self.report["finished_wall"] = time.time()
        path.write_text(
            json.dumps(self.report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return path


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime",
        default=os.environ.get("CONTAINER_RUNTIME", "podman"),
        help="容器运行时命令（默认 podman，CI 可用 docker）",
    )
    parser.add_argument("--gateway-image", required=True)
    parser.add_argument("--python-image", required=True)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    orchestrator = Harness(args)

    def interrupt(signum, frame):
        del signum, frame
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)

    interrupted = False
    try:
        try:
            orchestrator.setup()
        except Exception as error:
            orchestrator.setup_error = f"{type(error).__name__}: {error}"
            traceback.print_exc()

        scenarios = [
            ("sustained", orchestrator.scenario_sustained, ()),
            ("stop-one", orchestrator.scenario_stop_one, ("sustained",)),
            ("late-join", orchestrator.scenario_late_join, ("sustained",)),
            ("resubscribe", orchestrator.scenario_resubscribe, ()),
            ("publisher-restart", orchestrator.scenario_publisher_restart, ()),
            ("gateway-restart", orchestrator.scenario_gateway_restart, ()),
        ]
        for name, function, dependencies in scenarios:
            if orchestrator.setup_error is not None:
                orchestrator.mark_not_run(name, f"前置启动失败：{orchestrator.setup_error}")
                continue
            blocked = [dep for dep in dependencies if orchestrator.results.get(dep) != "PASS"]
            if blocked:
                orchestrator.mark_not_run(name, f"依赖未通过：{', '.join(blocked)}")
                continue
            orchestrator.run_scenario(name, function)
    except KeyboardInterrupt:
        interrupted = True
        print("\n收到中断信号，清理本次资源。", flush=True)
    finally:
        orchestrator.cleanup()
        report_path = orchestrator.write_report()

    failed = [
        name
        for name, status in orchestrator.results.items()
        if status != "PASS"
    ]
    if orchestrator.setup_error is not None:
        failed.append("setup")
    if orchestrator.report["cleanup"] and not orchestrator.report["cleanup"]["ok"]:
        failed.append("cleanup")
    if interrupted:
        failed.append("interrupted")

    print(f"\n本地报告：{report_path}")
    if failed:
        print(f"失败项：{', '.join(failed)}", file=sys.stderr)
        return 1
    print("多容器模拟通信验收通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
