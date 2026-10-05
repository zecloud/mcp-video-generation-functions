import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

import function_app
import models
from test_orchestrator import FakeContext, FakeTask, music_orchestrator_callable
from models import (
    CreateMusicInput,
    CreateMusicVideoInput,
    HDVideoWorkflowOutput,
    MusicGenerationResult,
    MusicMessage,
    MusicSlider,
    MusicWorkflowOutput,
    TrackSpec,
    VideoMessage,
    YUE2_SLIDER_COMPLETION_FIELDS,
)
from media_workflow import aggregate_results, seed_for_event_key
from music_workflow import aggregate_music_results, build_music_generation, serialize_music_message


SLIDER_IDS = {
    "female", "male", "pop", "hiphop", "rnb", "indie-rock", "pop-punk",
    "metal", "country", "acoustic-folk", "house", "disco-funk", "kpop",
    "reggaeton", "afrobeats", "lofi",
}
TRACK = {"style": "Acoustic pop", "lyrics": "[instrumental]"}
CORRELATION = {
    "videoid": "video-42", "type_prefix": "music-token-001",
    "instance_id": "workflow-1", "event_key": "key-0",
    "dts_event_name": "music-0-uuid", "seed": 777,
}


def request_and_descriptor(fields=None):
    request = CreateMusicInput(videoid="video-42", tracks=[{**TRACK, **(fields or {})}])
    descriptor, message = build_music_generation(
        request, index=0, instance_id="workflow-1", event_key="key-0",
        dts_event_name="music-0-uuid",
    )
    return request, descriptor, message


@pytest.mark.parametrize("model", [TrackSpec, MusicMessage])
@pytest.mark.parametrize("slider", sorted(SLIDER_IDS))
def test_all_native_slider_ids_accept_strict_json_arguments(model, slider):
    body = {**TRACK, "lora": "none", "slider": slider}
    if model is MusicMessage:
        body.update(CORRELATION)
    parsed = model.model_validate(body, strict=True)
    assert parsed.slider.value == slider
    assert parsed.slider_strength is None
    assert "slider_strength" not in parsed.model_fields_set
    dumped = parsed.model_dump(mode="json", exclude_none=True)
    assert dumped["slider"] == slider
    assert "slider_strength" not in dumped
    assert "slider_strength" not in model.model_validate(dumped).model_fields_set


INVALID_CONTROLS = [
    {"slider": "unknown"}, {"slider": "Female"}, {"slider": " female"},
    {"slider": ""}, {"slider": []}, {"slider": ["female", "metal"]},
    {"slider": 0}, {"slider": False},
    *[{"slider": "metal", "slider_strength": value} for value in (
        None, True, False, "0.5", "0", [], {}, float("nan"),
        float("inf"), -float("inf"), -0.1, 1.1,
    )],
    *[{"slider_strength": value} for value in (0, 0.5, None)],
    *[{"slider": None, "slider_strength": value} for value in (0, 0.5, None)],
]


@pytest.mark.parametrize("model", [TrackSpec, MusicMessage])
@pytest.mark.parametrize("fields", INVALID_CONTROLS)
def test_invalid_controls_are_rejected_before_serialization(model, fields):
    body = {**TRACK, "lora": "none", **fields}
    if model is MusicMessage:
        body.update(CORRELATION)
    with pytest.raises(ValidationError):
        model.model_validate(body)


@pytest.mark.parametrize("lora", [None, "two_steps_from_hell", "industrial_rock"])
@pytest.mark.parametrize("strength", [None, 0, 0.5])
def test_slider_requires_explicit_base_lora_even_at_zero(lora, strength):
    fields = {"slider": "metal"}
    if lora is not None:
        fields["lora"] = lora
    if strength is not None:
        fields["slider_strength"] = strength
    with pytest.raises(ValidationError, match='lora="none"'):
        request_and_descriptor(fields)
    with pytest.raises(ValidationError, match='lora="none"'):
        MusicMessage(**TRACK, **CORRELATION, **fields)


def test_explicit_null_lora_is_not_base_mode_with_slider():
    with pytest.raises(ValidationError, match='lora="none"'):
        request_and_descriptor({"lora": None, "slider": "female"})


