"""发布固定载荷的模拟 DDS 端，用于端到端验收。

由 ``acceptance.py`` 启动，持续发布直到被终止；不连接实体机器人。
"""

import time

from cyclonedds.domain import DomainParticipant
from cyclonedds.pub import DataWriter
from cyclonedds.topic import Topic

from dds_state import DOMAIN_ID, SEQ, TEMPERATURE, TOPIC, AcceptanceState


def main() -> None:
    participant = DomainParticipant(DOMAIN_ID)
    topic = Topic(participant, TOPIC, AcceptanceState)
    writer = DataWriter(participant, topic)
    while True:
        writer.write(AcceptanceState(seq=SEQ, temperature=TEMPERATURE))
        time.sleep(0.1)


if __name__ == "__main__":
    main()
