"""ALE orchestrator.

Implements the contracts declared in :mod:`ale.core`: sandbox providers, the model
gateway, the in-sandbox guest service, agent harnesses, the episode engine, the run
ledger and the command line interface.
"""

__all__ = ["IMAGE_REGISTRY", "__version__", "ale_image"]

__version__ = "0.1.1"

#: Where ALE publishes its images, each version once and never overwritten.
IMAGE_REGISTRY = "ghcr.io/agentslastexam"

#: The current version of each Linux base. A base carries no part of the framework, so it
#: has its own version, raised when the base itself changes.
BASES = {"container-ubuntu22-base": "0.1.1", "vm-ubuntu24-base": "0.1.1"}


def ale_image(repository: str) -> str:
    """An ALE image this engine uses: a base at its current version, and a runtime image
    such as ``ale-qemu-runner``, which runs the engine's own logic, at the engine's."""
    return f"{IMAGE_REGISTRY}/{repository}:{BASES.get(repository, __version__)}"
