from unittest.mock import Mock, call

import pytest
from PIL import Image

from ale.run.providers.macos_permission_vnc import OrchardVNC


def test_local_only():
    with pytest.raises(ValueError):
        OrchardVNC("192.0.2.1", 5900, "guest", "secret")


def test_literal_characters(monkeypatch):
    monkeypatch.setattr("ale.run.providers.macos_permission_vnc.time.sleep", lambda _: None)
    v = object.__new__(OrchardVNC)
    v.client = Mock()
    v.type("A_?")
    assert v.client.method_calls == [
        call.keyPress("A"),
        call.keyPress("_"),
        call.keyPress("?"),
    ]


def test_invalid_text_sends_nothing():
    v = object.__new__(OrchardVNC)
    v.client = Mock()
    with pytest.raises(ValueError):
        v.type("abc中文")
    assert not v.client.method_calls


def test_capture_keeps_connection_and_moves_pointer(monkeypatch, tmp_path):
    monkeypatch.setattr("ale.run.providers.macos_permission_vnc.time.sleep", lambda _: None)
    v = object.__new__(OrchardVNC)
    v.client = Mock()
    v._ready = True
    p = tmp_path / "frame.png"
    v.client.captureScreen.side_effect = lambda target: Image.new("RGB", (2, 2), "white").save(
        target
    )
    v.capture(p)
    assert v.client.method_calls == [call.mouseMove(0, 0), call.captureScreen(str(p))]


def test_capture_requires_created_file(monkeypatch, tmp_path):
    monkeypatch.setattr("ale.run.providers.macos_permission_vnc.time.sleep", lambda _: None)
    v = object.__new__(OrchardVNC)
    v.client = Mock()
    v._ready = True
    with pytest.raises(RuntimeError):
        v.capture(tmp_path / "missing.png")


def test_black_frame_fails_without_substituting_an_image(monkeypatch, tmp_path):
    monkeypatch.setattr("ale.run.providers.macos_permission_vnc.time.sleep", lambda _: None)
    v = object.__new__(OrchardVNC)
    v.client = Mock()
    v._ready = True
    p = tmp_path / "frame.png"
    v.client.captureScreen.side_effect = lambda target: Image.new("RGB", (2, 2)).save(target)
    with pytest.raises(RuntimeError, match="black framebuffer"):
        v.capture(p)
    with Image.open(p) as frame:
        assert frame.getbbox() is None
