"""Conservative screenshot policy for macOS 26 English light-mode Settings."""

import re
from dataclasses import dataclass


@dataclass
class Row:
    text: str
    x: float
    y: float
    w: float
    h: float

    @property
    def center(self):
        return self.x + self.w / 2, self.y + self.h / 2


def unique(rows, labels):
    found = [r for r in rows if r.text.casefold() in {s.casefold() for s in labels}]
    return found[0] if len(found) == 1 else None


def pane_heading(rows, titles):
    """A pane title is above/right of sidebar search; sidebar items aren't titles."""
    # OCR may merge the magnifying-glass glyph with the placeholder. Accept
    # one leading glyph, while retaining uniqueness and pane-relative checks.
    searches = [
        r for r in rows if re.fullmatch(r"(?:\S\s+)?(?:Search|搜索)", r.text, re.IGNORECASE)
    ]
    if len(searches) != 1:
        return None
    search = searches[0]
    candidates = [
        r for r in rows if r.y < search.y and r.x > search.x + search.w and r.text in titles
    ]
    return min(candidates, key=lambda r: r.y) if candidates else None


def gate_action(rows):
    def has(labels):
        return unique(rows, labels) is not None

    modify = unique(rows, ["Modify Settings", "修改设置"])
    if modify:
        scale = modify.h / 14
        # Settings' sidebar can repeat the title of the authentication modal.
        modal = [
            r
            for r in rows
            if modify.x - 120 * scale < r.x < modify.x + modify.w + 120 * scale
            and modify.y - 250 * scale <= r.y <= modify.y
        ]
        if unique(modal, ["Password", "密码"]) and unique(
            modal, ["Privacy & Security", "隐私与安全性"]
        ):
            return "authenticate"
    if has(["Quit & Reopen", "Quit and Reopen", "退出并重新打开"]) and has(["Later", "稍后"]):
        return "restart"
    return None


def capture_consent(rows, label):
    """Recognize the first-use direct screen-capture prompt for this app only."""

    allow = unique(rows, ["Allow"])
    settings = unique(rows, ["Open System Settings"])
    if not allow or not settings:
        return None
    scale = allow.h / 14
    # Restrict text to the modal above these buttons, excluding other windows.
    text = " ".join(
        r.text
        for r in sorted(rows, key=lambda r: r.y)
        if allow.x - 120 * scale < r.x < allow.x + 150 * scale
        and allow.y - 240 * scale < r.y < allow.y
    )
    text = text.replace("“", '"').replace("”", '"')
    target = re.search(r'"' + re.escape(label) + r'"\s+is requesting to', text)
    if target and "private window picker" in text and "screen and audio" in text:
        return allow.center
    return None


class RetryGuard:
    def __init__(self):
        self.counts = {}

    def take(self, key, maximum):
        count = self.counts.get(key, 0)
        if count >= maximum:
            raise RuntimeError(f"Retry limit reached: {key}")
        self.counts[key] = count + 1


def permission_content(rows, heading, permission):
    end = float("inf")
    if permission == "screen_recording":
        audio = unique(rows, ["System Audio Recording Only", "仅系统音频录制"])
        if audio:
            end = audio.y
    return [r for r in rows if r.x > heading.x - 100 and heading.y + 20 < r.y < end]


def add_target(rows):
    plus = unique(rows, ["+", "+-", "+ -", "+\u2212", "+ \u2212"])
    if not plus:
        return None
    if plus.text == "+":
        return plus.center
    # OCR may merge the adjacent plus and minus controls. Click the first glyph.
    return plus.x + plus.h / 2, plus.y + plus.h / 2


@dataclass
class Toggle:
    x: float
    y: float
    enabled: bool


def locate_toggle(image, row, scale=None):
    """Find one blue/gray switch capsule to the right of the exact app label.

    Unknown themes, multiple switches and unrelated wide separators are rejected.
    Dimensions are relative to OCR line height, not screen coordinates.
    """
    image = image.convert("RGB")
    scale = max(0.75, min(2.5, row.h / 14 if scale is None else scale))
    left = max(0, int(row.x + row.w + 20 * scale))
    top = max(0, int(row.center[1] - 15 * scale))
    bottom = min(image.height, int(row.center[1] + 15 * scale))
    pixels = image.load()
    candidates = []
    for enabled in (True, False):

        def matches(rgb, enabled=enabled):
            r, g, b = rgb
            return (
                (b > 170 and b - r > 65 and b - g > 30)
                if enabled
                # Exclude the light-gray pane background, which can otherwise
                # join the switch through its antialiased edge.
                else (195 <= r <= 225 and max(rgb) - min(rgb) < 10)
            )

        points = {
            (x, y)
            for y in range(top, bottom)
            for x in range(left, image.width)
            if matches(pixels[x, y])
        }
        while points:
            start = points.pop()
            stack = [start]
            component = [start]
            while stack:
                x, y = stack.pop()
                for p in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                    if p in points:
                        points.remove(p)
                        stack.append(p)
                        component.append(p)
            xs = [p[0] for p in component]
            ys = [p[1] for p in component]
            w = max(xs) - min(xs) + 1
            h = max(ys) - min(ys) + 1
            if (
                26 * scale <= w <= 52 * scale
                and 10 * scale <= h <= 25 * scale
                and len(component) > 80 * scale * scale
            ):
                candidates.append(Toggle((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, enabled))
    return candidates[0] if len(candidates) == 1 else None
