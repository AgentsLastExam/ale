"""The VM backend against the same suite the container backend passes.

One suite, two backends, no per-provider assertions: that is what makes "the sandbox
contract is real" a claim rather than an aspiration, and what turns the future OS
roadmap into "swap the guest image".

Needs KVM and a built guest image, so it skips rather than fails where either is absent.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ale.core.testkit import ProviderConformance
from ale.run.providers.qemu import QemuProvider

IMAGE = Path(os.environ.get("ALE_QEMU_IMAGE", Path.home() / ".cache/ale/images/ale-ubuntu22.qcow2"))

pytestmark = [
    pytest.mark.conformance,
    pytest.mark.needs_kvm,
    pytest.mark.skipif(
        not IMAGE.is_file(),
        reason=f"no guest image at {IMAGE}; build one with images/base/qemu/build.sh",
    ),
]


class TestQemuProvider(ProviderConformance):
    """Every assertion in the shared suite, run against a virtual machine."""

    provider = QemuProvider(image=IMAGE)
    image_ref = "ale-ubuntu22"
