"""The agent's elevation, set at every Linux sandbox start, and the check that proves it.

Elevation is not part of a prepared image, so one image serves a Task with and without
``[agent].sudo``, its solver and its verifier, and a Sandbox continued from a snapshot of
either. guestd runs as root, so the Provider sets the agent's elevation through it before
any Task code runs, then proves both identities.
"""

from __future__ import annotations

from ale.core.errors import ProviderCapabilityError, ProviderStartError, TaskDefinitionError
from ale.run import guestd
from ale.run.transport import GuestClient

#: The account's uid, then whether it can elevate without a password ("none": no sudo).
_PROBE = (
    'printf "%s %s" "$(id -u)" "$(if ! command -v sudo >/dev/null 2>&1; then echo none; '
    'elif sudo -n true >/dev/null 2>&1; then echo yes; else echo no; fi)"'
)

#: Run as root on the framework's Python. Sudo applies the last rule that matches, so the
#: rule appended to /etc/sudoers decides, whatever the image's own rules, the groups and
#: the account's running processes say. Revoking also takes the account out of the groups
#: distributions make administrators, which programs other than sudo, such as polkit, trust.
_SET = r"""
import os, pwd, sys

user, sudo = sys.argv[1], sys.argv[2] == "true"
if pwd.getpwnam(user).pw_uid == 0 and not sudo:
    sys.exit(3)
MARK = "# ALE: the agent's elevation in this sandbox, set at every start"
ADMINISTRATORS = {"sudo", "wheel", "admin"}


def rewrite(path, edit, create=False):
    try:
        with open(path) as file:
            lines = file.read().splitlines()
    except FileNotFoundError:
        if not create:
            return
        lines = []
    edited = edit(lines)
    if edited != lines:
        # In place, so the file keeps its owner and mode.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o440)
        with os.fdopen(descriptor, "w") as file:
            file.write("".join(line + "\n" for line in edited))


def policy(lines):
    kept = [
        line for index, line in enumerate(lines)
        if line != MARK and (index == 0 or lines[index - 1] != MARK)
    ]
    rule = f"{user} ALL=(ALL:ALL) " + ("NOPASSWD: ALL" if sudo else "!ALL")
    return [*kept, MARK, rule]


def without_user(*columns):
    def edit(lines):
        edited = []
        for line in lines:
            fields = line.split(":")
            if len(fields) == 4 and fields[0] in ADMINISTRATORS:
                for column in columns:
                    fields[column] = ",".join(
                        name for name in fields[column].split(",") if name and name != user
                    )
            edited.append(":".join(fields))
        return edited
    return edit


rewrite("/etc/sudoers", policy, create=sudo)
if not sudo:
    rewrite("/etc/group", without_user(3))
    rewrite("/etc/gshadow", without_user(2, 3))
"""


async def set_elevation(client: GuestClient, agent_user: str, *, sudo: bool) -> None:
    """Grant the agent elevation for this start when ``sudo``, else revoke every way up.

    An account that is root cannot be kept from elevating, so a Task that names one without
    declaring ``[agent].sudo`` is refused.
    """
    code, _, stderr, _ = await client.exec(
        [guestd.PYTHON["linux"], "-I", "-c", _SET, agent_user, "true" if sudo else "false"],
        timeout_sec=60,
    )
    if code == 3:
        raise TaskDefinitionError(
            f"[agent].user {agent_user} is root, which nothing can keep from elevating; "
            "name a non-root account or declare [agent].sudo = true"
        )
    if code != 0:
        raise ProviderStartError(
            f"could not {'grant' if sudo else 'revoke'} {agent_user}'s elevation: "
            f"{stderr.strip()[-2000:]}"
        )


async def verify_identities(client: GuestClient, agent_user: str, *, sudo: bool) -> None:
    """Prove the framework is root and the agent can elevate exactly when asked to.

    Both directions of the agent check matter. A grant that was made but does not work
    tells a Task it had a privilege it never received; a way up it never asked for (a root
    agent account, a rule the revocation did not override) lets the agent rewrite the
    conditions of its own measurement while provenance records ``sudo: false``. A Provider
    whose platform adds another way up checks that one itself.
    """
    framework_uid, _ = await _probe(client, None)
    if framework_uid != "0":
        raise ProviderCapabilityError(
            f"the guest service runs as uid {framework_uid}, not root, so framework and "
            "agent work cannot be told apart"
        )
    agent_uid, elevates = await _probe(client, agent_user)
    if sudo and elevates != "yes":
        why = (
            "the image has no sudo, and ALE found no package manager to install it with"
            if elevates == "none"
            else "sudo refuses the grant ALE made"
        )
        raise ProviderCapabilityError(
            f"elevated privileges were requested but {agent_user} cannot elevate in this "
            f"sandbox: {why}"
        )
    if not sudo and (agent_uid == "0" or elevates == "yes"):
        how = "is root" if agent_uid == "0" else "can still use sudo"
        raise ProviderCapabilityError(
            f"{agent_user} {how}, but the Task did not request elevated privileges"
        )


async def _probe(client: GuestClient, user: str | None) -> tuple[str, str]:
    code, stdout, stderr, _ = await client.exec(["sh", "-c", _PROBE], run_as=user, timeout_sec=30)
    fields = stdout.split()
    if code != 0 or len(fields) != 2 or not fields[0].isdigit():
        who = user or "the framework"
        raise ProviderStartError(f"could not check the privileges of {who}: {stderr.strip()}")
    return fields[0], fields[1]