@pytest.mark.parametrize("fields", [{}, {"slider": None}, {"lora": None}, {"slider": None, "lora": None}])
def test_historical_defaults_do_not_add_controls_to_worker_message(fields):
    request, _, message = request_and_descriptor(fields)
    dumped_request = json.loads(request.model_dump_json(exclude_none=True))
    assert dumped_request["tracks"] == [TRACK]
    dumped = json.loads(serialize_music_message(message))
    assert {name for name in ("lora", "slider", "slider_strength", "cot", "ar_scale", "nar_scale") if name in dumped} == set()
    assert dumped["seed"] == message.seed
    assert dumped["style"] == TRACK["style"]
    assert dumped["lyrics"] == TRACK["lyrics"]


@pytest.mark.parametrize("strength", [0, 0.0, 0.5, 1, 1.0])
def test_strength_survives_request_and_message_roundtrips(strength):
    request, _, message = request_and_descriptor({
        "lora": "none", "slider": "female", "slider_strength": strength,
    })
    dumped = json.loads(request.model_dump_json(exclude_none=True))
    assert CreateMusicInput.model_validate(dumped).tracks[0].slider_strength == strength
    wire = json.loads(serialize_music_message(message))
    assert wire["lora"] == "none" and wire["slider"] == "female"
    assert wire["slider_strength"] == strength
    assert MusicMessage.model_validate(wire).slider_strength == strength


def test_sliders_are_per_track_not_workflow_wide():
    request = CreateMusicInput(videoid="video-42", tracks=[
        {**TRACK, "lora": "none", "slider": "metal", "slider_strength": 0},
        {**TRACK, "lora": "industrial_rock"},
        TRACK,
    ])
    messages = [json.loads(serialize_music_message(build_music_generation(
        request, index=index, instance_id="workflow-1", event_key=f"key-{index}",
        dts_event_name=f"music-{index}-uuid",
    )[1])) for index in range(3)]
    assert messages[0]["slider_strength"] == 0
    assert messages[1]["lora"] == "industrial_rock"
    assert "lora" not in messages[2]
    assert all("slider" not in message and "slider_strength" not in message for message in messages[1:])
    with pytest.raises(ValidationError):
        CreateMusicInput(videoid="video-42", tracks=[TRACK], slider="female")


def test_track_schema_and_mcp_description_advertise_native_controls():
    schema = CreateMusicInput.model_json_schema()
    assert set(schema["$defs"]["MusicSlider"]["enum"]) == SLIDER_IDS
    track = schema["$defs"]["TrackSpec"]
    assert set(track["required"]) == {"style", "lyrics"}
    strength = track["properties"]["slider_strength"]
    assert strength["type"] == "number"
    assert strength["minimum"] == 0 and strength["maximum"] == 1
    assert "default" not in strength
    assert any(item.get("type") == "null" for item in track["properties"]["slider"]["anyOf"])
    target = function_app.create_music._function._func
    metadata = target.__mcp_tool_properties__["tracks"]
    assert metadata["propertyType"] == "object" and metadata["isArray"] is True
    description = metadata["description"]
    assert all(slider in description for slider in SLIDER_IDS)
    assert "slider_strength" in description and 'lora="none"' in description


@pytest.mark.parametrize("fields", [
    {"slider_strength": None}, {"slider_strength": 0},
    {"slider": "female"}, {"lora": "none", "slider": "male", "slider_strength": True},
])
def test_activity_rejects_invalid_job_without_setting_output(fields):
    output = Mock()
    with pytest.raises(ValidationError):
        function_app.enqueue_music_generation._function._func(
            {"index": 0, "message": {**TRACK, **CORRELATION, **fields}}, output,
        )
    output.set.assert_not_called()


