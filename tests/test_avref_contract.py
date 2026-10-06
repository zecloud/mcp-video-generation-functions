"""Offline producer coverage for the worker contract at 761ca11."""

import asyncio
import inspect
import json

import pytest
from pydantic import ValidationError

import function_app
from models import CreateHDVideoInput, MusicVideoMessage, ReferenceSpec, VideoMessage
from video_workflow import build_generation, serialize_video_message
from test_music_video_workflow import build as build_music_video, registered_bindings
from test_orchestrator import FakeContext, orchestrator_callable


def request_payload(**updates):
    return {
        "videoid": "video-42",
        "ref_speaker1_filename": "alice",
        "ref_speaker2_filename": "bob.jpg",
        "prompts": ["Narration"],
        **updates,
    }


def build(request):
    return build_generation(
        request, index=0, instance_id="workflow", event_key="event",
        dts_event_name="done",
    )[1]


def wire_payload(**updates):
    return {**build(CreateHDVideoInput(**request_payload())).model_dump(exclude_none=True), **updates}


@pytest.mark.parametrize("described", [False, True])
@pytest.mark.parametrize("voices", [
    {"audio_ref1": "alice.wav"},
    {"audio_ref2": "bob.FLAC"},
    {"audio_ref1": "alice.wav", "audio_ref2": "bob.FLAC"},
])
def test_voice_slots_survive_request_message_json_roundtrip(described, voices):
    descriptions = {"ref_speaker1_prompt": "Alice", "ref_speaker2_prompt": "Bob"} if described else {}
    request = CreateHDVideoInput(**request_payload(**descriptions, **voices))
    restored = CreateHDVideoInput.model_validate_json(request.model_dump_json(exclude_none=True))
    payload = json.loads(serialize_video_message(build(restored)))
    assert payload == json.loads(serialize_video_message(VideoMessage.model_validate(payload)))
    if described:
        assert [ref["file"] for ref in payload["references"]] == ["alice.png", "bob.jpg"]
        for slot in (1, 2):
            key = f"audio_ref{slot}"
            assert payload["references"][slot - 1].get("audio_ref") == voices.get(key)
            assert ("audio_ref" in payload["references"][slot - 1]) == (key in voices)
        assert not any(key in payload for key in ("pic1", "pic2", "audio_ref1", "audio_ref2"))
    else:
        assert payload["pic1"] == "alice.png"
        assert payload["pic2"] == "bob.jpg"
        assert "references" not in payload
        for slot in (1, 2):
            key = f"audio_ref{slot}"
            assert payload.get(key) == voices.get(key)
            assert (key in payload) == (key in voices)
    assert not any(key in payload for key in ("sound", "music_track", "music_plan"))


@pytest.mark.parametrize("described", [False, True])
def test_five_images_keep_silent_subjects_and_background(described):
    updates = {
        "audio_ref2": "bob.wav", "ref_speaker3_filename": "carol",
        "ref_speaker4_filename": "dave", "background_filename": "studio",
    }
    if described:
        updates.update({f"ref_speaker{i}_prompt": f"Subject {i}" for i in range(1, 5)})
        updates["background_prompt"] = "Studio"
    payload = json.loads(serialize_video_message(build(CreateHDVideoInput(**request_payload(**updates)))))
    if described:
        refs = payload["references"]
        assert len(refs) == 5
        assert refs[1]["audio_ref"] == "bob.wav"
        assert all("audio_ref" not in refs[i] for i in (0, 2, 3, 4))
        assert refs[4]["is_background"] is True
    else:
        assert [payload[f"pic{i}"] for i in range(1, 5)] == ["alice.png", "bob.jpg", "carol.png", "dave.png"]
        assert payload["background"] == "studio.png"


def test_visual_holes_preserved_but_vocal_legacy_holes_rejected():
    updates = {"ref_speaker4_filename": "dave"}
    assert build(CreateHDVideoInput(**request_payload(**updates))).pic4 == "dave.png"
    with pytest.raises(ValidationError, match="contiguës"):
        CreateHDVideoInput(**request_payload(**updates, audio_ref2="bob.wav"))


@pytest.mark.parametrize("updates", [
    {"pic1": None, "audio_ref1": "alice.wav"},
    {"pic1": None, "audio_ref2": "bob.wav"},
    {"pic2": None, "audio_ref2": "bob.wav"},
    {"pic4": "dave.png", "audio_ref2": "bob.wav"},
])
def test_wire_requires_matching_contiguous_images(updates):
    with pytest.raises(ValidationError):
        VideoMessage.model_validate(wire_payload(**updates))


