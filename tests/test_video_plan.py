import asyncio
import copy
import hashlib
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from azure.core.exceptions import ResourceExistsError
from pydantic import ValidationError

import function_app
import media_access
from models import CreateHDVideoInput, CompletedWorkflowResult, VideoMessage
from video_plan import VideoPlan, MAX_VIDEO_PLAN_BYTES
from video_workflow import aggregate_generation_results, build_generation, serialize_video_message
from test_orchestrator import FakeContext, orchestrator_callable

FIXTURE = Path(__file__).parent / "fixtures" / "videoplan_v1.json"
PLAN_BYTES = FIXTURE.read_bytes()
PLAN = json.loads(PLAN_BYTES)
DIGEST = "d1afb96608a80d5bc871ddba01a937aa197bbd5e01e40344a325fd37e20f36f1"
BACKGROUNDS = [{"filename": "studio", "description": "Studio"}, {"filename": "garden.png", "description": "Garden"}]


def request_payload(**changes):
    result = dict(videoid="video-42", ref_speaker1_filename="alice", ref_speaker2_filename="bob.jpg",
                  ref_speaker1_prompt="Alice", ref_speaker2_prompt="Bob", audio_ref2="Bob.wav",
                  prompts=["A complete narrative."], video_plan=copy.deepcopy(PLAN), backgrounds=BACKGROUNDS)
    result.update(changes)
    return result


def build(request, **kwargs):
    return build_generation(request, index=0, instance_id="instance", event_key="event", dts_event_name="done", **kwargs)


def test_shared_fixture_canonical_hash_and_json_forms():
    plan = VideoPlan.model_validate(PLAN)
    assert plan.serialize() == PLAN_BYTES
    assert hashlib.sha256(PLAN_BYTES).hexdigest() == DIGEST
    obj = CreateHDVideoInput.model_validate(request_payload())
    string = CreateHDVideoInput.model_validate(request_payload(video_plan=PLAN_BYTES.decode(), backgrounds=json.dumps(BACKGROUNDS)))
    assert obj == string
    assert obj.video_plan_blob_name() == f"video-42-{DIGEST}.videoplan.json"


@pytest.mark.parametrize("field,value", [("schema_version", True), ("schema_version", 1.0), ("schema_version", 2),
    ("fps", True), ("fps", 24.0), ("fps", 25), ("total_frames", True), ("total_frames", 241.0),
    ("total_frames", 249), ("duration_seconds", float("nan")), ("duration_seconds", float("inf")),
    ("duration_seconds", "10"), ("duration_seconds", True), ("duration_seconds", 9), ("videoid", "a/b"),
    ("locations", ["Studio", "Studio"]), ("unknown", "value")])
def test_invalid_plan_fields(field, value):
    plan = copy.deepcopy(PLAN)
    plan[field] = value
    with pytest.raises(ValidationError):
        VideoPlan.model_validate(plan)


@pytest.mark.parametrize("field,value", [("index", True), ("index", 1), ("frames", 120), ("frames", 121.0),
    ("frames", True), ("frames", 1), ("start", .1), ("end", 4), ("end", float("nan")),
    ("prompt", " "), ("prompt", 42), ("location", "Missing"), ("location", None), ("audio_ref", "x.wav")])
def test_invalid_scene_fields(field, value):
    plan = copy.deepcopy(PLAN)
    plan["scenes"][0][field] = value
    with pytest.raises(ValidationError):
        VideoPlan.model_validate(plan)


def test_duplicate_json_keys_rejected():
    with pytest.raises(ValidationError, match="dupliquée"):
        CreateHDVideoInput.model_validate(request_payload(video_plan=PLAN_BYTES.decode().replace('"fps":24', '"fps":24,"fps":24')))


@pytest.mark.parametrize("changes", [dict(prompts=["a", "b"]), dict(videoid="other"), dict(video_plan="plan.json"),
    dict(background_filename="studio"), dict(background_prompt="Studio"), dict(backgrounds=list(reversed(BACKGROUNDS))),
    dict(backgrounds=None), dict(video_plan=None), dict(ref_speaker2_prompt=None), dict(sound="song.wav"),
    dict(music_plan=True), dict(reuse_video_plan=True)])
