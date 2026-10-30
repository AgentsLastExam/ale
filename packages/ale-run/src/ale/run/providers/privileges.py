"""The identity check every Linux sandbox passes before it is handed out."""

from __future__ import annotations

from ale.core.errors import ProviderCapabilityError, ProviderStartError
from ale.run.transport import GuestClient

#: The account's uid, then whether it can elevate without a password.
_PROBE = (
    'printf "%s %s" "$(id -u)" '
    '"$(command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1 && echo yes || echo no)"'
)


async def verify_identities(client: GuestClient, agent_user: str, *, sudo: bool) -> None:
    """Prove the framework is root and the agent can elevate exactly when asked to.

    Both directions of the agent check matter. A grant that was configured but does not
    work tells a Task it had a privilege it never received; a way up it never asked for
    (a root agent account, a stray sudoers rule) lets the agent rewrite the conditions of
    its own measurement while provenance records ``sudo: false``. A Provider whose
    platform adds another way up checks that one itself.
    """
    framework_uid, _ = await _probe(client, None)
    if framework_uid != "0":
        raise ProviderCapabilityError(
            f"the guest service runs as uid {framework_uid}, not root, so framework and "
            "agent work cannot be told apart"
        )
    agent_uid, elevates = await _probe(client, agent_user)
    if sudo and not elevates:
        raise ProviderCapabilityError(
            f"elevated privileges were requested but {agent_user} cannot elevate in this "
            "sandbox; prepare the image with [agent].sudo = true"
        )
    if not sudo and (agent_uid == "0" or elevates):
        how = "is root" if agent_uid == "0" else "can use sudo"
        raise ProviderCapabilityError(
            f"{agent_user} {how}, but the Task did not request elevated privileges; use a "
            "non-root [agent].user without a sudo grant, or declare [agent].sudo = true"
        )


async def _probe(client: GuestClient, user: str | None) -> tuple[str, bool]:
    code, stdout, stderr, _ = await client.exec(["sh", "-c", _PROBE], run_as=user, timeout_sec=30)
    fields = stdout.split()
    if code != 0 or len(fields) != 2 or not fields[0].isdigit():
        who = user or "the framework"
        raise ProviderStartError(f"could not check the privileges of {who}: {stderr.strip()}")
    return fields[0], fields[1] == "yes"
