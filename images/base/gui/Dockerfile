# sandbox-base-gui — the desktop sandbox image.
#
# Adapted from cua-lite's `lite/gym/sandbox/docker/Dockerfile.linux`, kept close to the
# original on purpose. That file is 800 lines of debugged detail — GNOME-Shell without
# systemd, a dbusmock login1 shim, the AT-SPI stack, supervisord ordering, a readiness
# watchdog — and every line of it was paid for by someone hitting the failure it
# prevents. Rewriting it from scratch would mean paying again.
#
# It starts `FROM ubuntu:22.04`, which is what we wanted anyway: a clean base, not
# somebody's exported VM rootfs.
#
# What we change is one thing. cua-lite bakes its own in-container server at
# /opt/lite/stdio_server.py; we bake ours at /opt/ale/guestd instead. Ours speaks our
# sandbox contract and, unlike theirs, also serves over TCP — a virtual machine has no
# exec channel, so the same guest service has to be reachable both ways.
#
# Their isolation model for the guest service is worth keeping — it lives under a
# root-owned directory the agent cannot read, so the agent cannot learn the protocol
# driving its own sandbox. Their separate interpreter is not: we install what the guest
# service needs into the system Python instead.
#
# Rebuild with:
#   docker build -f images/base/gui/Dockerfile -t ghcr.io/agentslastexam/sandbox-base-gui .
#
FROM ubuntu:22.04

# Desktop session user. Default `user` (sandbox, lite.osworld, lite.demo).
# cuaworld builds this SAME Dockerfile with `--build-arg USER=ga` because
# its upstream gym-anything assets hardcode the unix user `ga` in task prompts,
# verifiers, and setup scripts (e.g. "save to /home/ga/Documents/..."); it's
# cleaner to match the container to the assets than to rewrite thousands of
# hardcoded references. EVERYTHING below is templated on ${USER} so the
# two builds differ only by this arg. (The env-side naming stays `cuaworld` — this
# is only the in-container unix user.)
ARG USER=user

ENV DEBIAN_FRONTEND=noninteractive
ENV HOME=/home/${USER}
ENV DISPLAY=:1
ENV VNC_PORT=5901
ENV NOVNC_PORT=6901
ENV VNC_RESOLUTION=1024x768
ENV VNC_COL_DEPTH=24