@pytest.mark.parametrize("key", ["audio_ref1", "audio_ref2", "pic1", "pic2", "pic3", "pic4", "background"])
@pytest.mark.parametrize("value", [None, "other.wav"])
def test_voiced_reference_format_rejects_legacy_keys_by_presence(key, value):
    payload = wire_payload()
    del payload["pic1"], payload["pic2"]
    payload.update(references=[{"file": "alice.png", "prompt": "Alice", "audio_ref": "alice.wav"}])
    payload[key] = value
    with pytest.raises(ValidationError, match="mélanger"):
        VideoMessage.model_validate(payload)


@pytest.mark.parametrize("key", ["audio_ref1", "audio_ref2"])
def test_unvoiced_references_also_reject_null_legacy_voice_keys(key):
    with pytest.raises(ValidationError, match="mélanger"):
        VideoMessage.model_validate(wire_payload(references=[{"file": "a.png", "prompt": "A"}], **{key: None}))


@pytest.mark.parametrize("slot", [3, 4])
def test_voice_only_on_first_two_subjects(slot):
    refs = [{"file": f"s{i}.png", "prompt": f"Subject {i}"} for i in range(1, slot + 1)]
    refs[slot - 1]["audio_ref"] = "voice.wav"
    payload = wire_payload(references=refs)
    del payload["pic1"], payload["pic2"]
    with pytest.raises(ValidationError, match="sujets 1/2"):
        VideoMessage.model_validate(payload)


def test_background_voice_and_more_than_five_references_rejected():
    with pytest.raises(ValidationError, match="décor"):
        ReferenceSpec(file="studio.png", prompt="Studio", is_background=True, audio_ref="voice.wav")
    refs = [{"file": f"s{i}.png", "prompt": str(i)} for i in range(6)]
    refs[0]["audio_ref"] = "voice.wav"
    payload = wire_payload(references=refs)
    del payload["pic1"], payload["pic2"]
    with pytest.raises(ValidationError, match="1 à 5"):
        VideoMessage.model_validate(payload)


@pytest.mark.parametrize("key,value", [("sound", "song.wav"), ("music_track", "music-001"), ("music_plan", True), ("music_plan", "plan.json")])
def test_target_soundtrack_rejected(key, value):
    with pytest.raises(ValidationError, match="incompatible"):
        VideoMessage.model_validate(wire_payload(audio_ref2="bob.wav", **{key: value}))


@pytest.mark.parametrize("plan", [None, True, "plan.json"])
def test_music_video_rejects_shared_reference_voice(plan):
    payload = build_music_video()[1].model_dump(exclude_none=True)
    payload["references"][0]["audio_ref"] = "voice.wav"
    payload["music_plan"] = plan
    with pytest.raises(ValidationError, match="incompatible"):
        MusicVideoMessage.model_validate(payload)


@pytest.mark.parametrize("key", ["reference_audio", "audio_id_lora", "audio_id_strength", "audio_id_seconds", "audio_ref3", "audio_ref4", "background_audio_ref"])
@pytest.mark.parametrize("value", [None, False, ""])
def test_removed_and_unsupported_voice_options_rejected(key, value):
    for model, payload in (
        (CreateHDVideoInput, request_payload()),
        (VideoMessage, wire_payload()),
        (MusicVideoMessage, build_music_video()[1].model_dump(exclude_none=True)),
    ):
        with pytest.raises(ValidationError):
            model.model_validate({**payload, key: value})


@pytest.mark.parametrize("filename", ["", " ", " voice.wav", "voice.wav ", "dir/voice.wav", "dir\\voice.wav", "..", False])
def test_audio_filename_must_be_exact_simple_name(filename):
    with pytest.raises(ValidationError):
        CreateHDVideoInput(**request_payload(audio_ref2=filename))
    with pytest.raises(ValidationError):
        ReferenceSpec(file="bob.png", prompt="Bob", audio_ref=filename)


@pytest.mark.parametrize("filename", ["voice.WAV", "voice.flac", "voix-échantillon.mp3", "extensionless"])
def test_audio_filename_never_gains_or_changes_extension(filename):
    payload = json.loads(serialize_video_message(build(CreateHDVideoInput(**request_payload(audio_ref2=filename)))))
    assert payload["audio_ref2"] == filename


@pytest.mark.parametrize("described", [False, True])
def test_voice_names_included_in_message_budget(described):
    descriptions = {"ref_speaker1_prompt": "Alice", "ref_speaker2_prompt": "Bob"} if described else {}
    base = request_payload(**descriptions, prompts=["N" * 150_000])
    CreateHDVideoInput(**base)
    with pytest.raises(ValidationError, match="enveloppe Service Bus"):
        CreateHDVideoInput(**base, audio_ref2="é" * 55_000 + ".wav")


