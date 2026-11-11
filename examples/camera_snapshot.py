"""相机图片通道示例：单帧查询与持续观测。

先启用媒体模块启动网关（配置见 ``examples/buqi-camera.toml``），
把 ENDPOINT 换成启动输出中的 ``tcp/...`` 端点，CAMERA 换成配置里的相机 id。

运行（仓库根目录）::

    cd sdk/python && uv run python ../../examples/camera_snapshot.py

示例只打印元数据与 JPEG 大小，不写文件、不解码图片。
"""

import buqi

ENDPOINT = "tcp/127.0.0.1:7447"
CAMERA = "pc2-rgb"

with buqi.connect(ENDPOINT) as session:
    frame = buqi.snapshot(session, CAMERA, timeout=1.0)
    print(
        "snapshot:",
        frame.metadata.session,
        frame.metadata.sequence,
        frame.metadata.width,
        frame.metadata.height,
        len(frame.jpeg),
        "bytes",
    )

    with buqi.frames(session, CAMERA) as stream:
        for _ in range(3):
            frame = stream.next(timeout=1.0)
            print("observation:", frame.metadata.sequence, len(frame.jpeg), "bytes")
