"""测试用最小通道插件（UMP over stdio）。

放在单独文件里而不是测试字符串里：省得跟多层引号 / 转义打架（这是踩过的坑）。
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def send(frame: dict) -> None:
    sys.stdout.write(json.dumps(frame, ensure_ascii=False) + "\n")
    sys.stdout.flush()


hello = {
    "ump": "1.0",
    "type": "hello",
    "id": "p-1",
    "ts": 0,
    "payload": {
        "channel": {"id": os.environ["ISEKAI_PLUGIN_ID"], "name": "回显插件", "version": "0.1.0"},
        "capabilities": {"segments": True, "status": True},
        "auth": {"credential": os.environ["ISEKAI_PLUGIN_CREDENTIAL"]},
    },
}
send(hello)

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    frame = json.loads(line)
    with open(os.path.join(HERE, "frames.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    if frame.get("type") == "hello_ack":
        with open(os.path.join(HERE, "seen.json"), "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "state": frame["payload"].get("state"),
                    "env_keys": sorted(os.environ.keys()),
                    "leaked": [
                        key
                        for key in os.environ
                        if any(token in key for token in ("TOKEN", "KEY", "SECRET"))
                    ],
                },
                fh,
            )
