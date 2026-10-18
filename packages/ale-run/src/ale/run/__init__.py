"""ALE orchestrator.

Implements the contracts declared in :mod:`ale.core`: sandbox providers, the model
gateway, the in-sandbox guest service, agent harnesses, the episode engine, the run
ledger and the command line interface.
"""

__all__ = ["IMAGE_REGISTRY", "__version__", "ale_image"]

__version__ = "0.1.1"

#: Where ALE publishes its images. Each is published once under every engine version and
#: never overwritten, so an engine always runs exactly the images of its own version.
IMAGE_REGISTRY = "ghcr.io/agentslastexam"


def ale_image(repository: str) -> str:
    """The ALE image this engine version uses, such as ``ale-qemu-runner``."""
    return f"{IMAGE_REGISTRY}/{repository}:{__version__}"
