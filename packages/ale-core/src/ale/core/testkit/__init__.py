"""Conformance suites.

Contracts are only real if every implementation is held to the same assertions. These
suites are parametrised over an implementation and shipped with ``ale-core`` so a new
provider — in this repository or in a domain extension — proves itself with the same
tests the built-in ones pass.

Usage in a test module::

    from ale.core.testkit import ProviderConformance

    class TestDocker(ProviderConformance):
        provider = DockerProvider()
"""

from ale.core.testkit.provider import ProviderConformance

__all__ = ["ProviderConformance"]
