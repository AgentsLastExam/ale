"""Agent harnesses.

Two families, distinguished by who owns the interaction loop:

* autonomous — the agent runs its own loop; we supply a prompt and accept the result.
* policy — the framework owns the observe/act loop and asks the harness per step.

Harnesses reach sandboxes through the ``ale.core`` contract, never through a provider.
"""