@pytest.mark.parametrize("slider,strength,applied", [(None, 0, False), ("metal", 0, False), ("female", 1, True)])
@pytest.mark.parametrize("string_payload", [False, True])
def test_completion_metadata_survives_aggregation_and_serialization(slider, strength, applied, string_payload):
    request, descriptor, _ = request_and_descriptor()
    metadata = {
        "slider": slider, "slider_strength": strength, "slider_applied": applied,
        "slider_release": "particle-gmix-1600-v2" if slider else None,
        "slider_revision": "33cf42fb0a54f60d8264d64cf6c20f038c4d172b" if slider else None,
        "slider_load_seconds": 0.125 if applied else 0,
    }
    payload = {"status": "completed", "event_key": "key-0", **metadata, "analysis_status": "skipped"}
    result = aggregate_music_results(
        request=request, descriptors=[descriptor],
        event_payloads={0: json.dumps(payload) if string_payload else payload},
        timed_out_indexes=set(),
    )
    assert isinstance(result.generations[0], MusicGenerationResult)
    for exclude_none in (False, True):
        dumped = result.model_dump(mode="json", exclude_none=exclude_none)
        generation = dumped["generations"][0]
        assert {key: generation[key] for key in metadata} == metadata
        restored = MusicWorkflowOutput.model_validate(dumped)
        assert restored.generations[0].slider_applied is applied
        assert restored.generations[0].analysis_status == "skipped"
    assert result.generations[0].blob_path == descriptor.blob_path


@pytest.mark.parametrize("payload,timed_out", [
    ({"status": "completed", "event_key": "key-0"}, False),
    ({"status": "failed", "event_key": "key-0", "error": "worker failed"}, False),
    ({"status": "completed", "event_key": "wrong-key", "slider": "metal"}, False),
    (None, True),
])
def test_old_callbacks_failure_and_timeout_do_not_invent_metadata(payload, timed_out):
    request, descriptor, _ = request_and_descriptor()
    result = aggregate_music_results(
        request=request, descriptors=[descriptor], event_payloads={0: payload},
        timed_out_indexes={0} if timed_out else set(),
    )
    for exclude_none in (False, True):
        generation = result.model_dump(mode="json", exclude_none=exclude_none)["generations"][0]
        assert not set(YUE2_SLIDER_COMPLETION_FIELDS) & generation.keys()
        assert not set(YUE2_SLIDER_COMPLETION_FIELDS) & MusicWorkflowOutput.model_validate(
            result.model_dump(mode="json")
        ).generations[0].model_fields_set


def test_video_aggregation_does_not_relay_yue2_completion_metadata():
    _, descriptor, _ = request_and_descriptor()
    result = aggregate_results(
        descriptors=[descriptor], event_payloads={0: {
            "status": "completed", "event_key": "key-0", "num_frames": 121,
            "slider": "metal", "slider_strength": 0.5, "slider_applied": True,
            "slider_release": "particle-gmix-1600-v2",
        }}, timed_out_indexes=set(),
    )[0]
    assert result.num_frames == 121 and result.status == "completed"
    assert not any(name.startswith("slider") for name in result.model_dump())


def test_other_media_models_do_not_advertise_yue2_controls():
    for model in (VideoMessage, CreateMusicVideoInput):
        assert not {"slider", "slider_strength"} & model.model_json_schema()["properties"].keys()
    assert "MusicGenerationResult" not in HDVideoWorkflowOutput.model_json_schema()["$defs"]
    assert set(MusicSlider) == {MusicSlider(slider) for slider in SLIDER_IDS}


