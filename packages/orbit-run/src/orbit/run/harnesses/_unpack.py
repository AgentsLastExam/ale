"""Unpack an agent program's archives inside a Sandbox, each accepted only by its digest.

Run by the framework's Python, as the account the files are for, with the standard library
alone: ``python -I _unpack.py <manifest> <target>``. The manifest lists the archives; each
is a file the Host uploaded beside the manifest or a URL this script downloads, and a
mismatching digest stops everything before anything is unpacked.

How an archive is unpacked:

- ``package``: an npm package, whose one top directory is dropped;
- ``members``: just the named members of a tar, zip or Debian archive, each checked by its
  own SHA-256 where the manifest names one;
- ``file``: the archive is the file itself.

The manifest's ``executables``, below the target, are then made executable, and the
uploaded and downloaded archives are removed.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import urllib.request
import zipfile


def main(manifest_path: str, target: str) -> None:
    staging = os.path.dirname(os.path.abspath(manifest_path))
    with open(manifest_path, encoding="utf-8") as stream:
        manifest = json.load(stream)
    entries = manifest["archives"]
    with concurrent.futures.ThreadPoolExecutor(16) as pool:
        paths = list(pool.map(lambda item: obtain(staging, *item), enumerate(entries)))
    for path, entry in zip(paths, entries, strict=True):
        unpack(path, entry, os.path.join(target, entry["into"]))
    if os.name != "nt":
        for executable in manifest["executables"]:
            os.chmod(os.path.join(target, executable), 0o755)
    shutil.rmtree(staging, ignore_errors=True)


def obtain(staging: str, index: int, entry: dict) -> str:
    """The archive's path in ``staging``, downloaded first when the manifest names a URL."""
    path = os.path.join(staging, str(index))
    if "url" in entry:
        try:
            with (
                urllib.request.urlopen(entry["url"], timeout=600) as response,
                open(path, "wb") as out,
            ):
                shutil.copyfileobj(response, out, 1 << 20)
        except OSError as exc:
            sys.exit(f"could not download {entry['url']}: {exc}")
    algorithm, expected = entry["digest"].split(":", 1)
    if digest(path, algorithm) != expected:
        sys.exit(f"{entry.get('url', path)} does not have the {algorithm} {expected}")
    return path


def unpack(path: str, entry: dict, into: str) -> None:
    os.makedirs(into, exist_ok=True)
    kind = entry["unpack"]
    if kind == "file":
        destination = os.path.join(into, entry["name"])
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        os.replace(path, destination)
    elif kind == "package":
        with tarfile.open(path) as archive:
            members = []
            for member in archive.getmembers():
                _, _, member.name = member.name.partition("/")
                if member.name:
                    members.append(member)
            archive.extractall(into, members=members, filter="data")
    elif kind == "members":
        wanted = {member: (name, sha256) for member, name, sha256 in entry["members"]}
        found = read_members(path, set(wanted))
        if missing := set(wanted) - set(found):
            sys.exit(f"the archive has no {', '.join(sorted(missing))}")
        for member, data in found.items():
            name, sha256 = wanted[member]
            if sha256 is not None and hashlib.sha256(data).hexdigest() != sha256:
                sys.exit(f"{member} is not the pinned build")
            destination = os.path.join(into, name)
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            with open(destination, "wb") as out:
                out.write(data)
        os.unlink(path)
    else:
        sys.exit(f"unknown unpacking: {kind}")


def read_members(path: str, wanted: set[str]) -> dict[str, bytes]:
    """The regular-file members named in ``wanted``; a Debian package's are in its data."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            return {name: archive.read(name) for name in archive.namelist() if name in wanted}
    with open(path, "rb") as stream:
        if stream.read(8) == b"!<arch>\n":
            while header := stream.read(60):
                name, size = header[:16].decode().strip().rstrip("/"), int(header[48:58])
                data = stream.read(size)
                stream.read(size % 2)
                if name.startswith("data.tar"):
                    return tar_members(io.BytesIO(data), wanted)
            return {}
    with open(path, "rb") as stream:
        return tar_members(stream, wanted)


def tar_members(stream, wanted: set[str]) -> dict[str, bytes]:  # type: ignore[no-untyped-def]
    found = {}
    with tarfile.open(fileobj=stream) as archive:
        for member in archive.getmembers():
            source = archive.extractfile(member) if member.isreg() else None
            if member.name in wanted and source is not None:
                found[member.name] = source.read()
    return found


def digest(path: str, algorithm: str) -> str:
    value = hashlib.new(algorithm)
    with open(path, "rb") as stream:
        while block := stream.read(1 << 20):
            value.update(block)
    return value.hexdigest()


if __name__ == "__main__":
    main(*sys.argv[1:3])
