"""buqi —— 机器人统一接口 SDK。

通过 Zenoh 连接网关（buqi-server：Zenoh Router + DDS 插件）访问机器人能力，
内部以可插拔 Adapter 对接不同机器人（如 Unitree G1 经 DDS 桥接）。

两层接口：
1. 原生兼容：允许用户继续使用厂商 SDK（如 unitree_sdk2py，经 DDS 桥接）。
2. 统一接口：``Robot`` 抽象（connect / get_state / camera / move 等），
   随架构演进而稳定。
"""

__version__ = "0.1.0"
