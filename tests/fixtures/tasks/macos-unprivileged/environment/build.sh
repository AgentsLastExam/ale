#!/bin/bash
# An administrator account with passwordless sudo that the image signs in, so that a Task
# declaring sudo = false shows Orbit taking both away at boot. ORBIT_HOME is the home the
# account must have, and the file left there shows the build wrote to the Task's account.
set -euo pipefail
user=tester
password=orbit-tester
sysadminctl -addUser "$user" -fullName "Orbit Tester" -password "$password" -admin \
    -home "$ORBIT_HOME" -shell /bin/zsh 2>&1
createhomedir -c -u "$user" > /dev/null
printf '%s ALL=(ALL) NOPASSWD: ALL\n' "$user" > "/etc/sudoers.d/$user"
chmod 0440 "/etc/sudoers.d/$user"
defaults write /Library/Preferences/com.apple.loginwindow autoLoginUser "$user"
/opt/orbit-python/bin/python3 -I - "$password" <<'PY'
import sys

key = bytes([0x7D, 0x89, 0x52, 0x23, 0xD2, 0xBC, 0xDD, 0xEA, 0xA3, 0xB9, 0x1F])
plain = sys.argv[1].encode()
plain += b"\0" * (12 - len(plain) % 12)
with open("/etc/kcpassword", "wb") as out:
    out.write(bytes(b ^ key[i % len(key)] for i, b in enumerate(plain)))
PY
chmod 0600 /etc/kcpassword
plist="$ORBIT_HOME/Library/Preferences/com.apple.SetupAssistant"
mkdir -p "$ORBIT_HOME/Library/Preferences"
for key in DidSeeCloudSetup DidSeePrivacy DidSeeSiriSetup DidSeeScreenTime \
    DidSeeAppearanceSetup DidSeeAccessibility DidSeeTouchIDSetup DidSeeActivationLock \
    DidSeeiCloudLoginForStorageServices DidSeeTermsOfAddress DidSeeApplePaySetup \
    DidSeeAvatarSetup DidSeeSyncSetup DidSeeSyncSetup2 DidSeeTrueTonePrivacy \
    DidSeeIntelligence DidSeeAppleIntelligence DidSeeWallpaper DidSeeLockdownMode; do
    defaults write "$plist" "$key" -bool true
done
defaults write "$plist" LastSeenCloudProductVersion "$(sw_vers -productVersion)"
defaults write "$plist" LastSeenBuddyBuildVersion "$(sw_vers -buildVersion)"
defaults write "$plist" LastPreLoginTasksPerformedVersion "$(sw_vers -productVersion)"
defaults write "$plist" LastPreLoginTasksPerformedBuild "$(sw_vers -buildVersion)"
echo built > "$ORBIT_HOME/from-build.txt"
chown -R "$user:staff" "$ORBIT_HOME/Library" "$ORBIT_HOME/from-build.txt"
