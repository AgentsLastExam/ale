"""Conformance suites.

Contracts are only real if every implementation is held to the same assertions. These
suites are parametrised over an implementation and shipped with ``orbit-core`` so a new
provider — in this repository or in a domain extension — proves itself with the same
tests the built-in ones pass.

Usage in a test module::

    from orbit.core.testkit import ProviderConformance

    class TestDocker(ProviderConformance):
        provider = DockerProvider()
"""

from orbit.core.testkit.harness import AutonomousHarnessConformance
from orbit.core.testkit.provider import NESTED_CHECKS, ProviderConformance

__all__ = ["NESTED_CHECKS", "AutonomousHarnessConformance", "ProviderConformance"]