def test_ambiguous_combinations_rejected(changes):
    with pytest.raises(ValidationError):
        CreateHDVideoInput.model_validate(request_payload(**changes))


def test_plan_without_backgrounds_preserves_legacy_and_slot2_voice():
    plan = copy.deepcopy(PLAN)
    plan["locations"] = []
    for scene in plan["scenes"]:
        scene.pop("location")
    request = CreateHDVideoInput.model_validate(request_payload(video_plan=plan, backgrounds=None,
        ref_speaker1_prompt=None, ref_speaker2_prompt=None))
    _, message = build(request)
    payload = json.loads(serialize_video_message(message))
    assert payload["pic1"] == "alice.png"
    assert payload["pic2"] == "bob.jpg"
    assert payload["audio_ref2"] == "Bob.wav"
    assert "audio_ref1" not in payload and "references" not in payload
    assert payload["video_plan"] == request.video_plan_blob_name()


def test_backgrounds_append_after_stable_subjects_and_preserve_replay():
    request = CreateHDVideoInput.model_validate(request_payload())
    blob = request.video_plan_blob_name()
    replay = CreateHDVideoInput.model_validate(request.model_dump(mode="json", exclude_none=True, exclude={"video_plan"}),
        context={"video_plan_blob": blob})
    _, message = build(replay, video_plan_blob=blob)
    assert [r.file for r in message.references] == ["alice.png", "bob.jpg", "studio.png", "garden.png"]
    assert message.references[1].audio_ref == "Bob.wav"
    assert message.references[0].audio_ref is None
    assert all(r.audio_ref is None for r in message.references[2:])
    payload = json.loads(serialize_video_message(message))
    assert payload["video_plan"] == blob
    assert payload["prompt"] == "A complete narrative."
    assert all(key not in payload for key in ("pic1", "pic2", "audio_ref1", "audio_ref2"))
    assert len(serialize_video_message(message).encode()) < 2000


def test_voice_limit_not_bypassed_by_many_backgrounds():
    backgrounds = BACKGROUNDS + [{"filename": f"bg{i}", "description": f"Bg{i}"} for i in range(4)]
    plan = copy.deepcopy(PLAN)
    plan["locations"] = [bg["description"] for bg in backgrounds]
    with pytest.raises(ValidationError, match="1 à 5"):
        CreateHDVideoInput.model_validate(request_payload(video_plan=plan, backgrounds=backgrounds))


def test_large_plan_not_carried_through_durable_and_plan_budget_is_separate():
    plan = copy.deepcopy(PLAN)
    for scene in plan["scenes"]:
        scene["prompt"] = "a" * 700000
    request = CreateHDVideoInput.model_validate(request_payload(video_plan=plan))
    assert len(request.video_plan.serialize()) > 1024 * 1024
    replay = request.model_dump(mode="json", exclude={"video_plan"}, exclude_none=True)
    assert len(json.dumps(replay)) < 2000
    plan["scenes"][0]["prompt"] = "x" * MAX_VIDEO_PLAN_BYTES
    with pytest.raises(ValidationError, match="4 MiB"):
        CreateHDVideoInput.model_validate(request_payload(video_plan=plan))


@pytest.mark.parametrize("value", [True, "../plan.json", "https://x/plan.json", "plan.json"])
def test_worker_plan_name_rejects_non_hash_names(value):
    _, message = build(CreateHDVideoInput.model_validate(request_payload()))
    payload = message.model_dump(exclude_none=True)
    payload["video_plan"] = value
    with pytest.raises(ValidationError):
        VideoMessage.model_validate(payload)


def test_worker_plan_rejects_legacy_bounds():
    _, message = build(CreateHDVideoInput.model_validate(request_payload()))
    with pytest.raises(ValidationError, match="min_seconds"):
        VideoMessage.model_validate({**message.model_dump(exclude_none=True), "min_seconds": 3})


