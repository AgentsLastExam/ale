import pytest
from PIL import Image, ImageDraw

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


@pytest.mark.parametrize("scale", [1, 1.5, 2])
def test_switch_detection_is_relative_to_app_row(scale):
    for ox, oy in [(0, 0), (100, 140)]:
        for enabled in (True, False):
            image = Image.new("RGB", (900, 500), (248, 248, 248))
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle(
                (700 + ox, 100 + oy, 736 + ox, 116 + oy),
                8,
                fill=(0, 122, 255) if enabled else (220, 220, 220),
            )
            x = 718 + ox if enabled else 702 + ox
            draw.rounded_rectangle((x, 102 + oy, x + 14, 114 + oy), 6, fill="white")
            row = Row("CuaDriver", 400 + ox, 101 + oy, 80, 14)
            image = image.resize(
                (round(image.width * scale), round(image.height * scale)),
                Image.Resampling.NEAREST,
            )
            row = Row(row.text, row.x * scale, row.y * scale, row.w * scale, row.h * scale)
            toggle = locate_toggle(image, row)
            assert toggle.enabled == enabled
            assert abs(toggle.x - (718 + ox) * scale) <= 2 * scale


def test_unknown_and_ambiguous_switches_fail_closed():
    image = Image.new("RGB", (900, 500), "white")
    row = Row("ChatGPT", 400, 101, 80, 14)
    assert locate_toggle(image, row) is None
    draw = ImageDraw.Draw(image)
    for x in (650, 750):
        draw.rounded_rectangle((x, 100, x + 36, 116), 8, fill=(0, 122, 255))
    assert locate_toggle(image, row) is None


def test_icon_in_app_label_box_does_not_inflate_display_scale():
    image = Image.new("RGB", (900, 500), (248, 248, 248))
    ImageDraw.Draw(image).rounded_rectangle((700, 100, 736, 116), 8, fill=(0, 122, 255))
    row = Row("CuaDriver", 390, 96, 90, 24)
    assert locate_toggle(image, row, scale=1).enabled is True


def test_exact_name_does_not_match_other_app():
    rows = [
        Row("CuaDriver Helper", 100, 100, 100, 15),
        Row("CuaDriver", 100, 200, 80, 15),
    ]
    assert unique(rows, ["CuaDriver"]).y == 200
    assert unique(rows + rows, ["CuaDriver"]) is None


def test_authentication_requires_complete_dialog():
    def row(s):
        return Row(s, 100, 100, 100, 15)

    assert gate_action([row("Password")]) is None
    assert (
        gate_action([row("Password"), row("Modify Settings"), row("Privacy & Security")])
        == "authenticate"
    )
    assert gate_action([row("Quit & Reopen"), row("Later")]) == "restart"


def test_retries_are_bounded_and_do_not_repeatedly_enter_password():
    guard = RetryGuard()
    guard.take("authenticate", 1)
    with pytest.raises(RuntimeError):
        guard.take("authenticate", 1)
    for _ in range(2):
        guard.take("toggle", 2)
    with pytest.raises(RuntimeError):
        guard.take("toggle", 2)


def test_screen_capture_does_not_target_audio_only_section():
    heading = Row("Screen & System Audio Recording", 670, 100, 250, 14)
    rows = [
        Row("+-", 600, 230, 30, 14),
        Row("System Audio Recording Only", 600, 280, 200, 14),
        Row("ChatGPT", 640, 320, 60, 14),
        Row("+-", 600, 350, 30, 14),
    ]
    content = permission_content(rows, heading, "screen_recording")
    assert unique(content, ["ChatGPT"]) is None
    assert add_target(content) == (607, 237)


@pytest.mark.parametrize("scale,offset", [(1, 0), (1.5, 120), (2, 200)])
def test_sidebar_label_cannot_be_mistaken_for_loaded_pane(scale, offset):
    def row(text, x, y, w):
        return Row(text, x * scale + offset, y * scale + offset, w * scale, 14 * scale)

    sidebar = [row("Search", 300, 140, 45), row("Accessibility", 340, 224, 100)]
    assert pane_heading([*sidebar, row("General", 590, 90, 100)], ["Accessibility"]) is None
    header = row("Accessibility", 590, 90, 100)
    assert pane_heading([*sidebar, header], ["Accessibility"]) is header


def test_pane_title_takes_precedence_over_repeated_body_heading():
    search = Row("Search", 400, 160, 50, 14)
    title = Row("Screen Recording", 670, 106, 200, 16)
    body = Row("Screen Recording", 609, 146, 200, 14)
    assert pane_heading([search, title, body], ["Screen Recording"]) is title


def test_capture_consent_requires_exact_app_and_permission_text():
    rows = [
        Row("“CuaDriver” is requesting to", 610, 250, 200, 14),
        Row("bypass the system private window picker and directly", 610, 270, 200, 14),
        Row("access your screen and audio.", 610, 300, 200, 14),
        Row("Allow", 700, 420, 40, 14),
        Row("Open System Settings", 640, 460, 150, 14),
    ]
    assert capture_consent(rows, "CuaDriver") == (720, 427)
    assert capture_consent(rows, "Driver") is None
    assert capture_consent(rows, "ChatGPT") is None
    assert capture_consent(rows[1:], "CuaDriver") is None


def test_authentication_title_in_sidebar_is_not_a_second_modal():
    rows = [
        Row("Privacy & Security", 420, 690, 100, 14),
        Row("Privacy & Security", 610, 320, 100, 14),
        Row("Password", 610, 470, 70, 14),
        Row("Modify Settings", 670, 520, 100, 14),
    ]
    assert gate_action(rows) == "authenticate"


@pytest.mark.parametrize("text", ["Q Search", "⌕ Search", "Q 搜索"])
def test_search_icon_merged_with_placeholder(text):
    search = Row(text, 300, 140, 75, 14)
    title = Row("Accessibility", 590, 90, 100, 14)
    assert pane_heading([search, title], ["Accessibility"]) is title


def test_arbitrary_search_text_is_not_a_sidebar_anchor():
    title = Row("Accessibility", 590, 90, 100, 14)
    assert pane_heading([Row("Web Search", 300, 140, 75, 14), title], ["Accessibility"]) is None


def test_gray_switch_is_distinct_from_light_gray_panel_background():
    image = Image.new("RGB", (900, 500), (234, 234, 234))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((700, 100, 736, 116), 8, fill=(214, 214, 214))
    draw.rounded_rectangle((702, 102, 716, 114), 6, fill=(247, 247, 247))
    toggle = locate_toggle(image, Row("Example", 400, 101, 80, 14))
    assert toggle is not None and toggle.enabled is False


@pytest.mark.parametrize("scale", [1, 1.5, 2])
def test_permission_dialogs_follow_text_scale(scale):
    def row(text, x, y, w):
        return Row(text, x * scale, y * scale, w * scale, 14 * scale)

    auth = [
        row("Privacy & Security", 420, 690, 100),
        row("Privacy & Security", 610, 320, 100),
        row("Password", 610, 470, 70),
        row("Modify Settings", 670, 520, 100),
    ]
    assert gate_action(auth) == "authenticate"
    capture = [
        row("“Example” is requesting to", 610, 250, 200),
        row("bypass the system private window picker and directly", 610, 270, 200),
        row("access your screen and audio.", 610, 300, 200),
        row("Allow", 700, 420, 40),
        row("Open System Settings", 640, 460, 150),
    ]
    assert capture_consent(capture, "Example") == (720 * scale, 427 * scale)