# ----- System utilities + common desktop apps -----
# The exec_stdio backend's own tools — xdotool (op_input), ffmpeg + ImageMagick
# (op_screenshot fallbacks) — plus desktop plumbing and the everyday GUI apps a
# task-agnostic desktop agent expects: a terminal, file manager, text editor,
# calculator, image viewer, archive + screenshot tools, fonts, and xdg-utils
# (xdg-open — without it file-open and url-handler launches silently no-op).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl wget sudo git supervisor \
        dbus dbus-x11 x11-utils x11-xserver-utils \
        xdotool ffmpeg imagemagick \
        xdg-utils \
        gnome-terminal nautilus gedit gnome-calculator \
        gnome-text-editor eog evince file-roller gnome-screenshot \
        fonts-dejavu fonts-liberation \
        psmisc procps less nano \
        python3 python3-pip \
        gedit \
        pulseaudio pulseaudio-utils \
    && rm -rf /var/lib/apt/lists/*

# One interpreter, and it is the image's own. The guest service, the task's stages and
# the agent all use /usr/bin/python3 — so nothing has to choose between interpreters, and
# nothing installs into the environment a task runs in.
#
# cua-lite kept a separate venv here for the same packages. We install them into the
# system interpreter instead, because an engine that has to be told which of several
# interpreters to use is one rule too many. It cost us: the hint was a symlink to the
# venv, and Python derives its prefix from the invocation path, so the symlinked
# interpreter silently lost the venv's packages and the fast screenshot path fell back
# for as long as the mechanism existed.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-pil python3-xlib \
    && rm -rf /var/lib/apt/lists/*

# ----- Agent-terminal isolation: gnome-terminal forwards the LAUNCHER's environment to
# every new terminal shell (the terminal server's own environment is ignored). A task
# that opens one from a stage script would hand the agent whatever PATH that stage had.
# Shadowing gnome-terminal with a wrapper that resets PATH means every terminal, however
# it was launched, starts from the same clean session environment.
#
# cua-lite needed this to keep an agent away from a separate engine interpreter. We have
# only one interpreter, so that reason is gone — but a predictable PATH for anything the
# agent opens is worth keeping on its own.
RUN cat > /usr/local/bin/gnome-terminal <<'EOF' && chmod +x /usr/local/bin/gnome-terminal
#!/bin/sh
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
exec /usr/bin/gnome-terminal "$@"
EOF

# ----- Real GNOME-Shell desktop (Ubuntu session) + dconf/gsettings stack -----
# We run the SAME desktop OSWorld's reference VM uses — GNOME-Shell (mutter) under
# the Ubuntu session — so a vision policy trained here sees the real Activities /
# centered-clock / aggregate-menu / full-height ubuntu-dock / app-grid, not a
# Flashback approximation. GNOME-Shell hard-requires logind (org.freedesktop.login1)
# at startup; a container has no systemd, so we provide a python3-dbusmock login1
# SHIM on the system bus (see start-gnome.sh) — this makes GNOME-Shell 42 boot on
# plain supervisord, NO systemd / NO GPU / NO KVM (mutter composites via llvmpipe,
# and idles ~0% CPU, lighter than the old Flashback session).
#   - gnome-shell / ubuntu-session:      the shell + the `ubuntu` session definition.
#   - gnome-shell-extension-ubuntu-dock: the vertical left dash (favorite-apps native).
#   - gnome-shell-extension-desktop-icons-ng (ding): desktop icons — Home + files under
#     ~/Desktop (osworld tasks put files there; the agent sees them as clickable icons).
#   - gnome-shell-extension-appindicator: app tray icons (VLC etc.) — the Ubuntu default.
#     All three are listed in the `ubuntu` session mode's enabledExtensions, so they
#     auto-load at session start once installed (no enabled-extensions gsetting needed).
#   - python3-dbusmock:                  provides the mock org.freedesktop.login1.
#   - gnome-settings-daemon:             stores + applies gsettings (eval round-trip).
#   - dconf-{cli,service}:               dconf user db backend (where gsettings persists)
#   - gsettings-desktop-schemas:         org.gnome.desktop.* schemas eval tasks read.
# Pins: gnome-shell=42.9-0ubuntu2.3 (matches gnome-shell-common); gnome-settings-daemon
#   42.1-1ubuntu2.2; gnome-terminal 3.44.0-1ubuntu1; nautilus 1:42.6-0ubuntu2;
#   gnome-control-center 1:41.7-0ubuntu0.22.04.9; adwaita-icon-theme 41.0-1ubuntu1.
#   ubuntu-session / ubuntu-dock / python3-dbusmock left unpinned (jammy-stable).
RUN apt-get update && apt-get install -y --no-install-recommends \
        gnome-shell=42.9-0ubuntu2.3 \
        ubuntu-session \
        gnome-shell-extension-ubuntu-dock \
        gnome-shell-extension-desktop-icons-ng \
        gnome-shell-extension-appindicator \
        python3-dbusmock \
        gnome-settings-daemon=42.1-1ubuntu2.2 \
        gsettings-desktop-schemas=42.0-1ubuntu1 \
        dconf-cli=0.40.0-3ubuntu0.1 \
        dconf-service=0.40.0-3ubuntu0.1 \
        dconf-gsettings-backend:amd64=0.40.0-3ubuntu0.1 \
        gnome-terminal=3.44.0-1ubuntu1 \
        nautilus=1:42.6-0ubuntu2 \
        gnome-control-center=1:41.7-0ubuntu0.22.04.9 \
        adwaita-icon-theme=41.0-1ubuntu1 \
        gnome-shell-common=42.9-0ubuntu2.3 \
        ubuntu-wallpapers \
        papirus-icon-theme \
        humanity-icon-theme \
        yaru-theme-gtk \
        yaru-theme-icon \
        yaru-theme-sound \
        librsvg2-common \
        fonts-ubuntu \
        hyphen-en-us \
        at-spi2-core=2.44.0-3 \
        python3-gi=3.42.1-0ubuntu1 \
        python3-gi-cairo=3.42.1-0ubuntu1 \
        gir1.2-gtk-3.0=3.24.33-1ubuntu2.2 \
        gobject-introspection=1.72.0-1 \
    && gdk-pixbuf-query-loaders --update-cache 2>/dev/null || true \
    && gtk-update-icon-cache /usr/share/icons/Yaru 2>/dev/null || true \
    && gtk-update-icon-cache /usr/share/icons/Papirus 2>/dev/null || true \
    && update-mime-database /usr/share/mime 2>/dev/null || true \
    && rm -rf /var/lib/apt/lists/*
# gnome-shell-common ships the org.gnome.shell.* schemas (e.g. favorite-apps, which
# OSWorld eval tasks set and read); gnome-shell renders favorite-apps as the dash
# natively, so no plank favorite-apps sync is needed (unlike the old Flashback path).
# yaru-theme-{gtk,icon,sound}: Ubuntu jammy's default user-facing theme stack
# (the aubergine/orange Yaru look used by stock Ubuntu desktop). Installs:
#   - Yaru GTK theme (LIGHT widgets) → matches OSWorld VM in-window content
#   - Yaru icon theme               → full-color MIME icons for docx/pptx/xlsx/...
#   - Yaru cursor theme             → consistent pointer across sessions
# Without these the desktop would look like raw Adwaita — visibly different from the
# Yaru look upstream's GNOME-Shell desktop uses, which a vision-trained agent expects.
# `org.gnome.desktop.wm.preferences theme=Yaru-dark` styles X11 server-side titlebars
# dark (matches osworld GNOME-Shell's dark titlebar appearance).
# papirus-icon-theme + humanity-icon-theme retained as fallback layers in case
# Yaru is missing a specific MIME glyph; theme inheritance handles lookup.
# update-mime-database refreshes /usr/share/mime/mime.cache so nautilus
# correctly classifies new file extensions seen by the agent.
# at-spi2-core + python3-gi/introspection: the accessibility (AT-SPI) stack.
# at-spi2-core ships the a11y bus (`org.a11y.Bus` / at-spi-bus-launcher);
# python3-gi/-cairo + gir1.2-gtk-3.0 + gobject-introspection load the GTK atk-bridge
# so GTK apps publish their UI to that bus. Generic desktop completeness (a base
# whose session references A11ySettings should ship the bus), and — the concrete
# consumer — OSWorld's accessibility oracle reads the app a11y tree off this bus.

# ----- GNOME-Shell needs a logind shim; harden the session (THE "Oh no" fix) -----
# GNOME-Shell 42 hard-requires org.freedesktop.login1 (systemd-logind) at JS startup;
# without it it throws (StartServiceByName login1 -> exit 1), respawns, and
# gnome-session falls to the "Oh no" fail-whale. A container has no systemd, so
# start-gnome.sh (below) runs a python3-dbusmock login1 SHIM on the system bus. The
# mock runs as the desktop user, so allow it to own that well-known name (default
# system policy only lets root own well-known names).
# (Two independent root config edits merged into one layer: the login1 D-Bus policy
# write and the RequiredComponents trim have no ordering dependency between them.
# Trim the Ubuntu session's RequiredComponents to the shell alone, so a missing
# logind/hardware gsd plugin (Power/Wacom/Smartcard/Rfkill/UsbProtection — none work
# in a container) can't fail the whole session. gsd still autostarts from
# /etc/xdg/autostart; start-gnome.sh disables the container-incompatible plugins at
# boot (apt-proof — a per-env gsd re-install would otherwise restore them).)
RUN <<'DONE'
cat > /etc/dbus-1/system.d/logind-shim.conf <<'EOF'
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <policy context="default">
    <allow own="org.freedesktop.login1"/>
    <allow send_destination="org.freedesktop.login1"/>
    <allow receive_sender="org.freedesktop.login1"/>
  </policy>
</busconfig>
EOF
f=/usr/share/gnome-session/sessions/ubuntu.session
[ -f "$f" ] && sed -i -E 's/^RequiredComponents=.*/RequiredComponents=org.gnome.Shell;/' "$f" || true
DONE

