"""最小订阅示例：连接网关并读取一条消息。

先以前台方式启动网关（``buqi server``），把 ENDPOINT 换成启动输出中的
``tcp/...`` 端点；消息 key 也按实际发布的 topic 修改。

运行（仓库根目录）::

    cd sdk/python && uv run python ../../examples/subscribe.py
"""

import buqi

ENDPOINT = "tcp/127.0.0.1:7447"
KEY_EXPR = "example/state"

with buqi.connect(ENDPOINT) as session:
    with session.declare_subscriber(KEY_EXPR) as subscriber:
        sample = subscriber.recv()
        print(sample.key_expr, sample.payload.to_bytes().hex())
