"""Tests for the RESTful task intake protocol, bundle adapter, ledger, and server."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sys
import tarfile
import time
import tomllib
import uuid
import zipfile
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from ale.core.errors import ConfigError, TaskDefinitionError
from ale.core.intake import (
    HEADER_DISPATCH_ID,
    HEADER_IDEMPOTENCY_KEY,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    INTAKE_PROTOCOL_VERSION,
    IntakeDispatchRequest,
    IntakeExecutionMode,
    IntakeStatus,
    sign_dispatch,
    verify_webhook_signature,
)
from ale.core.taskspec import TaskConfig
from ale.run.intake.bundle import stage_task_for_ale, unpack_archive, verify_unpacked_bundle
from ale.run.intake.server import intake_app
from ale.run.intake.service import IntakeServerSettings
from ale.run.intake.store import Store

pytestmark = pytest.mark.unit

SECRET = "intake-hmac-secret"
TOKEN = "intake-bearer-token"
WEBHOOK_SECRET = "brewer-webhook-secret"


def _make_sealed_bundle_zip(
    *,
    task_name: str = "openrsi-x/ising_mc",
    schema_version: str = "1.4+ale+openrsi",
    extra_toml: str = "",
    test_sh: str | None = None,
) -> tuple[bytes, str]:
    files: dict[str, bytes] = {}
    default_toml = (
        f'schema_version = "{schema_version}"\n\n'
        "[task]\n"
        f'name = "{task_name}"\n\n'
        "[environment]\n"
        "cpus = 2\n"
        "memory_mb = 4096\n"
        'workdir = "/workspace"\n'
        'network_mode = "allowlist"\n'
        'allowed_hosts = ["pypi.org"]\n\n'
    )
    if schema_version == "1.4+ale+openrsi":
        default_toml += (
            "[verifier]\n"
            'network_mode = "no-network"\n\n'
            "[rsi.domain]\n"
            'category = "Science"\n'
            'subcategory = "Statistical physics"\n\n'
            "[rsi.evaluation]\n"
            'mode = "benchmark"\n'
            "max_submissions = 1\n"
            'score_direction = "maximize"\n\n'
            "[comment]\n"
            'source = "ale-brew-science"\n'
        )
    else:
        default_toml = (
            f'schema_version = "{schema_version}"\n\n'
            "[task]\n"
            f'name = "{task_name}"\n\n'
            "[environment]\n"
            "cpus = 2\n"
            "memory_mb = 4096\n\n"
            "[agent]\n"
            'workdir = "/workspace"\n'
        )

    files["task.toml"] = (default_toml + extra_toml).encode("utf-8")
    files["instruction.md"] = b"# Ising Task\nCompute Tc and write /workspace/out.txt.\n"
    files["environment/Dockerfile"] = b"FROM python:3.12-slim@sha256:" + b"0" * 64 + b"\n"
    files["solution/solve.sh"] = b"#!/usr/bin/env bash\necho 2.269 > /workspace/out.txt\n"
    files["tests/test.sh"] = (
        test_sh
        if test_sh is not None
        else ("#!/usr/bin/env bash\nmkdir -p /logs/verifier\necho 1 > /logs/verifier/reward.txt\n")
    ).encode("utf-8")

    # Compute exact openrsi_common.task_artifact inventory + digests.
    dirs = {"environment", "solution", "tests"}
    inventory_entries: list[dict[str, Any]] = []
    for rel in sorted([*dirs, *files.keys()]):
        is_dir = rel in dirs
        mode = 0o755 if (is_dir or rel.endswith(".sh")) else 0o644
        raw = b"" if is_dir else files[rel]
        inventory_entries.append(
            {
                "path": rel,
                "kind": "directory" if is_dir else "file",
                "mode": mode,
                "size": 0 if is_dir else len(raw),
                "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
            }
        )
    task_digest = (
        "sha256:"
        + hashlib.sha256(json.dumps(inventory_entries, sort_keys=True).encode("utf-8")).hexdigest()
    )
    offline_report = {
        "schema_version": 1,
        "task_digest": task_digest,
        "checks": [
            {
                "name": "schema",
                "status": "passed",
                "summary": "valid",
                "stdout": "",
                "stderr": "",
            }
        ],
        "execution_verified": False,
    }
    report_bytes = json.dumps(offline_report, indent=2).encode("utf-8")
    artifact = {
        "schema_version": 1,
        "task_name": task_name.split("/")[-1],
        "created_at": "2026-11-16T23:00:00+00:00",
        "source_id": "submission:test-1",
        "task_digest": task_digest,
        "files": inventory_entries,
        "offline_report_digest": "sha256:" + hashlib.sha256(report_bytes).hexdigest(),
        "guidance_digest": None,
    }
    artifact_bytes = json.dumps(artifact, indent=2).encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("artifact.json", artifact_bytes)
        zf.writestr("offline-report.json", report_bytes)
        for rel, raw in files.items():
            info = zipfile.ZipInfo(f"task/{rel}")
            mode = 0o100755 if rel.endswith(".sh") else 0o100644
            info.external_attr = mode << 16
            zf.writestr(info, raw)
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def _dispatch_payload(
    *,
    dispatch_id: str | None = None,
    idempotency_key: str | None = None,
    sha256: str,
    size_bytes: int,
    download_url: str = "",
    execution_mode: str | None = None,
) -> dict[str, Any]:
    did = dispatch_id or str(uuid.uuid4())
    ikey = idempotency_key or f"ale-brewer-{uuid.uuid4().hex[:8]}-{sha256[:16]}"
    payload: dict[str, Any] = {
        "schema_version": INTAKE_PROTOCOL_VERSION,
        "dispatch_id": did,
        "idempotency_key": ikey,
        "submitted_at": "2026-11-16T23:00:00+00:00",
        "task": {
            "task_name": "ising_mc",
            "task_repo": "OpenRSI-Foundation/OpenRSI-X",
            "domain": "Science/Statistical physics",
            "title": "Ising MC",
            "summary": "Estimate critical temperature.",
            "submission_id": str(uuid.uuid4()),
            "job_id": str(uuid.uuid4()),
            "source_type": "skill_cli",
            "source_skill": "science-task-agent",
            "source_ref": "cli://local",
        },
        "author": {"id": str(uuid.uuid4()), "email": "expert@openrsi.org", "name": "Expert"},
        "review": {
            "decision": "approve",
            "reviewed_by": "lead@openrsi.org",
            "review_note": "ok",
            "ai_review": {"verdict": "pass", "score": 98},
        },
        "artifact": {
            "id": str(uuid.uuid4()),
            "name": "task-bundle.zip",
            "sha256": sha256,
            "size_bytes": size_bytes,
            "download_url": download_url,
            "offline_report": {"passed": True},
            "tree_manifest": {"entries": []},
        },
    }
    if execution_mode is not None:
        payload["execution_mode"] = execution_mode
    return payload


def _signed_headers(
    payload: dict[str, Any],
    *,
    secret: str = SECRET,
    token: str = TOKEN,
    ts: str | None = None,
    body_bytes: bytes | None = None,
) -> tuple[bytes, dict[str, str]]:
    raw = (
        body_bytes
        if body_bytes is not None
        else json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    stamp = ts or str(int(time.time()))
    sig = sign_dispatch(secret, stamp, payload["idempotency_key"], raw)
    headers = {
        HEADER_DISPATCH_ID: payload["dispatch_id"],
        HEADER_IDEMPOTENCY_KEY: payload["idempotency_key"],
        HEADER_TIMESTAMP: stamp,
        HEADER_SIGNATURE: sig,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return raw, headers


async def _wait_terminal(
    client: TestClient[Any, Any], ref: str, *, headers: dict[str, str], timeout: float = 5.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = await client.get(f"/v1/intake/tasks/{ref}", headers=headers)
        assert resp.status == 200
        body = await resp.json()
        if body["status"] in {IntakeStatus.COMPLETED.value, IntakeStatus.FAILED.value}:
            return body
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for intake task {ref}")


async def test_health_and_authentication_guards(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        intake_app(IntakeServerSettings(state_dir=tmp_path / "empty", runs_dir=tmp_path / "runs"))

    settings = IntakeServerSettings(
        state_dir=tmp_path / "state",
        runs_dir=tmp_path / "runs",
        secret=SECRET,
        token=TOKEN,
    )
    async with TestClient(TestServer(intake_app(settings))) as client:
        for route in ("/healthz", "/v1/health"):
            res = await client.get(route)
            assert res.status == 200
            data = await res.json()
            assert data["protocol"] == INTAKE_PROTOCOL_VERSION

        # Query routes reject missing or wrong Bearer token.
        assert (await client.get("/v1/intake/tasks")).status == 401
        assert (
            await client.get("/v1/intake/tasks", headers={"Authorization": "Bearer wrong"})
        ).status == 401

        bundle_bytes, sha = _make_sealed_bundle_zip()
        payload = _dispatch_payload(
            sha256=sha, size_bytes=len(bundle_bytes), download_url="http://127.0.0.1:9999/b.zip"
        )
        raw, headers = _signed_headers(payload)

        # Missing Bearer
        no_bearer = {k: v for k, v in headers.items() if k != "Authorization"}
        assert (
            await client.post(
                "/v1/intake/tasks",
                data=raw,
                headers={**no_bearer, "Content-Type": "application/json"},
            )
        ).status == 401

        # Bad HMAC
        bad_sig = {**headers, HEADER_SIGNATURE: "sha256=" + "0" * 64}
        assert (
            await client.post(
                "/v1/intake/tasks",
                data=raw,
                headers={**bad_sig, "Content-Type": "application/json"},
            )
        ).status == 401

        # Stale timestamp
        stale_raw, stale_headers = _signed_headers(payload, ts=str(int(time.time()) - 900))
        assert (
            await client.post(
                "/v1/intake/tasks",
                data=stale_raw,
                headers={**stale_headers, "Content-Type": "application/json"},
            )
        ).status == 401

        # Header mismatch vs body
        mismatch = {**headers, HEADER_DISPATCH_ID: str(uuid.uuid4())}
        assert (
            await client.post(
                "/v1/intake/tasks",
                data=raw,
                headers={**mismatch, "Content-Type": "application/json"},
            )
        ).status == 422


async def test_pull_url_openrsi_staging_and_brewer_webhook(tmp_path: Path) -> None:
    bundle_bytes, sha = _make_sealed_bundle_zip()
    webhooks: list[tuple[bool, dict[str, Any]]] = []

    async def handle_bundle(_req: web.Request) -> web.Response:
        return web.Response(body=bundle_bytes, content_type="application/zip")

    async def handle_webhook(req: web.Request) -> web.Response:
        raw = await req.read()
        sig = req.headers.get(HEADER_SIGNATURE, "")
        verified = verify_webhook_signature(WEBHOOK_SECRET, sig, raw)
        webhooks.append((verified, json.loads(raw.decode("utf-8"))))
        return web.json_response({"status": json.loads(raw)["status"]})

    brewer_app = web.Application()
    brewer_app.add_routes(
        [
            web.get("/api/v1/outbox/bundles/{dispatch_id}", handle_bundle),
            web.post("/api/v1/webhooks/ale-status", handle_webhook),
        ]
    )

    seen_staged: list[Path] = []

    async def fake_executor(
        task_dir: Path, mode: IntakeExecutionMode, _runs_dir: Path, run_id: str
    ) -> dict[str, Any]:
        seen_staged.append(task_dir)
        parsed = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
        TaskConfig.model_validate(parsed)
        assert parsed["schema_version"] == "1.4+ale.1"
        assert parsed["agent"]["workdir"] == "/workspace"
        assert parsed["agent"]["network_mode"] == "allowlist"
        assert parsed["agent"]["allowed_hosts"] == ["pypi.org"]
        assert "network_mode" not in parsed.get("verifier", {})
        assert parsed["metadata"]["openrsi"]["rsi"]["domain"]["category"] == "Science"
        assert parsed["metadata"]["openrsi"]["comment"]["source"] == "ale-brew-science"
        test_sh = (task_dir / "tests" / "test.sh").read_text(encoding="utf-8")
        assert "reward.json" in test_sh
        return {"mode": mode.value, "run_id": run_id, "exit_code": 0}

    async with TestServer(brewer_app) as brewer_server:
        dispatch_id = str(uuid.uuid4())
        download_url = str(
            brewer_server.make_url(
                f"/api/v1/outbox/bundles/{dispatch_id}?expires=1999999999&signature=ok"
            )
        )
        settings = IntakeServerSettings(
            state_dir=tmp_path / "state",
            runs_dir=tmp_path / "runs",
            secret=SECRET,
            token=TOKEN,
            webhook_url="auto",
            webhook_secret=WEBHOOK_SECRET,
            executor=fake_executor,
        )
        async with TestClient(TestServer(intake_app(settings))) as client:
            payload = _dispatch_payload(
                dispatch_id=dispatch_id,
                sha256=sha,
                size_bytes=len(bundle_bytes),
                download_url=download_url,
            )
            raw, headers = _signed_headers(payload)
            res = await client.post(
                "/v1/intake/tasks",
                data=raw,
                headers={**headers, "Content-Type": "application/json"},
            )
            assert res.status == 202
            receipt = await res.json()
            assert receipt["created"] is True
            assert receipt["ale_task_id"] == "openrsi-x/ising_mc"
            assert receipt["task_id"] == "openrsi-x/ising_mc"

            auth_headers = {"Authorization": f"Bearer {TOKEN}"}
            final = await _wait_terminal(client, receipt["id"], headers=auth_headers)
            assert final["status"] == "completed"
            assert final["result"]["sealed"] is True
            assert final["result"]["adapted"] is True
            assert final["webhook_delivered_at"] is not None

            # Idempotent replay returns 200 without re-executing.
            replay = await client.post(
                "/v1/intake/tasks",
                data=raw,
                headers={**headers, "Content-Type": "application/json"},
            )
            assert replay.status == 200
            assert (await replay.json())["created"] is False
            assert len(seen_staged) == 1

            # Same idempotency_key with a different digest conflicts (409).
            other_payload = _dispatch_payload(
                dispatch_id=dispatch_id,
                idempotency_key=payload["idempotency_key"],
                sha256="f" * 64,
                size_bytes=123,
                download_url=download_url,
            )
            other_raw, other_headers = _signed_headers(other_payload)
            conflict = await client.post(
                "/v1/intake/tasks",
                data=other_raw,
                headers={**other_headers, "Content-Type": "application/json"},
            )
            assert conflict.status == 409

            # Lookup by dispatch_id and idempotency_key + list route.
            assert (
                await client.get(f"/v1/intake/tasks/{dispatch_id}", headers=auth_headers)
            ).status == 200
            assert (
                await client.get(
                    f"/v1/intake/tasks/{payload['idempotency_key']}", headers=auth_headers
                )
            ).status == 200
            listing = await client.get("/v1/intake/tasks?status=completed", headers=auth_headers)
            assert listing.status == 200
            assert len((await listing.json())["tasks"]) == 1

    assert [event["status"] for _, event in webhooks] == ["running", "completed"]
    assert all(verified for verified, _ in webhooks)


async def test_multipart_bundle_and_token_only_mode(tmp_path: Path) -> None:
    bundle_bytes, sha = _make_sealed_bundle_zip(
        task_name="agentslastexam/native_task",
        schema_version="1.4+ale.1",
        test_sh=(
            "#!/usr/bin/env bash\n"
            "mkdir -p /logs/verifier\n"
            "echo '{\"reward\":1}' > /logs/verifier/reward.json\n"
        ),
    )

    async def fake_executor(
        task_dir: Path, mode: IntakeExecutionMode, _runs_dir: Path, run_id: str
    ) -> dict[str, Any]:
        parsed = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
        TaskConfig.model_validate(parsed)
        return {"mode": mode.value, "run_id": run_id}

    # Token-only mode: Brewer still sends X-ALE-Signature signed with its own session_secret.
    settings = IntakeServerSettings(
        state_dir=tmp_path / "state",
        runs_dir=tmp_path / "runs",
        secret="",
        token=TOKEN,
        executor=fake_executor,
    )
    async with TestClient(TestServer(intake_app(settings))) as client:
        payload = _dispatch_payload(sha256=sha, size_bytes=len(bundle_bytes))
        raw_meta, headers = _signed_headers(payload, secret="brewer-internal-session-secret")

        form = aiohttp.FormData()
        form.add_field("metadata", raw_meta.decode("utf-8"))
        form.add_field(
            "bundle",
            bundle_bytes,
            filename="task-bundle.zip",
            content_type="application/zip",
        )
        res = await client.post("/v1/intake/tasks", data=form, headers=headers)
        assert res.status == 202
        receipt = await res.json()

        final = await _wait_terminal(
            client, receipt["id"], headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert final["status"] == "completed"
        assert final["result"]["adapted"] is False
        assert final["ale_task_id"] == "agentslastexam/native_task"

        # Mismatched inline bundle SHA-256 fails synchronously with 422.
        bad_payload = _dispatch_payload(sha256="a" * 64, size_bytes=len(bundle_bytes))
        bad_meta, bad_headers = _signed_headers(bad_payload)
        bad_form = aiohttp.FormData()
        bad_form.add_field("metadata", bad_meta.decode("utf-8"))
        bad_form.add_field("bundle", bundle_bytes, filename="bad.zip")
        bad_res = await client.post("/v1/intake/tasks", data=bad_form, headers=bad_headers)
        assert bad_res.status == 422


async def test_corrupt_bundle_failure_webhook_and_retry(tmp_path: Path) -> None:
    good_bytes, good_sha = _make_sealed_bundle_zip()
    serve_good = False
    webhooks: list[str] = []

    async def handle_bundle(_req: web.Request) -> web.Response:
        body = good_bytes if serve_good else b"corrupted-zip-bytes"
        return web.Response(body=body, content_type="application/zip")

    async def handle_webhook(req: web.Request) -> web.Response:
        body = await req.json()
        webhooks.append(body["status"])
        return web.json_response({"ok": True})

    brewer_app = web.Application()
    brewer_app.add_routes(
        [
            web.get("/api/v1/outbox/bundles/{dispatch_id}", handle_bundle),
            web.post("/api/v1/webhooks/ale-status", handle_webhook),
        ]
    )

    async def fake_executor(
        _task_dir: Path, mode: IntakeExecutionMode, _runs_dir: Path, run_id: str
    ) -> dict[str, Any]:
        return {"mode": mode.value, "run_id": run_id}

    async with TestServer(brewer_app) as brewer_server:
        download_url = str(
            brewer_server.make_url("/api/v1/outbox/bundles/d1?expires=9999999999&signature=ok")
        )
        settings = IntakeServerSettings(
            state_dir=tmp_path / "state",
            runs_dir=tmp_path / "runs",
            secret=SECRET,
            webhook_url="auto",
            executor=fake_executor,
        )
        async with TestClient(TestServer(intake_app(settings))) as client:
            payload = _dispatch_payload(
                sha256=good_sha, size_bytes=len(good_bytes), download_url=download_url
            )
            raw, headers = _signed_headers(payload, token="")
            res = await client.post(
                "/v1/intake/tasks",
                data=raw,
                headers={**headers, "Content-Type": "application/json"},
            )
            assert res.status == 202
            intake_id = (await res.json())["id"]

            bearer = {"Authorization": f"Bearer {SECRET}"}
            failed = await _wait_terminal(client, intake_id, headers=bearer)
            assert failed["status"] == "failed"
            assert "sha256" in failed["detail"].lower()

            # Retry once upstream serves the valid archive.
            serve_good = True
            retry_res = await client.post(f"/v1/intake/tasks/{intake_id}/retry", headers=bearer)
            assert retry_res.status == 202
            recovered = await _wait_terminal(client, intake_id, headers=bearer)
            assert recovered["status"] == "completed"
            assert recovered["attempt_count"] == 1
            # Completed tasks cannot be retried again.
            assert (
                await client.post(f"/v1/intake/tasks/{intake_id}/retry", headers=bearer)
            ).status == 409

    assert webhooks == ["failed", "running", "completed"]


def test_traversal_archive_and_unsupported_rsi_rejected(tmp_path: Path) -> None:
    evil_zip = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil_zip, "w") as zf:
        zf.writestr("../escape.txt", "bad")
    with pytest.raises(TaskDefinitionError, match="traversal"):
        unpack_archive(evil_zip, tmp_path / "out_zip")

    evil_tar = tmp_path / "evil.tar.gz"
    with tarfile.open(evil_tar, "w:gz") as tf:
        info = tarfile.TarInfo("link.txt")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tf.addfile(info)
    with pytest.raises(TaskDefinitionError, match="symlink"):
        unpack_archive(evil_tar, tmp_path / "out_tar")

    # Unsupported OpenRSI capability fails cleanly at staging.
    raw_task = tmp_path / "multi_submit"
    (raw_task / "tests").mkdir(parents=True)
    (raw_task / "solution").mkdir(parents=True)
    (raw_task / "environment").mkdir(parents=True)
    (raw_task / "instruction.md").write_text("# Demo\n", encoding="utf-8")
    (raw_task / "environment" / "Dockerfile").write_text(
        "FROM python:3.12-slim@sha256:" + "0" * 64 + "\n", encoding="utf-8"
    )
    (raw_task / "solution" / "solve.sh").write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    (raw_task / "tests" / "test.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (raw_task / "task.toml").write_text(
        'schema_version = "1.4+ale+openrsi"\n\n'
        '[task]\nname = "openrsi-x/multi"\n\n'
        "[rsi.evaluation]\nmax_submissions = 3\n",
        encoding="utf-8",
    )
    with pytest.raises(TaskDefinitionError, match="max_submissions"):
        stage_task_for_ale(raw_task, tmp_path / "staged_multi")


def test_sqlite_store_persists_across_reopen(tmp_path: Path) -> None:
    bundle_bytes, sha = _make_sealed_bundle_zip()
    archive = tmp_path / "bundle.zip"
    archive.write_bytes(bundle_bytes)
    unpack_archive(archive, tmp_path / "unpacked")
    verified = verify_unpacked_bundle(tmp_path / "unpacked")
    assert verified.sealed is True

    db_path = tmp_path / "intake.db"
    payload = IntakeDispatchRequest.model_validate(
        _dispatch_payload(
            sha256=sha, size_bytes=len(bundle_bytes), download_url="https://example.org/b.zip"
        )
    )
    from ale.run.intake.store import IntakeRecord

    record = IntakeRecord(
        id="rec-1",
        dispatch_id=payload.dispatch_id,
        idempotency_key=payload.idempotency_key,
        ale_task_id="openrsi-x/ising_mc",
        artifact_sha256=sha,
        request=payload,
        received_at="2026-11-16T23:00:00+00:00",
        updated_at="2026-11-16T23:00:00+00:00",
        updated_epoch=1.0,
    )
    store1 = Store(db_path)
    store1.put(record)
    store1.close()

    store2 = Store(db_path)
    try:
        assert store2.get("rec-1") is not None
        assert store2.get(payload.dispatch_id) is not None
        assert store2.get(payload.idempotency_key) is not None
        assert len(store2.incomplete_records()) == 1
    finally:
        store2.close()


@pytest.mark.skipif(sys.platform == "win32", reason="ale.run.assets requires POSIX fcntl")
def test_posix_lint_load_and_cli_compatibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from ale.run.cli.main import app
    from ale.run.lint import lint_repository
    from ale.run.tasksets.manifest import load_tasks

    bundle_bytes, _ = _make_sealed_bundle_zip()
    archive = tmp_path / "bundle.zip"
    archive.write_bytes(bundle_bytes)
    unpack_archive(archive, tmp_path / "unpacked")
    verified = verify_unpacked_bundle(tmp_path / "unpacked")
    staged = stage_task_for_ale(verified.task_dir, tmp_path / "staged")

    assert lint_repository(staged.path) == []
    loaded = load_tasks(staged.path)
    assert [str(item.spec.name) for item in loaded] == ["openrsi-x/ising_mc"]

    runner = CliRunner()
    help_res = runner.invoke(app, ["--help"])
    assert help_res.exit_code == 0
    assert "serve" in help_res.output
    assert "run" in help_res.output

    monkeypatch.delenv("ALE_INTAKE_SECRET", raising=False)
    monkeypatch.delenv("ALE_INTAKE_TOKEN", raising=False)
    serve_res = runner.invoke(app, ["serve"])
    assert serve_res.exit_code == 2
