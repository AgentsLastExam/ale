#!/usr/bin/env bash
# Bring up a desktop, then run whatever the provider asked for.
#
# A container has no display manager, so nothing starts X on its own. Without this the
# image looks like a desktop image and behaves like a headless one: every screenshot
# fails with "unable to open display", which reads as a broken sandbox rather than a
# missing service.
#
# Distilled from the previous framework's entrypoint for the same base image. What is
# deliberately *not* carried over is its computer-server on :5000 — that is the piece
# our guest service replaces, and running both would mean two things owning the screen.
set -u

export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-/tmp/.Xauthority}"

mkdir -p /tmp/.X11-unix 2>/dev/null || true
chmod 1777 /tmp/.X11-unix 2>/dev/null || true
rm -f /tmp/.X0-lock 2>/dev/null || true
touch "$XAUTHORITY" 2>/dev/null || true

Xvfb :0 -screen 0 "${ALE_SCREEN_RESOLUTION:-1024x768}x24" -ac -nolisten tcp \
    >/tmp/xvfb.log 2>&1 &

# Wait for the socket rather than sleeping a fixed amount: the first screenshot can
# arrive as soon as the provider says the sandbox is ready.
for _ in $(seq 1 50); do
    [ -S /tmp/.X11-unix/X0 ] && break
    sleep 0.2
done

# A bare Xvfb has no window manager, so there is no focus, no raising, no panel — just a
# black root window until something draws on it. XFCE needs a session bus but not
# systemd-logind, so unlike GNOME it starts cleanly in a container.
if [ "${ALE_DESKTOP:-1}" = "1" ] && command -v startxfce4 >/dev/null 2>&1; then
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/xdg-$(id -u)}"
    mkdir -p "$XDG_RUNTIME_DIR" && chmod 700 "$XDG_RUNTIME_DIR" 2>/dev/null || true
    if command -v dbus-launch >/dev/null 2>&1; then
        eval "$(dbus-launch --sh-syntax)"
        export DBUS_SESSION_BUS_ADDRESS
    fi
    (
        startxfce4 >/tmp/xfce.log 2>&1 &
        for _ in $(seq 1 40); do
            xdotool search --class xfdesktop >/dev/null 2>&1 && break
            sleep 0.5
        done
        # No GPU under Xvfb, so compositing costs frames and buys nothing.
        xfconf-query -c xfwm4 -p /general/use_compositing -s false 2>/dev/null || true
    ) &
fi

exec "$@"
