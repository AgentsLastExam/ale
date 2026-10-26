"""Local Orchard forwarding to an already enabled guest Screen Sharing service."""

import time
from pathlib import Path

from PIL import Image
from vncdotool import api
from vncdotool.keys import KEYMAP


class OrchardVNC:
    def __init__(self, host, port, username, password):
        if host != "127.0.0.1" or not 1 <= port <= 65535:
            raise ValueError("A loopback forwarding endpoint is required")
        self.client = api.connect(
            f"{host}::{port}", username=username, password=password, timeout=20
        )
        self._ready = False

    def capture(self, path: Path):
        self.client.mouseMove(0, 0)
        time.sleep(0.2)
        path.unlink(missing_ok=True)
        self.client.captureScreen(str(path))
        if not path.is_file():
            raise RuntimeError("VNC capture completed without an image file")
        with Image.open(path) as frame:
            if frame.convert("RGB").getbbox() is None:
                raise RuntimeError("VNC capture returned a black framebuffer")
        if not self._ready:
            # Bounded conservative settling delay observed during the local probe.
            # A first framebuffer alone does not establish input readiness.
            time.sleep(5)
            self._ready = True

    def click(self, x, y):
        self.client.mouseMove(round(x), round(y))
        time.sleep(0.2)
        self.client.mouseDown(1)
        time.sleep(0.12)
        self.client.mouseUp(1)
        time.sleep(0.2)

    def key(self, key):
        parts = [key] if len(key) == 1 else key.split("-")
        if any(len(p) != 1 and p not in KEYMAP for p in parts):
            raise ValueError("Unsupported key")
        self.client.keyPress(key)
        time.sleep(0.2)

    def type(self, text):
        if any(not 32 <= ord(c) <= 126 for c in text):
            raise ValueError("Only printable ASCII is supported")
        for char in text:
            self.key(char)

    def close(self):
        self.client.disconnect()
