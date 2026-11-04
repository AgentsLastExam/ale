#!/bin/sh
# Install the ALE framework into a stopped Tart VM's disk, offline, as root on the Mac.
#
# usage: framework-macos DISK USER CONTROL IDENTITY < payload.tar
#
# DISK is the disk.img of an ale-frame-* Tart VM owned by the calling user, USER the Task's
# account, CONTROL the account the Provider logs in as, and IDENTITY the framework identity
# recorded in /opt/ale/framework. The payload holds python.tar.gz, driver.tar.gz and
# guestd/*.py. On success it prints the account's home and uid as JSON.
#
# The disk is the image's own, so nothing on it is trusted: no path is followed through a
# link, its volumes are mounted without device files or set-uid programs, and none of its
# programs run. scripts/setup_macos.py installs this file root-owned with a sudoers rule
# for this program alone; the Provider refuses to frame when it differs from its own copy.
set -eu
umask 022
PATH=/usr/bin:/bin:/usr/sbin:/sbin
LC_ALL=C
export PATH LC_ALL

fail() {
    echo "ALE: $*" >&2
    exit 1
}

[ "$#" -eq 4 ] || fail "usage: framework-macos DISK USER CONTROL IDENTITY < payload.tar"
disk=$1 user=$2 control=$3 identity=$4
caller=${SUDO_UID:-}
[ -n "$caller" ] && [ "$caller" != 0 ] || fail "run this through sudo as the Orchard user"
for name in "$user" "$control"; do
    case "$name" in
        "" | *[!a-z0-9_.-]* | [!a-z_]*) fail "'$name' is not a macOS account name" ;;
    esac
done
case "$identity" in
    sha256:*[!0-9a-f]* | sha256:) fail "invalid framework identity" ;;
    sha256:*) ;;
    *) fail "invalid framework identity" ;;
esac
case "$disk" in
    /*/ale-frame-*/disk.img) ;;
    *) fail "the disk must be the disk.img of an ale-frame-* VM" ;;
esac
[ -f "$disk" ] && [ ! -L "$disk" ] || fail "the disk is not a regular file"
[ "$(stat -f %u "$disk")" = "$caller" ] || fail "the disk does not belong to the caller"

staging=$(mktemp -d /private/var/run/ale-framework.XXXXXX)
mnt=$staging/mnt
dev=
cleanup() {
    if mount | grep -q " on $mnt ("; then
        umount "$mnt" 2>/dev/null || umount -f "$mnt" 2>/dev/null || true
    fi
    if [ -n "$dev" ]; then
        hdiutil detach "$dev" -quiet 2>/dev/null || hdiutil detach -force "$dev" -quiet || true
    fi
    rm -rf "$staging"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
mkdir "$mnt" "$staging/payload" "$staging/driver"
tar -xf - -C "$staging/payload"

# Attach without mounting: nothing appears in the Finder, and only the Data volume, which
# holds everything framing writes, is mounted, hidden from the desktop.
dev=$(hdiutil attach -nomount -noverify -noautofsck \
    -imagekey diskimage-class=CRawDiskImage "$disk" | awk '/GUID_partition_scheme/ {print $1}')
[ -n "$dev" ] || fail "the VM disk has no partition table"
data=
for part in $(diskutil list "$dev" | grep -o "${dev#/dev/}s[0-9]*"); do
    container=$(diskutil info -plist "$part" |
        plutil -extract APFSContainerReference raw - 2>/dev/null) || continue
    volumes=$(diskutil apfs list -plist "$container")
    n=0
    while volume=$(printf '%s' "$volumes" |
        plutil -extract "Containers.0.Volumes.$n.DeviceIdentifier" raw - 2>/dev/null); do
        role=$(printf '%s' "$volumes" |
            plutil -extract "Containers.0.Volumes.$n.Roles.0" raw - 2>/dev/null || true)
        if [ "$role" = Data ]; then
            [ -z "$data" ] || fail "the VM disk has more than one macOS Data volume"
            data=$volume
        fi
        n=$((n + 1))
    done
done
[ -n "$data" ] || fail "the VM disk has no macOS Data volume"
mount_apfs -o nobrowse,nodev,nosuid "/dev/$data" "$mnt" 2>/dev/null ||
    fail "could not mount the image's Data volume; FileVault must be off in the image"

# inside REL: REL names a path on the image that passes through no link.
inside() {
    path=$mnt rest=$1
    while [ -n "$rest" ]; do
        part=${rest%%/*}
        case "$rest" in */*) rest=${rest#*/} ;; *) rest= ;; esac
        case "$part" in "" | .) continue ;; ..) return 1 ;; esac
        path=$path/$part
        [ ! -L "$path" ] || return 1
    done
}
need() {
    inside "$1" || fail "/$1 on the image passes through a link"
}

