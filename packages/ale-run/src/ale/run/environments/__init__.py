"""Environments: how one task becomes one episode.

``StandardEnvironment`` implements the linear provision/setup/agent/verify flow that
most tasks need. Domain-specific orchestration lives in extension packages that
subclass the ``ale.core`` contract.
"""
