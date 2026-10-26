"""Score saved content and RTF attributes; workflow acceptance is separate."""

import json
import subprocess
from pathlib import Path

from ale_verify import write

TITLE = "Community Design Meetup"
BODY = [
    "Date: 12 November 2026",
    "Time: 15:30-17:00",
    "Location: Harbour Studio, Room 2",
    "Agenda",
    "• Welcome and introductions",
    "• Poster design demo",
    "• Questions and next steps",
    "Contact: Maya Chen | hello@example.org",
]


def score(lines):
    lines = [line for line in lines if line["text"].strip()]
    texts = [" ".join(line["text"].split()) for line in lines]

    def formatted(line, size, alignment, bold=None):
        styles = line["styles"]
        return bool(styles) and all(
            abs(style["size"] - size) <= 0.1
            and style["alignment"] == alignment
            and (bold is None or style["bold"] == bold)
            for style in styles
        )

    checks = {
        "readable_rtf": bool(lines),
        "title_text": bool(texts) and texts[0] == TITLE,
        "event_details": all(text in texts for text in (BODY[0], BODY[2])),
        "updated_time": BODY[1] in texts and not any("14:00" in text for text in texts),
        "agenda": all(text in texts for text in BODY[3:7]),
        "contact": BODY[7] in texts,
        "content_order": texts == [TITLE, *BODY],
        "title_format": bool(lines) and texts[0] == TITLE and formatted(lines[0], 24, 1, True),
        "body_format": len(lines) > 1 and all(formatted(line, 12, 0) for line in lines[1:]),
    }
    return {
        "overall": sum(checks.values()) / len(checks),
        "checks": {name: {"score": float(value)} for name, value in checks.items()},
    }


def main():
    path = Path("/Users/admin/output/notice.rtf")
    if not path.is_file() or not path.read_bytes().startswith(b"{\\rtf"):
        write(score([]))
        return
    # Parser/runtime failures are verification errors, not silently scored as agent failure.
    result = subprocess.run(
        [
            "/usr/bin/osascript",
            "-l",
            "JavaScript",
            str(Path(__file__).with_name("inspect.js")),
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    write(score(json.loads(result.stdout)))


if __name__ == "__main__":
    main()