def test_mcp_upload_before_start_and_timeout(monkeypatch):
    calls = []
    async def upload(path, content, **kwargs):
        calls.append(("upload", path, content, kwargs))
        return "unused"
    class Client:
        async def start_new(self, name, *, client_input):
            calls.append(("start", name, client_input))
            return "workflow"
    async def wait(*args, **kwargs):
        return None
    async def blocks(*args, **kwargs):
        return []
    monkeypatch.setattr(function_app, "upload_media_blob", upload)
    monkeypatch.setattr(function_app, "_wait_for_workflow", wait)
    monkeypatch.setattr(function_app, "_to_content_blocks", blocks)
    original = inspect.unwrap(function_app.create_hd_video._function._func)
    asyncio.run(original(client=Client(), **request_payload()))
    assert calls[0][0] == "upload"
    assert calls[0][1] == f"video/video-42/video-42-{DIGEST}.videoplan.json"
    assert calls[0][2] == PLAN_BYTES
    assert calls[0][3]["overwrite"] is False and calls[0][3]["verify_existing"] is True
    assert calls[1][2]["video_plan_blob"] == f"video-42-{DIGEST}.videoplan.json"
    assert "video_plan" not in calls[1][2]["request"]
    assert calls[1][2]["timeout_seconds"] == function_app.VIDEO_PLAN_ORCHESTRATION_TIMEOUT_SECONDS
    calls.clear()
    args = request_payload(video_plan=None, backgrounds=None)
    asyncio.run(original(client=Client(), **args))
    assert calls[0][0] == "start"
    assert calls[0][2]["timeout_seconds"] == function_app.ORCHESTRATION_TIMEOUT_SECONDS
    assert "video_plan_blob" not in calls[0][2]


def test_upload_failure_prevents_orchestration(monkeypatch):
    async def upload(*args, **kwargs):
        raise RuntimeError("storage unavailable")
    class Client:
        async def start_new(self, *args, **kwargs):
            pytest.fail("Started before successful upload")
    monkeypatch.setattr(function_app, "upload_media_blob", upload)
    with pytest.raises(RuntimeError, match="storage unavailable"):
        asyncio.run(inspect.unwrap(function_app.create_hd_video._function._func)(client=Client(), **request_payload()))


def test_orchestrator_replay_queues_only_blob(monkeypatch):
    request = CreateHDVideoInput.model_validate(request_payload())
    envelope = {"request": request.model_dump(mode="json", exclude_none=True, exclude={"video_plan"}),
        "video_plan_blob": request.video_plan_blob_name(), "timeout_seconds": 14400}
    captured = {}
    def run(context, **kwargs):
        captured.update(kwargs)
        descriptors, jobs = kwargs["build_dispatch"]()
        captured["jobs"] = jobs
        if False:
            yield
        return descriptors, {0: {"status": "failed", "event_key": descriptors[0].event_key}}, set()
    monkeypatch.setattr(function_app, "run_media_orchestration", run)
    generator = orchestrator_callable()(FakeContext(envelope))
    with pytest.raises(StopIteration):
        next(generator)
    assert captured["timeout_seconds"] == 14400
    assert len(captured["jobs"]) == 1
    assert captured["jobs"][0]["message"]["video_plan"] == request.video_plan_blob_name()
    assert "scenes" not in json.dumps(captured["jobs"])


def completion(request, descriptor):
    base = f"{descriptor.type_prefix}-{request.videoid}"
    return dict(status="completed", event_key=descriptor.event_key, num_frames=241,
        video_plan=request.video_plan_blob_name(), video_plan_artifact=f"{base}.videoplan.json",
        prompts_srt=f"{base}.prompts.srt", render_metadata=f"{base}.render.json", fps=24,
        duration_seconds=10.0, rendered_duration_seconds=241/24, scene_count=2, chunk_count=2)


def test_completion_metadata_and_sas_sidecars():
    request = CreateHDVideoInput.model_validate(request_payload())
    descriptor, _ = build(request)
    output = aggregate_generation_results(request=request, descriptors=[descriptor],
        event_payloads={0: completion(request, descriptor)}, timed_out_indexes=set())
    assert output.generations[0].scene_count == 2
    async def sas(paths, **kwargs):
        return {path: f"https://example.invalid/{path}" for path in paths}
    blocks = asyncio.run(function_app._to_content_blocks(CompletedWorkflowResult(workflow_id="job", result=output), sas_uri_provider=sas))
    assert len(blocks) == 5
    assert [block.name.rsplit('.', 1)[-1] for block in blocks[1:]] == ["mp4", "json", "srt", "json"]


