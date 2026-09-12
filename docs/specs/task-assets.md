# Task assets

Task assets are the four separately managed `image/assets`, `setup/assets`,
`verify/assets`, and `oracle/assets` roots described by the
[Task authoring contract](task-authoring.md). Runtime stages consume their materialized
files normally; running a Task never implicitly downloads assets.

## Repository and access

Each Task source repository has one private Hugging Face dataset named
`agents-last-exam/<source-repository-name>`, in that organization's `assets` collection.
The source repository name comes from its `origin` URL, falling back to the main Git
directory. A linked worktree's directory name is never the dataset name. Dataset paths
mirror source paths, for example `tasks/example/verify/assets/expected.json`.

The first asset checkpoint may create the missing private dataset and empty collection.
It does not upload Task files. An existing public dataset is rejected. Commands use the
operator's HF authentication, including `HF_TOKEN`; credentials never enter a remote
Git URL or the Task definition. Git and Git LFS must be installed for local checkpoints
and uploads. `ALE_ASSETS_COLLECTION` or `--collection`, when supplied for manual sync,
must identify the organization's collection titled `assets`; otherwise it is discovered.

## Local versions

ALE keeps one Git/LFS clone under the source repository's common Git directory and a
separate branch and worktree per Job. Concurrent Jobs cannot overwrite each other's
working files. Accepted asset changes are committed locally. Unchanged content reuses
its commit; no per-attempt asset archives or separate content-hash manifests are stored.
Git/LFS owns object storage and history. A small checkpoint JSON records the dataset,
Task path, local worktree, base commit, and accepted commit. With no assets or previous
asset state, the commit is null and no HF access is needed.
Checkpoints also accept incomplete Task folders before a valid manifest exists. As with
ordinary Git, empty directories are not versioned.

The materialized Task's dirty inventory records file and directory paths, sizes, and
modification times, and executable bits. Checking dirtiness does not read or hash every file. This deliberately
uses normal filesystem change detection; modifications preserving all observed metadata
are outside that guarantee. Restoring a checkpoint restores exactly that Git commit and
refreshes the inventory. Missing LFS objects are errors, never accepted pointer files.

The public CLI for an orchestrator is:

```bash
ale assets checkpoint TASK --workspace JOB_ASSET_WORKTREE --branch brew/JOB --output REF.json
ale assets check TASK --checkpoint REF.json
ale assets restore TASK --checkpoint REF.json
ale assets diff --before BEFORE.json --after AFTER.json
ale assets materialize --checkpoint REF.json --output EMPTY_DIRECTORY
ale assets publish --checkpoint REF.json --title 'Build example' --description BODY.md --output PUBLICATION.json
ale assets comment-pr --checkpoint REF.json --publication PUBLICATION.json --message-file SOURCE_PR_LINK.md
```

`diff` emits a JSON array of Task-relative paths, such as `verify/assets/expected.json`.
`materialize` writes only the four asset roots relative to its empty output directory.
It reads the recorded commit, not the latest asset worktree contents. Source commit and
asset commit are paired in the orchestrator's existing records; no extra source commit
or reference file in the Task repository is required.

## Publication and manual synchronization

After the complete build succeeds, `publish` creates a draft HF PR targeting `main` and
pushes the accepted Git commit and LFS objects to its `refs/pr/N`. The local and remote
commit SHA are identical; intermediate local commits remain ordinary Git history.
The PR title names the Task; HF owns the numeric PR reference. Publication saves its PR
number before pushing and can resume the same draft. Setting the new PR's head uses an
explicit lease so a concurrent update is not overwritten. Publication never merges.
Independent Task PRs change only their respective asset paths. Concurrent changes to the
same Task may require an administrator to resolve a merge conflict.

If the final Task asset tree equals the original HF version, that existing commit is
retained and no empty PR is created. Removing previously published assets is a real
change and does create a deletion PR. Publication records include `repo_id`, `commit`,
`pr_number`, `pr_url`, and `status` (`no_assets`, `unchanged`, `pending`, or `published`).

Manual `ale assets push TASK` explicitly publishes selected assets to `main`, using the
same local Git/LFS implementation and requiring a fast-forward. Brew uses `publish`
instead, leaving both source and assets as draft PRs for administrator review.

```bash
ale assets status TASK
ale assets pull TASK                       # current main
ale assets pull TASK --revision HF_SHA     # fixed published version
ale assets push TASK                       # explicit manual sync to main
```

Pull replaces only the selected Task's asset roots and refuses dirty files unless
`--force` is supplied. A revision is resolved to an immutable SHA before downloading.
Recovery, verification, and export from a recorded version must use that fixed SHA or
the saved local checkpoint, never a moving branch name. The accepted asset commit also
participates in completed-episode reuse identity; dirty assets cannot reuse an episode.
