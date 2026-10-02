# MCP HD video and music generation

Azure Functions Python 3.13 MCP server that starts one media generation per
prompt or track, sends all jobs to the matching Service Bus queue, and
collects their Durable Task Scheduler events. Video and music share the same
orchestration core (`src/media_workflow.py`), the same working folder, and two
dedicated queues.

## MCP tools

- `create_hd_video` validates `CreateHDVideoInput`, starts the fan-out/fan-in
  orchestration, and waits up to `MCP_WAIT_BUDGET_SECONDS`. It returns the
  completed result inline or a `workflow_id`.
- `get_hd_video_result` accepts a required, strongly validated `workflow_id`
  and returns `running`, `completed`, `failed`, or `not_found`.
- `create_music` validates `CreateMusicInput` (an existing `videoid` plus a
  non-empty list of tracks, each with `style`, `lyrics` and an optional `lora`
  among `two_steps_from_hell` and `industrial_rock`) and runs the same
  fan-out/fan-in orchestration on the music queue.
- `get_music_result` mirrors `get_hd_video_result` for music workflows.
- `create_music_video` validates `CreateMusicVideoInput` and starts one
  "music video" generation on the video queue (`VIDEO_SERVICE_BUS_QUEUE_NAME`,
  same ltx25 worker as `create_hd_video`). It turns a song produced by
  `create_music` into a clip.
- `get_music_video_result` mirrors `get_hd_video_result` for music video
  workflows.

### Music video workflow (`create_music` -> `create_music_video`)

1. Generate the reference images beforehand in
   `fluxjob/agentvideo/{videoid}/` (performer portraits, one image per
   location). A single performer is enough: only `ref_speaker1_filename` and
   `ref_speaker1_prompt` are required; `ref_speaker2..4` are optional and every
   provided file needs its prompt. Locations are passed as the ordered
   `backgrounds` list (`[{"filename": ..., "description": ...}]`); the legacy
   single-location pair `background_filename` / `background_prompt` is still
   accepted and normalized to a one-item list, but the two forms cannot be
   mixed. Subject prompts describe the performer identity; each background
   description is the location text used to stage the scenes.
2. Call `create_music` with the same `videoid`; each generation result exposes
   its `type_prefix`, such as `music-0123456789-001` (the `.flac` blob is
   `{type_prefix}-{videoid}.flac`).
3. Call `create_music_video` with `music_track` set to that `type_prefix`.
   Optional inputs: `lyrics` (reference lyrics as sent to `create_music`, used to
   correct the Whisper transcription), `theme_style`, `story`,
   `scene_min_seconds` / `scene_max_seconds` (worker defaults 3 / 8),
   `scene_bias` (-1..+1, default 0), `whisper_language` (auto-detected when
   omitted), `orientation` (`Vertical` 704x1280 by default, or `Horizontal`
   1280x704), `backgrounds`, `music_plan` and `reuse_music_plan`.
4. The clip is written as `{VIDEO_BLOB_PATH_PREFIX}/{videoid}/musicvideo-{token}-001-{videoid}.mp4`.
   The worker also writes `{music_track}-{videoid}.musicplan.json`,
   `.scenes.srt` and `.prompts.srt` next to it. A completed response returns a
   `video/mp4` `ResourceLink` plus SAS links to these three artifacts; the
   JSON result exposes their paths (`artifacts`) and the `music_plan` blob name
   reported by the worker.
5. `reuse_music_plan=true` re-renders the clip from the existing
   `.musicplan.json` without audio analysis nor LLM calls (for example after
   changing the reference images or the orientation). It is reserved for
   re-renders and is mutually exclusive with `music_plan`.
6. `music_plan` takes a **pre-computed MusicPlan v1 JSON object** (not a blob
   name, not a JSON string). The MCP server validates it, serializes it and
   uploads it to
   `{VIDEO_BLOB_PATH_PREFIX}/{videoid}/{music_track}-{videoid}.musicplan.json`
   before starting the orchestration, then sends the worker only the simple
   blob name `{music_track}-{videoid}.musicplan.json`. The plan therefore never
   transits through the Durable Task input nor the Service Bus message.
   Validation covers `schema_version=1`, `videoid` / `music_track` matching the
   call, contiguous scenes indexed from 0, scene `frames` and `total_frames` on
   the LTX `8k+1` grid with `total_frames == 1 + sum(frames - 1)`, and each
   scene `prompt` shaped as `"<location>: <action>"` matching exactly one entry
   of `locations`. `locations` stays in the plan (LTX schema unchanged) and
   must line up with the separate `backgrounds` argument:
   `music_plan.locations[i] == backgrounds[i].description`, one reference image
   per location.

Rendering is slow: expect about 29 minutes of GPU time per minute of song
(~40 minutes for an 83-second song). The workflow therefore has its own
timeout, `MUSIC_VIDEO_ORCHESTRATION_TIMEOUT_SECONDS` (4 hours by default, i.e.
songs up to roughly 8 minutes); poll with `get_music_video_result`.

All tools return `List[ContentBlock]`. The first block is a `TextContent`
containing the JSON status contract used for polling. A completed response also
contains one `ResourceLink` per successful generation, with
`mimeType="video/mp4"` or `mimeType="audio/flac"` and a short-lived, read-only
user delegation SAS URL.

Music generations reuse the video working folder
(`{VIDEO_BLOB_PATH_PREFIX}/{videoid}/`) and are written as `.flac`. Their
`type_prefix` starts with `music-` instead of `hdvideo-`, so both media types
coexist without collision. The queue message carries `videoid`, `style`,
`lyrics`, the optional `lora`, and the same correlation fields
(`type_prefix`, `instance_id`, `event_key`, `dts_event_name`, `seed`). The
worker answers on the DTS event `music-{index}-{uuid}` with the same
`status` / `event_key` / `error` / `retryable` contract as video.

