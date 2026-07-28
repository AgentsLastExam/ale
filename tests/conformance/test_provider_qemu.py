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

DEFAULT_IMAGE = Path.home() / ".cache/ale/images/ale-ubuntu-desktop.qcow2"
IMAGE = Path(os.environ.get("ALE_QEMU_IMAGE", DEFAULT_IMAGE))

pytestmark = [
    pytest.mark.conformance,
    pytest.mark.needs_kvm,
    pytest.mark.skipif(
        not IMAGE.is_file(),
        reason=f"no guest image at {IMAGE}; build one with images/base/qemu/build-desktop.sh",
    ),
]


class TestQemuProvider(ProviderConformance):
    """Every assertion in the shared suite, run against a virtual machine."""

    provider = QemuProvider(image=IMAGE)
    image_ref = "ale-ubuntu-desktop"
    #: The same disk. There is one VM guest and it has a desktop, so the GUI half of
    #: the shared suite runs against it rather than being skipped.
    gui_image_ref = "ale-ubuntu-desktop"
