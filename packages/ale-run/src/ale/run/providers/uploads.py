"""Destination permissions for directories uploaded by either Provider."""

from __future__ import annotations

from ale.core.errors import ProviderStartError
from ale.core.taskspec import OperatingSystem
from ale.run.transport import GuestClient

_LINUX_PERMISSIONS = """
import os, pwd, stat, sys
root, owner = sys.argv[1:]
account = pwd.getpwnam(owner)
def apply(path):
    mode = os.lstat(path).st_mode
    os.chown(path, account.pw_uid, account.pw_gid, follow_symlinks=False)
    if not stat.S_ISLNK(mode):
        os.chmod(path, 0o755 if stat.S_ISDIR(mode) else 0o644)
apply(root)
for parent, directories, files in os.walk(root, followlinks=False):
    for name in directories + files:
        apply(os.path.join(parent, name))
"""

_WINDOWS_PERMISSIONS = """
$ErrorActionPreference = 'Stop'
$system = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-18')
$admins = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
$users = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-32-545')
$owner = $admins
if ($agent) {
    $owner = [System.Security.Principal.NTAccount]::new($agent).Translate(
        [System.Security.Principal.SecurityIdentifier])
}
$items = @(Get-Item -LiteralPath $target -Force) +
    @(Get-ChildItem -LiteralPath $target -Force -Recurse)
foreach ($item in $items) {
    if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { continue }
    $inherit = [System.Security.AccessControl.InheritanceFlags]::None
    if ($item.PSIsContainer) {
        $acl = [System.Security.AccessControl.DirectorySecurity]::new()
        $inherit = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    } else {
        $acl = [System.Security.AccessControl.FileSecurity]::new()
    }
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($owner)
    foreach ($sid in @($system, $admins, $users)) {
        $rights = if ($sid -eq $users) { 'ReadAndExecute' } else { 'FullControl' }
        $acl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
            $sid, $rights, $inherit, 'None', 'Allow'))
    }
    if ($agent) {
        $acl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
            $owner, 'Modify', $inherit, 'None', 'Allow'))
    }
    Set-Acl -LiteralPath $item.FullName -AclObject $acl
}
"""


async def set_upload_permissions(
    client: GuestClient, target: str, *, os: OperatingSystem, agent: str | None
) -> None:
    """Ignore Host modes and inherited ACLs; give the selected identity write access."""
    if os is OperatingSystem.WINDOWS:
        target_literal = target.replace("'", "''")
        agent_literal = (agent or "").replace("'", "''")
        command = f"$target = '{target_literal}'; $agent = '{agent_literal}';\n"
        argv = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command + _WINDOWS_PERMISSIONS,
        ]
    else:
        argv = ["python3", "-c", _LINUX_PERMISSIONS, target, agent or "root"]
    code, _, stderr, _ = await client.exec(argv)
    if code != 0:
        raise ProviderStartError(f"could not set upload permissions for {target}: {stderr.strip()}")
