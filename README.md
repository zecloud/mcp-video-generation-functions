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
  among `two_steps_from_hell`, `industrial_rock` and `none`, plus optional
  native YuE2 `slider` / `slider_strength` controls) and runs the same
  fan-out/fan-in orchestration on the YuE2 music queue.
- `get_music_result` mirrors `get_hd_video_result` for music workflows.
- `create_music_video` validates `CreateMusicVideoInput`, prepares a frozen
  MusicPlan, fans out one silent LTX25 generation per scene, then fans in to a
  CPU assembly/remux job on the video queue (`VIDEO_SERVICE_BUS_QUEUE_NAME`,
  same worker as `create_hd_video`). This is the default, with hard cuts only
  and no transition keyframes. Its public result still contains one final clip.
- `get_music_video_result` mirrors `get_hd_video_result` for music video
  workflows.

### Optional visual references (`create_hd_video`)

Only `videoid` and `prompts` are required. All four subject filenames, including
`ref_speaker1_filename` and `ref_speaker2_filename`, are optional and default to
`null` in the Pydantic input and Python handler. This applies both with and
without `video_plan`: zero or one subject, a global background alone, and
VideoPlan `backgrounds` without subjects are supported by a compatible worker.
Omitted references are not replaced by synthetic identities. With zero subject
references, character identity is not visually fixed; this option does not
create or clone a voice automatically.

In described `references[]` mode, only supplied image files need their matching
non-empty descriptions; a description without its image is rejected. Ordered
VideoPlan backgrounds also select this mode, even when there are no subjects.
A visual-only second image may be supplied without the first. A voice always
requires its corresponding image, and earlier subject slots up to that voice
must be present: `audio_ref2` needs both images 1 and 2, while `audio_ref1` can
be used with image 1 alone. Missing earlier images cannot silently move voice 2
to subject 1. Existing legacy voiced image-contiguity and reference limits remain
in force. Music-video inputs keep their separate required-performer contract.

The locally registered MCP tool metadata marks both filenames optional. Already
deployed tool schemas do not change until this MCP fix and a worker supporting
zero references are deployed; no deployment is performed by these changes.

### Paired voice references (`create_hd_video`, MSR V2)

`audio_ref1` and `audio_ref2` are optional **exact blob filenames**, paired with
`ref_speaker1_filename` and `ref_speaker2_filename` respectively. Prepare the
images and voice files beforehand in `fluxjob/agentvideo/{videoid}/`; this tool
neither uploads nor generates them. WAV is recommended. No extension is added
to audio filenames (unlike extensionless image names, which gain `.png`). Paths,
URLs and surrounding whitespace are rejected; filenames retain their case and
extension. The worker rejects missing images or voice files for voiced jobs.

```json
{
  "videoid": "video-42",
  "ref_speaker1_filename": "alice",
  "ref_speaker2_filename": "bob.jpg",
  "audio_ref2": "bob.wav",
  "prompts": ["Alice listens while Bob speaks to her."]
}
```

This emits `pic1="alice.png"`, `pic2="bob.jpg"`, `audio_ref2="bob.wav"`.
Image 1 stays present and non-vocal: voice 2 is never moved to slot 1. With
reference descriptions supplied, the same association is emitted instead as
`references[1].audio_ref`; all provided images still require non-empty prompts.
The message never mixes `references[]` with `audio_ref1/2` (even null keys), nor
voiced `references[]` with legacy picture/background keys. Absent/null voices
are omitted, preserving historical visual messages.

Only subjects 1/2 may have voices. Subjects 3/4 and the background remain
non-vocal; at most five effective images are allowed with voices. In legacy
`picN` mode, voiced jobs require contiguous `pic1..picN` (so image 4 requires
image 3); existing visual-only requests keep their behavior. AVref cannot be
combined with `sound`, `music_track` or `music_plan`, including reuse plans.
Music tools do not expose these voice parameters and reject voiced references.
Removed `reference_audio`, `audio_id_lora`, `audio_id_strength` and
`audio_id_seconds` remain rejected, even when null, false or empty.