# ----- TigerVNC + tools (Xvnc server) -----
# Pin bumped: 1.12.0+dfsg-1.2ubuntu0.1 -> 1.12.0+dfsg-4ubuntu0.22.04.1 (actual jammy version)
RUN apt-get update && apt-get install -y --no-install-recommends \
        tigervnc-standalone-server=1.12.0+dfsg-4ubuntu0.22.04.1 \
        tigervnc-common=1.12.0+dfsg-4ubuntu0.22.04.1 \
    && rm -rf /var/lib/apt/lists/*

# ----- noVNC (pinned upstream trycua fork tag, mirroring cua-xfce) -----
# Note: pin to whatever commit cua-xfce uses; for now use HEAD until verified.
RUN git clone --depth 1 https://github.com/trycua/noVNC.git /opt/noVNC && \
    git clone --depth 1 https://github.com/novnc/websockify /opt/noVNC/utils/websockify && \
    ln -s /opt/noVNC/vnc.html /opt/noVNC/index.html

# ----- [ISOLATION 4/5] Desktop account: ${USER} — NO sudo (the load-bearing edit) (templated; default `user`) -----
# Note: /home/${USER} may already exist (apt drag-in during the GNOME
# stack install). We force-chown after useradd so subsequent `USER` RUN steps can
# write into HOME. `-m` creates the home dir if absent.
# The agent user is deliberately NOT in group `sudo` and gets no sudoers grant here.
# Ubuntu's stock `%sudo` rule is password-gated and the password baked below is a
# constant, so group membership would be a standing route to root that no task asked
# for — and elevation is meant to be something a task declares and provenance records.
# When a task does declare it, the provider writes the grant at provisioning time.
RUN useradd -m -s /bin/bash "${USER}" && \
    echo "${USER}:${USER}" | chpasswd && \
    chown -R "${USER}:${USER}" "/home/${USER}"

# ===== USER-side config (run as ${USER} so HOME perms are right) =====
USER ${USER}
WORKDIR /home/${USER}

# Pre-create the runtime's HOME skeleton in ONE layer (all pure USER-side dir
# creation + static config writes; no ordering dependency beyond dirs-before-files,
# which the script order preserves). Covers, in order:
#   - the base runtime dirs. `$HOME/.local/share` must exist + be user-writable so
#     GTK apps (gnome-screenshot, file pickers, anything that touches
#     recently-used.xbel) don't fail "Permission denied" silently. osworld baseline
#     has it writable; without it the multi_apps_02ce9a50 screenshot OCR task fails
#     because gnome-screenshot can't write its history file (verified Gtk-WARNING).
#   - XDG user dirs for GTK file-chooser sidebar parity with OSWorld. OSWorld
#     baseline (stock Ubuntu) has $HOME/{Documents,Downloads,Pictures,...} pre-created
#     + ~/.config/user-dirs.dirs map, which GTK's file chooser turns into sidebar
#     entries (Recent/Documents/Downloads/Music/Pictures/Videos). Without these our
#     save/open dialogs only show "Home" + "Other Locations" → agents that target a
#     sidebar slot get visually shifted vs OSWorld priors.
#   - Welcome / first-run popup suppression (mirrors cua-xfce's xfce4-tips-autostart
#     suppression + Ubuntu-specific extras). (The container-incompatible gsd plugins
#     are disabled at boot in start-gnome.sh, not here — a ~/.config/autostart
#     override is ignored by gnome-session for those Initialization-phase plugins.)
#   - Suppress GNOME-Shell's one-shot "Screen Lock disabled" toast.
#     gnome-shell/ui/main.js _handleLockScreenWarning() shows "Screen Locking requires
#     the GNOME display manager." once per fresh userdatadir when screenShield is null.
#     screenShield is null here because our dbusmock shims login1 but not
#     org.gnome.DisplayManager, so LoginManager.canLock() is false and no ScreenShield
#     is created. It notifies only if ${userdatadir}/lock-warning-shown does not exist;
#     pre-creating it makes touch_async() early-return before notify(). Suppresses ONLY
#     this toast (an internal Main.notify, unrelated to the org.freedesktop.Notifications
#     path the agent legitimately sees), needs no gnome-shell patch, and is OOD-safe —
#     the real osworld VM has GDM so canLock() is true and this branch never runs.
#     dconf/gsettings cannot fix this: the string is gated on screenShield === null.
RUN <<'DONE'
mkdir -p $HOME/.config/autostart $HOME/.cache/dconf $HOME/.vnc \
         $HOME/storage $HOME/shared $HOME/.local/share $HOME/.local/state \
         $HOME/Desktop $HOME/Documents $HOME/Downloads $HOME/Music \
         $HOME/Pictures $HOME/Public $HOME/Templates $HOME/Videos \
         $HOME/.local/share/gnome-shell
cat > $HOME/.config/user-dirs.dirs <<'EOF'
XDG_DESKTOP_DIR="$HOME/Desktop"
XDG_DOCUMENTS_DIR="$HOME/Documents"
XDG_DOWNLOAD_DIR="$HOME/Downloads"
XDG_MUSIC_DIR="$HOME/Music"
XDG_PICTURES_DIR="$HOME/Pictures"
XDG_PUBLICSHARE_DIR="$HOME/Public"
XDG_TEMPLATES_DIR="$HOME/Templates"
XDG_VIDEOS_DIR="$HOME/Videos"
EOF
echo 'enabled=True' > $HOME/.config/user-dirs.conf
printf '[Desktop Entry]\nHidden=true\n' > $HOME/.config/autostart/gnome-initial-setup-first-login.desktop
printf '[Desktop Entry]\nHidden=true\n' > $HOME/.config/autostart/org.gnome.Software.desktop
printf '[Desktop Entry]\nHidden=true\n' > $HOME/.config/autostart/update-notifier.desktop
touch $HOME/.local/share/gnome-shell/lock-warning-shown
DONE

# ===== back to root for system-level config (the sections above ran as `user`) =====
USER root

# ----- supervisord configs (start Xvnc → noVNC → dbus → gnome-shell) -----
# NOTE on heredoc usage: supervisord configs reference %(ENV_X)s — the parens
# break `printf` (it tries to interpret %( as a format directive). We use
# `cat <<'EOF'` with single-quoted EOF so the shell does not interpolate $vars
# or % directives — the file lands on disk verbatim.
# The dir creation + the master supervisord.conf + the Xvnc/noVNC program configs
# are all static writes with no ordering dependency (beyond conf.d existing first),
# so they collapse into ONE layer. The ${USER}-templating sed pass at the end of
# this file still rewrites `user=user` across conf.d/*.conf regardless of grouping.
RUN <<'DONE'
mkdir -p /etc/supervisor/conf.d /var/log/supervisor
# Main supervisord.conf
cat > /etc/supervisor/supervisord.conf <<'EOF'
[supervisord]
nodaemon=true
user=root
logfile=/var/log/supervisor/supervisord.log
pidfile=/var/run/supervisord.pid

[include]
files = /etc/supervisor/conf.d/*.conf
EOF
# Xvnc — TigerVNC server on :1
cat > /etc/supervisor/conf.d/10-xvnc.conf <<'EOF'
[program:xvnc]
command=/usr/bin/Xvnc :1 -depth 24 -geometry %(ENV_VNC_RESOLUTION)s -SecurityTypes None -localhost no -alwaysshared
autostart=true
autorestart=true
user=user
environment=HOME="/home/user",USER="user"
priority=10
stdout_logfile=/var/log/supervisor/xvnc.log
stderr_logfile=/var/log/supervisor/xvnc.err
EOF
# noVNC websockify (browser-side X access on :6901 → :5901)
cat > /etc/supervisor/conf.d/20-novnc.conf <<'EOF'
[program:novnc]
command=/opt/noVNC/utils/websockify/run --web /opt/noVNC %(ENV_NOVNC_PORT)s localhost:5901
autostart=true
autorestart=true
user=user
priority=20
stdout_logfile=/var/log/supervisor/novnc.log
stderr_logfile=/var/log/supervisor/novnc.err
EOF
DONE

# ----- System D-Bus bring-up (root supervisor program, priority 15) -----
# GNOME-Shell needs a reachable SYSTEM bus, and its bring-up (pkill/rm/mkdir +
# dbus-daemon) plus the container-incompatible gsd autostart cleanup all need root.
# start-gnome.sh runs as ${USER}, and the agent user is NO LONGER in group `sudo`
# (§2.A.5), so those `sudo` calls would hard-abort under `set -e` and brick boot.
# Move them into THIS root-run program instead. It runs at priority 15 (before
# gnome at 30); start-gnome.sh's socket-wait loop bridges the ~4s gap. The script
# execs `dbus-daemon --system --nofork` so the daemon stays supervised in the
# foreground (autorestart reaps+respawns it on death).
# CRITICAL: both this script and its .conf are baked root:root (we are USER root
# here). A user-writable copy of either would be root RCE via supervisor autorestart.
RUN <<'DONE'
cat > /usr/local/bin/system-dbus.sh <<'EOF'
#!/bin/bash
set -e

# Ensure a FRESH system dbus. Without a reachable system bus, gnome-session-binary
# aborts ("Failed to connect to system bus: connection refused"). A plain
# `[ ! -S socket ]` guard is not enough across a `docker restart`: the container
# filesystem (hence /run) persists, so the socket FILE survives while the daemon
# behind it is dead. Reap any old system daemon + stale socket, then start clean.
# On a first `docker run` /run is empty, so the pkill/rm are harmless no-ops.
pkill -f 'dbus-daemon --system' 2>/dev/null || true
rm -f /run/dbus/system_bus_socket /run/dbus/pid 2>/dev/null || true
mkdir -p /run/dbus

# Disable the container-incompatible gnome-settings-daemon plugins EVERY boot
# (apt-proof). gsd-power exits code 1 with no logind and crash-loops; Wacom/
# Smartcard/Rfkill/UsbProtection are likewise useless here. A build-time rm doesn't
# survive a per-env image that re-installs gnome-settings-daemon (lite.osworld
# does), so remove the system autostart entries here — after any build-time apt,
# before gnome-session (this program is priority 15, gnome is 30). Runs as root.
rm -f /etc/xdg/autostart/org.gnome.SettingsDaemon.Power.desktop \
      /etc/xdg/autostart/org.gnome.SettingsDaemon.Wacom.desktop \
      /etc/xdg/autostart/org.gnome.SettingsDaemon.Smartcard.desktop \
      /etc/xdg/autostart/org.gnome.SettingsDaemon.Rfkill.desktop \
      /etc/xdg/autostart/org.gnome.SettingsDaemon.UsbProtection.desktop

exec dbus-daemon --system --nofork
EOF
chmod +x /usr/local/bin/system-dbus.sh
cat > /etc/supervisor/conf.d/15-system-dbus.conf <<'EOF'
[program:system-dbus]
command=/usr/local/bin/system-dbus.sh
autostart=true
autorestart=true
user=root
priority=15
stdout_logfile=/var/log/supervisor/system-dbus.log
stderr_logfile=/var/log/supervisor/system-dbus.err
EOF
DONE

# Session launcher (kept as a separate file so escaping is trivial). It (1) waits for
# Xvnc to publish display :1, (2) starts a system dbus + the login1 shim, (3) runs
# `dbus-run-session` to create a session bus and records its address to
# /tmp/dbus-session-bus-address so the rest of the stack (the exec-stdio server's
# run_command children, ad-hoc shells) can attach to the SAME session bus, then (4)
# execs `gnome-session --session=ubuntu` (GNOME-Shell) under llvmpipe.
#
# gnome-session-binary needs a reachable SYSTEM dbus even in a container; without
# /run/dbus/system_bus_socket it logs "Failed to connect to system bus" and aborts
# before the shell starts. We launch `dbus-daemon --system` first (then the session
# bus via `dbus-run-session`) so GNOME-Shell boots cleanly.
RUN <<'DONE'
cat > /usr/local/bin/start-gnome.sh <<'EOF'
#!/bin/bash
set -e

# Wait for Xvnc to be up before launching the desktop session.
while ! xdpyinfo -display :1 >/dev/null 2>&1; do sleep 0.3; done

# The SYSTEM dbus is brought up by the root-run [program:system-dbus] supervisor
# program (priority 15), NOT here: this script runs as ${USER}, who is no longer in
# group `sudo` (§2.A.5), so a `sudo dbus-daemon` here would hard-abort under set -e.
# Just wait for that program's socket to appear (~4s bridge; system-dbus is priority
# 15, this gnome program is 30, and supervisord does not block between priorities).
for _ in $(seq 1 20); do [ -S /run/dbus/system_bus_socket ] && break; sleep 0.2; done

# logind SHIM: GNOME-Shell 42 hard-requires org.freedesktop.login1 at startup, but a
# container has no systemd. Run a python3-dbusmock mock owning that name on the system
# bus, then load the 'logind' template so it answers the Manager/Session/Seat calls
# gnome-session + the shell make. This is what lets GNOME-Shell boot without systemd.
# Use the SYSTEM python3.10 EXPLICITLY (not bare `python3`): a downstream layer may add
# another interpreter or repoint the `python`/`python3` alternative, but
# python3-dbus/python3-dbusmock + their _dbus_bindings C extension are built for the jammy
# system 3.10 — running the shim under any other interpreter dies "No module named
# _dbus_bindings", the mock never owns login1, and GNOME-Shell then crash-loops. python3.10
# is the jammy system python, always present in the base and every env built on it.
# Reap any stale mock first so a supervisor autorestart of this program (which re-runs
# this script) doesn't leave two mocks contending for the well-known name.
pkill -f 'python3.10 -m dbusmock' 2>/dev/null || true
nohup /usr/bin/python3.10 -m dbusmock --system org.freedesktop.login1 /org/freedesktop/login1 \
    org.freedesktop.login1.Manager >/tmp/logind-mock.log 2>&1 &
for _ in $(seq 1 25); do
    dbus-send --system --dest=org.freedesktop.DBus --print-reply /org/freedesktop/DBus \
        org.freedesktop.DBus.NameHasOwner string:org.freedesktop.login1 2>/dev/null | grep -q true && break
    sleep 0.2
done
/usr/bin/python3.10 - <<'PY' >/tmp/logind-tmpl.log 2>&1 || true
import dbus
obj = dbus.SystemBus().get_object('org.freedesktop.login1', '/org/freedesktop/login1')
obj.AddTemplate('logind', dbus.Dictionary({}, signature='sv'),
                dbus_interface='org.freedesktop.DBus.Mock')
PY

# (The container-incompatible gnome-settings-daemon autostart plugins are removed
# by the root-run [program:system-dbus] program at boot — that step needs root and
# so moved off this ${USER}-run script alongside the system-dbus bring-up.)

# CPU-only: force the mesa software rasterizer (llvmpipe) for mutter's GL compositor.
# No /dev/dri is mapped and no KVM is used — mutter composites entirely on CPU.
export LIBGL_ALWAYS_SOFTWARE=1
export GALLIUM_DRIVER=llvmpipe
export XDG_SESSION_ID=1

exec dbus-run-session -- bash -c '
  echo "$DBUS_SESSION_BUS_ADDRESS" > /tmp/dbus-session-bus-address
  chmod 644 /tmp/dbus-session-bus-address
  # Wallpaper (classic Ubuntu aubergine) — set inside the session bus before
  # gnome-session so it paints from the first frame. enable-animations=false keeps
  # idle CPU near zero under llvmpipe and makes screenshots deterministic.
  gsettings set org.gnome.desktop.background picture-uri "file:///usr/share/backgrounds/warty-final-ubuntu.png" || true
  gsettings set org.gnome.desktop.background picture-uri-dark "file:///usr/share/backgrounds/warty-final-ubuntu.png" || true
  gsettings set org.gnome.desktop.background picture-options "zoom" || true
  gsettings set org.gnome.desktop.background primary-color "#2c001e" || true
  gsettings set org.gnome.desktop.interface enable-animations false || true
  exec gnome-session --session=ubuntu --disable-acceleration-check
'
EOF
chmod +x /usr/local/bin/start-gnome.sh
cat > /etc/supervisor/conf.d/30-gnome.conf <<'EOF'
[program:gnome]
command=/usr/local/bin/start-gnome.sh
autostart=true
autorestart=true
user=user
environment=HOME="/home/user",USER="user",DISPLAY=":1",XDG_RUNTIME_DIR="/tmp/runtime-user",XDG_CONFIG_HOME="/home/user/.config",XDG_DATA_DIRS="/usr/share:/usr/local/share",XDG_CURRENT_DESKTOP="ubuntu:GNOME",XDG_SESSION_TYPE="x11",GNOME_SHELL_SESSION_MODE="ubuntu"
priority=30
stdout_logfile=/var/log/supervisor/gnome.log
stderr_logfile=/var/log/supervisor/gnome.err
EOF
DONE

# ----- gnome-ready: one-shot readiness watchdog -----
# supervisord starts programs in priority order but does NOT wait for one to finish
# initialising. The DBUS-address gate (/tmp/dbus-session-bus-address, written by
# start-gnome.sh before exec'ing gnome-session) is necessary but not sufficient: its
# presence doesn't mean GNOME-Shell actually came up. This watchdog polls for
# gnome-shell (WM + compositor + top panel + dash all live in that one process) and
# writes /tmp/gnome-ready on success or /tmp/gnome-failed on timeout; downstream
# wrappers gate on those flags so they fail-fast when the desktop is broken.
RUN <<'DONE'
cat > /usr/local/bin/wait-for-gnome-ready.sh <<'EOF'
#!/bin/bash
# One-shot watchdog: wait for GNOME-Shell to come up.
# Writes /tmp/gnome-ready on success, /tmp/gnome-failed on timeout.

TIMEOUT="${GNOME_READY_TIMEOUT:-60}"

# 1) X server (30s budget)
for _ in $(seq 1 30); do
    xdpyinfo -display :1 >/dev/null 2>&1 && break
    sleep 1
done
xdpyinfo -display :1 >/dev/null 2>&1 || {
    echo "FATAL: Xvnc never published display :1" > /tmp/gnome-failed
    cat /tmp/gnome-failed >&2
    exit 1
}

# 2) start-gnome.sh reached its exec point (writes the dbus file)
for _ in $(seq 1 30); do
    [ -f /tmp/dbus-session-bus-address ] && break
    sleep 1
done
[ -f /tmp/dbus-session-bus-address ] || {
    echo "FATAL: start-gnome.sh never wrote /tmp/dbus-session-bus-address" > /tmp/gnome-failed
    cat /tmp/gnome-failed >&2
    exit 1
}

# 3) GNOME-Shell HEALTHY: gnome-shell running AND the gnome-session-failed fallback
# ("Oh no") NOT running — held stable for a few seconds. gnome-session-failed can
# flicker up transiently during a cold start, so require a stable window. NOTE pgrep
# -f (not -x) for the fail-whale: its comm is truncated to 15 chars ("gnome-session-f").
STABLE_NEEDED=3
stable=0
for _ in $(seq 1 "$TIMEOUT"); do
    if pgrep -x gnome-shell >/dev/null && ! pgrep -f gnome-session-failed >/dev/null; then
        stable=$((stable + 1))
        if [ "$stable" -ge "$STABLE_NEEDED" ]; then
            touch /tmp/gnome-ready
            echo "GNOME-Shell ready (gnome-shell up, no fail-whale)" >&2
            exit 0
        fi
    else
        stable=0
    fi
    sleep 1
done

cat > /tmp/gnome-failed <<DIAG
FATAL: GNOME-Shell not healthy within ${TIMEOUT}s after dbus came up.
gnome-shell up: $(pgrep -x gnome-shell >/dev/null && echo yes || echo no)
Fail-whale (gnome-session-failed) up: $(pgrep -f gnome-session-failed >/dev/null && echo yes || echo no)
Common causes:
  - The login1 shim (python3-dbusmock) failed to claim org.freedesktop.login1 —
    check /tmp/logind-mock.log and whether gnome-shell is respawning in
    /var/log/supervisor/gnome.err.
  - Host inotify exhaustion at high container concurrency.
    Fix on host: sudo sysctl fs.inotify.max_user_instances=8192
DIAG
cat /tmp/gnome-failed >&2
exit 1
EOF
chmod +x /usr/local/bin/wait-for-gnome-ready.sh
cat > /etc/supervisor/conf.d/31-gnome-ready.conf <<'EOF'
[program:gnome-ready]
command=/usr/local/bin/wait-for-gnome-ready.sh
autostart=true
autorestart=false
startsecs=0
user=user
environment=HOME="/home/user",USER="user",DISPLAY=":1"
priority=31
stdout_logfile=/var/log/supervisor/gnome-ready.log
stderr_logfile=/var/log/supervisor/gnome-ready.err
EOF
DONE

RUN mkdir -p /tmp/runtime-${USER} && chown ${USER}:${USER} /tmp/runtime-${USER} && chmod 700 /tmp/runtime-${USER}

# ----- Desktop reachability: Ctrl+Alt+T -> gnome-terminal -----
# GNOME-Shell provides the launcher surface natively (the vertical ubuntu-dock on the
# left + the app-grid button), so no separate dock is needed. Keep the standard
# terminal shortcut (the first thing an agent tries) as a system-wide dconf default
# via the gnome-settings-daemon media-keys plugin.
# All dconf system-db writes + the single `dconf update` collapse into ONE layer
# (independent static config files; dconf update just needs to run last). Per-file
# rationale is kept as inline shell comments below.
RUN <<'DONE'
mkdir -p /etc/dconf/profile /etc/dconf/db/local.d
printf 'user-db:user\nsystem-db:local\n' > /etc/dconf/profile/user
cat > /etc/dconf/db/local.d/00-terminal-keybinding <<'EOF'
[org/gnome/settings-daemon/plugins/media-keys]
custom-keybindings=['/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/terminal/']

[org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/terminal]
name='Terminal'
command='gnome-terminal'
binding='<Primary><Alt>t'
EOF
# Enable AT-SPI publishing so GTK apps load the atk-bridge and expose their UI on
# the a11y bus (pairs with at-spi2-core above; OSWorld's accessibility-tree oracle
# reads this). System-wide default via the same dconf local db.
cat > /etc/dconf/db/local.d/01-toolkit-accessibility <<'EOF'
[org/gnome/desktop/interface]
toolkit-accessibility=true
EOF
# Desktop icons (ding): stack from the TOP-RIGHT corner down the right edge, with Home
# pinned bottom-right — matching osworld (where ~/Desktop files appear as a right-edge
# column). Set as a system-db default so ding reads it at session start (setting it at
# runtime after ding has laid out doesn't re-flow the icons).
cat > /etc/dconf/db/local.d/02-desktop-icons <<'EOF'
[org/gnome/shell/extensions/ding]
start-corner='top-right'
EOF
# Dock content (GNOME-Shell's ubuntu-dash renders org.gnome.shell favorite-apps).
# Generic default for every env — the base apps a bare sandbox ships; a per-env image
# overwrites this same file with ITS app set (osworld does). A task that sets
# favorite-apps at runtime writes the user dconf db, which overrides this default.
cat > /etc/dconf/db/local.d/03-shell-dock <<'EOF'
[org/gnome/shell]
favorite-apps=['org.gnome.Terminal.desktop', 'org.gnome.Nautilus.desktop', 'org.gnome.gedit.desktop', 'org.gnome.Calculator.desktop']
EOF
# Suppress the "Screen Lock disabled — Screen Locking requires the GNOME display
# manager" notification that GNOME-Shell's ScreenShield raises on EVERY turn-0 when
# lock-enabled is true but no GDM/logind can actually lock (our supervisord + dbusmock
# logind shim has no display manager). The real osworld VM has GDM and never shows it,
# so the lite agent sees a distracting banner the osworld agent never does → OOD.
# Setting lock-enabled=false as a system DEFAULT stops the shield from attempting the
# lock → no banner. Safe for the lock-family tasks (os a4d98375 + its perturb/synth
# variants): their `config` seeds `lock-enabled false` at reset and the agent flips it
# to true, so they override this default anyway. idle-delay=0 means "never idle" (0 is
# disabled, not immediate) so the screen never blanks mid-episode; disable-lock-screen
# blocks the manual lock UI too. Must be a build-time default (the banner fires at
# session start, before apply-settings.sh runs).
cat > /etc/dconf/db/local.d/04-no-lock <<'EOF'
[org/gnome/desktop/screensaver]
lock-enabled=false
idle-activation-enabled=false

[org/gnome/desktop/session]
idle-delay=uint32 0

[org/gnome/desktop/lockdown]
disable-lock-screen=true
EOF
dconf update
DONE

# ----- Ubuntu/GNOME-Shell appearance defaults (osworld-VM-matched; NOT neutral) -----
# Honest label: this is an OSWORLD-VM-matched look, not a generic/neutral one — the base
# bakes GNOME-Shell (Ubuntu session) + Yaru theme + warty wallpaper + 24h clock +
# appmenu:minimize,maximize,close as the shared DEFAULT, so cuaworld / lite.osworld /
# lite.demo currently all match the real osworld VM. The base is deliberately the single
# place desktop appearance lives; an env that needs a different look OVERRIDES by shadowing
# the same dconf `local.d` files at a later apply-settings priority (osworld already does
# this for the dock / file-chooser / desktop-icon slices). ARG-gate the theme/clock/color
# block only when a genuinely non-osworld env lands (none today). GNOME-Shell draws its own top bar (Activities /
# centered clock / aggregate menu) + the vertical ubuntu-dock, so there is no
# gnome-panel layout, no plank, and no panel gtk.css to maintain. Build-time gsettings
# writes go to a transient dconf store and don't persist to the runtime profile, so
# re-apply at container start (gated on gnome-ready). Per-env app settings (e.g.
# LibreOffice profile) live in the per-env image at a later supervisor priority.
RUN <<'DONE'
cat > /usr/local/bin/apply-settings.sh <<'EOF'
#!/bin/bash
for _ in $(seq 1 150); do
    [ -f /tmp/gnome-failed ] && { echo "FATAL: GNOME failed, aborting apply-settings" >&2; exit 1; }
    [ -f /tmp/gnome-ready ]  && break
    sleep 1
done
[ -f /tmp/gnome-ready ] || { echo "FATAL: /tmp/gnome-ready never appeared" >&2; exit 1; }
export DBUS_SESSION_BUS_ADDRESS="$(cat /tmp/dbus-session-bus-address)"

gsettings set org.gnome.desktop.interface gtk-theme "Yaru" 2>/dev/null || true
gsettings set org.gnome.desktop.interface icon-theme "Yaru" 2>/dev/null || true
gsettings set org.gnome.desktop.interface cursor-theme "Yaru" 2>/dev/null || true
gsettings set org.gnome.desktop.interface color-scheme "default" 2>/dev/null || true
gsettings set org.gnome.desktop.wm.preferences theme "Yaru-dark" 2>/dev/null || true
# Window controls: minimize + maximize + close (the Ubuntu/osworld layout). Vanilla
# GNOME defaults to `appmenu:close` (close button only); osworld's titlebars show all
# three, so match it — otherwise a vision agent can't find minimize/maximize.
gsettings set org.gnome.desktop.wm.preferences button-layout 'appmenu:minimize,maximize,close' 2>/dev/null || true
gsettings set org.gnome.desktop.interface toolkit-accessibility true 2>/dev/null || true
gsettings set org.gnome.desktop.background picture-uri "file:///usr/share/backgrounds/warty-final-ubuntu.png" 2>/dev/null || true
# Top-bar clock: match osworld ("Jul 10 06:37") — date + time, no weekday, no seconds.
gsettings set org.gnome.desktop.interface clock-show-date true 2>/dev/null || true
gsettings set org.gnome.desktop.interface clock-show-weekday false 2>/dev/null || true
gsettings set org.gnome.desktop.interface clock-show-seconds false 2>/dev/null || true
gsettings set org.gnome.desktop.interface clock-format "24h" 2>/dev/null || true
# gnome-terminal's CSD headerbar follows theme-variant; unset → 'system' → Yaru LIGHT
# → a white headerbar. Force the dark variant so it matches the dark terminal body.
gsettings set org.gnome.Terminal.Legacy.Settings theme-variant 'dark' 2>/dev/null || true
# Keep gnome-terminal-server RESIDENT: gnome-terminal is a thin D-Bus client, so the
# FIRST launch (the agent's Ctrl+Alt+T, grabbed by gsd-media-keys → `gnome-terminal`)
# otherwise pays a ~2 s cold-start to D-Bus-activate the server — longer than the
# agent's screenshot window, so the very first Ctrl+Alt+T appears to "do nothing" and
# derails multi-app tasks (observed: the terminal never appears in time; e.g. an agent
# then reads a stale focused-app state). A throwaway `gnome-terminal --wait -- true`
# does NOT keep it warm: the server exits again once that window closes. Start the
# server STANDALONE instead — with no windows it stays resident on the session bus,
# owning org.gnome.Terminal, so Ctrl+Alt+T is warm for the whole episode (matches the
# osworld VM's always-warm session). A watchdog respawns it if it ever dies.
# Run the server in the foreground of a detached keep-alive loop: setsid puts it in a
# NEW session so apply-settings.sh (a one-shot supervisord program) exiting doesn't
# SIGHUP it; if the server ever exits (e.g. the agent closes its last terminal), the
# loop respawns it after 2 s. DBUS_SESSION_BUS_ADDRESS (exported above) is inherited so
# it binds the live session bus.
setsid bash -c 'while true; do DISPLAY=:1 /usr/libexec/gnome-terminal-server >/dev/null 2>&1; sleep 2; done' >/dev/null 2>&1 &
EOF
chmod +x /usr/local/bin/apply-settings.sh
# user=user / HOME are templated by the supervisor-config sed pass below.
cat > /etc/supervisor/conf.d/35-apply-settings.conf <<'EOF'
[program:apply-settings]
command=/usr/local/bin/apply-settings.sh
autostart=true
autorestart=false
startsecs=0
user=user
# XDG_CURRENT_DESKTOP must be set here: apply-settings.sh starts the resident
# gnome-terminal-server, and without it the server can't detect GNOME and falls
# back to the legacy titlebar + menubar instead of the stock GNOME CSD header-bar
# (the [program:gnome] env above has it, but this program's env is separate).
environment=HOME="/home/user",USER="user",DISPLAY=":1",XDG_RUNTIME_DIR="/tmp/runtime-user",XDG_CONFIG_HOME="/home/user/.config",XDG_CURRENT_DESKTOP="ubuntu:GNOME"
priority=35
stdout_logfile=/var/log/supervisor/apply-settings.log
stderr_logfile=/var/log/supervisor/apply-settings.err
EOF
DONE

# PulseAudio for volume/audio tasks. A headless container has no ALSA card, so add a
# null sink (volume state lives there) and autostart the daemon (priority 25, before
# apply-settings; XDG_RUNTIME_DIR=/tmp/runtime-user so a terminal the agent opens finds
# the socket). Without a running daemon `pactl` returns "Connection refused".
RUN <<'DONE'
echo 'load-module module-null-sink sink_name=dummy sink_properties=device.description=Dummy' >> /etc/pulse/default.pa
cat > /etc/supervisor/conf.d/25-pulseaudio.conf <<'EOF'
[program:pulseaudio]
command=/usr/bin/pulseaudio --exit-idle-time=-1 --daemonize=no --log-target=stderr
autostart=true
autorestart=true
startsecs=1
user=user
environment=HOME="/home/user",USER="user",XDG_RUNTIME_DIR="/tmp/runtime-user"
priority=25
stdout_logfile=/var/log/supervisor/pulseaudio.log
stderr_logfile=/var/log/supervisor/pulseaudio.err
EOF
DONE

# Template the desktop user into the supervisor program configs. These are
# written above via quoted (<<'EOF') heredocs so ${USER} can't expand
# inline; patch them here in one controlled pass over /etc/supervisor/conf.d
# ONLY (precise anchors — no false hits; a no-op when USER=user). This is
# what makes the GNOME-Shell session run as ${USER} with a matching HOME/runtime
# dir. The `user=root` supervisord master (supervisord.conf) is untouched (no
# `user=user` match); the xvnc/novnc/gnome programs all run as the templated
# desktop user.
RUN sed -i \
        -e "s/^user=user$/user=${USER}/" \
        -e "s#HOME=\"/home/user\"#HOME=\"/home/${USER}\"#g" \
        -e "s#USER=\"user\"#USER=\"${USER}\"#g" \
        -e "s#/tmp/runtime-user#/tmp/runtime-${USER}#g" \
        -e "s#/home/user/.config#/home/${USER}/.config#g" \
        /etc/supervisor/conf.d/*.conf

# Node, and only Node. Several autonomous harnesses are distributed as npm packages, and
# a runtime is a property of the platform — the kind of thing an image is for.
#
# Node, from nodejs.org's own tarball — see sandbox-base-cli for why.
# xz-utils to unpack it; the desktop base does not ship one.
RUN apt-get update && apt-get install -y --no-install-recommends xz-utils \
    && rm -rf /var/lib/apt/lists/*

ARG NODE_VERSION=v22.11.0
ARG NODE_SHA256=83bf07dd343002a26211cf1fcd46a9d9534219aad42ee02847816940bf610a72
RUN set -eux; \
    curl -fsSLo /tmp/node.tar.xz \
        "https://nodejs.org/dist/${NODE_VERSION}/node-${NODE_VERSION}-linux-x64.tar.xz"; \
    echo "${NODE_SHA256}  /tmp/node.tar.xz" | sha256sum -c -; \
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
        --exclude=CHANGELOG.md --exclude=LICENSE --exclude=README.md; \
    rm /tmp/node.tar.xz; \
    node --version; npm --version

# One agent, at one pinned version — the same pin sandbox-base-cli carries.
#
# Which agent a run uses is a property of the experiment, not of the image, and the
# harness treats it that way: it compares what is here against what the run pinned and
# installs the difference. What makes baking necessary anyway is that a sandbox has no
# network — the only egress is the gateway — so an image that ships nothing can only
# ever run an agent in a task that opened its network, and every GUI task is closed.
#
# So this is a cache of the default, not a decision. A run that pins another version and
# can reach a registry gets that version instead, ahead of this one on PATH.
# Bake the guest service. Every exec, file transfer, screenshot and input event in a
# sandbox goes through this, over stdio for containers and TCP for virtual machines.
# Stdlib-only by contract, so it runs on whatever interpreter an image happens to have.
COPY packages/ale-run/src/ale/run/guestd/ /opt/ale/guestd/

# [ISOLATION 5/5] Lock /opt/ale to root:700. Same reason cua-lite locks /opt/lite: the
# agent should not be able to read the guest service's source and learn the protocol
# that drives its own sandbox. Free, because the service runs as root.
RUN chown -R root:root /opt/ale && chmod -R go-rwx /opt/ale


# What this image is, so the engine reads rather than guesses. Its command starts the
# desktop session below, and a sandbox built from it is not ready until that session can
# actually be photographed. See docs/specs/sandbox-image.md.
LABEL ale.gui="true" \
      ale.user="${USER}"

EXPOSE 5901 6901

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/supervisord.conf"]
