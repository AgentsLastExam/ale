"""Finish a prepared Windows base offline, before compact.sh compresses it.

Run by compact.sh inside ALE's VM builder, next to ``framework_windows.py``, whose disk
helpers it uses. These steps need Windows not to be running: Tamper Protection keeps
Defender's services on while it runs, and it holds its page file open.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import framework_windows as disk

#: Defender, and the Security Center that would warn that it is off. A sandbox is
#: disposable and scanned by nothing that reads its verdicts; Defender only costs CPU.
SERVICES = (
    "WinDefend", "WdNisSvc", "WdNisDrv", "WdFilter", "WdBoot", "Sense",
    "SecurityHealthService", "wscsvc",
)  # fmt: skip
RUN = r"\Microsoft\Windows\CurrentVersion\Run"
#: Data a running Windows rebuilds or never reads back. The component store keeps what
#: servicing needs, so the last cumulative update's unpacked copy is not among it.
REMOVED = (
    "/ProgramData/Microsoft/Windows Defender/Definition Updates",
    "/ProgramData/Microsoft/Windows Defender/Platform",
    "/ProgramData/Microsoft/Windows Defender/Scans",
    "/pagefile.sys",
    "/swapfile.sys",
    "/hiberfil.sys",
    "/DumpStack.log",
    "/DumpStack.log.tmp",
)


def main(image: Path) -> int:
    try:
        with tempfile.TemporaryDirectory(prefix="ale-hives-") as temporary:
            hives = Path(temporary)
            for hive in ("SYSTEM", "SOFTWARE"):
                disk.guestfish(
                    image, f"download {disk.CONFIG}/{hive} {hives}/{hive}", readonly=True
                )
            control = disk.control_set(hives)
            present = [
                name
                for name in SERVICES
                if "[" in disk.export(hives, "SYSTEM", f"\\{control}\\Services\\{name}")
            ]
            disk.merge(
                hives / "SYSTEM",
                _registry(
                    f'[HKEY_LOCAL_MACHINE\\SYSTEM\\{control}\\Services\\{name}]\r\n"Start"=dword:00000004'
                    for name in present
                ),
            )
            if "SecurityHealth" in disk.values(disk.export(hives, "SOFTWARE", RUN), RUN):
                disk.merge(
                    hives / "SOFTWARE",
                    _registry([f'[HKEY_LOCAL_MACHINE\\SOFTWARE{RUN}]\r\n"SecurityHealth"=-']),
                )
            removals = "\n".join(f'rm-rf "{path}"' for path in REMOVED)
            disk.guestfish(
                image,
                f"""
                {removals}
                glob rm-rf /Windows/servicing/LCU/*
                upload {hives}/SYSTEM {disk.CONFIG}/SYSTEM
                upload {hives}/SOFTWARE {disk.CONFIG}/SOFTWARE
                zero-free-space /
                """,
            )
            print(f"disabled: {', '.join(present)}", flush=True)
    except disk.Lacks as reason:
        print(f"ALE: {reason}", file=sys.stderr)
        return 1
    return 0


def _registry(keys) -> str:  # type: ignore[no-untyped-def]
    return "\r\n\r\n".join(["Windows Registry Editor Version 5.00", *keys]) + "\r\n"


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