account() {
    plutil -extract "$2.0" raw "$mnt/private/var/db/dslocal/nodes/Default/users/$1.plist" \
        2>/dev/null
}
for name in "$user" "$control"; do
    need "private/var/db/dslocal/nodes/Default/users/$name.plist"
done
[ -f "$mnt/private/var/db/dslocal/nodes/Default/users/$user.plist" ] ||
    fail "the agent account '$user' does not exist in this image; name an existing account" \
        "with [agent].user in task.toml"
[ -f "$mnt/private/var/db/dslocal/nodes/Default/users/$control.plist" ] ||
    fail "the image has no '$control' account, which ALE logs in to the VM as"
uid=$(account "$user" uid) && gid=$(account "$user" gid) && home=$(account "$user" home) ||
    fail "the agent account '$user' has no uid, group or home"
case "$uid" in "" | *[!0-9]* | 0) fail "the agent account '$user' must not be root" ;; esac
case "$gid" in "" | *[!0-9]*) fail "the agent account '$user' has no primary group" ;; esac
case "$home" in
    *[\"\\]*) fail "the agent account '$user' has an unusable home path" ;;
    /*) ;;
    *) fail "the agent account '$user' has no absolute home" ;;
esac
need "${home#/}"
[ -d "$mnt$home" ] || fail "the agent account '$user' has no home directory"

# ALE drives the desktop of the Task's account, so the image must sign it in at boot.
need Library/Preferences/com.apple.loginwindow.plist
[ "$(plutil -extract autoLoginUser raw "$mnt/Library/Preferences/com.apple.loginwindow.plist" \
    2>/dev/null)" = "$user" ] ||
    fail "the image does not sign '$user' in automatically; enable automatic login for the" \
        "Task's account"
need private/etc/kcpassword
[ -f "$mnt/private/etc/kcpassword" ] ||
    fail "automatic login for '$user' has no stored password (/etc/kcpassword)"

# The framework: guestd on a Python of its own, readable by every account, writable by root.
need opt/ale
[ -d "$mnt/opt" ] || mkdir -m 0755 "$mnt/opt"
rm -rf "$mnt/opt/ale"
mkdir -m 0755 "$mnt/opt/ale"
tar -xzf "$staging/payload/python.tar.gz" -C "$mnt/opt/ale"
chown -R 0:0 "$mnt/opt/ale"
chmod -R go-w "$mnt/opt/ale/python"
mkdir -m 0700 "$mnt/opt/ale/guestd" "$mnt/opt/ale/bin"
cp -P "$staging"/payload/guestd/*.py "$mnt/opt/ale/guestd/"
chmod 0600 "$mnt"/opt/ale/guestd/*.py
ln -s /opt/ale/python/bin/python3 "$mnt/opt/ale/bin/python3"

# Cua Driver, signed, where LaunchServices starts it under its own identity.
tar -xzf "$staging/payload/driver.tar.gz" -C "$staging/driver"
app=$(find "$staging/driver" -name CuaDriver.app -type d -prune)
[ -n "$app" ] && [ "$(printf '%s\n' "$app" | wc -l)" -eq 1 ] ||
    fail "the Cua Driver archive does not hold one CuaDriver.app"
codesign --verify --deep --strict "$app" || fail "the Cua Driver app is not validly signed"
need Applications/CuaDriver.app
rm -rf "$mnt/Applications/CuaDriver.app"
ditto "$app" "$mnt/Applications/CuaDriver.app"
chown -R 0:0 "$mnt/Applications/CuaDriver.app"
requirement=$(codesign -d -r- "$app" 2>&1 | sed -n 's/^designated => //p')
[ -n "$requirement" ] || fail "the Cua Driver app has no designated requirement"
printf '%s' "$requirement" | csreq -r- -b "$staging/requirement"
blob=$(od -An -v -tx1 "$staging/requirement" | tr -d ' \n')

# Accessibility and Screen Recording for the Driver, as System Settings records them.
tcc="Library/Application Support/com.apple.TCC"
for name in TCC.db TCC.db-journal TCC.db-wal TCC.db-shm; do need "$tcc/$name"; done
[ -f "$mnt/$tcc/TCC.db" ] || fail "the image has no system privacy database"
for name in TCC.db TCC.db-journal TCC.db-wal TCC.db-shm; do
    [ ! -f "$mnt/$tcc/$name" ] || cp "$mnt/$tcc/$name" "$staging/$name"
done
values=
for service in kTCCServiceAccessibility kTCCServiceScreenCapture; do
    values="$values${values:+, }('$service', 'com.trycua.driver', 0, 2, 4, 1, X'$blob', 0, 'UNUSED', 0)"
done
sqlite3 "$staging/TCC.db" "PRAGMA journal_mode=DELETE; INSERT OR REPLACE INTO access
    (service, client, client_type, auth_value, auth_reason, auth_version, csreq,
     indirect_object_identifier_type, indirect_object_identifier, flags) VALUES $values;" \
    >/dev/null || fail "could not record the Driver's permissions in the image"
cat "$staging/TCC.db" > "$mnt/$tcc/TCC.db"
rm -f "$mnt/$tcc/TCC.db-journal" "$mnt/$tcc/TCC.db-wal" "$mnt/$tcc/TCC.db-shm"

# The account's approval for the Driver to capture without the system window picker. A hint
# date far ahead keeps macOS from asking again later; each sandbox clones this state.
group="${home#/}/Library/Group Containers"
replayd="$group/group.com.apple.replayd"
need "$replayd/ScreenCaptureApprovals.plist"
for dir in "${home#/}/Library" "$group" "$replayd"; do
    if [ ! -d "$mnt/$dir" ]; then
        mkdir -m 0700 "$mnt/$dir"
        chown "$uid:$gid" "$mnt/$dir"
    fi
done
approvals="$mnt/$replayd/ScreenCaptureApprovals.plist"
now=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
cat > "$staging/approval.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>com.trycua.driver</key><dict>
<key>kScreenCaptureAlertableUsageCount</key><integer>0</integer>
<key>kScreenCaptureApprovalLastAlerted</key><date>$now</date>
<key>kScreenCaptureApprovalLastUsed</key><date>$now</date>
<key>kScreenCapturePrivacyHintDate</key><date>4000-01-01T00:00:00Z</date>
<key>kScreenCapturePrivacyHintPolicy</key><integer>2592000</integer>
</dict></dict></plist>
PLIST
buddy=/usr/libexec/PlistBuddy
if [ -f "$approvals" ]; then
    cp "$approvals" "$staging/approvals.plist"
    "$buddy" -c "Delete :com.trycua.driver" "$staging/approvals.plist" >/dev/null 2>&1 || true
    "$buddy" -c "Merge $staging/approval.plist" "$staging/approvals.plist" >/dev/null
else
    cp "$staging/approval.plist" "$staging/approvals.plist"
fi
"$buddy" -c "Print :com.trycua.driver:kScreenCaptureApprovalLastAlerted" \
    "$staging/approvals.plist" >/dev/null 2>&1 || fail "could not record the Driver's capture approval"
cat "$staging/approvals.plist" > "$approvals"
chown "$uid:$gid" "$approvals"
chmod 0600 "$approvals"

# The account ALE logs in as: Remote Login on, and sudo without a password.
need private/etc/sudoers.d/ale-control
[ -d "$mnt/private/etc/sudoers.d" ] || mkdir -m 0755 "$mnt/private/etc/sudoers.d"
printf '%s ALL=(ALL) NOPASSWD: ALL\n' "$control" > "$mnt/private/etc/sudoers.d/ale-control"
chown 0:0 "$mnt/private/etc/sudoers.d/ale-control"
chmod 0440 "$mnt/private/etc/sudoers.d/ale-control"
overrides=private/var/db/com.apple.xpc.launchd/disabled.plist
need "$overrides"
[ ! -f "$mnt/$overrides" ] || cp "$mnt/$overrides" "$staging/disabled.plist"
"$buddy" -c "Delete :com.openssh.sshd" "$staging/disabled.plist" >/dev/null 2>&1 || true
"$buddy" -c "Add :com.openssh.sshd bool false" "$staging/disabled.plist" >/dev/null
[ -d "$mnt/${overrides%/*}" ] || mkdir -m 0755 "$mnt/${overrides%/*}"
cat "$staging/disabled.plist" > "$mnt/$overrides"
chown 0:0 "$mnt/$overrides"
chmod 0644 "$mnt/$overrides"

printf '%s\n' "$identity" > "$mnt/opt/ale/framework"
printf '{"agent_home": "%s", "agent_uid": %s}\n' "$home" "$uid"
