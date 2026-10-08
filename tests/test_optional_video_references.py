import asyncio
import copy
import inspect
import json

import pytest
from pydantic import ValidationError

import function_app
from models import CreateHDVideoInput, CreateMusicVideoInput
from video_workflow import build_generation, serialize_video_message
from test_video_plan import PLAN, BACKGROUNDS
from test_music_video_workflow import registered_bindings


def payload(*, plan=False, backgrounds=False, **kwargs):
    data = {"videoid": "video-42", "prompts": ["Narrative."]}
    if plan:
        value = copy.deepcopy(PLAN)
        if not backgrounds:
            value["locations"] = []
            for scene in value["scenes"]:
                scene.pop("location")
        data["video_plan"] = value
    if backgrounds:
        data["backgrounds"] = BACKGROUNDS
    data.update(kwargs)
    return data


def wire(request, **kwargs):
    return json.loads(serialize_video_message(build_generation(request, index=0,
        instance_id="instance", event_key="event", dts_event_name="done", **kwargs)[1]))


@pytest.mark.parametrize("plan", [False, True])
@pytest.mark.parametrize("subjects", [0, 1, 2])
@pytest.mark.parametrize("described", [False, True])
def test_optional_subject_counts_map_without_synthetic_references(plan, subjects, described):
    fields = {}
    for slot in range(1, subjects + 1):
        fields[f"ref_speaker{slot}_filename"] = f"subject{slot}"
        if described:
            fields[f"ref_speaker{slot}_prompt"] = f"Subject {slot}"
    request = CreateHDVideoInput.model_validate(payload(plan=plan, **fields))
    message = wire(request)
    if described and subjects:
        assert [ref["file"] for ref in message["references"]] == [f"subject{i}.png" for i in range(1, subjects + 1)]
    else:
        assert "references" not in message
        assert [message[f"pic{i}"] for i in range(1, subjects + 1)] == [f"subject{i}.png" for i in range(1, subjects + 1)]
    for slot in range(subjects + 1, 3):
        assert f"pic{slot}" not in message
    assert "audio_ref1" not in message and "audio_ref2" not in message


@pytest.mark.parametrize("subjects", [0, 1, 2])
def test_backgrounds_only_and_mixed_plan_survive_durable_replay(subjects):
    fields = {}
    for slot in range(1, subjects + 1):
        fields[f"ref_speaker{slot}_filename"] = f"subject{slot}"
        fields[f"ref_speaker{slot}_prompt"] = f"Subject {slot}"
    request = CreateHDVideoInput.model_validate(payload(plan=True, backgrounds=True, **fields))
    name = request.video_plan_blob_name()
    replay = CreateHDVideoInput.model_validate(request.model_dump(mode="json", exclude_none=True, exclude={"video_plan"}), context={"video_plan_blob": name})
    original = wire(request)
    assert wire(replay, video_plan_blob=name) == original
    assert len(original["references"]) == subjects + 2
    assert [ref["prompt"] for ref in original["references"][-2:]] == ["Studio", "Garden"]
    assert all(ref["is_background"] for ref in original["references"][-2:])
    assert original["video_plan"] == name


@pytest.mark.parametrize("described", [False, True])
def test_visual_second_subject_alone_keeps_legacy_slot_or_visual_identity(described):
    fields = {"ref_speaker2_filename": "bob"}
    if described:
        fields["ref_speaker2_prompt"] = "Bob"
    message = wire(CreateHDVideoInput.model_validate(payload(**fields)))
    if described:
        assert message["references"] == [{"file": "bob.png", "prompt": "Bob", "is_background": False}]
    else:
        assert message["pic2"] == "bob.png" and "pic1" not in message


