import os
from pathlib import Path

from ale_verify import write

# Notepad may save UTF-8 with a byte order mark and a final line break.
note = Path(os.environ["ALE_HOME"], "output", "result.txt")
text = note.read_text(encoding="utf-8-sig").strip() if note.is_file() else ""
write({"reward": float(text == "ALE station verified")})
