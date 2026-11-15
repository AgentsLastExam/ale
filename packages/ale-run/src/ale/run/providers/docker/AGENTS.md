# Docker Provider: problems and decisions

How this Provider works is in the Providers [README](../README.md). This file records the
problems peculiar to it: what we saw, what we measured, and what we decided.

## A GNOME desktop's browsers under `allowlist`

**Problem.** On the container base's desktop (GNOME 42), Chrome 157 and Firefox 160
reached no allowlisted host (November 2026). Started from the desktop they went direct and
met `DNS_PROBE_FINISHED_NO_INTERNET`; started from an agent's command, with proxy
variables that carried the token, Chrome met `ERR_PROXY_AUTH_UNSUPPORTED`.

**Context.** In a GNOME session (`XDG_CURRENT_DESKTOP` set) Chromium and Firefox take
their proxy from `org.gnome.system.proxy`, whose mode is `none` in the base, and a program
started from the dock inherits the session's environment, which holds no proxy variables.
An agent's command carries no `XDG_CURRENT_DESKTOP`, so Chrome started from one reads the
variables, and Chromium drops credentials found there. Python's urllib drops them too when
the password is empty.

**Solution.** The [allowlist relay](../README.md#allowlist-relay): the variables name the
relay with no token in them, and `gsettings`, run as the agent in its desktop session
(`gui.session()` finds the session's bus), points GNOME's settings at it as well.

**Decisions.**
- The variables name the relay rather than carrying the token, so a client that cannot
  send credentials gets through, and the token stays inside guestd.
- guestd connects over `docker exec` and ends with its run, sealed or not, so what it
  changed is kept in `/run/ale/system-proxy.json`. A container's `/run` is part of its
  writable layer, so a commit keeps that file with the settings it explains; a snapshot
  restores the settings before it commits. A run that reattaches to a sealed container
  starts the relay again in a guestd of its own.
