# QEMU backend images

The QEMU Provider boots one fresh qcow2 overlay per Sandbox inside
`images/base/qemu-runner`. The Host needs Docker and `/dev/kvm`; Tasks declare
`image.kind: vm`, not a Provider name.

Linux VM Tasks normally end their `image/Dockerfile` in `vm-ubuntu24-base`. ALE builds
that OCI content and converts it with the pinned VM materializer. The old ISO builder in
this directory remains only for existing local guests.

Windows uses the private BYOL seed and maintained post-processing recipe under
`images/base/windows/`. ALE does not redistribute Windows, claim an ISO-reproducible
Win10 build, create ready snapshots, or maintain a warm pool.

At runtime the Provider:

1. resolves the Task's prepared VM image;
2. creates an episode overlay over the read-only qcow2;
3. cold-boots it through the runner;
4. validates guestd's OS, agent user/home, and desktop contract;
5. enforces Task egress in the runner network namespace;
6. destroys both runner and overlay by default, or retains both under a `qemu:` handle.

Unit behavior is covered by `tests/unit/test_qemu_provider.py`; live Linux behavior is
covered by `tests/conformance/test_provider_qemu.py`.
