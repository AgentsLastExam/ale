"""Run a secondary Kubernetes Pod's node-local workers as the episode opens allocations."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from orbit.run.providers.cluster.delegated import delegate, own_cgroup
from orbit.run.providers.cluster.job import exists, read, without_credentials, write


def serve(job: Path, rank: int) -> None:
    group = delegate(own_cgroup(), os.getpid())
    env = {**without_credentials(), "ORBIT_KUBERNETES_PEER_CGROUP": str(group)}
    try:
        while not read(job / "stop"):
            for directory in sorted((job / "allocations").iterdir()):
                done = directory / f"peer-done-{rank}"
                if not exists(directory / "start-peers") or exists(done):
                    continue
                if not read(directory / "stop"):
                    with (directory / f"job-{rank}.log").open("ab") as log:
                        result = subprocess.run(
                            [sys.executable, "-I", "worker.py", "supervise"],
                            cwd=directory,
                            env=env,
                            stdin=subprocess.DEVNULL,
                            stdout=log,
                            stderr=subprocess.STDOUT,
                            check=False,
                        )
                    if result.returncode:
                        raise RuntimeError(
                            f"worker {directory.name} on rank {rank} exited {result.returncode}"
                        )
                write(done, b"1")
            time.sleep(0.5)
    finally:
        group.rmdir()


def main() -> None:
    job = Path(os.environ["ORBIT_KUBERNETES_JOB_DIRECTORY"])
    rank = int(os.environ["ORBIT_KUBERNETES_RANK"])
    try:
        serve(job, rank)
    except Exception as error:
        # A peer can fail before any allocation opens (e.g. no delegated CPU
        # controller). Propagate its failure to rank zero and release its cleanup wait.
        write(job / f"peer-error-{rank}", str(error).encode())
        raise


if __name__ == "__main__":
    main()
