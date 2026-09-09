#!/usr/bin/env python3
"""Environment doctor: prove this checkout behaves exactly like main.

Standard library only, so it runs before (and independently of) `uv sync`.

Every check prints one line and, on failure, the exact command that fixes it. Hard
failures exit non-zero; advisory findings are warnings. The goal is that nobody ever
debugs a "works on main but not in my worktree" problem by hand again.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

OK, WARN, FAIL = "ok", "warn", "fail"

_GLYPH = {OK: "\033[32m✓\033[0m", WARN: "\033[33m!\033[0m", FAIL: "\033[31m✗\033[0m"}


@dataclass
class Result:
    status: str
    name: str
    detail: str
    fix: str = ""


def _run(*argv: str, cwd: Path | None = None) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, cwd=cwd, timeout=120, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:  # pragma: no cover - defensive
        return 1, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def repo_root() -> Path:
    code, out = _run("git", "rev-parse", "--show-toplevel")
    return Path(out) if code == 0 else Path.cwd()


def main_root(root: Path) -> Path:
    code, out = _run("git", "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(out).parent if code == 0 else root


def check_tooling() -> list[Result]:
    results: list[Result] = []
    for tool, fix, hard in (
        ("uv", "curl -LsSf https://astral.sh/uv/install.sh | sh", True),
        ("just", "uv tool install rust-just", False),
        ("gh", "see https://cli.github.com/", False),
        ("git", "install git", True),
    ):
        path = shutil.which(tool)
        if path:
            code, out = _run(tool, "--version")
            results.append(Result(OK, tool, out.splitlines()[0] if code == 0 else path))
        else:
            results.append(Result(FAIL if hard else WARN, tool, "not found", fix))
    return results


def check_python(root: Path) -> list[Result]:
    results: list[Result] = []
    pin_file = root / ".python-version"
    pinned = pin_file.read_text().strip() if pin_file.exists() else ""
    venv_python = root / ".venv" / "bin" / "python"
    if not venv_python.exists():
        results.append(Result(FAIL, "venv", "missing .venv", "just bootstrap"))
        return results
    _, out = _run(str(venv_python), "--version")
    version = out.replace("Python ", "").strip()
    if pinned and not version.startswith(pinned):
        results.append(
            Result(
                FAIL,
                "python",
                f".venv has {version}, .python-version pins {pinned}",
                "rm -rf .venv && just bootstrap",
            )
        )
    else:
        results.append(Result(OK, "python", f"{version} (pinned {pinned or 'unset'})"))
    return results


def check_lock(root: Path) -> list[Result]:
    if not (root / "uv.lock").exists():
        return [Result(FAIL, "uv.lock", "missing", "uv lock && git add uv.lock")]
    code, _ = _run("uv", "lock", "--check", cwd=root)
    if code != 0:
        return [
            Result(
                FAIL,
                "uv.lock",
                "out of sync with pyproject.toml",
                "uv lock  (then commit uv.lock)",
            )
        ]
    return [Result(OK, "uv.lock", "in sync")]


def check_caches(root: Path) -> list[Result]:
    results: list[Result] = []

    code, out = _run("uv", "cache", "dir")
    uv_cache = Path(out) if code == 0 and out else Path.home() / ".cache" / "uv"
    # Hardlinked installs (the reason a fresh worktree is fast) require the cache and
    # the checkout to live on the same filesystem.
    try:
        same_fs = uv_cache.exists() and os.stat(uv_cache).st_dev == os.stat(root).st_dev
    except OSError:
        same_fs = False
    if uv_cache.exists() and same_fs:
        results.append(Result(OK, "uv cache", f"{uv_cache} (same filesystem, hardlinks)"))
    elif uv_cache.exists():
        results.append(
            Result(
                WARN,
                "uv cache",
                f"{uv_cache} is on another filesystem — installs copy instead of linking",
                "point UV_CACHE_DIR at a directory on this filesystem",
            )
        )
    else:
        results.append(Result(WARN, "uv cache", f"{uv_cache} not created yet", "just bootstrap"))

    ale_cache = Path(
        os.environ.get("ALE_CACHE_DIR")
        or Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ale"
    )
    try:
        ale_cache.mkdir(parents=True, exist_ok=True)
        probe = ale_cache / ".doctor-probe"
        probe.write_text("ok")
        probe.unlink()
        results.append(Result(OK, "ale cache", f"{ale_cache} (shared, writable)"))
    except OSError as exc:
        results.append(
            Result(
                FAIL,
                "ale cache",
                f"{ale_cache} not writable: {exc}",
                "fix permissions or set ALE_CACHE_DIR",
            )
        )
    return results


def check_secrets(root: Path) -> list[Result]:
    """Credentials live in the checkout, so each tree can target its own provider."""
    path = Path(os.environ.get("ALE_ENV_FILE") or root / ".env")
    if not path.exists():
        return [
            Result(
                WARN,
                "secrets",
                f"{path} missing — live agent runs and gated assets will fail",
                "cp .env.example .env  (then fill in ANTHROPIC_API_KEY)",
            )
        ]
    keys = {
        line.split("=", 1)[0].strip()
        for line in path.read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    missing = sorted({"ANTHROPIC_API_KEY"} - {k for k in keys if k})
    if missing:
        return [
            Result(WARN, "secrets", f"{path} lacks {', '.join(missing)}", f"add them to {path}")
        ]
    return [Result(OK, "secrets", f"{path} ({len(keys)} keys)")]


def _group_membership_pending(group: str) -> bool:
    """Whether the user belongs to ``group`` on disk but not in this process."""
    code, out = _run("getent", "group", group)
    if code != 0 or ":" not in out:
        return False
    members = out.rsplit(":", 1)[-1].split(",")
    user = os.environ.get("USER", "")
    if user not in members:
        return False
    _, current = _run("id", "-nG")
    return group not in current.split()


def check_sandbox_backends() -> list[Result]:
    results: list[Result] = []

    runtime = next((r for r in ("docker", "podman") if shutil.which(r)), None)
    if runtime is None:
        container_status = WARN if sys.platform == "darwin" else FAIL
        container_fix = (
            "install Docker Desktop to enable the optional container provider"
            if sys.platform == "darwin"
            else "install Docker Engine: https://docs.docker.com/engine/install/"
        )
        results.append(
            Result(
                container_status,
                "container runtime",
                "neither docker nor podman found — the container backend cannot run",
                container_fix,
            )
        )
    else:
        code, out = _run(runtime, "info", "--format", "{{.ServerVersion}}")
        if code == 0:
            results.append(Result(OK, "container runtime", f"{runtime} {out.strip()}"))
        elif _group_membership_pending(runtime):
            # Classic footgun: usermod succeeded, but this shell predates it.
            results.append(
                Result(
                    FAIL,
                    "container runtime",
                    f"you are in the '{runtime}' group, but this shell started before that",
                    f"log out and back in, or run commands as: sg {runtime} -c '<cmd>'",
                )
            )
        else:
            results.append(
                Result(
                    WARN if sys.platform == "darwin" else FAIL,
                    "container runtime",
                    f"{runtime} present but daemon unreachable",
                    (
                        "start Docker Desktop"
                        if sys.platform == "darwin"
                        else f"sudo systemctl start {runtime} && sudo usermod -aG {runtime} $USER"
                    ),
                )
            )

    if sys.platform == "darwin":
        apple_silicon = platform.machine().lower() in {"arm64", "aarch64"}
        cache = Path(
            os.environ.get("ALE_CACHE_DIR")
            or Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ale"
        )
        qemu_root = Path(
            os.environ.get("ALE_DARWIN_QEMU_ROOT") or cache / "darwin-qemu" / "v10.0.2-utm"
        )
        pinned_arm_qemu = qemu_root / "bin" / "qemu-system-aarch64-utm"
        native_qemu = str(pinned_arm_qemu) if apple_silicon else "qemu-system-x86_64"
        code, accelerators = _run(native_qemu, "-accel", "help")
        if (
            (Path(native_qemu).is_file() or shutil.which(native_qemu))
            and code == 0
            and ("hvf" in accelerators)
        ):
            results.append(Result(OK, "hvf", "Hypervisor.framework available to QEMU"))
        else:
            results.append(
                Result(
                    WARN,
                    "hvf",
                    f"{native_qemu} cannot use Hypervisor.framework",
                    "scripts/build-darwin-qemu.sh" if apple_silicon else "brew install qemu",
                )
            )
        if apple_silicon:
            code, devices = _run(str(pinned_arm_qemu), "-device", "help")
            if code == 0 and 'name "virtio-ramfb"' in devices:
                results.append(Result(OK, "qemu-system-aarch64-utm", str(pinned_arm_qemu)))
            else:
                results.append(
                    Result(
                        WARN,
                        "qemu-system-aarch64-utm",
                        "pinned ARM QEMU is missing or lacks virtio-ramfb",
                        "scripts/build-darwin-qemu.sh",
                    )
                )
        qemu_tools = ("qemu-system-x86_64", "qemu-img")
        qemu_fix = "brew install qemu"
    else:
        kvm = Path("/dev/kvm")
        if not kvm.exists():
            results.append(
                Result(
                    WARN,
                    "kvm",
                    "/dev/kvm missing — the qemu backend is unavailable",
                    "enable virtualisation in BIOS, or use --provider docker",
                )
            )
        elif not os.access(kvm, os.R_OK | os.W_OK):
            results.append(
                Result(
                    WARN,
                    "kvm",
                    "/dev/kvm not accessible",
                    "sudo usermod -aG kvm $USER && re-login",
                )
            )
        else:
            results.append(Result(OK, "kvm", "/dev/kvm accessible"))
        qemu_tools = ("qemu-system-x86_64", "qemu-img")
        qemu_fix = "sudo apt install qemu-system-x86 qemu-utils"

    for tool in qemu_tools:
        found = shutil.which(tool)
        if found is None:
            results.append(
                Result(
                    WARN,
                    tool,
                    "not found — qemu backend unavailable",
                    qemu_fix,
                )
            )
        else:
            results.append(Result(OK, tool, found))
    return results


def check_worktree(root: Path, main: Path) -> list[Result]:
    if root == main:
        return [Result(OK, "worktree", f"main checkout at {root}")]
    expected_parent = main.parent / "ale-worktrees"
    if root.parent != expected_parent:
        return [
            Result(
                WARN,
                "worktree",
                f"{root} is outside {expected_parent}",
                f"just wt <name> creates worktrees in {expected_parent}",
            )
        ]
    return [Result(OK, "worktree", f"{root.name} under ale-worktrees/")]


def main() -> int:
    root = repo_root()
    main_dir = main_root(root)

    results: list[Result] = []
    results += check_tooling()
    results += check_worktree(root, main_dir)
    results += check_python(root)
    results += check_lock(root)
    results += check_caches(root)
    results += check_secrets(root)
    results += check_sandbox_backends()

    width = max(len(r.name) for r in results)
    print(f"ale doctor — {root}\n")
    for r in results:
        print(f"  {_GLYPH[r.status]} {r.name.ljust(width)}  {r.detail}")
        if r.fix:
            print(f"    {' ' * width}  fix: {r.fix}")

    failures = [r for r in results if r.status == FAIL]
    warnings = [r for r in results if r.status == WARN]
    print(
        f"\n{len(results) - len(failures) - len(warnings)} ok, "
        f"{len(warnings)} warning(s), {len(failures)} failure(s)"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
