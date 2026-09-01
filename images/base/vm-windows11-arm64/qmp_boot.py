#!/usr/bin/env python3
"""Send the UEFI and optical-boot keys through a headless QMP connection."""

from __future__ import annotations

import argparse
import json
import socket
import time
from pathlib import Path
from typing import BinaryIO


def response(stream: BinaryIO) -> dict[str, object]:
    while True:
        line = stream.readline()
        if not line:
            raise RuntimeError("QMP closed unexpectedly")
        message = json.loads(line)
        if "event" not in message:
            return message


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("socket", type=Path)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--boot-device", default="ale-boot-device")
    args = parser.parse_args()

    deadline = time.monotonic() + 10
    connection = socket.socket(socket.AF_UNIX)
    while True:
        try:
            connection.connect(str(args.socket))
            break
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)

    with connection, connection.makefile("rwb", buffering=0) as stream:
        greeting = response(stream)
        if "QMP" not in greeting:
            raise RuntimeError("QMP did not send a greeting")
        stream.write(b'{"execute":"qmp_capabilities"}\n')
        capabilities = response(stream)
        if "error" in capabilities:
            raise RuntimeError(f"QMP capabilities failed: {capabilities['error']}")

        deadline = time.monotonic() + args.seconds
        command = (
            json.dumps(
                {
                    "execute": "send-key",
                    "arguments": {"keys": [{"type": "qcode", "data": "x"}]},
                },
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        while time.monotonic() < deadline:
            stream.write(command)
            result = response(stream)
            if "error" in result:
                raise RuntimeError(f"QMP send-key failed: {result['error']}")
            time.sleep(0.5)

        eject = (
            json.dumps(
                {
                    "execute": "device_del",
                    "arguments": {"id": args.boot_device},
                },
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        stream.write(eject)
        result = response(stream)
        if "error" in result:
            raise RuntimeError(f"QMP boot-device removal failed: {result['error']}")


if __name__ == "__main__":
    main()
