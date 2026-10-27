"""UI-only permission bootstrap prototype; no TCC writes, SIP changes or host UI."""

import json
import subprocess
import tempfile
import time
from pathlib import Path
from statistics import median

from PIL import Image

from ale.run.providers.macos_permission_policy import (
    RetryGuard,
    Row,
    add_target,
    capture_consent,
    gate_action,
    locate_toggle,
    pane_heading,
    permission_content,
    unique,
)

PANES = {
    "accessibility": ("Privacy_Accessibility", ["Accessibility", "辅助功能"]),
    "screen_recording": (
        "Privacy_ScreenCapture",
        [
            "Screen & System Audio Recording",
            "Screen Recording",
            "屏幕与系统音频录制",
            "屏幕录制",
        ],
    ),
}


class Bootstrap:
    def __init__(self, guest, vnc, ocr, password, timeout=120):
        self.guest, self.vnc, self.ocr, self.password = guest, vnc, ocr, password
        self.timeout = timeout
        self.events = []

    def read_rows(self, path):
        raw = json.loads(subprocess.check_output([str(self.ocr), str(path)], text=True, timeout=15))
        return [
            Row(**{k: r[k] for k in ("text", "x", "y", "w", "h")})
            for r in raw
            if r["confidence"] >= 0.6
        ]

    def region_rows(self, image, box, path):
        """OCR a larger view of a region, mapping boxes back to VM pixels."""
        left, top, right, bottom = map(int, box)
        if right <= left or bottom <= top:
            return []
        image.crop((left, top, right, bottom)).resize(
            ((right - left) * 3, (bottom - top) * 3)
        ).save(path)
        return [
            Row(r.text, left + r.x / 3, top + r.y / 3, r.w / 3, r.h / 3)
            for r in self.read_rows(path)
        ]

    def zoomed_add(self, image, content, heading, bottom, path):
        # Reuse OCR on an enlarged control column instead of encoding icon pixels.
        scale = heading.h / 16
        upper = [r for r in content if r.y < heading.y + 110 * scale]
        if not upper:
            return None
        left = max(0, int(min(r.x for r in upper) - 15 * scale))
        top = max(0, int(heading.y + 40 * scale))
        right = min(image.width, int(left + 90 * scale))
        bottom = min(image.height, int(bottom))
        if right <= left or bottom <= top:
            return None
        return add_target(self.region_rows(image, (left, top, right, bottom), path))

    def recover(self):
        # Close only the guest Settings UI, including stale sheets. Grants persist.
        # The next ensure() reopens the pane and re-observes all permissions.
        self.guest("/bin/sh", "-c", "/usr/bin/killall 'System Settings' || test $? = 1")
        self.record("flow", "restart_settings")
        time.sleep(2)

    def confirm_capture_ready(self, label, probe):
        """Require repeated Driver capture and stable VNC observations after consent."""
        deadline = time.monotonic() + self.timeout
        next_probe = 0.0
        ready = False
        clear_since = None
        clear_reads = successful_probes = 0
        guard = RetryGuard()
        with tempfile.TemporaryDirectory(prefix="permission-frame-") as td:
            screenshot = Path(td) / "frame.png"
            while time.monotonic() < deadline:
                if time.monotonic() >= next_probe:
                    ready = probe()
                    next_probe = time.monotonic() + 3
                    if ready:
                        successful_probes += 1
                    else:
                        clear_since = None
                        clear_reads = successful_probes = 0
                # Capture may return before macOS displays its consent dialog.
                time.sleep(1)
                self.vnc.capture(screenshot)
                rows = self.read_rows(screenshot)
                target = capture_consent(rows, label)
                if target is not None:
                    guard.take("allow_direct_capture", 3)
                    self.vnc.click(*target)
                    self.record("screen_recording", "allow_direct_capture")
                # Incomplete OCR or a different app's consent cannot establish readiness.
                pending = not rows or any(
                    r.text.casefold() == "open system settings"
                    or "private window picker" in r.text.casefold()
                    or "is requesting to" in r.text.casefold()
                    for r in rows
                )
                now = time.monotonic()
                if target is not None or pending or not ready:
                    clear_since = None
                    clear_reads = successful_probes = 0
                    ready = False
                    continue
                if clear_since is None:
                    clear_since = now
                clear_reads += 1
                if (
                    now < deadline
                    and now - clear_since >= 5
                    and clear_reads >= 3
                    and successful_probes >= 2
                ):
                    self.record("screen_recording", "capture_confirmed")
                    return
        raise RuntimeError("Driver capture consent did not become ready before the deadline")

    def ensure(self, app, label, permission):
        if not app.startswith("/Applications/") or not app.endswith(".app"):
            raise ValueError("Prototype accepts explicit /Applications/*.app bundles only")
        pane, titles = PANES[permission]
        # Caller selects VM explicitly. Verify target exists before any UI operation.
        self.guest("/bin/test", "-d", app)
        self.guest(
            "/usr/bin/open",
            f"x-apple.systempreferences:com.apple.preference.security?{pane}",
        )
        guard = RetryGuard()
        deadline = time.monotonic() + self.timeout
        stable = 0
        path_entered = False
        awaiting_path = False
        change_started = False
        event_start = len(self.events)
        with tempfile.TemporaryDirectory(prefix="permission-frame-") as td:
            screenshot = Path(td) / "frame.png"
            while time.monotonic() < deadline:
                time.sleep(1)
                self.vnc.capture(screenshot)
                rows = self.read_rows(screenshot)
                image = Image.open(screenshot).convert("RGB")
                action = gate_action(rows)
                if action == "authenticate":
                    stable = 0
                    if not change_started:
                        raise RuntimeError("Unexpected authentication dialog")
                    guard.take("authenticate", 1)
                    if not self.password:
                        raise RuntimeError("Guest administrator password required")
                    # The account is preselected by macOS. Never type into an unidentified field.
                    field = unique(rows, ["Password", "密码"])
                    self.vnc.click(*field.center)
                    self.vnc.type(self.password)
                    button = unique(rows, ["Modify Settings", "修改设置"])
                    self.vnc.click(*button.center)
                    self.record(permission, "authenticate")
                    time.sleep(5)
                    continue
                if action == "restart":
                    stable = 0
                    if not change_started:
                        raise RuntimeError("Unexpected restart dialog")
                    guard.take("restart", 1)
                    self.vnc.click(
                        *unique(rows, ["Quit & Reopen", "Quit and Reopen", "退出并重新打开"]).center
                    )
                    self.record(permission, "restart_application")
                    time.sleep(2)
                    self.guest(
                        "/usr/bin/open",
                        f"x-apple.systempreferences:com.apple.preference.security?{pane}",
                    )
                    continue
                # Go-to-path sheet takes precedence over underlying chooser.
                goto = unique(rows, ["Go to:", "前往\uff1a"])
                if goto:
                    stable = 0
                    if not awaiting_path:
                        raise RuntimeError("Unexpected path dialog")
                    guard.take("type_path", 1)
                    self.vnc.type(app[1:])  # '/' opened the sheet and is already present.
                    self.vnc.key("enter")
                    path_entered = True
                    awaiting_path = False
                    self.record(permission, "select_app_path")
                    time.sleep(2)
                    continue
                open_button = unique(rows, ["Open", "打开"])
                cancel = unique(rows, ["Cancel", "取消"])
                if open_button and cancel:
                    stable = 0
                    if not change_started:
                        raise RuntimeError("Unexpected file chooser")
                    if not path_entered:
                        guard.take("open_path_sheet", 1)
                        self.vnc.type("/")
                        awaiting_path = True
                        self.record(permission, "open_path_sheet")
                    else:
                        # Name must be visible and Open must be blue (selection is valid).
                        # The chooser can show the selected filename both in its
                        # list and preview. Either exact name is sufficient here.
                        selected = any(r.text.casefold() == label.casefold() for r in rows)
                        x, y = open_button.center
                        r, _g, b = image.getpixel((round(x - open_button.w / 2 - 8), round(y)))
                        if selected and b > 170 and b - r > 65:
                            guard.take("open_app", 1)
                            self.vnc.click(*open_button.center)
                            self.record(permission, "add_application")
                            time.sleep(2)
                    continue
                # Settings' menu title and pane heading must both be present.
                settings = unique([r for r in rows if r.y < 40], ["System Settings", "系统设置"])
                heading = pane_heading(rows, titles)
                if not settings or heading is None:
                    stable = 0
                    continue
                content = permission_content(rows, heading, permission)
                scale = median(r.h for r in rows if len(r.text) > 3) / 14
                row = unique(content, [label])
                if row is None:
                    # Settings lists have an icon column before their labels.
                    # Exclude that column, so OCR doesn't read logos as letters.
                    upper = [r for r in content if r.y < heading.y + 110 * scale]
                    if upper:
                        left = min(r.x for r in upper) + 30 * scale
                        audio = unique(rows, ["System Audio Recording Only", "仅系统音频录制"])
                        bottom = (
                            audio.y if permission == "screen_recording" and audio else image.height
                        )
                        labels = self.region_rows(
                            image,
                            (left, heading.y + 40 * scale, image.width, bottom),
                            Path(td) / "labels.png",
                        )
                        row = unique(labels, [label])
                if row:
                    # App-name OCR boxes may include the icon. Estimate display
                    # scale from many text lines rather than this single box.
                    toggle = locate_toggle(image, row, scale=scale)
                    if toggle is None:
                        stable = 0
                        continue
                    if toggle.enabled:
                        stable += 1
                        if stable >= 2:
                            self.record(permission, "ui_enabled")
                            return {
                                "permission": permission,
                                "status": "ui_enabled",
                                "capability_verified": False,
                                "events": self.events[event_start:],
                            }
                    else:
                        stable = 0
                        guard.take("enable_toggle", 1)
                        change_started = True
                        self.vnc.click(toggle.x, toggle.y)
                        self.record(permission, "enable_toggle")
                        time.sleep(2)
                else:
                    stable = 0
                    plus = add_target(content)
                    if plus is None:
                        audio = unique(rows, ["System Audio Recording Only", "仅系统音频录制"])
                        bottom = (
                            audio.y
                            if permission == "screen_recording" and audio
                            else heading.y + 550
                        )
                        plus = self.zoomed_add(
                            image, content, heading, bottom, Path(td) / "controls.png"
                        )
                    if plus:
                        guard.take("add_button", 1)
                        change_started = True
                        self.vnc.click(*plus)
                        self.record(permission, "add_button")
                        time.sleep(2)
            raise RuntimeError("Permission UI did not reach a verified state before deadline")

    def record(self, permission, event):
        self.events.append({"permission": permission, "event": event})
        print(json.dumps({"event": self.events[-1]}), flush=True)
