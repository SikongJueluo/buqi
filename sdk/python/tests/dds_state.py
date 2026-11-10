"""验收用固定 DDS 类型与载荷；不包含机器人业务字段。"""

import struct
from dataclasses import dataclass

from cyclonedds.idl import IdlStruct
from cyclonedds.idl.types import float32, uint32

# 测试专用 domain 与 topic；只在本机回环上使用。
DOMAIN_ID = 199
TOPIC = "buqi/acceptance/state"

SEQ = 0x11223344
TEMPERATURE = 1.5


@dataclass
class AcceptanceState(IdlStruct, typename="AcceptanceState"):
    seq: uint32
    temperature: float32


# Zenoh 上的载荷是 RTPS SerializedPayload：4 字节表示头 + CDR 正文。
# little-endian CDR 的表示标识为 0x0001、选项为 0x0000，即 00 01 00 00；
# seq 是 uint32，temperature 是 float32，二者都不需要额外对齐填充。
CDR_LE_HEADER = bytes([0x00, 0x01, 0x00, 0x00])
EXPECTED_PAYLOAD = CDR_LE_HEADER + struct.pack("<If", SEQ, TEMPERATURE)
