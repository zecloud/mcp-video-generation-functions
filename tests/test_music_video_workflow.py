import asyncio
import json

import pytest
from pydantic import ValidationError

import function_app
from function_app import (
    MUSIC_VIDEO_ORCHESTRATION_TIMEOUT_SECONDS,
    MUSIC_VIDEO_PROFILE,
    ORCHESTRATION_TIMEOUT_SECONDS,
    _running_result,
    _to_content_blocks,
    _workflow_response,
)
from mcp.types import ResourceLink, TextContent
from media_workflow import seed_for_event_key, type_prefix_for
from models import (
    CompletedMusicVideoWorkflowResult,
    CreateMusicVideoInput,
    DTS_INPUT_BUDGET_BYTES,
    MusicVideoMessage,
    SERVICE_BUS_BODY_BUDGET_BYTES,
)
from music_video_workflow import (
    aggregate_music_video_results,
    build_music_video_generation,
    music_video_artifacts,
    output_blob_path,
    serialize_music_video_message,
)
from test_mcp_results import DurableStatus, RuntimeStatus, fake_sas_uri_provider
from test_orchestrator import (
    FakeContext,
    FakeTask,
    complete_continued_orchestration,
    durable_callable,
)

TRACK = "music-0123456789-001"


def make_request(**overrides):
    payload = {
        "videoid": "video-42",
        "music_track": TRACK,
        "ref_speaker1_filename": "lena",
        "ref_speaker1_prompt": "Lena, 25 ans, cheveux platine",
    }
    payload.update(overrides)
    return CreateMusicVideoInput.model_validate(payload, strict=True)


def build(request=None, event_key="event-key-0"):
    return build_music_video_generation(
        request or make_request(),
        instance_id="instance-1",
        event_key=event_key,
        dts_event_name="music-video-0-uuid",
    )


# --- Modèle d'entrée -------------------------------------------------------


def test_single_performer_is_enough_and_defaults_to_vertical():
    request = make_request()

    assert request.orientation.value == "Vertical"
    assert request.reuse_music_plan is False
    assert [spec.model_dump() for spec in request.reference_specs()] == [
        {
            "file": "lena.png",
            "prompt": "Lena, 25 ans, cheveux platine",
            "is_background": False,
        }
    ]
    assert request.worker_options() == {}


def test_first_reference_and_its_prompt_are_required():
    with pytest.raises(ValidationError):
        CreateMusicVideoInput.model_validate(
            {"videoid": "video-42", "music_track": TRACK}, strict=True
        )
    with pytest.raises(ValidationError, match="ref_speaker1_prompt"):
        CreateMusicVideoInput.model_validate(
            {
                "videoid": "video-42",
                "music_track": TRACK,
                "ref_speaker1_filename": "lena",
            },
            strict=True,
        )


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"ref_speaker2_filename": "bob"}, "ref_speaker2_filename fourni sans"),
        ({"ref_speaker3_prompt": "Bob"}, "ref_speaker3_prompt fourni sans"),
        ({"background_filename": "studio"}, "background_filename fourni sans"),
    ],
)
def test_every_provided_file_needs_its_prompt(overrides, message):
    with pytest.raises(ValidationError, match=message):
        make_request(**overrides)


def test_background_and_extra_performers_become_references():
    request = make_request(
        ref_speaker2_filename="bob.jpg",
        ref_speaker2_prompt="Bob, guitariste",
        background_filename="studio",
        background_prompt="Studio néon ; toit de Paris la nuit",
    )

    assert [spec.model_dump() for spec in request.reference_specs()] == [
        {
            "file": "lena.png",
            "prompt": "Lena, 25 ans, cheveux platine",
            "is_background": False,
        },
        {"file": "bob.jpg", "prompt": "Bob, guitariste", "is_background": False},
        {
            "file": "studio.png",
            "prompt": "Studio néon ; toit de Paris la nuit",
            "is_background": True,
        },
    ]


@pytest.mark.parametrize("track", ["dir/music-1", "..\\music", ".."])
def test_music_track_must_be_a_simple_blob_name(track):
    with pytest.raises(ValidationError, match="music_track"):
        make_request(music_track=track)


@pytest.mark.parametrize(
    "overrides",
    [
        {"scene_min_seconds": 0},
        {"scene_max_seconds": 61},
        {"scene_bias": 1.5},
        {"scene_bias": -1.01},
        {"scene_min_seconds": 9.0},
        {"scene_min_seconds": 5, "scene_max_seconds": 4},
        {"whisper_language": "x" * 17},
        {"orientation": "Diagonal"},
        {"unknown": "field"},
    ],
)
def test_invalid_options_are_rejected(overrides):
    with pytest.raises(ValidationError):
        make_request(**overrides)