def test_old_callback_does_not_invent_artifacts():
    request = CreateHDVideoInput.model_validate(request_payload())
    descriptor, _ = build(request)
    result = aggregate_generation_results(request=request, descriptors=[descriptor],
        event_payloads={0: {"status": "completed", "event_key": "event"}}, timed_out_indexes=set())
    data = result.model_dump(mode="json")
    assert "video_plan" not in data["generations"][0]
    assert function_app._video_plan_artifact_links(result) == []


@pytest.mark.parametrize("existing", [b"same", b"evil", b"longer"])
def test_immutable_upload_verifies_existing_exact_content_and_closes_credentials(existing):
    class Credential:
        closed = False
        async def close(self):
            self.closed = True
    class Blob:
        async def upload_blob(self, content, **kwargs):
            assert kwargs["overwrite"] is False
            raise ResourceExistsError("exists")
        async def get_blob_properties(self):
            return SimpleNamespace(size=len(existing))
        async def download_blob(self, **kwargs):
            assert kwargs == {"offset": 0, "length": 5}
            return self
        async def readall(self):
            return existing
    class Service:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def get_blob_client(self, **kwargs):
            return Blob()
    cred = Credential()
    async def run():
        return await media_access.upload_media_blob("video/video-42/hash.json", b"same",
            base_url="https://example.invalid/video", overwrite=False, verify_existing=True,
            credential_factory=lambda: cred, service_client_factory=lambda **kwargs: Service())
    if existing == b"same":
        assert asyncio.run(run()).endswith("hash.json")
    else:
        with pytest.raises(ValueError, match="différent"):
            asyncio.run(run())
    assert cred.closed


def test_actual_mcp_registration_exposes_optional_plan_and_backgrounds():
    from test_music_video_workflow import registered_bindings
    trigger = next(binding for binding in registered_bindings()["create_hd_video"] if binding.get("type") == "mcpToolTrigger")
    properties = {p["propertyName"]: p for p in json.loads(trigger["toolProperties"])}
    assert properties["video_plan"]["propertyType"] == "object"
    assert properties["video_plan"]["isRequired"] is False
    assert properties["backgrounds"]["isRequired"] is False


@pytest.mark.parametrize("as_string", [False, True])
def test_real_mcp_validation_wrapper_accepts_golden_and_hashes_uploaded_bytes(monkeypatch, as_string):
    golden_bytes = (FIXTURE.parent / "videoplan-v1-golden.json").read_bytes()
    golden = json.loads(golden_bytes)
    assert hashlib.sha256(golden_bytes).hexdigest() == "853499b120585835968fcd92eca280e40525c4ef469f688f044057d6467ed5e8"
    assert VideoPlan.from_json(golden_bytes).videoid == "video-plan-contract-42"
    calls = []
    async def upload(path, content, **kwargs):
        calls.append((path, content))
        return path
    class Client:
        async def start_new(self, name, *, client_input):
            calls.append(client_input)
            return "workflow"
    async def wait(*args, **kwargs):
        return None
    async def blocks(*args, **kwargs):
        return []
    monkeypatch.setattr(function_app, "upload_media_blob", upload)
    monkeypatch.setattr(function_app, "_wait_for_workflow", wait)
    monkeypatch.setattr(function_app, "_to_content_blocks", blocks)
    trigger = function_app.create_hd_video._function._func
    durable_wrapper = inspect.getclosurevars(trigger).nonlocals["target_func"]
    validated_wrapper = inspect.getclosurevars(durable_wrapper).nonlocals["user_code"]
    kwargs = request_payload(videoid=golden["videoid"], video_plan=golden_bytes.decode() if as_string else golden)
    asyncio.run(validated_wrapper(client=Client(), **kwargs))
    path, actual = calls[0]
    digest = hashlib.sha256(actual).hexdigest()
    assert digest == "3a365a6e14368b567a97fa76aefaa1a25160499eb4c37386ab7cb6c83f90e40f"
    assert path.endswith(f"-{digest}.videoplan.json")
    assert calls[1]["video_plan_blob"].endswith(f"-{digest}.videoplan.json")
    assert VideoPlan.from_json(actual).serialize() == actual
    calls.clear()
    kwargs["video_plan"] = golden_bytes.decode().replace('"fps": 24', '"fps": 24, "fps": 24')
    with pytest.raises(ValidationError, match="dupliqu"):
        asyncio.run(validated_wrapper(client=Client(), **kwargs))
    assert calls == []


