from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from ale.core.errors import TaskDefinitionError
from ale.run.content import tree_digest

pytestmark = pytest.mark.unit


def test_tree_digest_tracks_bytes_mode_and_symlink_target(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    file = root / "run.sh"
    file.write_text("one")
    first = tree_digest(root)
    file.write_text("two")
    second = tree_digest(root)
    file.chmod(0o755)
    third = tree_digest(root)
    (root / "link").symlink_to("run.sh")
    fourth = tree_digest(root)
    assert len({first, second, third, fourth}) == 4


def test_tree_digest_is_parent_path_independent(tmp_path: Path) -> None:
    one = tmp_path / "one"
    one.mkdir()
    (one / "a").write_text("a")
    two = tmp_path / "elsewhere" / "two"
    shutil.copytree(one, two)
    assert tree_digest(one) == tree_digest(two)


def test_tree_digest_rejects_escaping_symlink(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    outside = tmp_path / "secret"
    outside.write_text("no")
    os.symlink(outside, root / "escape")
    with pytest.raises(TaskDefinitionError, match="escapes"):
        tree_digest(root)
