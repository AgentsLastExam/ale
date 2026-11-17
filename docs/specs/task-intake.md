# Runtime Task Intake API (`ale.task.intake.v1`)

`ale serve` exposes a RESTful intake endpoint (`ale.task.intake.v1`) so upstream task
brewers such as `ale-brew-science` can push approved, offline-sealed Task bundles without
giving up CLI-first operation (`ale run`, `ale validate`, `ale lint`) or Fleet pooling.

## Port and Routes

Default listen port is **`7472`** (`7470` Fleet control, `7471` Fleet node, `7472` Task
intake). Bind `127.0.0.1` or place behind TLS/private networking in production.

| Method & Path | Auth | Purpose |
| --- | --- | --- |
| `GET /healthz`, `GET /v1/health` | Public | Liveness and protocol check (`ale.task.intake.v1`) |
| `POST /v1/intake/tasks` | HMAC and/or Bearer | Accept a `pull_url` JSON dispatch or `multipart_bundle` upload (`202` / `200`) |
| `GET /v1/intake/tasks` | Bearer | List persisted intake tasks (`?status=&limit=`) |
| `GET /v1/intake/tasks/{ref}` | Bearer | Inspect status by `id`, `dispatch_id`, or `idempotency_key` |
| `POST /v1/intake/tasks/{ref}/retry` | Bearer | Re-enqueue a `failed` intake task (`202`) |

## Authentication

The server fails closed at startup unless at least one of `ALE_INTAKE_SECRET` or
`ALE_INTAKE_TOKEN` is set.

- **HMAC dispatch signature (`ALE_INTAKE_SECRET`)**:
  Requires `X-ALE-Timestamp` (Unix seconds within `+/- 300` seconds by default),
  `X-ALE-Idempotency-Key`, and `X-ALE-Signature: sha256=<hex>`, where `<hex>` is
  `HMAC-SHA256(secret, f"{timestamp}.{idempotency_key}.".encode("utf-8") + metadata_bytes)`.
  In `application/json` mode, `metadata_bytes` is the exact raw HTTP body. In
  `multipart/form-data` mode, `metadata_bytes` is the exact UTF-8 byte sequence of the
  `metadata` form part.
- **Bearer token (`ALE_INTAKE_TOKEN`)**:
  When configured, `POST /v1/intake/tasks` also requires `Authorization: Bearer <token>`.
  Query/retry routes require `Authorization: Bearer` matching `ALE_INTAKE_TOKEN` (or
  `ALE_INTAKE_SECRET` when no dedicated token is set).

## Idempotency and Bundle Integrity

1. `idempotency_key` uniquely identifies a logical task artifact delivery.
   - Re-delivering the same `idempotency_key` with the same `artifact.sha256` returns `200 OK`
     with `created: false` and the current status without duplicating work.
   - Re-delivering the same `idempotency_key` with a different `artifact.sha256` returns
     `409 Conflict`.
2. Every accepted bundle (whether uploaded via `multipart/form-data` `bundle` part or pulled
   from `artifact.download_url`) is verified against `artifact.sha256` before unpacking.
3. Unpacking rejects symlinks, hardlinks, special files, path traversal, and archives above
   2,000 files or 512 MiB.
4. Sealed Brewer bundles (`artifact.json`, `offline-report.json`, `task/`) have their report
   digest, passing offline check status, and full `task/` file inventory verified prior to
   staging.

## OpenRSI Staging Compatibility

`ale.core.taskspec.TaskConfig` requires `schema_version = "1.4+ale.1"`. When an unpacked
bundle carries `schema_version = "1.4+ale+openrsi"`, `stage_task_for_ale` writes a staged
copy (leaving the verified unpacked tree untouched) that:

- Rejects explicit OpenRSI declarations ALE cannot execute yet (`max_submissions != 1`,
  `score_direction != "maximize"`, `cooldown_sec > 0`, `rsi.runtime.sandbox`,
  `rsi.runtime.clean_image`, `rsi.resources.world`).
- Migrates `[environment]` aliases (`workdir`, `network_mode`, `allowed_hosts`) into
  `[agent]` when consistent, and strips unsupported `[verifier]` network overrides with
  a recorded notice.
- Preserves `[rsi]` and `[comment]` under `[metadata.openrsi]` and rewrites
  `schema_version = "1.4+ale.1"`.
- Bridges Linux `tests/test.sh` scripts that only write `/logs/verifier/reward.txt` so
  `/logs/verifier/reward.json` (`{"reward": <float>}`) is emitted on exit when absent.

## Outbound Status Callback

When `ALE_INTAKE_WEBHOOK_URL` (or `status_webhook_url`, or `ALE_INTAKE_WEBHOOK_URL=auto`
derived from Brewer's `/api/v1/outbox/bundles/...` origin) is configured, the worker POSTs
`running`, `completed`, and `failed` status updates signed with
`X-ALE-Signature: sha256=<HMAC-SHA256(webhook_secret, raw_json_bytes)>`.