def test_from_json_rejects_duplicates_and_oversize_before_validation():
    with pytest.raises(ValueError, match="dupliqu"):
        VideoPlan.from_json(b'{"fps":24,"fps":24}')
    with pytest.raises(ValueError, match="4 MiB"):
        VideoPlan.from_json(b' ' * (MAX_VIDEO_PLAN_BYTES + 1))


def test_plan_duration_and_scene_count_boundaries():
    plan = {"videoid": "limit", "fps": 24, "duration_seconds": 3600.0, "total_frames": 86401,
        "scenes": [{"index": 0, "start": 0.0, "end": 3600.0, "frames": 86401, "prompt": "Long scene."}]}
    assert VideoPlan.model_validate(plan).total_frames == 86401
    plan["duration_seconds"] = 3601.0
    with pytest.raises(ValidationError):
        VideoPlan.model_validate(plan)
    plan = {"videoid": "limit", "fps": 24, "duration_seconds": 512 * 8 / 24, "total_frames": 512 * 8 + 1,
        "scenes": [{"index": i, "start": i * 8 / 24, "end": (i + 1) * 8 / 24, "frames": 9, "prompt": "Scene."} for i in range(512)]}
    assert len(VideoPlan.model_validate(plan).scenes) == 512
    plan["scenes"].append({"index": 512, "start": 512 * 8 / 24, "end": 513 * 8 / 24, "frames": 9, "prompt": "Scene."})
    with pytest.raises(ValidationError):
        VideoPlan.model_validate(plan)


def test_plan_single_described_and_legacy_background_fallback():
    plan = copy.deepcopy(PLAN)
    plan["locations"] = ["Studio"]
    for scene in plan["scenes"]:
        scene.pop("location")
    request = CreateHDVideoInput.model_validate(request_payload(video_plan=plan, backgrounds=None,
        background_filename="studio", background_prompt="Studio"))
    assert build(request)[1].references[-1].is_background
    plan["locations"] = []
    request = CreateHDVideoInput.model_validate(request_payload(video_plan=plan, backgrounds=None,
        ref_speaker1_prompt=None, ref_speaker2_prompt=None, background_filename="studio"))
    assert build(request)[1].background == "studio.png"


def test_upload_rejects_non_video_prefix_and_missing_plan(monkeypatch):
    request = CreateHDVideoInput.model_validate(request_payload())
    monkeypatch.setattr(function_app, "MEDIA_BLOB_PATH_PREFIX", "custom/")
    with pytest.raises(ValueError, match="VIDEO_BLOB_PATH_PREFIX"):
        asyncio.run(function_app._upload_video_plan(request))
    with pytest.raises(ValueError, match="Aucun"):
        asyncio.run(function_app._upload_video_plan(CreateHDVideoInput.model_validate(request_payload(video_plan=None, backgrounds=None))))


@pytest.mark.parametrize("field,value", [("videoid", " video42"), ("locations", ["Studio", " Garden"])])
def test_plan_exact_text_not_silently_trimmed(field, value):
    plan = copy.deepcopy(PLAN)
    plan[field] = value
    with pytest.raises(ValidationError):
        VideoPlan.from_json(json.dumps(plan))


@pytest.mark.parametrize("field", ["prompt", "location"])
def test_scene_exact_text_not_silently_trimmed(field):
    plan = copy.deepcopy(PLAN)
    plan["scenes"][0][field] = " " + plan["scenes"][0][field]
    with pytest.raises(ValidationError):
        VideoPlan.from_json(json.dumps(plan))