The worker encodes each voice once per job and reuses its stable image slot
across chunks. It truncates long voices to five seconds without silence-padding
short voices. No worker LoRA, isolation or chunk-limit controls are added here.
Callbacks, result JSON, orientation and workflow polling remain unchanged.

This contract targets worker commit
[`761ca11`](https://github.com/zecloud/func_tts_eurovibe/tree/761ca11/ltx25)
in zecloud/func_tts_eurovibe#69. Roll out these MCP options only after the V2
worker is deployed and GPU AVref acceptance is complete; there is no V1 fallback.

### Narrative VideoPlan v1 (`create_hd_video`)

`video_plan` optionally supplies a precomputed, **non-musical** narrative plan.
It accepts a JSON object or its JSON-encoded string, never a blob name or reuse
flag. Keep exactly **one** entry in `prompts`: the global narration remains
unchanged, but the worker uses the ordered scene prompts directly without LLM
scene splitting. A plan with multiple prompts is rejected rather than silently
fan-out or reuse the same plan across independent videos. Without a plan,
historical fan-out, references, voices, outputs and the 2-hour timeout remain
unchanged.

```json
{
  "videoid": "video-42",
  "ref_speaker1_filename": "alice",
  "ref_speaker2_filename": "bob.jpg",
  "ref_speaker1_prompt": "Alice",
  "ref_speaker2_prompt": "Bob",
  "audio_ref2": "Bob.wav",
  "prompts": ["A complete narrative."],
  "backgrounds": [
    {"filename": "studio", "description": "Studio"},
    {"filename": "garden.png", "description": "Garden"}
  ],
  "video_plan": {
    "schema_version": 1,
    "videoid": "video-42",
    "fps": 24,
    "duration_seconds": 10.0,
    "total_frames": 241,
    "locations": ["Studio", "Garden"],
    "scenes": [
      {"index": 0, "start": 0.0, "end": 5.0, "frames": 121,
       "prompt": "Alice listens while Bob speaks in the studio.", "location": "Studio"},
      {"index": 1, "start": 5.0, "end": 10.0, "frames": 121,
       "prompt": "Alice and Bob walk through the garden.", "location": "Garden"}
    ]
  }
}
```

The canonical example is also `tests/fixtures/videoplan_v1.json` (SHA256
`d1afb96608a80d5bc871ddba01a937aa197bbd5e01e40344a325fd37e20f36f1`).
The independently supplied cross-repository fixture is copied unchanged at
`tests/fixtures/videoplan-v1-golden.json` (source SHA256
`853499b120585835968fcd92eca280e40525c4ef469f688f044057d6467ed5e8`).
MCP normalizes timeline numbers to floats and uploads canonical bytes, whose
SHA256 is `3a365a6e14368b567a97fa76aefaa1a25160499eb4c37386ab7cb6c83f90e40f`.
The worker verifies the bytes actually downloaded, not a reserialization of the
source fixture. The strict standalone decoder is
`video_plan.VideoPlan.from_json(bytes_or_string)`; `serialize()` returns the
canonical upload bytes and `blob_name()` includes their full SHA256.
Validation rejects unknown fields, booleans/coerced numeric integers, non-finite
numbers, duplicate JSON keys, non-contiguous indexes and inconsistent timings.
Only integer `fps=24` and `schema_version=1` are supported. Limits: 512 scenes,
16 described backgrounds, 3600 interval seconds and 4 MiB canonical UTF-8 JSON.
These are validation limits, **not GPU throughput guarantees**.

Frames are authoritative: each scene has at least 9 frames on the `8k+1` grid;
`start=sum(previous frames-1)/24`, `end=start+(frames-1)/24`,
`total_frames=1+sum(frames-1)` and `duration_seconds=(total_frames-1)/24`.
Timing comparisons use absolute tolerance `1e-6` seconds. The worker expands
large scenes into deterministic chunks, repeats their scene prompt and removes
one shared frame at every join. The final MP4 contains `total_frames` frames:
its rendered duration is **`duration_seconds + 1/24`**, reported separately as
`rendered_duration_seconds`.

`backgrounds` is available only with a plan (also accepts a JSON string), and
cannot be mixed with `background_filename`/`background_prompt`. Every supplied
subject image needs its visual description in this mode. `locations` must
exactly match the ordered background descriptions; with multiple locations,
each scene must select an exact `location`. With one described global background,
its location may be omitted per scene. With no described background, use
`locations=[]` and omit scene locations. A legacy background without description
remains a global visual reference. Subject and voice slots remain global and
stable; `audio_ref2` never moves to slot 1, backgrounds cannot carry voices, and
voiced jobs still have at most five total reference images. No per-scene identity,
voice override, TTS synthesis or musical soundtrack is introduced.

Before Durable orchestration, MCP uploads canonical sorted-key compact JSON
(`ensure_ascii=False`, `allow_nan=False`) to
`video/{videoid}/{videoid}-<full-sha256>.videoplan.json`. Upload uses create-only
storage semantics; concurrent identical requests verify existing bytes, whereas
a conflicting blob fails without overwrite or starting a workflow. Durable
input excludes the plan and carries `video_plan_blob`; Service Bus carries only
`video_plan=<simple blob name>`, with the existing correlation/reference fields.
The separate plan budget does not consume the DTS 960 KiB or Service Bus 252 KiB
budgets (including their existing safety margins).

A successful compatible worker reports `video_plan`, `video_plan_artifact`,
`prompts_srt`, `render_metadata`, `fps`, `duration_seconds`,
`rendered_duration_seconds`, `scene_count` and `chunk_count`. The completed HD
result preserves these optional fields and provides SAS links for reported
`{type_prefix}-{videoid}.videoplan.json`, `.prompts.srt` and `.render.json` beside
the MP4. Missing metadata from an old worker never invents artifact links;
failed/timed-out jobs never advertise completed artifacts. There is no dialogue
SRT: the plan contains scene prompts, not independently synthesized dialogue.

**Deployment prerequisites (not provisioned or verified here):** deploy the
compatible LTX worker before using this option; its existing `VoiceStorage`
account must match the account targeted by MCP `VIDEO_BLOB_BASE_URL` and permit
reading/writing the `video` container. VideoPlan v1 requires
`VIDEO_BLOB_PATH_PREFIX=video/`; plan, MP4 and sidecars live directly in
`video/{videoid}/` without any assumed bridge. Images and voice references
**stay in `fluxjob/agentvideo/{videoid}/`**, as for historical video jobs.
Historical music storage and worker bindings are unchanged; staged MusicPlan
snapshots use the explicit music-video mapping below. GPU acceptance,
account/container mapping, permissions and end-to-end downloads require separate
verification before rollout.

Only plan requests use `VIDEO_PLAN_ORCHESTRATION_TIMEOUT_SECONDS` (default
14400 seconds / 4 hours), configurable independently of the historical
`ORCHESTRATION_TIMEOUT_SECONDS` (7200). Increase the plan timeout deliberately
for long renders after measuring GPU capacity; a one-hour validated plan is
not guaranteed to render within the default deadline. No Azure setting, RBAC,
infrastructure or deployment is changed by this implementation.

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
6. `music_plan` takes a **pre-computed MusicPlan v1 JSON object**. The MCP
   transport also accepts that object and the `backgrounds` array JSON-encoded
   as strings; native JSON values and their string forms are decoded and
   validated identically (a blob name is never accepted). The MCP server
   validates the plan, serializes it and
   uploads it to
   `{VIDEO_BLOB_PATH_PREFIX}/{videoid}/{music_track}-{videoid}-<plan-hash>.musicplan.json`
   before starting the orchestration, then sends the worker only that simple
   blob name. The content hash makes concurrent requests with different plans
   immutable and collision-free; the plan therefore never transits through the
   Durable Task input nor the Service Bus message.
   Validation covers `schema_version=1`, `videoid` / `music_track` matching the
   call, contiguous scenes indexed from 0, scene `frames` and `total_frames` on
   the LTX `8k+1` grid with `total_frames == 1 + sum(frames - 1)`, and each
   scene `prompt` shaped as `"<location>: <action>"` matching exactly one entry
   of `locations`. `locations` stays in the plan (LTX schema unchanged) and
   must line up with the separate `backgrounds` argument:
   `music_plan.locations[i] == backgrounds[i].description`, one reference image
   per location.

Rendering is slow: the historical estimate is about 29 minutes of aggregate GPU
time per minute of song (~40 minutes for an 83-second song). Fan-out can reduce
wall-clock time only with enough independently available worker/GPU capacity;
queue order does not guarantee execution order. The workflow has one global
timeout, `MUSIC_VIDEO_ORCHESTRATION_TIMEOUT_SECONDS` (4 hours by default),
covering preparation, scene dispatch/results and assembly, not four hours per
stage. Poll with `get_music_video_result`.

#### LTX25 scene worker integration (internal contract v1)

New music-video workflows always use `music_video_contract=1` and
`music_video_stage="prepare" | "scene" | "assemble"` on the existing video
queue. No opt-in or monolithic fallback is exposed. HD video, narrative
VideoPlan and YuE2 generation messages are unchanged. A compatible worker must
implement all three stages before rollout; no deployment or GPU acceptance is
performed here.

Every message retains the existing `videoid`, `music_track`, `references`,
`width`, `height`, `type_prefix`, `instance_id`, `event_key`, `dts_event_name`,
`seed` and supplied music options. `render_id` is the final music-video
`type_prefix`, stable for the lifetime of the Durable instance. A scene adds
`music_scene_index` (the original zero-based plan index) and has
`type_prefix="{render_id}-scene-{music_scene_index:04d}"`. Both scene index and
correlation keys remain unchanged when dispatch is sorted by decreasing frame
count (ties by increasing index). The full plan/prompts never enter DTS input,
Service Bus messages or callback history; preparation returns only a compact
manifest.

| Stage | Additional input | Required successful callback fields |
|---|---|---|
| `prepare` | `music_plan_source="mcp"` for an uploaded `music_plan` filename; otherwise `"legacy"` with `music_plan=true` for reuse or no plan for construction | `music_plan` (immutable snapshot filename), `plan_sha256` (64 lowercase hex characters), integer `fps`, `total_frames`, `scenes=[{"index":0,"frames":49}, ...]` |
| `scene` | `music_plan=<snapshot>`, `music_plan_source="mcp"`, `plan_sha256`, `music_scene_index` | integer `music_scene_index`, integer `num_frames` equal to that scene's `frames` |
| `assemble` | `music_plan=<snapshot>`, `music_plan_source="mcp"`, `plan_sha256`; no scene index, `type_prefix=render_id` | `music_plan=<same snapshot>`, integer `num_frames=total_frames` |

All callbacks also require `status="completed"` and the exact `event_key`,
raised against the supplied `instance_id` / `dts_event_name`. Failure uses the
existing `status="failed"`, `error`, optional `retryable=true` contract.
`run_media_orchestration` awaits retries on that same event without re-enqueueing
the job; the worker's existing Service Bus retry/redelivery mechanism owns
execution retries. Wrong correlation, malformed manifests, mismatched scene
indices/frame counts, terminal failures or timeouts prevent assembly. No
successful scene is dispatched again within the workflow, including DTS replay.
The public failed/timeout generation identifies unfinished scene indices;
completed scenes stay in Blob and must not be deleted on partial failure.
A separate `create_music_video` call is a new render, not an implicit resume.

Preparation must freeze the exact validated MusicPlan v1 before returning.
The manifest is ordered/indexed `0..n-1`, has the existing MusicPlan scene limit,
each `frames >= 9` is on the `8k+1` grid and
`total_frames = 1 + sum(frames - 1)`. It validates `videoid`, `music_track`,
references/location matching and actual song duration, then creates/verifies an
immutable snapshot and returns its SHA256 over the downloaded bytes. Subsequent
stages validate that same digest and identity, never regenerate the plan.

Storage mapping is explicit: MCP-uploaded plans live in
`video/{videoid}/`; historical worker songs and reusable plans live in
`ltxavatarjob/agentvideo/{videoid}/` on the worker's `VoiceStorage` account.
YuE2 currently produces `.musicanalysis.json`, **not** a prompted MusicPlan.
Without an explicit/reused plan, `prepare` constructs MusicPlan with the existing
LTX builder, reusing that YuE2 analysis. Its existing Demucs/Whisper fallback
can still require GPU when no reusable analysis exists; preparation must not
load the LTX renderer. Image references remain in
`fluxjob/agentvideo/{videoid}/`. The worker must read MCP plans from `video/`
when source is `"mcp"`, and freeze snapshots back into `video/{videoid}/`.
Music fan-out v1 requires `VIDEO_BLOB_PATH_PREFIX=video/` and matching storage
accounts/permissions between `VIDEO_BLOB_BASE_URL` and `VoiceStorage`; it does
not reuse the non-musical VideoPlan decoder.

Each scene renders independently, using its plan prompt/references/frames,
without cross-scene latent/keyframe carryover and **without audio**, to
`video/{videoid}/{render_id}-scene-{index:04d}-{videoid}.mp4`.
Scene callbacks occur only after a verified durable upload. Worker receipts
must key on render ID, snapshot digest and scene index (and validate the
effective rendering inputs), so duplicate deliveries or retries after an upload
reuse the finished clip and resend its callback rather than consuming GPU
again. The same idempotency is required for preparation and final assembly;
output existence alone is not proof of a matching completed render.

Only after every scene succeeds does MCP enqueue `assemble`. It derives the
ordered scene filenames from the snapshot/render ID, validates all receipts and
clips, and runs on **CPU without loading LTX/CUDA**. Keep the first scene's
frames and drop the first frame from each subsequent scene to honor the existing
`1 + sum(frames - 1)` contract, using hard cuts with no interpolation or fades.
Concatenate in plan order, not completion order, then remux the original full
song exactly once (no generated/per-scene audio). Publish the final MP4 under
the unchanged final prefix and the historical
`{music_track}-{videoid}.musicplan.json`, `.scenes.srt`, `.prompts.srt` sidecars
in `video/{videoid}/`, then signal completion. Those canonical sidecar links and
the single final `generations[0]` preserve `create_music_video` /
`get_music_video_result`; intermediate scene clips are not public generations.
Also copy the canonical `.musicplan.json` to the historical
`ltxavatarjob/agentvideo/{videoid}/` folder so `reuse_music_plan=true` can reuse
newly rendered or explicitly supplied plans without rebuilding them.

All tools return `List[ContentBlock]`. The first block is a `TextContent`
containing the JSON status contract used for polling. A completed response also
contains one `ResourceLink` per successful generation, with
`mimeType="video/mp4"` or `mimeType="audio/flac"` and a short-lived, read-only
user delegation SAS URL.

Music generations reuse the video working folder
(`{VIDEO_BLOB_PATH_PREFIX}/{videoid}/`) and are written as `.flac`. Their
`type_prefix` starts with `music-` instead of `hdvideo-`, so both media types
coexist without collision. The queue message carries `videoid`, `style`,
`lyrics`, the optional `lora`, `slider` and `slider_strength`, and the same correlation fields
(`type_prefix`, `instance_id`, `event_key`, `dts_event_name`, `seed`). The
worker answers on the DTS event `music-{index}-{uuid}` with the same
`status` / `event_key` / `error` / `retryable` contract as video.

Use the explicit string `"lora": "none"` to generate with the Yue2 base
model without applying an adapter. For example:

```json
{
  "videoid": "video-42",
  "tracks": [
    {"style": "Acoustic pop", "lyrics": "[instrumental]", "lora": "none"}
  ]
}
```

An omitted or JSON `null` `lora` is still omitted from the queue message;
Yue2 then uses its historical `two_steps_from_hell` default, not base mode.
The worker must support `"none"` before this new MCP option is used.

### Native YuE2 sliders (`create_music` only)

Each entry of `tracks` can select **one** experimental native concept slider:
`female`, `male`, `pop`, `hiphop`, `rnb`, `indie-rock`, `pop-punk`, `metal`,
`country`, `acoustic-folk`, `house`, `disco-funk`, `kpop`, `reggaeton`,
`afrobeats`, or `lofi`. Arrays and stacking are rejected. These controls are
not exposed to video/music-video tools or other music engines.

```json
{
  "videoid": "video-42",
  "tracks": [
    {
      "style": "Acoustic pop, warm vocals, guitar and piano",
      "lyrics": "[instrumental]",
      "lora": "none",
      "slider": "female",
      "slider_strength": 0.5
    }
  ]
}
```

- An omitted or `null` `slider` means no selection. `slider_strength` must
  then be **omitted**, even for zero; explicitly supplying `null` is invalid.
- With a slider, strength is a finite JSON number in `[0,1]`; booleans,
  strings, `null`, NaN and infinity are rejected. Omission is preserved through
  MCP, Durable and Service Bus, leaving the worker's effective default `1`.
  Historical requests do not gain a strength/default/null in their messages.
- Every selection requires explicit `"lora": "none"`, even strength `0`.
  Missing/null or named-preset LoRA is rejected, never silently replaced.
  Strength zero preserves the selected identity/provenance but applies no
  slider and triggers no slider loader/download on the worker.
- The worker must support this contract and have `YUE2_ENABLE_SLIDERS=true`
  (off by default), including for strength zero. A positive strength additionally
  requires worker backend `torch` or `torch-eager`, quantization `none` and
  `offload_ar=false`. These are **worker-side requirements**, not MCP parameters
  or settings changed by this server.
- Worker `cot` remains `full` by default; `off` is only a guide recommendation,
  not a new default. This change does not add `cot`, `ar_scale` or `nar_scale`
  to the MCP API. Presets, deterministic seed, analysis and correlation are
  unchanged. The worker forces effective LoRA scales to zero with `lora=none`.

The source contract is
[zecloud/func_tts_eurovibe#68](https://github.com/zecloud/func_tts_eurovibe/pull/68),
verified at commit
[`ce6de3f`](https://github.com/zecloud/func_tts_eurovibe/tree/ce6de3f/yue2)
(`function_app.py`, `sliders.py`, `README.md`). Provenance/downloads stay on
that worker: release `particle-gmix-1600-v2`, revision
`33cf42fb0a54f60d8264d64cf6c20f038c4d172b`. Weights have non-commercial
CC BY-NC 4.0 restrictions; controls are experimental, not quality guarantees.
No loader, weights, image build or remote configuration is added here. A worker
rollout and GPU/audio acceptance remain separate prerequisites.

Music completion results relay `slider`, `slider_strength`, `slider_applied`,
`slider_release`, `slider_revision` and `slider_load_seconds` when the worker
reports them. Without a selection the worker reports identity/release/revision
as `null`, strength `0` and applied `false`; those explicit nulls survive MCP
serialization. A zero selection retains its ID/release/revision with applied
`false`. Older callbacks without these fields remain valid and do not gain
invented metadata. Video result schemas/structure stay unchanged.

Every generation result contains its prompt/index, terminal status,
deterministic blob path and `type_prefix`, optional `num_frames` (and
`music_plan` for music videos), and any error or timeout.
Music results also relay the yue2 analysis fields when present:
`analysis_status` (`completed` / `failed` / `skipped`), `music_analysis`
(simple `{type_prefix}-{videoid}.musicanalysis.json` blob name),
`music_analysis_path`, `analysis_scenes` and `analysis_error`. A completed
analysis adds an `application/json` `ResourceLink` with a SAS URL. An analysis
failure never fails the song generation itself.
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

Targeted offline music/MCP/Durable regression tests (no Azure access):

```powershell
python -m pytest -q tests\test_yue2_controls.py tests\test_music_workflow.py tests\test_mcp_results.py tests\test_orchestrator.py
```

These cover validation, omission versus null, per-track queue mapping, the MCP
handler → Durable activity → output binding path and completion metadata. They
use mocked queues/events/storage, not a deployed worker or real GPU/audio.

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
