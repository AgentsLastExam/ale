"""Sandbox providers: local container and virtual machine backends.

A provider creates and destroys sandboxes, attaches a guest transport, applies the
network policy, and declares its capabilities. All in-sandbox operations go through
the guest service, never through provider-specific code paths.
"""
