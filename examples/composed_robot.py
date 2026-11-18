"""组合式 Robot 的纯模拟示例：发现、显式绑定、排队、抢占与相机。

运行：``uv run python examples/composed_robot.py``。它只使用内存 Adapter，
不连接网关或实体机器人。
"""

from __future__ import annotations

import asyncio

import buqi
from buqi.simulation import SimulatedAdapter, SimulatedDevice, SimulatedPart


class Arm(buqi.Part):
    trajectory: buqi.JointTrajectory
    motors: buqi.MotorControl | None


class MyRobot(buqi.Robot):
    arm: Arm
    camera: buqi.CameraPart | None


def waypoints(*positions: tuple[float, ...]) -> tuple[buqi.JointWaypoint, ...]:
    return tuple(
        buqi.JointWaypoint(position, time_from_start=0.5 * (index + 1))
        for index, position in enumerate(positions)
    )


async def main() -> None:
    adapter = SimulatedAdapter(
        SimulatedDevice(
            "g1",
            (SimulatedPart("left_arm", joints=("shoulder_pitch", "elbow")),),
        ),
        SimulatedDevice(
            "pc2",
            (
                SimulatedPart(
                    "rgb", trajectory=False, motor=False, camera=True
                ),
            ),
        ),
        duration=0.05,
    )

    devices = await buqi.discover(adapter)
    for device in devices:
        for part in device.parts:
            print(f"{device.id}/{part.part_id}: {', '.join(part.capability_names)}")

    robot = await MyRobot.bind(
        adapter,
        arm=devices[0].part("left_arm"),
        camera=devices[1].part("rgb"),
    )

    first = buqi.Trajectory(
        joints=("shoulder_pitch", "elbow"),
        waypoints=waypoints((0.2, 0.1), (0.4, 0.3)),
    )
    queued = buqi.Trajectory(
        joints=("shoulder_pitch", "elbow"),
        waypoints=waypoints((0.5, 0.2)),
    )
    replacement = buqi.Trajectory(
        joints=("shoulder_pitch", "elbow"),
        waypoints=waypoints((0.1, 0.0)),
    )

    async with robot.arm.trajectory.control() as control:
        running = await control.enqueue(first)
        waiting = await control.enqueue(queued)
        now = await control.run(replacement)
        print("被抢占：", (await running.result()).state.value)
        print("被丢弃：", (await waiting.result()).state.value)
        print("新动作：", (await now.result()).state.value)

    if robot.arm.motors is not None:
        async with robot.arm.motors.control() as motors:
            await motors.send(buqi.MotorTarget((0.1, 0.2), valid_for=0.1))

    if robot.camera is not None:
        frame = robot.camera.snapshot()
        print(
            "相机：",
            frame.metadata.camera,
            frame.metadata.width,
            frame.metadata.height,
            len(frame.jpeg),
            "字节 JPEG",
        )


if __name__ == "__main__":
    asyncio.run(main())
