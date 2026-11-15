# Daytona Provider: problems and decisions

How this Provider works is in the Providers [README](../README.md#daytona). This file
records the problems peculiar to it: what we saw, what we measured, and what we decided.

## Daytona's domain allowlist terminates TLS

**Problem.** Under `allowlist`, Chrome 157 and Firefox 160 on a Linux desktop Task refused
every allowed host with a certificate error (`ERR_CERT_AUTHORITY_INVALID`, "Warning:
Security Risk"), while `curl` and Python reached it (November 2026, CPU runner).

**Context.** The Provider used to seal an allowlist with Daytona's `domainAllowList`.
Daytona's proxy behind it ("netleash") terminates TLS: `openssl s_client` from the sandbox
shows `example.com` presenting a certificate issued by "netleash ephemeral CA". Daytona
puts that CA at `/etc/daytona/netleash/ca.crt` and names it in `SSL_CERT_FILE`,
`CURL_CA_BUNDLE`, `REQUESTS_CA_BUNDLE` and `NODE_EXTRA_CA_CERTS`, which curl, Python and
Node read; browsers keep their own trust stores and reject it, with or without Encrypted
Client Hello. Daytona therefore sees allowed traffic in plaintext. Its allowlist also took
only domain names, at most 100, and passed plain HTTP and other ports of an allowed host.

**Solution.** `close_egress` seals `allowlist` with `networkBlockAll`, as `block`. The SSH
tunnel that carries the Gateway also carries the Host's egress proxy to the sandbox's
loopback (`PROXY_PORT`), and guestd's [relay](../README.md#allowlist-relay) fronts it, so
the agent's commands and its desktop reach allowed hosts exactly as on Docker.

**Decisions.**
- Our proxy rather than trusting Daytona's CA in each browser: TLS stays end to end, the
  allowlist means the same on every Provider, and no browser or image needs changing. The
  cost is that allowed traffic passes through the Host's connection.
- Trusting the netleash CA in the agent's NSS databases was rejected: it depends on tools in
  the image, differs per browser, and leaves Daytona reading the traffic.

## `networkBlockAll` accepts connections it passes nothing on

**Problem.** The episode test that checks setup's network against the agent's began to
report the agent's network open (November 2026).

**Context.** Under `networkBlockAll` every runner, CPU as GPU, accepts TCP connections to
ports 80 and 443 of any address, a GPU runner also to 53, and passes nothing on them: a TLS
handshake ends at once and no name resolves. The test checked a bare connect.

**Decision.** The sandbox is closed in every way that carries data, so nothing changes in
the Provider. Whether the network is open is judged by a completed TLS handshake with a
public host, in `_set_egress` and in every test.
