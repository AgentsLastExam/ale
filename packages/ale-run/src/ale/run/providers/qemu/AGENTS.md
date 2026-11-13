# QEMU Provider: problems and decisions

How this Provider works is in the Providers [README](../README.md#qemu). This file records
the problems peculiar to it: what we saw, what we measured, and what we decided.

## Windows programs under `allowlist` take their proxy from WinINet

**Problem.** Under `allowlist` a Windows GUI Task reached none of its hosts: Chrome showed
`DNS_PROBE_FINISHED_NO_INTERNET` (Agents' Last Exam v1.1 Ketcher Task, November 2026).

**Context.** Chrome, Edge and PowerShell 5.1's `Invoke-WebRequest` read the account's
WinINet settings, not `HTTPS_PROXY`. Pointed at the egress proxy, Chrome fails with
`ERR_PROXY_AUTH_UNSUPPORTED`: the proxy's 407 carries no `Proxy-Authenticate` challenge,
WinINet settings hold no credentials, and Chrome ignores credentials in manual proxy
settings. Windows guestd runs elevated as the agent's own account, in its session.

**Solution.** The [allowlist relay](../README.md#allowlist-relay). `guestd/egress.py` sets
the default connection's WinINet per-connection options (flags, proxy, bypass) with
`InternetSetOptionW` and announces the change, so a browser already running switches as
well. It reads the flags as `INTERNET_PER_CONN_FLAGS_UI`: the plain flags lose automatic
detection once detection has failed, as it does offline, and written back they would turn
it off for good.

**Decisions.**
- A relay, not a Basic challenge from the proxy: a browser answers a challenge by asking
  whoever sits at the screen, which no unattended episode can do.
- `open_egress` and a power-off give the settings back, since the framework's own phases
  run as the same account and a snapshot outlives the relay. What guestd changed is kept
  in `%ProgramData%\ALE`, which only SYSTEM and Administrators may write, so a guestd the
  service restarts still restores it.

## `curl.exe` fails its revocation check under `allowlist`

**Problem.** Windows' own `curl.exe` honors the proxy variables, yet every HTTPS request
fails with `CRYPT_E_REVOCATION_OFFLINE`.

**Context.** schannel fetches certificate revocation lists and OCSP answers over plain
HTTP, which the CONNECT-only proxy does not carry.

**Decision.** Left to Tasks, which pass `--ssl-revoke-best-effort`: letting plain HTTP to
revocation endpoints through would widen every allowlist.