@pytest.mark.parametrize("plan", [False, True])
@pytest.mark.parametrize("fields", [
    {"ref_speaker1_prompt": "orphan"}, {"ref_speaker2_prompt": "orphan"},
    {"audio_ref1": "a.wav"}, {"audio_ref2": "b.wav"},
    {"ref_speaker2_filename": "bob", "audio_ref2": "b.wav"},
    {"ref_speaker2_filename": "bob", "ref_speaker2_prompt": "Bob", "audio_ref2": "b.wav"},
    {"ref_speaker1_filename": "alice", "ref_speaker2_filename": "bob", "ref_speaker1_prompt": "Alice"},
    {"reference_audio": None}, {"audio_id_lora": False},
])
def test_orphans_voice_gaps_and_removed_fields_rejected(plan, fields):
    with pytest.raises(ValidationError):
        CreateHDVideoInput.model_validate(payload(plan=plan, **fields))


def test_voice1_one_subject_and_voice2_two_subjects_preserve_association():
    one = wire(CreateHDVideoInput.model_validate(payload(ref_speaker1_filename="alice", ref_speaker1_prompt="Alice", audio_ref1="a.wav")))
    assert one["references"][0]["audio_ref"] == "a.wav"
    two = wire(CreateHDVideoInput.model_validate(payload(ref_speaker1_filename="alice", ref_speaker2_filename="bob", ref_speaker1_prompt="Alice", ref_speaker2_prompt="Bob", audio_ref2="b.wav")))
    assert "audio_ref" not in two["references"][0]
    assert two["references"][1]["audio_ref"] == "b.wav"


def test_real_pydantic_and_mcp_metadata_optional_defaults_and_music_unchanged():
    schema = CreateHDVideoInput.model_json_schema()
    assert set(schema["required"]) == {"videoid", "prompts"}
    for name in ("ref_speaker1_filename", "ref_speaker2_filename"):
        assert schema["properties"][name]["default"] is None
        assert {choice["type"] for choice in schema["properties"][name]["anyOf"]} == {"string", "null"}
    trigger = next(binding for binding in registered_bindings()["create_hd_video"] if binding.get("type") == "mcpToolTrigger")
    properties = {prop["propertyName"]: prop for prop in json.loads(trigger["toolProperties"])}
    assert {name for name, prop in properties.items() if prop["isRequired"]} == {"videoid", "prompts"}
    for name in ("ref_speaker1_filename", "ref_speaker2_filename"):
        assert not properties[name]["isRequired"]
        assert inspect.signature(inspect.unwrap(function_app.create_hd_video._function._func)).parameters[name].default is None
    assert "ref_speaker1_filename" in CreateMusicVideoInput.model_json_schema()["required"]


@pytest.mark.parametrize("plan,backgrounds", [(False, False), (True, False), (True, True)])
def test_real_mcp_wrapper_omitted_subject_kwargs_reaches_durable(monkeypatch, plan, backgrounds):
    calls = []
    class Client:
        async def start_new(self, name, *, client_input):
            calls.append(client_input)
            return "workflow"
    async def upload(*args, **kwargs):
        return "uploaded"
    async def wait(*args, **kwargs):
        return None
    async def blocks(*args, **kwargs):
        return []
    monkeypatch.setattr(function_app, "upload_media_blob", upload)
    monkeypatch.setattr(function_app, "_wait_for_workflow", wait)
    monkeypatch.setattr(function_app, "_to_content_blocks", blocks)
    trigger = function_app.create_hd_video._function._func
    durable = inspect.getclosurevars(trigger).nonlocals["target_func"]
    validated = inspect.getclosurevars(durable).nonlocals["user_code"]
    asyncio.run(validated(client=Client(), **payload(plan=plan, backgrounds=backgrounds)))
    request = calls[0]["request"]
    assert "ref_speaker1_filename" not in request and "ref_speaker2_filename" not in request
    replay = CreateHDVideoInput.model_validate(request, context={"video_plan_blob": calls[0].get("video_plan_blob")})
    message = wire(replay, video_plan_blob=calls[0].get("video_plan_blob"))
    assert "pic1" not in message and "pic2" not in message
    assert len(message.get("references", [])) == (2 if backgrounds else 0)
