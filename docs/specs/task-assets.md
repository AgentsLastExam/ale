# Task assets

Task source, scripts, configuration, and ordinary small fixtures are tracked by Git. Task
assets are files managed separately because of their size or lifecycle: ignored by Git and
synchronized with Hugging Face by the commands below. They live in an `assets/` directory
beside the files of the stage that reads them, at these fixed roots of a
[Task folder](task-authoring.md):

| Root | Read by |
|---|---|
| `environment/assets/` | the image build, container or VM, through its ordinary context |
| `tests/assets/` | verification |
| `solution/assets/` | the oracle |

To the runtime they are ordinary files of their stage, uploaded with it and read through
relative paths such as `assets/reference.json` or `.\assets\seed.json`; `ALE_STAGE_DIR`
gives a subprocess the stage's absolute path. A stage's assets are therefore as visible as
the stage: the image build places what the agent sees, and tests and solution assets are
absent while the evaluated agent works. Setup has no asset root: large data it needs is built
into the image, at a root-only location when the agent must not read it directly. Running a
Task never implicitly downloads assets.
Source digests leave the asset roots out; assets have their own commit identity.

## Offline software

On either OS, install software directly in the image recipe when a clean base can acquire the
pinned version and deploy it unattended from declared inputs. Machine authentication is allowed;
interactive login, verification codes, browser sessions and manual installer steps are not a
repeatable build input. Version pins alone do not guarantee that an upstream keeps old files.

Otherwise, acquire and, if needed, install or compile the software once in a preparation
environment. Keep the reusable offline materials as real files under
`environment/assets/softwares/<software_id>/`, for example
`environment/assets/softwares/editor_1/`. Use one directory per
software/version, with underscores rather than nested version directories. Reuse a compatible
existing package before preparing another. Slow acquisition or compilation can also justify an
offline asset. Installed VM state alone is not a rebuildable asset.

Keep deployment scripts and Task-specific configuration beside the build definition: the
Dockerfile on Linux, `build.ps1` on Windows. Scripts consume Task-local materials, choose their
own extraction or installation method, and declare external dependencies. Linux has no required
package format. For Windows software requiring repackaging, prefer the
[MSIX workflow](../guides/windows-task-images.md). No Task depends on another Task's files.

Record the source, version, integrity hash, compatible base, and deployment requirements. Keep
credentials out of assets. Validate from a clean base as the actual agent user, including required
writes, child tools and save/reopen, without repeating the interactive acquisition process.

Asset commands treat these as ordinary Task-local files and mirror their paths to HF. They do
not consolidate software into a repository-level directory; the agreed
[repository software publication design](../../specs/task-software-publication.md) describes
that pending change, not the current CLI contract.

## Repository and access

Each Task source repository has one private Hugging Face dataset named
`<namespace>/<source-repository-name>`, in that namespace's `assets` collection. The
namespace is `agents-last-exam` unless `ALE_ASSETS_NAMESPACE` names another organization
or user, so a Task repository maintained elsewhere keeps its assets in its own namespace.
The source repository name comes from its `origin` URL, falling back to the main Git
directory. A linked worktree's directory name is never the dataset name. Dataset paths
mirror source paths, for example `tasks/example/tests/assets/expected.json`.

The first asset checkpoint may create the missing private dataset and empty collection.
It does not upload Task files. An existing public dataset is rejected. Commands pass
`ALE_HF_TOKEN` from the checkout's `.env` explicitly to every Hugging Face call and Git
request; without it they are anonymous. The operator's own `HF_TOKEN` or cached Hugging
Face login is never used, so the checkout alone decides which account ALE acts as.
Credentials never enter a remote Git URL or the Task definition. Git and Git LFS must be
installed for local checkpoints and uploads. `ALE_ASSETS_COLLECTION` or `--collection`, when supplied for manual sync,
must identify the namespace's collection titled `assets`; otherwise it is discovered.

## Local versions

ALE keeps one Git/LFS clone under the source repository's common Git directory and a
separate branch and worktree per Job. Concurrent Jobs cannot overwrite each other's
working files. Accepted asset changes are committed locally. Unchanged content reuses
its commit; no per-attempt asset archives or separate content-hash manifests are stored.
Git/LFS owns object storage and history. A small checkpoint JSON records the dataset,
Task path, local worktree, base commit, and accepted commit. With no assets or previous
asset state, the commit is null and no HF access is needed.
Checkpoints also accept incomplete Task folders before a valid `task.toml` exists. As with
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

`diff` emits a JSON array of Task-relative paths, such as `tests/assets/expected.json`.
`materialize` writes only the asset roots relative to its empty output directory.
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

Pull refuses any existing selected asset root, including empty or clean roots;
`--force` explicitly replaces them. A revision is resolved to an immutable SHA before downloading.
Recovery, verification, and export from a recorded version must use that fixed SHA or
the saved local checkpoint, never a moving branch name. The accepted asset commit also
participates in completed-episode reuse identity; dirty assets cannot reuse an episode.