Every generation result contains its prompt/index, terminal status,
deterministic blob path and `type_prefix`, optional `num_frames` (and
`music_plan` for music videos), and any error or timeout.
Vertical videos are 704x1280; horizontal videos are 1280x704.
Each queue message also carries a deterministic seed derived from its
`event_key`, so an at-least-once activity replay produces the same video at the
same blob path instead of racing with a randomly different generation.
The video `type_prefix` includes a short stable token derived from the Durable
instance ID and prompt index. Retries of one workflow keep the same blob path,
while concurrent workflows for the same `videoid` write distinct blobs.
When the global timer wins, the orchestrator uses `continue_as_new` to enter a
terminal phase without recreating pending external-event listeners, then
completes with timeout results.

Transport limits are enforced on UTF-8 bytes rather than character counts:

- at most 64 prompts (or 64 tracks) per workflow;
- DTS input is capped at 960 KiB, retaining 64 KiB for the orchestration wrapper;
- each Service Bus body is capped at 252 KiB, retaining 4 KiB below the Basic
  tier's 256 KiB limit;
- prompt validation reserves an additional 16 KiB for generated correlation
  fields and the JSON envelope, and the final serialized message is checked
  again immediately before the output binding.

## Local validation

Python 3.13 and Azure Functions Core Tools are expected.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pytest -q
```

`src/local.settings.json` contains no secret. Start the Durable Task Scheduler
emulator on port 8080 before running the Function App locally. The same
`src/host.json` selects DTS locally and in every deployment path, preventing a
CI package from silently falling back to the Azure Storage provider. Running
the Service Bus output binding locally requires an Azure identity that already
has sender access to the existing queue; unit tests do not access Azure.
`local.settings.json` is excluded from both AZD and GitHub Actions deployment
packages.

Production resource identifiers are injected through AZD environment variables:
`VIDEO_SERVICE_BUS_RESOURCE_GROUP_NAME`, `VIDEO_SERVICE_BUS_NAMESPACE_NAME`,
`VIDEO_SERVICE_BUS_QUEUE_NAME`, `MUSIC_SERVICE_BUS_QUEUE_NAME`,
`VIDEO_DTS_RESOURCE_GROUP_NAME`,
`VIDEO_DTS_SCHEDULER_NAME`, `VIDEO_DTS_TASKHUB_NAME`, `VIDEO_DTS_ENDPOINT`,
`VIDEO_LOG_ANALYTICS_RESOURCE_GROUP_NAME`,
`VIDEO_LOG_ANALYTICS_WORKSPACE_NAME`, `VIDEO_STORAGE_RESOURCE_GROUP_NAME`,
`VIDEO_STORAGE_ACCOUNT_NAME`, `VIDEO_BLOB_BASE_URL`, and
`VIDEO_BLOB_PATH_PREFIX`.

The runtime pins the maintained MCP SDK 1.x line because stable
`azure-functions` 2.2 serializes its `mcp.types` content blocks natively. MCP
SDK 2.x support requires a later Azure Functions release.

## Configuration

| Setting | Default / purpose |
| --- | --- |
| `MCP_WAIT_BUDGET_SECONDS` | `20`, inline MCP wait budget |
| `MCP_POLL_INTERVAL_SECONDS` | `5`, suggested polling delay |
| `ORCHESTRATION_TIMEOUT_SECONDS` | `7200`, durable global timeout for `create_hd_video` and `create_music` |
| `MUSIC_VIDEO_ORCHESTRATION_TIMEOUT_SECONDS` | `14400`, durable timeout for `create_music_video` (~29 min of GPU per minute of song) |
| `VIDEO_BLOB_BASE_URL` | HTTPS Blob URL prefix for generated videos |
| `VIDEO_SAS_TTL_SECONDS` | `3600`, lifetime of read-only video SAS links; maximum 86400 |
| `VIDEO_SERVICE_BUS_QUEUE_NAME` | Video Service Bus queue name, injected per environment |
| `MUSIC_SERVICE_BUS_QUEUE_NAME` | Music Service Bus queue name in the same namespace, injected per environment |
| `VIDEO_BLOB_PATH_PREFIX` | Blob container and path prefix shared by video and music, injected per environment |
| `VIDEO_FUNCTION_APP_NAME` | GitHub Actions variable containing the Function App name |
| `ServiceBusConnection__fullyQualifiedNamespace` | Existing namespace endpoint |
| `ServiceBusConnection__credential` | `managedidentity` |
| `ServiceBusConnection__clientId` | Function UAMI client ID in Azure |
| `DURABLE_TASK_SCHEDULER_CONNECTION_STRING` | DTS endpoint/task hub/UAMI |
| `TASKHUB_NAME` | `default` |

The Azure configuration is assembled from the official
`functions-quickstart-python-http-azd` base plus the MCP, Durable and Service
Bus recipes. It creates the Function App, FC1 plan, storage, identity and
Application Insights, while referencing the existing Service Bus, DTS and Log
Analytics resources.

`ASSIGN_EXISTING_RESOURCE_ROLES` defaults to `false`. Enabling it would create
sender/contributor role assignments on existing Service Bus, DTS, and output
Blob Storage resources and therefore requires separate explicit approval. The
Function identity needs `Storage Blob Data Contributor` on the configured video
storage account to
request a user delegation key; it currently has no such assignment.

## Deployment gate

This repository is prepared for validation only. Do not run `azd up`,
`azd provision`, `azd deploy`, or change any Azure setting/RBAC without a new
explicit approval from the resource owner.
