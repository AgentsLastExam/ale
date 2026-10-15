# 0009 - Images are versioned and the engine adds its own layer to every Task image

**Status:** Accepted

## Context

Base images were published under tags that every change overwrote (`latest`, `0.1.0`),
and the Linux bases carried a copy of guestd. One host's Docker daemon serves several
checkouts pinned to different engine commits, and remote builders pull the same tags. A
merged guestd change therefore reached every consumer at its next build: a checkout still
on an older engine got a guest service whose protocol it did not speak, and an existing
Task could build from a different image than the one it was validated on. Content-hash
tags were already immutable but meant nothing to the people choosing them.

## Decision

- Every ALE image is published under the engine's own version, once. The workflow skips a
  version the registry already has, so a changed image ships with the next engine
  version. No `latest` tag is published; an already-published image can be given a
  version by pointing a new tag at the same manifest.
- Tasks may build from any image. Task repositories maintained with ALE name an exact
  base version rather than a floating tag.
- Images carry nothing of ALE's own. When it prepares a Linux Task image the engine adds
  a framework layer on top of the Task's build: guestd, a pinned Cua Driver, for a VM the
  unit that starts guestd, a check that the Task's `image.user` account exists, and working
  sudo when `resources.sudo` asks for it. A Task may therefore build from any image with
  Python and its agent account. No image labels are read: the account comes from the Task
  and a desktop is recognized from the image's own configuration (an enabled display
  manager, or a `DISPLAY` the image sets). The layer is part of the prepared image's
  identity, so a guestd change rebuilds Task images, not bases.
- A Windows Task image is built in a running VM rather than by Docker, so the same pieces
  are installed there after the Task's script, through the guestd the Windows base carries
  as its bootstrap. That bootstrap must speak the current authentication handshake; any
  newer guestd replaces it in every Task image built on the base.

## Consequences

An engine update no longer changes what an existing Task builds from or which protocol its
guest speaks; a checkout syncs to a newer engine and newer bases when it chooses to.
Older bases stay usable with newer engines. A changed image takes effect with the next
engine version, and a version under review is built locally until CI publishes it.
