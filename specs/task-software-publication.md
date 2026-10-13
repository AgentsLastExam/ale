# Task-local software and repository publication

Status: agreed design, not implemented. Current behavior is in the
[asset contract](../docs/specs/task-assets.md). Authoring and package-format guidance belongs to
[Task authoring](../docs/specs/task-authoring.md#software-assets).

## Local authoring

Authors prepare real files inside each Task; no repository-level installation or symlink is
required. A materialized Task contains its software inputs and deployment scripts and can be
moved independently of sibling Tasks. Declared base images and ordinary scripted dependencies
remain external inputs. Software assets are installation materials, not the software's eventual
installation path inside the VM.

```text
tasks/a/image/assets/softwares/editor_1/Editor.msix
tasks/b/image/assets/softwares/editor_1/Editor.msix
```

The planned `image.softwares` list contains software directory IDs, such as `[editor_1]`.
It declares offline materials, not every package installed by the recipe. IDs are single path
components; there is no nested software/version layout or automatic platform/version selection.
Every declaration must have complete local materials before building; publication rejects
undeclared software directories. New packages can be authored and tested before they exist on HF.
The current strict Task YAML schema does not yet accept this field.

## Publish and restore

Use the existing private dataset `agents-last-exam/<source-repository-name>`:

```text
softwares/editor_1/Editor.msix      # one shared package directory
tasks/a/image/assets/...           # A's other assets
tasks/b/image/assets/...           # B's other assets
```

- Publication maps each Task's software directory to the dataset root's `softwares/<id>/`.
  It does not also upload the package under Task paths. Equal IDs with identical files reuse the
  existing entry; different contents under an existing ID are a conflict, never an overwrite.
  Changed packages need a new ID. Task-specific configuration stays in Task scripts.
- Checkpoint/publication records pin the HF asset commit together with Task source. That commit
  covers both ordinary assets and referenced software. Parallel publication must check conflicts
  against the destination revision. Removing one Task's dependency never deletes shared software.
- Asset pull/restore fetches only declared software, reuses verified cached content for that
  revision and restores real files at `image/assets/softwares/<id>/`. Missing or incomplete
  published materials are errors; there is no fallback to supplier websites or interactive login.
  Checkpoint, restore and export must preserve this mapping and Task self-containment.
  All Tasks on one Host share the download cache. Starting with an empty cache, pulling two Tasks
  that reference the same package transfers its content once, including concurrent pulls.
  Local copies into each Task are allowed; reuse must not trigger a second download.
- Build recipes consume the restored paths unchanged. Linux uses ordinary Docker `COPY`;
  Windows uploads the files into the build VM. No links escaping the build context are needed.
  Software bytes participate in image cache identity. An episode reuses its prepared image.

This consolidates published packages and avoids repeated network downloads. Real local Task
copies and installed software in independent Task images still occupy space. Writable hardlinks
to a shared cache must not be used. No format-specific installer, dependency resolver or separate
software HF repo is required. Large-file hashing and Windows transfer also need bounded-memory
processing.