@pytest.mark.parametrize("fields", [
    {}, {"slider": None}, {"lora": "none", "slider": "female"},
    {"lora": "none", "slider": "metal", "slider_strength": 0},
    {"lora": "none", "slider": "pop", "slider_strength": 0.5},
])
@pytest.mark.parametrize("string_tracks", [False, True])
def test_mcp_to_durable_activity_to_queue_preserves_controls(monkeypatch, fields, string_tracks):
    client = Mock(start_new=AsyncMock(return_value="workflow-1"))
    monkeypatch.setattr(function_app.df.DurableOrchestrationClient, "__init__", lambda self, *args: None)
    monkeypatch.setattr(function_app.df.DurableOrchestrationClient, "start_new", client.start_new)
    monkeypatch.setattr(function_app, "_wait_for_workflow", AsyncMock(
        return_value=function_app._running_result("workflow-1", profile=function_app.MUSIC_PROFILE),
    ))
    tracks = [{**TRACK, **fields}]
    if string_tracks:
        tracks = json.dumps(tracks)
    response = asyncio.run(function_app.create_music._function._func(
        context=json.dumps({"arguments": {"videoid": "video-42", "tracks": tracks}}),
        client="{}",
    ))
    assert "workflow-1" in response
    call = client.start_new.call_args
    assert call.args == ("run_music_orchestrator",)
    orchestration_input = json.loads(json.dumps(call.kwargs["client_input"]))

    class CapturingContext(FakeContext):
        def __init__(self, payload):
            super().__init__(orchestration_input=payload)
            self.jobs = []

        def call_activity(self, name, payload):
            self.jobs.append((name, payload))
            return FakeTask("dispatch", "dispatch-0", "SUCCEEDED", {"index": 0})

    context = CapturingContext(orchestration_input)
    generator = music_orchestrator_callable()(context)
    pending = next(generator)
    activity_name, job = context.jobs[0]
    assert activity_name == "enqueue_music_generation"
    output = Mock()
    acknowledgement = function_app.enqueue_music_generation._function._func(
        json.loads(json.dumps(job)), output,
    )
    wire = json.loads(output.set.call_args.args[0])
    assert wire == job["message"]
    assert wire["videoid"] == "video-42"
    assert wire["style"] == TRACK["style"] and wire["lyrics"] == TRACK["lyrics"]
    for name in ("lora", "slider", "slider_strength"):
        if fields.get(name) is not None:
            assert wire[name] == fields[name]
        else:
            assert name not in wire
    assert not {"cot", "ar_scale", "nar_scale", "YUE2_ENABLE_SLIDERS"} & wire.keys()
    assert wire["seed"] == seed_for_event_key(wire["event_key"])
    function_app.enqueue_music_generation._function._func(job, output)
    assert output.set.call_args_list[0] == output.set.call_args_list[1]
    assert wire["instance_id"] == "workflow-1"
    assert acknowledgement == {
        "index": 0, "event_key": wire["event_key"], "dts_event_name": wire["dts_event_name"],
    }
    pending = generator.send(next(task for task in pending.tasks if task.kind == "dispatch"))
    event = next(task for task in pending.tasks if task.kind == "event")
    assert event.name == wire["dts_event_name"]
    slider = wire.get("slider")
    strength = wire.get("slider_strength", 1 if slider else 0)
    metadata = {
        "slider": slider, "slider_strength": strength,
        "slider_applied": slider is not None and strength > 0,
        "slider_release": "particle-gmix-1600-v2" if slider else None,
        "slider_revision": "33cf42fb0a54f60d8264d64cf6c20f038c4d172b" if slider else None,
        "slider_load_seconds": 0,
    }
    event.result = json.dumps({"status": "completed", "event_key": wire["event_key"], **metadata})
    with pytest.raises(StopIteration) as completion:
        generator.send(event)
    generation = completion.value.value["generations"][0]
    assert generation["status"] == "completed"
    assert generation["type_prefix"] == wire["type_prefix"]
    assert {name: generation[name] for name in metadata} == metadata
    assert context.timer.cancelled


@pytest.mark.parametrize("fields", [
    {"slider": "metal"}, {"slider_strength": None}, {"slider_strength": 0},
    {"lora": None, "slider": "pop"},
    {"lora": "none", "slider": "metal", "slider_strength": float("nan")},
])
def test_invalid_mcp_input_does_not_start_workflow(monkeypatch, fields):
    client = Mock(start_new=AsyncMock())
    monkeypatch.setattr(function_app.df.DurableOrchestrationClient, "__init__", lambda self, *args: None)
    monkeypatch.setattr(function_app.df.DurableOrchestrationClient, "start_new", client.start_new)
    with pytest.raises(ValidationError):
        asyncio.run(function_app.create_music._function._func(
            context=json.dumps({"arguments": {
                "videoid": "video-42", "tracks": json.dumps([{**TRACK, **fields}]),
            }}),
            client="{}",
        ))
    client.start_new.assert_not_called()


def test_transport_budget_accounts_for_new_controls(monkeypatch):
    baseline_size = len(json.dumps(
        {"videoid": "video-42", **TRACK}, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8"))
    monkeypatch.setattr(models, "SERVICE_BUS_BODY_BUDGET_BYTES", (
        baseline_size + models.SERVICE_BUS_DYNAMIC_ENVELOPE_BUDGET_BYTES
    ))
    CreateMusicInput(videoid="video-42", tracks=[TRACK])
    with pytest.raises(ValidationError, match="enveloppe Service Bus"):
        CreateMusicInput(videoid="video-42", tracks=[{
            **TRACK, "lora": "none", "slider": "metal", "slider_strength": 0.5,
        }])