def test_null_voice_omitted_and_prompt_requirement_unchanged():
    null_request = CreateHDVideoInput(**request_payload(audio_ref1=None, audio_ref2=None))
    assert "audio_ref1" not in null_request.model_dump(exclude_none=True)
    assert serialize_video_message(build(null_request)) == serialize_video_message(build(CreateHDVideoInput(**request_payload())))
    assert ReferenceSpec(file="a.png", prompt="A").model_dump() == {"file": "a.png", "prompt": "A", "is_background": False}
    with pytest.raises(ValidationError):
        CreateHDVideoInput(**request_payload(audio_ref2="bob.wav", ref_speaker1_prompt="Alice"))
    with pytest.raises(ValidationError):
        ReferenceSpec(file="a.png", prompt="", audio_ref="voice.wav")


def test_hd_mcp_voice_metadata_not_exposed_on_music_tools():
    bindings = registered_bindings()
    for tool in ("create_hd_video", "create_music", "create_music_video"):
        trigger = next(binding for binding in bindings[tool] if binding.get("type") == "mcpToolTrigger")
        props = {prop["propertyName"]: prop for prop in json.loads(trigger["toolProperties"])}
        for key in ("audio_ref1", "audio_ref2"):
            if tool == "create_hd_video":
                assert props[key]["propertyType"] == "string"
                assert props[key]["isRequired"] is False
            else:
                assert key not in props


def test_orchestrator_activity_roundtrip_preserves_slot2_without_real_dispatch():
    class CaptureContext(FakeContext):
        def call_activity(self, name, payload):
            self.captured.append((name, payload))
            return super().call_activity(name, payload)

    context = CaptureContext({"request": request_payload(audio_ref2="bob.wav"), "timeout_seconds": 7200})
    context.captured = []
    generator = orchestrator_callable()(context)
    next(generator)
    name, job = context.captured[0]
    assert name == "enqueue_video_generation"

    class FakeOutput:
        def set(self, body):
            self.body = body

    output = FakeOutput()
    activity = function_app.enqueue_video_generation._function._func
    result = activity(job, output)
    payload = json.loads(output.body)
    assert payload["audio_ref2"] == "bob.wav"
    assert "audio_ref1" not in payload
    assert payload["pic1"] == "alice.png"
    assert result["event_key"] == payload["event_key"]
    generator.close()


def test_described_sparse_optional_images_keep_voice2_slot():
    request = CreateHDVideoInput(**request_payload(
        ref_speaker1_prompt="Alice", ref_speaker2_prompt="Bob",
        ref_speaker4_filename="dave", ref_speaker4_prompt="Dave", audio_ref2="bob.wav",
    ))
    refs = json.loads(serialize_video_message(build(request)))["references"]
    assert [ref["file"] for ref in refs] == ["alice.png", "bob.jpg", "dave.png"]
    assert refs[1]["audio_ref"] == "bob.wav"
    assert "audio_ref" not in refs[2]


def test_normalized_background_flag_cannot_hide_third_subject_voice():
    refs = [
        {"file": "a.png", "prompt": "A"},
        {"file": "b.png", "prompt": "B"},
        {"file": "c.png", "prompt": "C", "is_background": "false", "audio_ref": "c.wav"},
    ]
    payload = wire_payload(references=refs)
    del payload["pic1"], payload["pic2"]
    with pytest.raises(ValidationError, match="sujets 1/2"):
        VideoMessage.model_validate(payload)


def test_background_before_subjects_keeps_subject_voice_association():
    refs = [
        {"file": "studio.png", "prompt": "Studio", "is_background": True},
        {"file": "alice.png", "prompt": "Alice"},
        {"file": "bob.jpg", "prompt": "Bob", "audio_ref": "bob.wav"},
    ]
    payload = wire_payload(references=refs)
    del payload["pic1"], payload["pic2"]
    serialized = json.loads(serialize_video_message(VideoMessage.model_validate(payload)))
    assert serialized["references"] == [
        {"is_background": False, **ref} for ref in refs
    ]


def test_mcp_call_carries_voice_to_durable_input(monkeypatch):
    class FakeClient:
        async def start_new(self, name, *, client_input):
            self.name = name
            self.input = client_input
            return "workflow"

    async def fake_wait(*args, **kwargs):
        return {}

    async def fake_blocks(result):
        return []

    monkeypatch.setattr(function_app, "_wait_for_workflow", fake_wait)
    monkeypatch.setattr(function_app, "_to_content_blocks", fake_blocks)
    client = FakeClient()
    wrapper = function_app.create_hd_video._function._func
    original = inspect.unwrap(wrapper)
    asyncio.run(original(client=client, **request_payload(audio_ref2="bob.wav")))
    assert client.name == "run_hd_video_orchestrator"
    assert client.input["request"]["audio_ref2"] == "bob.wav"
    assert "audio_ref1" not in client.input["request"]
