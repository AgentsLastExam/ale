# ATIF v1.8 upgrade

Compared against official Harbor commit
`bfc6c005e9a572ba22fd7e2d3b414bf014ae1ab0`. The generated v1.8 schema was compared
with ALE's checked-in v1.7 schema, not inferred from the version number alone.

## What changed from v1.7

| Area | v1.7 | v1.8 |
|---|---|---|
| Content parts | `text`, `image` | Adds `audio` in user/agent messages and tool observations |
| Audio source | Absent | Required `media_type` and `path`; optional nonnegative `duration_sec` |
| Source validation | Image source for image parts | Image/audio parts must carry the matching source kind; text cannot carry a media source |
| Media detection | Images | Images and audio |
| Version | Default `ATIF-v1.7` | Default `ATIF-v1.8` |

Canonical audio types are WAV (`audio/wav`), MP3 (`audio/mpeg`), MP4/M4A
(`audio/mp4`), AAC, Ogg, FLAC, WebM and AIFF. Harbor accepts documented MIME aliases
and normalizes them, including `audio/mp3` to `audio/mpeg` and `audio/x-wav` to
`audio/wav`. ALE uses the same normalization.

Audio is referenced by a file path or URL in Harbor; ALE stores its recorded audio
in episode-relative `blobs/audio/` paths. Audio bytes are not base64 inside canonical
JSON. Unknown duration stays absent. ATIF v1.8 does not add video, raw PCM, G.711, or
streaming/realtime audio because these need metadata that its source model does not
represent.

There are no new session, tool-call, subagent, continuation, reasoning, or token fields
in v1.8. The embedded subagents, `trajectory_id`, optional run-scoped `session_id`,
`llm_call_count`, and tool/observation `extra` fields already existed in v1.7.

Harbor's reader still accepts older version labels. ALE continues its existing policy
of writing and reading the schema belonging to the recorded engine commit, with no
legacy conversion layer; both its trajectory and RunLock now say `ATIF-v1.8`.

## Implementation and validation

- Added the strict `AtifAudioSource` model and audio content/source validation.
- Updated the trajectory, RunLock, trusted-verification check, and current test records
  to v1.8. Replaced the checked schema fixture with Harbor's generated v1.8 schema.
- Added deterministic extensions for all eight audio MIME types in blob storage.
- Prime-RL export now requires exact multimodal processor evidence for audio, as it
  already does for images. Prime Verifiers' adjacent v1 schema only represents text
  and images, so its exporter raises for audio instead of mislabeling or dropping it.
- Unit tests cover canonical MIME types and aliases, missing/mismatched sources,
  optional and invalid durations, unsupported formats, blob storage, and exporter
  behavior. Conformance tests compare the complete official schema and validate an ALE
  document containing audio, images, tools, embedded subagents, and continuation with
  Harbor's own validator.

## Official sources

- [ATIF version history](https://github.com/harbor-framework/harbor/blob/bfc6c005e9a572ba22fd7e2d3b414bf014ae1ab0/rfcs/0001-trajectory-format.md#version-history)
- [Audio source specification](https://github.com/harbor-framework/harbor/blob/bfc6c005e9a572ba22fd7e2d3b414bf014ae1ab0/rfcs/0001-trajectory-format.md#audiosourceschema-v18)
- [Content models and MIME normalization](https://github.com/harbor-framework/harbor/blob/bfc6c005e9a572ba22fd7e2d3b414bf014ae1ab0/src/harbor/models/trajectories/content.py)
- [Version default and multimodal detection](https://github.com/harbor-framework/harbor/blob/bfc6c005e9a572ba22fd7e2d3b414bf014ae1ab0/src/harbor/models/trajectories/trajectory.py)
