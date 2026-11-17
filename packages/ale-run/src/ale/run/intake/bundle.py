"""Archive unpacking, sealed bundle verification, and OpenRSI-to-ALE staging."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import tomllib
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import zstandard
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ale.core.errors import TaskDefinitionError
from ale.core.ids import TaskId
from ale.core.taskspec import SCHEMA_VERSION as ALE_SCHEMA_VERSION
from ale.core.taskspec import TaskConfig

__all__ = [
    "MAX_ARCHIVE_FILES",
    "MAX_ARCHIVE_UNCOMPRESSED_BYTES",
    "OPENRSI_SCHEMA_VERSION",
    "StagedTask",
    "UnpackedBundle",
    "stage_task_for_ale",
    "unpack_archive",
    "verify_unpacked_bundle",
]

OPENRSI_SCHEMA_VERSION = "1.4+ale+openrsi"
MAX_ARCHIVE_FILES = 2_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
_COPY_CHUNK = 65_536
_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
_BARE_TOML_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_BRIDGE_MARKER = "# ALE reward.txt -> reward.json bridge"


class _ArtifactFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    kind: Literal["file", "directory"]
    mode: int = Field(ge=0, le=0o777)
    size: int = Field(ge=0)
    digest: str = Field(pattern=_DIGEST_PATTERN)

    # --- validation ---

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in value
            or ":" in value
            or not path.parts
            or path.as_posix() != value
        ):
            raise ValueError("artifact paths must be canonical relative paths")
        return value


class _OfflineCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    status: Literal["passed", "failed", "pending"]
    summary: str
    stdout: str = ""
    stderr: str = ""


class _OfflineReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    task_digest: str = Field(pattern=_DIGEST_PATTERN)
    checks: tuple[_OfflineCheck, ...] = Field(min_length=1)
    execution_verified: Literal[False] = False

    @property
    def passed(self) -> bool:
        return all(check.status != "failed" for check in self.checks)


class _TaskArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    task_name: str = Field(min_length=1)
    created_at: datetime
    source_id: str
    task_digest: str = Field(pattern=_DIGEST_PATTERN)
    files: tuple[_ArtifactFile, ...] = Field(min_length=1)
    offline_report_digest: str = Field(pattern=_DIGEST_PATTERN)
    guidance_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)


@dataclass(frozen=True)
class UnpackedBundle:
    """Verified task content unpacked from an intake bundle archive."""

    bundle_root: Path
    task_dir: Path
    sealed: bool
    task_name: str
    task_digest: str | None
    source_id: str | None
    offline_report: dict[str, Any] | None


@dataclass(frozen=True)
class StagedTask:
    """Runnable task directory prepared for ALE execution."""

    path: Path
    task_id: str
    schema_version: str
    adapted: bool
    notices: tuple[str, ...]


def _validate_member_path(raw_name: str) -> PurePosixPath | None:
    if "\\" in raw_name or any(ord(ch) < 32 or ord(ch) == 127 for ch in raw_name):
        raise TaskDefinitionError(f"unsafe archive entry name: {raw_name!r}")
    cleaned = raw_name.strip()
    if cleaned.startswith("/") or ":" in cleaned:
        raise TaskDefinitionError(f"unsafe archive path: {raw_name!r}")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    cleaned = cleaned.strip("/")
    if not cleaned or cleaned == ".":
        return None
    posix = PurePosixPath(cleaned)
    if ".." in posix.parts:
        raise TaskDefinitionError(f"unsafe archive path traversal: {raw_name!r}")
    return posix


def _safe_target(destination: Path, rel: PurePosixPath) -> Path:
    root = destination.resolve()
    target = (root / Path(*rel.parts)).resolve()
    if target != root and not target.is_relative_to(root):
        raise TaskDefinitionError(f"archive entry escapes destination: {rel.as_posix()}")
    return target


def _write_stream_checked(
    src: Any,
    target: Path,
    *,
    running_bytes: int,
    max_bytes: int,
) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with target.open("wb") as out:
        while chunk := src.read(_COPY_CHUNK):
            written += len(chunk)
            if running_bytes + written > max_bytes:
                raise TaskDefinitionError("archive exceeds uncompressed size limit")
            out.write(chunk)
    return written


def _extract_zip(archive_path: Path, destination: Path) -> int:
    total_files = 0
    total_bytes = 0
    with zipfile.ZipFile(archive_path) as zf:
        for info in zf.infolist():
            rel = _validate_member_path(info.filename)
            if rel is None:
                continue
            unix_mode = info.external_attr >> 16
            file_type = stat.S_IFMT(unix_mode)
            if stat.S_ISLNK(unix_mode) or (
                file_type and file_type not in {stat.S_IFREG, stat.S_IFDIR}
            ):
                raise TaskDefinitionError(
                    f"archive contains unsupported entry type: {info.filename}"
                )
            target = _safe_target(destination, rel)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            total_files += 1
            if total_files > MAX_ARCHIVE_FILES:
                raise TaskDefinitionError("archive exceeds maximum file count")
            if info.file_size > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise TaskDefinitionError("archive entry exceeds uncompressed size limit")
            with zf.open(info, "r") as src:
                total_bytes += _write_stream_checked(
                    src,
                    target,
                    running_bytes=total_bytes,
                    max_bytes=MAX_ARCHIVE_UNCOMPRESSED_BYTES,
                )
            if os.name != "nt" and ((unix_mode & 0o111) or target.suffix == ".sh"):
                target.chmod(0o755)
    return total_files


def _extract_tar_stream(tf: tarfile.TarFile, destination: Path) -> int:
    total_files = 0
    total_bytes = 0
    for member in tf:
        rel = _validate_member_path(member.name)
        if rel is None:
            continue
        if member.issym() or member.islnk():
            raise TaskDefinitionError(f"archive contains symlink or hardlink: {member.name}")
        target = _safe_target(destination, rel)
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not member.isfile():
            raise TaskDefinitionError(f"archive contains unsupported special file: {member.name}")
        total_files += 1
        if total_files > MAX_ARCHIVE_FILES:
            raise TaskDefinitionError("archive exceeds maximum file count")
        if member.size > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            raise TaskDefinitionError("archive entry exceeds uncompressed size limit")
        extracted = tf.extractfile(member)
        if extracted is None:
            continue
        with extracted:
            total_bytes += _write_stream_checked(
                extracted,
                target,
                running_bytes=total_bytes,
                max_bytes=MAX_ARCHIVE_UNCOMPRESSED_BYTES,
            )
        if os.name != "nt" and ((member.mode & 0o111) or target.suffix == ".sh"):
            target.chmod(0o755)
    return total_files


def unpack_archive(archive_path: Path, destination: Path) -> Path:
    """Safely unpack ``archive_path`` into ``destination`` with traversal and size guards."""
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    name = archive_path.name.lower()
    try:
        if zipfile.is_zipfile(archive_path):
            count = _extract_zip(archive_path, destination)
        elif name.endswith((".tar.zst", ".tzst")):
            dctx = zstandard.ZstdDecompressor()
            with (
                archive_path.open("rb") as raw,
                dctx.stream_reader(raw) as reader,
                tarfile.open(fileobj=reader, mode="r|") as tf,
            ):
                count = _extract_tar_stream(tf, destination)
        else:
            with tarfile.open(archive_path, "r:*") as tf:
                count = _extract_tar_stream(tf, destination)
    except TaskDefinitionError:
        raise
    except (OSError, zipfile.BadZipFile, tarfile.TarError, zstandard.ZstdError) as exc:
        raise TaskDefinitionError(f"invalid task archive {archive_path.name}: {exc}") from exc

    if count == 0:
        raise TaskDefinitionError("task archive contains no files")
    return destination


def _digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _file_digest(path: Path) -> str:
    with path.open("rb") as handle:
        return "sha256:" + hashlib.file_digest(handle, "sha256").hexdigest()


def _tree_digest(entries: tuple[_ArtifactFile, ...]) -> str:
    encoded = json.dumps([entry.model_dump() for entry in entries], sort_keys=True).encode("utf-8")
    return _digest_bytes(encoded)


def _locate_bundle_root(root: Path) -> tuple[Path, bool]:
    entries = list(root.iterdir())
    names = {entry.name for entry in entries}
    if names == {"artifact.json", "offline-report.json", "task"}:
        return root, True
    if "task.toml" in names:
        return root, False
    if len(entries) == 1 and entries[0].is_dir() and not entries[0].is_symlink():
        inner = entries[0]
        inner_names = {entry.name for entry in inner.iterdir()}
        if inner_names == {"artifact.json", "offline-report.json", "task"}:
            return inner, True
        if "task.toml" in inner_names:
            return inner, False
    raise TaskDefinitionError(
        "unpacked bundle must be a sealed artifact (artifact.json, offline-report.json, task/) "
        "or a task folder containing task.toml"
    )


def _verify_sealed_tree(task_dir: Path, artifact: _TaskArtifact) -> None:
    if task_dir.is_symlink() or not task_dir.is_dir():
        raise TaskDefinitionError("sealed bundle task/ directory is missing or unsafe")
    expected_modes = {item.path: item.mode for item in artifact.files}
    observed: list[_ArtifactFile] = []
    for item in sorted(task_dir.rglob("*")):
        st = item.lstat()
        if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
            raise TaskDefinitionError(f"task tree contains symlink or special file: {item}")
        rel = item.relative_to(task_dir).as_posix()
        is_dir = stat.S_ISDIR(st.st_mode)
        if os.name == "nt":
            # NTFS lacks POSIX permission bits; keep recorded mode while verifying file metadata.
            mode = expected_modes.get(rel, 0o755 if is_dir else 0o644)
        else:
            mode = stat.S_IMODE(st.st_mode) & 0o777
        observed.append(
            _ArtifactFile(
                path=rel,
                kind="directory" if is_dir else "file",
                mode=mode,
                size=0 if is_dir else st.st_size,
                digest=_digest_bytes(b"") if is_dir else _file_digest(item),
            )
        )
    observed_tuple = tuple(observed)
    if observed_tuple != artifact.files or _tree_digest(observed_tuple) != artifact.task_digest:
        raise TaskDefinitionError("sealed task files, modes, or digests do not match artifact.json")


def verify_unpacked_bundle(root: Path) -> UnpackedBundle:
    """Validate an unpacked sealed Brewer bundle (or raw task directory) on disk."""
    bundle_root, sealed = _locate_bundle_root(root)
    if not sealed:
        task_toml = bundle_root / "task.toml"
        if not task_toml.is_file():
            raise TaskDefinitionError("task.toml is missing from task bundle")
        try:
            raw = tomllib.loads(task_toml.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise TaskDefinitionError(f"invalid task.toml: {exc}") from exc
        raw_name = str((raw.get("task") or {}).get("name") or bundle_root.name)
        return UnpackedBundle(
            bundle_root=bundle_root,
            task_dir=bundle_root,
            sealed=False,
            task_name=raw_name,
            task_digest=None,
            source_id=None,
            offline_report=None,
        )

    try:
        artifact = _TaskArtifact.model_validate_json(
            (bundle_root / "artifact.json").read_text(encoding="utf-8")
        )
        report_path = bundle_root / "offline-report.json"
        report = _OfflineReport.model_validate_json(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TaskDefinitionError(f"invalid sealed artifact metadata: {exc}") from exc

    if _file_digest(report_path) != artifact.offline_report_digest:
        raise TaskDefinitionError("offline-report.json digest does not match artifact.json")
    if report.task_digest != artifact.task_digest:
        raise TaskDefinitionError("offline-report.json task_digest does not match artifact.json")
    if not report.passed:
        failed = ", ".join(c.name for c in report.checks if c.status == "failed")
        raise TaskDefinitionError(f"sealed bundle carries failed offline checks: {failed}")

    task_dir = bundle_root / "task"
    _verify_sealed_tree(task_dir, artifact)
    if not (task_dir / "task.toml").is_file():
        raise TaskDefinitionError("task.toml is required by ALE; task.yaml tasks are not runnable")

    return UnpackedBundle(
        bundle_root=bundle_root,
        task_dir=task_dir,
        sealed=True,
        task_name=artifact.task_name,
        task_digest=artifact.task_digest,
        source_id=artifact.source_id,
        offline_report=json.loads(report.model_dump_json()),
    )


def _toml_key(key: str) -> str:
    return key if _BARE_TOML_KEY.fullmatch(key) else json.dumps(key, ensure_ascii=False)


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (datetime, date, time)):
        return json.dumps(value.isoformat(), ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        if any(isinstance(item, dict) for item in value):
            raise ValueError("array of tables must be emitted as table blocks")
        return "[" + ", ".join(_toml_scalar(item) for item in value if item is not None) + "]"
    raise TypeError(f"unsupported TOML value type: {type(value).__name__}")


def _emit_toml_table(lines: list[str], table: dict[str, Any], prefix: tuple[str, ...]) -> None:
    scalars: list[tuple[str, Any]] = []
    subtables: list[tuple[str, dict[str, Any]]] = []
    array_tables: list[tuple[str, list[dict[str, Any]]]] = []
    for key, value in table.items():
        if value is None:
            continue
        if isinstance(value, dict):
            subtables.append((key, value))
        elif isinstance(value, (list, tuple)) and value and all(isinstance(v, dict) for v in value):
            array_tables.append((key, list(value)))
        else:
            scalars.append((key, value))
    if prefix and (scalars or not subtables):
        lines.append("[" + ".".join(_toml_key(part) for part in prefix) + "]")
    for key, value in scalars:
        lines.append(f"{_toml_key(key)} = {_toml_scalar(value)}")
    if prefix and (scalars or not subtables):
        lines.append("")
    for key, items in array_tables:
        header = "[[" + ".".join(_toml_key(part) for part in (*prefix, key)) + "]]"
        for item in items:
            lines.append(header)
            _emit_toml_table(lines, item, ())
            lines.append("")
    for key, child in subtables:
        _emit_toml_table(lines, child, (*prefix, key))


def _dump_toml(data: dict[str, Any]) -> str:
    lines: list[str] = []
    _emit_toml_table(lines, data, ())
    rendered = (
        "\n".join(
            line
            for idx, line in enumerate(lines)
            if not (line == "" and (idx == 0 or lines[idx - 1] == ""))
        ).rstrip()
        + "\n"
    )
    tomllib.loads(rendered)
    return rendered


def _check_supported_rsi_fields(rsi: Any) -> None:
    if not isinstance(rsi, dict):
        return
    evaluation = rsi.get("evaluation")
    if isinstance(evaluation, dict):
        max_sub = evaluation.get("max_submissions")
        if max_sub is not None and max_sub != 1:
            raise TaskDefinitionError(
                "rsi.evaluation.max_submissions != 1 is not supported by ALE yet"
            )
        direction = evaluation.get("score_direction")
        if direction is not None and direction != "maximize":
            raise TaskDefinitionError(
                f"rsi.evaluation.score_direction={direction!r} is not supported by ALE yet"
            )
        cooldown = evaluation.get("cooldown_sec")
        if isinstance(cooldown, (int, float)) and cooldown > 0:
            raise TaskDefinitionError("rsi.evaluation.cooldown_sec is not supported by ALE yet")
    runtime = rsi.get("runtime")
    if isinstance(runtime, dict):
        sandbox = runtime.get("sandbox")
        if sandbox is not None and sandbox is not False:
            raise TaskDefinitionError("rsi.runtime.sandbox is not supported by ALE yet")
        if runtime.get("clean_image") is True:
            raise TaskDefinitionError("rsi.runtime.clean_image=true is not supported by ALE yet")
    resources = rsi.get("resources")
    if isinstance(resources, dict) and resources.get("world") is not None:
        raise TaskDefinitionError("rsi.resources.world is not supported by ALE yet")


def _slug_part(raw: str, fallback: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_-]+", "_", raw.strip().lower()).strip("_-")
    if not cleaned:
        cleaned = fallback
    if not cleaned[0].isalnum():
        cleaned = f"t_{cleaned}"
    return cleaned[:100]


def _canonical_task_id(raw_name: str, fallback_org: str) -> str:
    if "/" in raw_name:
        org, _, name = raw_name.partition("/")
        candidate = f"{_slug_part(org, fallback_org)}/{_slug_part(name, 'task')}"
    else:
        candidate = f"{_slug_part(fallback_org, 'openrsi')}/{_slug_part(raw_name, 'task')}"
    return str(TaskId(candidate))


def _bridge_reward_script(test_sh: Path) -> bool:
    if not test_sh.is_file():
        return False
    text = test_sh.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    if _BRIDGE_MARKER in text or "reward.json" in text or "reward.txt" not in text:
        return False
    bridge = "\n".join(
        [
            _BRIDGE_MARKER,
            "_ale_bridge_reward() {",
            "  local rc=$?",
            "  if [ -f /logs/verifier/reward.txt ] && [ ! -f /logs/verifier/reward.json ]; then",
            "    if command -v python3 >/dev/null 2>&1; then",
            "      python3 - <<'PY'",
            "import json, pathlib",
            "p = pathlib.Path('/logs/verifier/reward.txt')",
            "val = float(p.read_text(encoding='utf-8').strip())",
            "pathlib.Path('/logs/verifier/reward.json').write_text(",
            "    json.dumps({'reward': val}), encoding='utf-8'",
            ")",
            "PY",
            "    else",
            '      _v="$(tr -d " \\t\\r\\n" < /logs/verifier/reward.txt)"',
            '      [ -n "$_v" ] && printf \'{"reward":%s}\\n\' "$_v" > /logs/verifier/reward.json',
            "    fi",
            "  fi",
            '  return "$rc"',
            "}",
            "trap _ale_bridge_reward EXIT",
        ]
    )
    lines = text.splitlines()
    if lines and lines[0].startswith("#!"):
        updated = "\n".join([lines[0], bridge, *lines[1:]]) + "\n"
    else:
        updated = bridge + "\n" + text
        if not updated.endswith("\n"):
            updated += "\n"
    test_sh.write_bytes(updated.encode("utf-8"))
    if os.name != "nt":
        test_sh.chmod(0o755)
    return True


def stage_task_for_ale(
    task_source: Path,
    workspace_dir: Path,
    *,
    fallback_org: str = "openrsi",
) -> StagedTask:
    """Copy a verified task into ``workspace_dir`` and normalize ``1.4+ale+openrsi`` if needed."""
    if workspace_dir.exists():
        shutil.rmtree(workspace_dir)
    shutil.copytree(task_source, workspace_dir, symlinks=False)

    toml_path = workspace_dir / "task.toml"
    if not toml_path.is_file():
        raise TaskDefinitionError(f"missing task.toml in {workspace_dir}")
    try:
        raw = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise TaskDefinitionError(f"invalid task.toml: {exc}") from exc

    schema_version = str(raw.get("schema_version") or "")
    if schema_version == ALE_SCHEMA_VERSION:
        try:
            config = TaskConfig.model_validate(raw)
        except Exception as exc:
            raise TaskDefinitionError(f"invalid task.toml: {exc}") from exc
        return StagedTask(
            path=workspace_dir,
            task_id=str(config.task.name),
            schema_version=schema_version,
            adapted=False,
            notices=(),
        )

    if schema_version != OPENRSI_SCHEMA_VERSION:
        raise TaskDefinitionError(
            f"unsupported task.toml schema_version {schema_version!r}; "
            f"expected {ALE_SCHEMA_VERSION!r} or {OPENRSI_SCHEMA_VERSION!r}"
        )

    notices: list[str] = []
    data: dict[str, Any] = dict(raw)
    rsi = data.pop("rsi", None)
    comment = data.pop("comment", None)
    _check_supported_rsi_fields(rsi)

    env_tbl = (
        dict(data.get("environment") or {}) if isinstance(data.get("environment"), dict) else {}
    )
    agent_tbl = dict(data.get("agent") or {}) if isinstance(data.get("agent"), dict) else {}
    verifier_tbl = (
        dict(data.get("verifier") or {}) if isinstance(data.get("verifier"), dict) else {}
    )
    metadata_tbl = (
        dict(data.get("metadata") or {}) if isinstance(data.get("metadata"), dict) else {}
    )

    if "workdir" in env_tbl:
        env_workdir = env_tbl.pop("workdir")
        if "workdir" in agent_tbl and agent_tbl["workdir"] != env_workdir:
            raise TaskDefinitionError("[environment].workdir disagrees with [agent].workdir")
        if "workdir" not in agent_tbl:
            agent_tbl["workdir"] = env_workdir
            notices.append("migrated [environment].workdir to [agent].workdir")

    env_net = env_tbl.pop("network_mode", None)
    env_hosts = env_tbl.pop("allowed_hosts", None)
    if env_net is not None:
        if "network_mode" in agent_tbl and agent_tbl["network_mode"] != env_net:
            raise TaskDefinitionError(
                "[environment].network_mode disagrees with [agent].network_mode"
            )
        if "network_mode" not in agent_tbl:
            agent_tbl["network_mode"] = env_net
            notices.append("migrated [environment].network_mode to [agent].network_mode")
    if env_hosts is not None:
        if "allowed_hosts" in agent_tbl and list(agent_tbl["allowed_hosts"]) != list(env_hosts):
            raise TaskDefinitionError(
                "[environment].allowed_hosts disagrees with [agent].allowed_hosts"
            )
        if "allowed_hosts" not in agent_tbl:
            agent_tbl["allowed_hosts"] = env_hosts

    for verifier_key in ("network_mode", "allowed_hosts"):
        if verifier_key in verifier_tbl:
            verifier_tbl.pop(verifier_key)
            notices.append(f"stripped unsupported [verifier].{verifier_key}")

    task_tbl = dict(data.get("task") or {}) if isinstance(data.get("task"), dict) else {}
    raw_name = str(task_tbl.get("name") or workspace_dir.name)
    task_tbl["name"] = _canonical_task_id(raw_name, fallback_org)

    openrsi_meta: dict[str, Any] = {"original_schema_version": OPENRSI_SCHEMA_VERSION}
    if rsi is not None:
        openrsi_meta["rsi"] = rsi
    if comment is not None:
        openrsi_meta["comment"] = comment
    metadata_tbl["openrsi"] = openrsi_meta

    data["schema_version"] = ALE_SCHEMA_VERSION
    data["task"] = task_tbl
    if env_tbl:
        data["environment"] = env_tbl
    else:
        data.pop("environment", None)
    if agent_tbl:
        data["agent"] = agent_tbl
    else:
        data.pop("agent", None)
    if verifier_tbl:
        data["verifier"] = verifier_tbl
    else:
        data.pop("verifier", None)
    data["metadata"] = metadata_tbl

    rendered = _dump_toml(data)
    toml_path.write_bytes(rendered.encode("utf-8"))
    try:
        validated = TaskConfig.model_validate(tomllib.loads(rendered))
    except Exception as exc:
        raise TaskDefinitionError(f"adapted task.toml failed ALE schema validation: {exc}") from exc

    os_name = str(env_tbl.get("os") or "linux").lower()
    if os_name == "linux" and _bridge_reward_script(workspace_dir / "tests" / "test.sh"):
        notices.append("added reward.txt -> reward.json bridge in tests/test.sh")

    return StagedTask(
        path=workspace_dir,
        task_id=str(validated.task.name),
        schema_version=ALE_SCHEMA_VERSION,
        adapted=True,
        notices=tuple(notices),
    )