def test_scene_bounds_are_checked_against_worker_defaults():
    with pytest.raises(ValidationError, match="scene_max_seconds"):
        make_request(scene_min_seconds=9.0)
    assert make_request(scene_max_seconds=2.5, scene_min_seconds=2).scene_max_seconds == 2.5


def test_worker_options_carry_only_provided_fields_and_music_plan():
    request = make_request(
        lyrics="[verse] néons",
        theme_style="rétro-futuriste",
        scene_min_seconds=2,
        scene_bias=-0.5,
        whisper_language="fr",
        reuse_music_plan=True,
    )

    assert request.worker_options() == {
        "lyrics": "[verse] néons",
        "theme_style": "rétro-futuriste",
        "whisper_language": "fr",
        "scene_min_seconds": 2,
        "scene_bias": -0.5,
        "music_plan": True,
    }


def test_long_lyrics_over_service_bus_budget_are_rejected():
    with pytest.raises(ValidationError, match="enveloppe Service Bus"):
        make_request(lyrics="😀" * (SERVICE_BUS_BODY_BUDGET_BYTES // 4))


def test_lyrics_over_dts_budget_are_rejected():
    with pytest.raises(ValidationError, match="entrée DTS"):
        make_request(lyrics="😀" * (DTS_INPUT_BUDGET_BYTES // 4 + 1))


def test_long_but_reasonable_lyrics_are_accepted():
    lyrics = "\n".join(f"[verse {i}] paroles accentuées éèà" for i in range(2000))

    assert make_request(lyrics=lyrics).lyrics == lyrics


# --- Message worker --------------------------------------------------------


def test_message_mapping_carries_music_fields_without_prompt():
    request = make_request(
        orientation="Horizontal",
        lyrics="[chorus] lumière",
        story="Une nuit à Paris",
        scene_max_seconds=6.0,
    )
    descriptor, message = build(request)

    type_prefix = type_prefix_for("instance-1", 0, kind="musicvideo")
    body = json.loads(serialize_music_video_message(message))
    assert body == {
        "videoid": "video-42",
        "music_track": TRACK,
        "references": [
            {
                "file": "lena.png",
                "prompt": "Lena, 25 ans, cheveux platine",
                "is_background": False,
            }
        ],
        "lyrics": "[chorus] lumière",
        "story": "Une nuit à Paris",
        "scene_max_seconds": 6.0,
        "width": 1280,
        "height": 704,
        "type_prefix": type_prefix,
        "instance_id": "instance-1",
        "event_key": "event-key-0",
        "dts_event_name": "music-video-0-uuid",
        "seed": seed_for_event_key("event-key-0"),
    }
    assert "prompt" not in body
    assert "num_frames" not in body
    assert type_prefix.startswith("musicvideo-")
    assert descriptor.index == 0
    assert descriptor.blob_path == f"video/video-42/{type_prefix}-video-42.mp4"
    assert descriptor.prompt == f"Clip musical de {TRACK}"


def test_vertical_message_uses_704x1280_and_reuse_sends_music_plan_true():
    _, message = build(make_request(reuse_music_plan=True))

    body = json.loads(serialize_music_video_message(message))
    assert (body["width"], body["height"]) == (704, 1280)
    assert body["music_plan"] is True


def test_message_rejects_prompt_dimensions_and_missing_references():
    _, message = build()
    payload = message.model_dump(mode="json", exclude_none=True)

    with pytest.raises(ValidationError):
        MusicVideoMessage.model_validate({**payload, "prompt": "narration"})
    with pytest.raises(ValidationError):
        MusicVideoMessage.model_validate({**payload, "width": 720})
    with pytest.raises(ValidationError):
        MusicVideoMessage.model_validate({**payload, "references": []})
    assert MusicVideoMessage.model_validate(payload) == message


def test_final_serialized_message_over_service_bus_budget_is_rejected():
    _, message = build()
    oversized = message.model_copy(
        update={"lyrics": "😀" * (SERVICE_BUS_BODY_BUDGET_BYTES // 4)}
    )

    with pytest.raises(ValueError, match="limite Service Bus Basic"):
        serialize_music_video_message(oversized)


def test_output_and_artifact_paths_share_the_video_folder():
    assert output_blob_path("video-42", "musicvideo-token-001") == (
        "video/video-42/musicvideo-token-001-video-42.mp4"
    )
    assert music_video_artifacts("video-42", TRACK).model_dump() == {
        "music_plan_path": f"video/video-42/{TRACK}-video-42.musicplan.json",
        "scenes_srt_path": f"video/video-42/{TRACK}-video-42.scenes.srt",
        "prompts_srt_path": f"video/video-42/{TRACK}-video-42.prompts.srt",
    }


# --- Agrégation ------------------------------------------------------------


def test_aggregation_exposes_music_plan_type_prefix_and_artifacts():
    request = make_request()
    descriptor, _ = build(request, event_key="key-0")

    result = aggregate_music_video_results(
        request=request,
        descriptors=[descriptor],
        event_payloads={
            0: {
                "status": "completed",
                "event_key": "key-0",
                "num_frames": 2000,
                "music_plan": f"{TRACK}-video-42.musicplan.json",
            }
        },
        timed_out_indexes=set(),
    )

    assert result.music_track == TRACK
    assert result.orientation.value == "Vertical"
    generation = result.generations[0]
    assert generation.status == "completed"
    assert generation.music_plan == f"{TRACK}-video-42.musicplan.json"
    assert generation.type_prefix == descriptor.type_prefix
    assert generation.num_frames == 2000
    assert result.artifacts.music_plan_path.endswith(".musicplan.json")


def test_aggregation_reports_worker_failure_and_timeout():
    request = make_request()
    descriptor, _ = build(request, event_key="key-0")

    failed = aggregate_music_video_results(
        request=request,
        descriptors=[descriptor],
        event_payloads={
            0: {"status": "failed", "event_key": "key-0", "error": "no references"}
        },
        timed_out_indexes=set(),
    )
    timed_out = aggregate_music_video_results(
        request=request,
        descriptors=[descriptor],
        event_payloads={},
        timed_out_indexes={0},
    )

    assert failed.generations[0].status == "failed"
    assert failed.generations[0].error == "no references"
    assert failed.generations[0].music_plan is None
    assert timed_out.generations[0].status == "timeout"


# --- Orchestrateur ---------------------------------------------------------


MUSIC_VIDEO_INPUT = {
    "request": {
        "videoid": "video-42",
        "music_track": TRACK,
        "ref_speaker1_filename": "lena",
        "ref_speaker1_prompt": "Lena",
        "orientation": "Vertical",
    },
    "timeout_seconds": 14400,
}


def music_video_orchestrator_callable():
    return durable_callable(function_app.run_music_video_orchestrator)


def music_video_context():
    context = FakeContext(orchestration_input=MUSIC_VIDEO_INPUT)
    context.dispatch_tasks = [
        FakeTask("dispatch", "dispatch-0", "SUCCEEDED", {"index": 0})
    ]
    return context


def test_music_video_orchestrator_waits_for_single_event():
    context = music_video_context()
    generator = music_video_orchestrator_callable()(context)

    request = next(generator)
    assert len(request.tasks) == 2
    request = generator.send(
        next(task for task in request.tasks if task.name == "dispatch-0")
    )
    event = next(task for task in request.tasks if task.kind == "event")
    assert event.name.startswith("music-video-0-")
    event.result = {
        "status": "completed",
        "event_key": "workflow-1:0:uuid-1",
        "num_frames": 2000,
        "music_plan": f"{TRACK}-video-42.musicplan.json",
    }

    try:
        generator.send(event)
    except StopIteration as completed:
        output = completed.value
    else:
        raise AssertionError("The orchestration should have completed.")

    assert output["music_track"] == TRACK
    assert output["generations"][0]["status"] == "completed"
    assert output["generations"][0]["music_plan"] == (
        f"{TRACK}-video-42.musicplan.json"
    )
    assert output["generations"][0]["blob_path"].endswith("-video-42.mp4")
    assert len(context.event_tasks) == 1
    assert context.timer.cancelled


def test_music_video_orchestration_timeout_continues_as_new():
    context = music_video_context()
    generator = music_video_orchestrator_callable()(context)

    request = next(generator)
    generator.send(next(task for task in request.tasks if task.name == "dispatch-0"))
    try:
        generator.send(context.timer)
    except StopIteration as completed:
        assert completed.value is None
    else:
        raise AssertionError("The timed-out execution should continue as new.")

    output = complete_continued_orchestration(
        context, orchestrator=music_video_orchestrator_callable()
    )
    assert output["generations"][0]["status"] == "timeout"
    assert output["artifacts"]["scenes_srt_path"].endswith(".scenes.srt")


def test_music_video_timeout_defaults_to_four_hours():
    assert MUSIC_VIDEO_ORCHESTRATION_TIMEOUT_SECONDS == 4 * 60 * 60
    assert ORCHESTRATION_TIMEOUT_SECONDS == 2 * 60 * 60


# --- Résultats MCP ---------------------------------------------------------


def music_video_workflow_output(status="completed"):
    generation = {
        "index": 0,
        "prompt": f"Clip musical de {TRACK}",
        "status": status,
        "blob_path": "video/video-42/musicvideo-001-video-42.mp4",
        "type_prefix": "musicvideo-001",
    }
    if status == "completed":
        generation.update(
            num_frames=2000, music_plan=f"{TRACK}-video-42.musicplan.json"
        )
    else:
        generation["error"] = "GPU unavailable"
    return {
        "videoid": "video-42",
        "orientation": "Vertical",
        "music_track": TRACK,
        "artifacts": music_video_artifacts("video-42", TRACK).model_dump(),
        "generations": [generation],
    }


def test_completed_music_video_returns_video_and_artifact_links():
    result = _workflow_response(
        DurableStatus(
            RuntimeStatus("Completed"), "workflow-1", music_video_workflow_output()
        ),
        "workflow-1",
        profile=MUSIC_VIDEO_PROFILE,
    )
    assert isinstance(result, CompletedMusicVideoWorkflowResult)

    blocks = asyncio.run(
        _to_content_blocks(result, sas_uri_provider=fake_sas_uri_provider)
    )

    assert isinstance(blocks[0], TextContent)
    text = json.loads(blocks[0].text)
    assert text["result"]["generations"][0]["music_plan"] == (
        f"{TRACK}-video-42.musicplan.json"
    )
    links = blocks[1:]
    assert all(isinstance(link, ResourceLink) for link in links)
    assert [(link.name, link.mimeType) for link in links] == [
        ("musicvideo-001-video-42.mp4", "video/mp4"),
        (f"{TRACK}-video-42.musicplan.json", "application/json"),
        (f"{TRACK}-video-42.scenes.srt", "application/x-subrip"),
        (f"{TRACK}-video-42.prompts.srt", "application/x-subrip"),
    ]
    assert str(links[1].uri) == (
        f"https://storage.example.invalid/video/video-42/"
        f"{TRACK}-video-42.musicplan.json?sp=r&sig=test-signature"
    )
    assert links[0].description == f"Clip musical de {TRACK}, 2000 images."


def test_failed_music_video_generation_has_no_artifact_links():
    result = _workflow_response(
        DurableStatus(
            RuntimeStatus("Completed"),
            "workflow-1",
            music_video_workflow_output(status="failed"),
        ),
        "workflow-1",
        profile=MUSIC_VIDEO_PROFILE,
    )

    blocks = asyncio.run(
        _to_content_blocks(result, sas_uri_provider=fake_sas_uri_provider)
    )

    assert len(blocks) == 1
    assert '"status":"failed"' in blocks[0].text


def test_music_video_running_result_points_to_get_music_video_result():
    assert "get_music_video_result" in _running_result(
        "workflow-1", profile=MUSIC_VIDEO_PROFILE
    ).next


# --- Enregistrement des fonctions -----------------------------------------


def registered_bindings():
    return {
        function.get_function_name(): [
            binding.get_dict_repr() for binding in function.get_bindings()
        ]
        for function in function_app.app.get_functions()
    }


def test_music_video_tools_are_registered_alongside_existing_tools():
    bindings = registered_bindings()

    for name in (
        "create_hd_video",
        "get_hd_video_result",
        "create_music",
        "get_music_result",
        "create_music_video",
        "get_music_video_result",
        "run_music_video_orchestrator",
        "enqueue_music_video_generation",
    ):
        assert name in bindings

    queues = [
        binding["queueName"]
        for binding in bindings["enqueue_music_video_generation"]
        if binding.get("type") == "serviceBus"
    ]
    assert queues == ["%VIDEO_SERVICE_BUS_QUEUE_NAME%"]

    trigger = next(
        binding
        for binding in bindings["create_music_video"]
        if binding.get("type") == "mcpToolTrigger"
    )
    properties = {
        prop["propertyName"]: prop for prop in json.loads(trigger["toolProperties"])
    }
    required = {name for name, prop in properties.items() if prop["isRequired"]}
    assert required == {
        "videoid",
        "music_track",
        "ref_speaker1_filename",
        "ref_speaker1_prompt",
    }
    assert properties["reuse_music_plan"]["propertyType"] == "boolean"
    assert "prompt" not in properties
