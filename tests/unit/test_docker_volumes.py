"""How a Docker leaf stacks the layers of volumes, views, copies and flattens them, measures
what is written to them, and removes them."""

from __future__ import annotations

import itertools
import json

import pytest

from ale.run.providers.docker import volumes

pytestmark = pytest.mark.unit

HELPER = "helper"


class _FakeDocker:
    """The Docker CLI calls the volumes module makes, answered from what it was told."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.volumes: dict[str, dict[str, object]] = {}
        self.images: dict[str, dict[str, str]] = {}
        self.vanished: set[str] = set()
        self.directories: list[str] = []
        self.measured = (0, 0)

    def copies(self) -> list[tuple[str, str]]:
        return [
            (call[call.index("-v") + 1].split(":")[0], call[call.index("-v", 8) + 1].split(":")[0])
            for call in self.calls
            if call[:2] == ("run", "--rm") and "sh" in call
        ]

    async def __call__(self, *argv: str, timeout: float = 0) -> tuple[int, str, str]:
        assert argv[0] == "docker"
        args = argv[1:]
        self.calls.append(args)
        match args[:2]:
            case ("volume", "create"):
                labels = dict(
                    value.split("=", 1)
                    for flag, value in itertools.pairwise(args)
                    if flag == "--label"
                )
                self.volumes[args[-1]] = {"Labels": labels}
                return 0, args[-1], ""
            case ("volume", "inspect") if args[2] == "--format":
                return 0, f"/var/lib/docker/volumes/{args[-1]}/_data", ""
            case ("volume", "inspect"):
                found = [self.volumes[name] for name in args[2:] if name in self.volumes]
                missing = [name for name in args[2:] if name not in self.volumes]
                errors = "".join(f"Error: No such volume: {name}\n" for name in missing)
                return (1 if missing else 0), json.dumps(found), errors
            case ("volume", "ls"):
                wanted = args[-1].removeprefix("label=")
                listed = [n for n, v in self.volumes.items() if wanted in v["Labels"]]  # type: ignore[operator]
                return 0, "\n".join(listed), ""
            case ("volume", "rm"):
                if self.volumes.pop(args[2], None) is None:
                    return 1, "", "no such volume"
                return 0, "", ""
            case ("image", "ls"):
                return 0, "\n".join([*self.images, *self.vanished]), ""
            case ("image", "inspect"):
                found = [
                    {"Config": {"Labels": self.images[i]}} for i in args[2:] if i in self.images
                ]
                errors = "".join(
                    f"Error: No such image: {i}\n" for i in args[2:] if i not in self.images
                )
                return (1 if errors else 0), json.dumps(found), errors
            case ("run", "--rm") if "-I" in args:
                asked = list(args[args.index("-c") + 2 :])
                return 0, json.dumps([p for p in asked if p in self.directories]), ""
            case ("run", _):
                return 0, "", ""
            case ("exec", _):
                return 0, f"{self.measured[0]}\n{self.measured[1]}\n", ""
            case ("rm", "-f"):
                return 0, "", ""
        raise AssertionError(args)


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> _FakeDocker:
    fake = _FakeDocker()
    monkeypatch.setattr(volumes, "run", fake)
    return fake


ROOT = volumes.Root(1000, 1000, 0o755)


@pytest.mark.asyncio
async def test_only_a_candidate_that_is_a_directory_in_the_image_is_a_volume(docker) -> None:
    docker.directories = ["/work"]
    found = await volumes.directories("image", "/opt/ale/python/bin/python3", ["/work", "/x.txt"])
    assert found == ("/work",)


@pytest.mark.asyncio
async def test_a_snapshot_stacks_views_that_go_once_nothing_stacks_them(docker) -> None:
    (fresh,) = await volumes.mount("s1", fresh=["/work"], helper=HELPER)
    assert fresh.argument == f"type=volume,src={fresh.volume},dst=/work" and not fresh.scratch
    label, kept = await volumes.freeze("s1", [fresh], {"/work": ROOT}, HELPER)
    first = volumes.decode(label)
    assert first == {"/work": volumes.Stack((fresh.volume,), ROOT)} and kept == {fresh.volume}
    (view,) = await volumes.mount("s2", stacks=first, helper=HELPER)
    assert view.argument.endswith(",volume-nocopy") and view.root == ROOT
    overlay = next(call for call in docker.calls if "type=overlay" in call)
    options = next(value for value in overlay if value.startswith("o="))
    assert options == (
        f"o=lowerdir=/var/lib/docker/volumes/{fresh.volume}/_data,"
        f"upperdir=/var/lib/docker/volumes/{view.upper}/_data,"
        f"workdir=/var/lib/docker/volumes/{view.work}/_data"
    )
    label, kept = await volumes.freeze("s2", [view], {"/work": ROOT}, HELPER)
    assert volumes.decode(label)["/work"].layers == (view.upper, fresh.volume)
    assert kept == {view.upper}
    await volumes.remove([view], kept=kept)
    assert set(docker.volumes) == {fresh.volume, view.upper}
    # The first snapshot goes while the second, listed but gone by the time it is inspected
    # along with another's, still stacks its layer.
    docker.images = {"second": {volumes.LAYERS_LABEL: label}}
    docker.vanished = {"gone"}
    await volumes.collect([fresh.volume])
    assert fresh.volume in docker.volumes
    docker.images = {}
    await volumes.collect([view.upper, fresh.volume])
    assert docker.volumes == {}


@pytest.mark.asyncio
async def test_a_stack_that_would_pass_the_limit_is_copied_into_one_layer(docker) -> None:
    lowers = tuple(f"layer-{index}" for index in range(volumes.MAX_LAYERS - 1))
    stacks = {"/work": volumes.Stack(lowers, ROOT)}
    (view,) = await volumes.mount("deep", stacks=stacks, helper=HELPER)
    label, kept = await volumes.freeze("deep", [view], {"/work": ROOT}, HELPER)
    (merged,) = volumes.decode(label)["/work"].layers
    assert kept == {merged} and merged not in lowers
    # What the view holds, merged, is copied into the new layer.
    assert docker.copies() == [(view.volume, merged)]


@pytest.mark.asyncio
async def test_an_engines_storage_is_copied_never_viewed(docker) -> None:
    (engine,) = await volumes.mount("s1", engines=["/var/lib/docker"], helper=HELPER)
    assert engine.copied and not engine.restored
    label, kept = await volumes.freeze("s1", [engine], {"/var/lib/docker": ROOT}, HELPER)
    stacks = volumes.decode(label)
    assert stacks["/var/lib/docker"].copied and kept == {engine.volume}
    (copy,) = await volumes.mount("s2", engines=["/var/lib/docker"], stacks=stacks, helper=HELPER)
    assert copy.copied and copy.restored and not copy.lowers
    assert copy.argument.endswith(",volume-nocopy")
    assert docker.copies() == [(engine.volume, copy.volume)]
    # A Sandbox that runs no engine takes none of the snapshot's.
    assert await volumes.mount("s3", stacks=stacks, helper=HELPER) == ()


@pytest.mark.asyncio
async def test_a_meter_counts_what_was_written_since_the_sandbox_started(docker) -> None:
    (fresh,) = await volumes.mount("s1", fresh=["/work"], helper=HELPER)
    meter = await volumes.Meter.start("s1", [fresh], HELPER)
    started = next(call for call in docker.calls if call[:2] == ("run", "-d"))
    assert f"-v={fresh.volume}:/m/0:ro" in started
    docker.measured = (4096, 4096 + (5 << 20))
    assert await meter.written() == 5 << 20
    docker.measured = (4096, 0)
    assert await meter.written() == 0
    await meter.stop()
    assert docker.calls[-1] == ("rm", "-f", "ale-meter-s1")
