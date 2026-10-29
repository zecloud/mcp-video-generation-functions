import asyncio
import functools
import inspect
import json
from datetime import timedelta
from unittest.mock import Mock

import pytest
from azure.durable_functions import DurableOrchestrationContext, Orchestrator
from azure.durable_functions.models.history import HistoryEventType
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
    MusicPlan,
    MusicVideoMessage,
    MusicVideoPreparation,
    MusicVideoStageMessage,
    SERVICE_BUS_BODY_BUDGET_BYTES,
)
from music_video_workflow import (
    aggregate_music_video_results,
    build_music_video_generation,
    build_music_video_stage,
    music_plan_blob_path,
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
BACKGROUNDS = [
    {"filename": "street", "description": "Neon street"},
    {"filename": "roof", "description": "Rooftop"},
]
SCENES = [
    {
        "index": 0,
        "start": 0,
        "end": 2.0,
        "frames": 49,
        "lyrics": "néons",
        "instrumental": False,
        "prompt": "Neon street: The singer walks while the camera tracks left.",
    },
    {
        "index": 1,
        "start": 2.0,
        "end": 5.0,
        "frames": 73,
        "lyrics": "la nuit",
        "instrumental": False,
        "prompt": "Rooftop: The singer looks up while the camera rises.",
    },
]
SINGLE_SCENE = {
    "index": 0,
    "start": 0,
    "end": 5.0,
    "frames": 121,
    "instrumental": True,
    "prompt": "Neon street: The camera drifts along the wet pavement.",
}


def make_plan(**overrides):
    plan = {
        "schema_version": 1,
        "videoid": "video-42",
        "music_track": TRACK,
        "fps": 24,
        "duration_seconds": 5.0,
        "total_frames": 121,
        "subject": "Image 1: the singer",
        "locations": ["Neon street", "Rooftop"],
        "scenes": SCENES,
    }
    plan.update(overrides)
    return plan


def make_plan_model(**overrides):
    return MusicPlan.model_validate(make_plan(**overrides))


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
    assert request.music_plan is None
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


def test_explicit_music_plan_is_uploaded_and_sent_as_the_simple_blob_name():
    request = make_request(music_plan=make_plan(), backgrounds=BACKGROUNDS)
    expected_name = (
        f"{TRACK}-video-42-"
        "0fa341946645976c.musicplan.json"
    )

    assert request.music_plan_blob_name() == expected_name
    assert request.worker_options() == {"music_plan": expected_name}
    assert music_plan_blob_path(request) == f"video/video-42/{expected_name}"

    _, message = build(request)
    body = json.loads(serialize_music_video_message(message))
    assert body["music_plan"] == expected_name
    # Le plan lui-même ne transite ni par Service Bus ni par DTS.
    assert "locations" not in body and "scenes" not in body
    assert "music_plan" not in json.loads(
        request.model_dump_json(exclude_none=True, exclude={"music_plan"})
    )


def test_music_plan_accepts_the_json_string_form_and_serializes_back():
    plan = make_plan()
    request = make_request(
        music_plan=json.dumps(plan), backgrounds=json.dumps(BACKGROUNDS)
    )

    assert request.music_plan == MusicPlan.model_validate(plan)
    assert json.loads(request.music_plan.serialize())["total_frames"] == 121


def test_music_plan_blob_name_survives_the_dts_round_trip():
    request = make_request(music_plan=make_plan(), backgrounds=BACKGROUNDS)
    blob_name = request.music_plan_blob_name()
    # Ce que le client Durable envoie réellement à l'orchestrateur.
    replayed = CreateMusicVideoInput.model_validate(
        request.model_dump(mode="json", exclude_none=True, exclude={"music_plan"})
    )

    assert replayed.music_plan is None
    _, message = build_music_video_generation(
        replayed,
        instance_id="instance-1",
        event_key="event-key-0",
        dts_event_name="music-video-0-uuid",
        music_plan_blob=blob_name,
    )
    assert json.loads(serialize_music_video_message(message))["music_plan"] == blob_name


@pytest.mark.parametrize(
    "mutation, message",
    [
        ({"schema_version": 2}, "schema_version"),
        ({"videoid": "autre"}, "music_plan.videoid"),
        ({"music_track": "music-autre"}, "music_plan.music_track"),
        ({"total_frames": 999}, "total_frames"),
        ({"locations": ["Neon street", "Neon street"]}, "doublons"),
    ],
)
def test_invalid_music_plans_are_rejected(mutation, message):
    with pytest.raises(ValidationError, match=message):
        make_request(music_plan=make_plan(**mutation), backgrounds=BACKGROUNDS)


def test_plan_scenes_must_be_contiguous_and_on_the_frame_grid():
    with pytest.raises(ValidationError, match="grille 8k"):
        make_plan_model(scenes=[{**SCENES[0], "frames": 50}, SCENES[1]])
    with pytest.raises(ValidationError, match="supérieur ou égal à 9"):
        make_plan_model(
            total_frames=113,
            scenes=[{**SCENES[0], "frames": 1}, SCENES[1]],
        )
    with pytest.raises(ValidationError, match="prolonge pas"):
        make_plan_model(scenes=[SCENES[0], {**SCENES[1], "start": 2.5}])
    with pytest.raises(ValidationError, match="ordonnées"):
        make_plan_model(scenes=[SCENES[1], SCENES[0]])
    with pytest.raises(ValidationError, match="sa durée"):
        make_plan_model(
            scenes=[
                {**SCENES[0], "frames": 73},
                {**SCENES[1], "frames": 49},
            ],
        )


def test_scene_frame_count_keeps_an_existing_8k_plus_1_boundary():
    scene = {
        **SINGLE_SCENE,
        "end": 49 / 24,
        "frames": 49,
    }

    plan = make_plan_model(
        duration_seconds=49 / 24,
        total_frames=49,
        locations=["Neon street"],
        scenes=[scene],
    )

    assert plan.scenes[0].frames == 49


def test_plan_scenes_must_cover_the_full_duration():
    with pytest.raises(ValidationError, match="se terminent à 4"):
        make_plan_model(scenes=[SCENES[0], {**SCENES[1], "end": 4.0}])


def test_plan_total_frames_must_match_duration_and_fps():
    scenes = [
        {**SCENES[0], "frames": 65},
        {**SCENES[1], "frames": 65},
    ]
    with pytest.raises(ValidationError, match="la grille 8k\\+1 exige 121"):
        make_plan_model(total_frames=129, scenes=scenes)


def test_plan_duration_must_be_finite():
    with pytest.raises(ValidationError, match="duration_seconds doit être fini"):
        make_plan_model(duration_seconds=float("inf"))


@pytest.mark.parametrize(
    "prompt",
    [
        "The singer walks under the neon lights.",
        "Unknown place: The singer walks.",
        "Neon street:",
        "Neon street: ",
    ],
)
def test_each_scene_prompt_must_match_exactly_one_location(prompt):
    with pytest.raises(ValidationError, match="exactement un décor"):
        make_plan_model(scenes=[{**SCENES[0], "prompt": prompt}, SCENES[1]])


def test_ambiguous_location_prefixes_are_rejected():
    with pytest.raises(ValidationError, match="exactement un décor"):
        make_plan_model(
            locations=["Neon street", "Neon street: the alley"],
            scenes=[
                {**SCENES[0], "prompt": "Neon street: the alley: The singer walks."},
                {**SCENES[1], "prompt": "Neon street: the alley: The camera rises."},
            ],
        )


def test_plan_scene_locations_are_exposed_in_order():
    assert make_plan_model().scene_locations() == ["Neon street", "Rooftop"]


def test_backgrounds_must_match_plan_locations_one_per_place():
    with pytest.raises(ValidationError, match="1 décor"):
        make_request(music_plan=make_plan(), backgrounds=BACKGROUNDS[:1])
    with pytest.raises(ValidationError, match="Décors incohérents"):
        make_request(
            music_plan=make_plan(),
            backgrounds=[
                BACKGROUNDS[0],
                {"filename": "roof", "description": "Autre toit"},
            ],
        )


def test_backgrounds_become_ordered_background_references():
    request = make_request(music_plan=make_plan(), backgrounds=BACKGROUNDS)

    assert [spec.model_dump() for spec in request.reference_specs()] == [
        {
            "file": "lena.png",
            "prompt": "Lena, 25 ans, cheveux platine",
            "is_background": False,
        },
        {"file": "street.png", "prompt": "Neon street", "is_background": True},
        {"file": "roof.png", "prompt": "Rooftop", "is_background": True},
    ]


def test_legacy_single_background_is_normalized_to_a_one_item_list():
    request = make_request(
        background_filename="street",
        background_prompt="Neon street",
        music_plan=make_plan(locations=["Neon street"], scenes=[SINGLE_SCENE]),
    )

    assert [spec.model_dump() for spec in request.background_specs()] == [
        {"filename": "street", "description": "Neon street"}
    ]
    assert request.reference_specs()[-1].model_dump() == {
        "file": "street.png",
        "prompt": "Neon street",
        "is_background": True,
    }


def test_legacy_background_fields_cannot_be_mixed_with_backgrounds():
    with pytest.raises(ValidationError, match="mutuellement exclusifs"):
        make_request(
            background_filename="street",
            background_prompt="Neon street",
            backgrounds=BACKGROUNDS,
        )


def test_music_plan_and_reuse_music_plan_together_are_rejected():
    with pytest.raises(ValidationError, match="mutuellement exclusifs"):
        make_request(
            music_plan=make_plan(), backgrounds=BACKGROUNDS, reuse_music_plan=True
        )
    with pytest.raises(ValueError, match="mutuellement exclusifs"):
        build_music_video_generation(
            make_request(reuse_music_plan=True),
            instance_id="instance-1",
            event_key="event-key-0",
            dts_event_name="music-video-0-uuid",
            music_plan_blob=f"{TRACK}-video-42.musicplan.json",
        )


@pytest.mark.parametrize(
    "plan", ["dossier/plan.musicplan.json", "dossier\\plan.musicplan.json", ".."]
)
def test_worker_music_plan_must_be_a_simple_blob_name(plan):
    with pytest.raises(ValidationError, match="music_plan"):
        MusicVideoMessage.model_validate(
            {
                **build()[1].model_dump(mode="json", exclude_none=True),
                "music_plan": plan,
            }
        )


def test_music_plan_upload_targets_the_working_folder_and_returns_the_blob_name():
    request = make_request(music_plan=make_plan(), backgrounds=BACKGROUNDS)
    calls = []

    async def fake_uploader(
        blob_path, content, *, base_url, content_type, overwrite, verify_existing
    ):
        assert overwrite is False
        assert verify_existing is True
        calls.append((blob_path, content, base_url, content_type))
        return f"{base_url}/uploaded"

    blob_name = asyncio.run(
        function_app._upload_music_plan(request, uploader=fake_uploader)
    )

    assert blob_name.endswith(".musicplan.json")
    assert blob_name.startswith(f"{TRACK}-video-42-")
    blob_path, content, base_url, content_type = calls[0]
    assert blob_path == f"video/video-42/{blob_name}"
    assert base_url == function_app.VIDEO_BLOB_BASE_URL
    assert content_type == "application/json"
    uploaded = json.loads(content)
    assert uploaded["schema_version"] == 1
    assert uploaded["locations"] == ["Neon street", "Rooftop"]
    assert len(uploaded["scenes"]) == 2


def test_music_plan_blob_name_rejects_path_separators_before_upload():
    with pytest.raises(ValidationError, match="music_plan"):
        make_request(
            videoid="nested/video-42",
            music_plan=make_plan(videoid="nested/video-42"),
            backgrounds=BACKGROUNDS,
        )


def test_music_plan_upload_requires_a_plan():
    with pytest.raises(ValueError, match="Aucun music_plan"):
        asyncio.run(function_app._upload_music_plan(make_request()))


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
    return durable_callable(function_app.run_music_video_orchestrator_v2)


def music_video_context():
    context = FakeContext(orchestration_input=MUSIC_VIDEO_INPUT)
    context.dispatch_tasks = [
        FakeTask("dispatch", "dispatch-0", "SUCCEEDED", {"index": 0})
    ]
    return context


PLAN_SNAPSHOT = f"{TRACK}-video-42-{'a' * 64}.musicplan.json"
PREPARATION = {
    "music_plan": PLAN_SNAPSHOT,
    "plan_sha256": "a" * 64,
    "fps": 24,
    "total_frames": 121,
    "scenes": [{"index": 0, "frames": 49}, {"index": 1, "frames": 73}],
}


class MusicVideoContext(FakeContext):
    def __init__(self, *, failed_scene=None, orchestration_input=None):
        super().__init__(orchestration_input=orchestration_input or MUSIC_VIDEO_INPUT)
        self.jobs = []
        self.failed_scene = failed_scene
        self.timers = []

    def call_activity(self, name, job):
        assert name == "enqueue_music_video_generation_v2"
        self.jobs.append(job)
        body = MusicVideoStageMessage.model_validate(job["message"])
        if body.music_video_stage == "scene" and body.music_scene_index == self.failed_scene:
            return FakeTask("dispatch", body.event_key, "FAILED", RuntimeError("queue down"))
        return FakeTask("dispatch", body.event_key, "SUCCEEDED", {"index": job["index"]})

    def create_timer(self, deadline):
        task = super().create_timer(deadline)
        self.timers.append((deadline, task))
        return task


def drive_music_video(
    context, *, update_event=None, timeout_stage=None, timeout_scene_index=None
):
    generator = music_video_orchestrator_callable()(context)
    request = next(generator)
    while True:
        dispatch = next((task for task in request.tasks if task.kind == "dispatch"), None)
        if dispatch is not None:
            winner = dispatch
        else:
            event = next(task for task in request.tasks if task.kind == "event")
            job = next(job["message"] for job in context.jobs
                       if job["message"]["dts_event_name"] == event.name)
            stage = job["music_video_stage"]
            if stage == timeout_stage and (
                stage != "scene"
                or timeout_scene_index is None
                or job["music_scene_index"] == timeout_scene_index
            ):
                winner = context.timer
            else:
                payload = {"status": "completed", "event_key": job["event_key"]}
                if stage == "prepare":
                    payload.update(PREPARATION)
                elif stage == "scene":
                    index = job["music_scene_index"]
                    payload.update(music_scene_index=index,
                                   num_frames=PREPARATION["scenes"][index]["frames"])
                else:
                    payload.update(music_plan=PLAN_SNAPSHOT, num_frames=121)
                if update_event:
                    update_event(context, job, payload)
                event.result = payload
                winner = event
        try:
            request = generator.send(winner)
        except StopIteration as completed:
            if context.continued_input:
                return complete_continued_orchestration(
                    context, orchestrator=music_video_orchestrator_callable()
                )
            return completed.value


def test_music_video_orchestrator_fans_out_longest_first_and_assembles_once():
    context = MusicVideoContext()
    output = drive_music_video(context)
    messages = [job["message"] for job in context.jobs]
    assert [message["music_video_stage"] for message in messages] == [
        "prepare", "scene", "scene", "assemble"
    ]
    assert [job["index"] for job in context.jobs] == [0, 1, 0, 0]
    assert [message["music_scene_index"] for message in messages[1:3]] == [1, 0]
    render_id = messages[0]["render_id"]
    assert messages[1]["type_prefix"] == f"{render_id}-scene-0001"
    assert messages[-1]["type_prefix"] == render_id
    for message in messages[1:]:
        assert message["music_plan"] == PLAN_SNAPSHOT
        assert message["plan_sha256"] == "a" * 64
        assert message["music_plan_source"] == "mcp"
        assert "scenes" not in message
        assert message["references"] == messages[0]["references"]
    assert output["music_track"] == TRACK
    assert len(output["generations"]) == 1
    assert output["generations"][0]["status"] == "completed"
    assert output["generations"][0]["num_frames"] == 121
    assert output["generations"][0]["music_plan"] == PLAN_SNAPSHOT
    assert output["generations"][0]["blob_path"].endswith(f"{render_id}-video-42.mp4")
    assert len(context.event_tasks) == 4
    assert all(timer.cancelled for _, timer in context.timers)


@pytest.mark.parametrize("stage", ["prepare", "scene", "assemble"])
def test_music_video_timeout_covers_every_stage_and_keeps_public_shape(stage):
    context = MusicVideoContext()
    output = drive_music_video(context, timeout_stage=stage)
    assert output["generations"][0]["status"] == "timeout"
    assert len(output["generations"]) == 1
    assert output["artifacts"]["scenes_srt_path"].endswith(".scenes.srt")
    if stage != "assemble":
        assert not any(job["message"]["music_video_stage"] == "assemble" for job in context.jobs)


@pytest.mark.parametrize("failure", ["dispatch", "terminal", "wrong-index", "wrong-frames", "wrong-key", "json"])
def test_partial_scene_failure_never_assembles_or_redispatches_successes(failure):
    context = MusicVideoContext(failed_scene=0 if failure == "dispatch" else None)

    def update(_context, job, payload):
        if job["music_video_stage"] != "scene" or job["music_scene_index"] != 0:
            return
        if failure == "terminal":
            payload.update(status="failed", error="GPU failure")
        elif failure == "wrong-index":
            payload["music_scene_index"] = 1
        elif failure == "wrong-frames":
            payload.update(status="Completed", num_frames=9)
        elif failure == "wrong-key":
            payload["event_key"] = "other-workflow"
        elif failure == "json":
            payload.clear()

    output = drive_music_video(context, update_event=update)
    assert output["generations"][0]["status"] == "failed"
    assert "0 (failed:" in output["generations"][0]["error"]
    assert len(context.jobs) == 3
    assert [job["index"] for job in context.jobs[1:]] == [1, 0]
    if failure == "dispatch":
        assert "Envoi Service Bus impossible : queue down" in output["generations"][0]["error"]


@pytest.mark.parametrize("stage", ["prepare", "scene", "assemble"])
def test_retryable_failure_waits_without_resending_finished_scenes(stage):
    context = MusicVideoContext()
    failures = []

    def update(_context, job, payload):
        if (
            job["music_video_stage"] == stage
            and (stage != "scene" or job["music_scene_index"] == 0)
            and not failures
        ):
            failures.append(0)
            payload.update(status="failed", retryable=True, error="temporary OOM")

    output = drive_music_video(context, update_event=update)
    assert output["generations"][0]["status"] == "completed"
    assert len(context.jobs) == 4
    assert len(context.event_tasks) == 5


def test_partial_timeout_keeps_finished_scene_and_identifies_unfinished_index():
    context = MusicVideoContext()
    output = drive_music_video(context, timeout_stage="scene", timeout_scene_index=0)
    assert output["generations"][0]["status"] == "timeout"
    assert "0 (timeout:" in output["generations"][0]["error"]
    assert "1 (" not in output["generations"][0]["error"]
    assert len(context.jobs) == 3


def test_longest_scene_dispatch_failure_keeps_original_index_and_correlation():
    context = MusicVideoContext(failed_scene=1)
    output = drive_music_video(context)
    error = output["generations"][0]["error"]
    assert "1 (failed: Envoi Service Bus impossible : queue down)" in error
    assert "0 (" not in error
    assert len(context.jobs) == 3


@pytest.mark.parametrize("invalid", [
    {"scenes": []},
    {"scenes": [{"index": 1, "frames": 121}]},
    {"scenes": [{"index": 0, "frames": 48}]},
    {"total_frames": 129},
    {"plan_sha256": "bad-digest"},
    {"music_plan": "../plan.json"},
    {"fps": 24.0},
    {"fps": True},
    {"scenes": [{"index": False, "frames": 121}]},
])
def test_invalid_preparation_never_dispatches_scenes(invalid):
    context = MusicVideoContext()

    def update(_context, job, payload):
        if job["music_video_stage"] == "prepare":
            payload.update(invalid)

    output = drive_music_video(context, update_event=update)
    assert output["generations"][0]["status"] == "failed"
    assert "Manifest" in output["generations"][0]["error"]
    assert len(context.jobs) == 1


@pytest.mark.parametrize("invalid", [{"num_frames": 129}, {"music_plan": "other.musicplan.json"}])
def test_invalid_assembly_result_is_not_reported_as_success(invalid):
    context = MusicVideoContext()

    def update(_context, job, payload):
        if job["music_video_stage"] == "assemble":
            payload.update(invalid)

    output = drive_music_video(context, update_event=update)
    assert output["generations"][0]["status"] == "failed"
    assert "Assemblage" in output["generations"][0]["error"]


def test_music_video_has_one_global_deadline_not_a_fresh_budget_per_stage():
    context = MusicVideoContext()

    def update(context, _job, _payload):
        context.current_utc_datetime += timedelta(seconds=5)

    drive_music_video(context, update_event=update)
    assert len(context.timers) == 3
    assert len({deadline for deadline, _ in context.timers}) == 1


def test_budget_exhaustion_after_prepare_dispatches_no_gpu_work():
    context = MusicVideoContext()

    def update(context, job, _payload):
        if job["music_video_stage"] == "prepare":
            context.current_utc_datetime += timedelta(seconds=14401)

    output = drive_music_video(context, update_event=update)
    assert output["generations"][0]["status"] == "timeout"
    assert len(context.jobs) == 1


def test_stage_envelope_addresses_uploaded_and_reused_plans_explicitly():
    request = make_request(music_plan=make_plan(), backgrounds=BACKGROUNDS)
    _, supplied = build_music_video_stage(
        request, instance_id="workflow", stage="prepare",
        music_plan_blob=request.music_plan_blob_name(),
    )
    assert supplied.music_plan_source == "mcp"
    assert supplied.music_plan == request.music_plan_blob_name()
    _, reused = build_music_video_stage(
        make_request(reuse_music_plan=True), instance_id="workflow", stage="prepare",
    )
    assert reused.music_plan_source == "legacy"
    assert reused.music_plan is True
    first, first_message = build_music_video_stage(
        make_request(), instance_id="workflow", stage="scene", index=1,
        preparation=MusicVideoPreparation.model_validate(PREPARATION),
    )
    replay, replay_message = build_music_video_stage(
        make_request(), instance_id="workflow", stage="scene", index=1,
        preparation=MusicVideoPreparation.model_validate(PREPARATION),
    )
    assert first == replay
    assert first_message == replay_message


@pytest.mark.parametrize("overrides", [
    {"music_video_contract": True},
    {"music_video_contract": 2},
    {"music_scene_index": None},
    {"music_scene_index": True},
    {"music_scene_index": 0},
    {"music_video_stage": "assemble"},
    {"music_plan_source": "legacy"},
    {"music_plan": True},
    {"plan_sha256": None},
    {"transition_keyframes": []},
])
def test_invalid_stage_envelopes_fail_explicitly(overrides):
    _, message = build_music_video_stage(
        make_request(), instance_id="workflow", stage="scene", index=1,
        preparation=MusicVideoPreparation.model_validate(PREPARATION),
    )
    with pytest.raises(ValidationError):
        MusicVideoStageMessage.model_validate({
            **message.model_dump(exclude_none=True), **overrides
        })


@pytest.mark.parametrize("as_string", [False, True])
def test_public_mcp_uploads_plan_before_starting_and_excludes_it_from_dts(monkeypatch, as_string):
    calls = []

    async def upload(path, content, **kwargs):
        assert kwargs["overwrite"] is False
        assert kwargs["verify_existing"] is True
        calls.append(("upload", path, content))
        return path

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
    trigger = function_app.create_music_video._function._func
    durable_wrapper = inspect.getclosurevars(trigger).nonlocals["target_func"]
    validated_wrapper = inspect.getclosurevars(durable_wrapper).nonlocals["user_code"]
    plan = make_plan()
    request = make_request(music_plan=plan, backgrounds=BACKGROUNDS)
    payload = request.model_dump(mode="json", exclude_none=True)
    if as_string:
        payload.update(music_plan=json.dumps(plan), backgrounds=json.dumps(BACKGROUNDS))
    asyncio.run(validated_wrapper(client=Client(), **payload))
    assert [call[0] for call in calls] == ["upload", "start"]
    assert calls[0][1] == f"video/video-42/{request.music_plan_blob_name()}"
    assert json.loads(calls[0][2]) == json.loads(request.music_plan.serialize())
    assert calls[1][1] == "run_music_video_orchestrator_v2"
    client_input = calls[1][2]
    assert client_input["music_plan_blob"] == request.music_plan_blob_name()
    assert "music_plan" not in client_input["request"]
    assert client_input["timeout_seconds"] == 14400
    assert not any(name.startswith("music_video_") for name in client_input["request"])


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


LEGACY_EVENT_KEY = "workflow-1:0:9aa63d0e-2a0f-5d1a-b2fd-340ee06b796a"
LEGACY_EVENT_NAME = "music-video-0-256103f0-e502-525d-95cf-2f6a450f386e"


def replay_legacy_history(*, dispatched, completion=None):
    def event(event_type, event_id, **fields):
        return {
            "EventType": int(event_type), "EventId": event_id,
            "IsPlayed": True, "Timestamp": "2026-08-26T00:00:00Z",
            **fields,
        }

    history = [
        event(HistoryEventType.ORCHESTRATOR_STARTED, -1),
        event(
            HistoryEventType.EXECUTION_STARTED, -1, Version="",
            Name="run_music_video_orchestrator", Input=json.dumps(MUSIC_VIDEO_INPUT),
        ),
    ]
    if dispatched:
        history.extend([
            event(HistoryEventType.TIMER_CREATED, 0, FireAt="2026-08-26T04:00:00Z"),
            event(
                HistoryEventType.TASK_SCHEDULED, 1,
                Name="enqueue_music_video_generation",
            ),
            event(
                HistoryEventType.TASK_COMPLETED, 2, TaskScheduledId=1,
                Result=json.dumps({
                    "index": 0, "event_key": LEGACY_EVENT_KEY,
                    "dts_event_name": LEGACY_EVENT_NAME,
                }),
            ),
        ])
    if completion is not None:
        history.append(event(
            HistoryEventType.EVENT_RAISED, 3,
            Name=LEGACY_EVENT_NAME, Input=json.dumps(completion),
        ))
    context = DurableOrchestrationContext(
        history=history, instanceId="workflow-1", parentInstanceId=None,
        isReplaying=True, input=json.dumps(MUSIC_VIDEO_INPUT),
    )
    return json.loads(Orchestrator(
        durable_callable(function_app.run_music_video_orchestrator)
    ).handle(context))


@pytest.mark.parametrize("dispatched", [False, True])
def test_legacy_replay_preserves_pending_activity_payload_and_event_name(dispatched):
    state = replay_legacy_history(dispatched=dispatched)
    assert state["isDone"] is False
    actions = [action for batch in state["actions"] for action in batch]
    assert actions[0]["actionType"] == 5  # Timer retains its historical task ID.
    dispatch = actions[1]
    assert dispatch["functionName"] == "enqueue_music_video_generation"
    message = json.loads(dispatch["input"])["message"]
    assert message["event_key"] == LEGACY_EVENT_KEY
    assert message["dts_event_name"] == LEGACY_EVENT_NAME
    assert "music_video_stage" not in message
    assert "music_video_contract" not in message


def test_legacy_replay_consumes_old_worker_callback_and_completes_final_clip():
    state = replay_legacy_history(dispatched=True, completion={
        "status": "completed", "event_key": LEGACY_EVENT_KEY,
        "num_frames": 2000, "music_plan": f"{TRACK}-video-42.musicplan.json",
    })
    assert state["isDone"] is True
    generation = state["output"]["generations"][0]
    assert generation["status"] == "completed"
    assert generation["num_frames"] == 2000
    assert generation["music_plan"] == f"{TRACK}-video-42.musicplan.json"
    assert len(state["output"]["generations"]) == 1
    assert "music-video-prepare" not in json.dumps(state)


def test_legacy_timeout_continuation_remains_replayable():
    context = music_video_context()
    legacy = durable_callable(function_app.run_music_video_orchestrator)
    generator = legacy(context)
    request = next(generator)
    generator.send(next(task for task in request.tasks if task.kind == "dispatch"))
    assert context.event_tasks[0].name == "music-video-0-uuid-2"
    with pytest.raises(StopIteration):
        generator.send(context.timer)
    output = complete_continued_orchestration(context, orchestrator=legacy)
    assert output["generations"][0]["status"] == "timeout"


def test_legacy_enqueue_accepts_pending_payload_without_stage_fields():
    descriptor, body = build(
        event_key=LEGACY_EVENT_KEY,
    )
    pending = {"index": descriptor.index, "message": body.model_dump(
        mode="json", exclude_none=True
    )}
    message = Mock(spec=function_app.func.Out)
    activity = function_app.enqueue_music_video_generation._function._func
    result = activity(pending, message)
    assert json.loads(message.set.call_args.args[0]) == pending["message"]
    assert result == {
        "index": 0, "event_key": LEGACY_EVENT_KEY,
        "dts_event_name": body.dts_event_name,
    }


def test_versioned_enqueue_accepts_stages_without_weakening_legacy_validation():
    descriptor, body = build_music_video_stage(
        make_request(), instance_id="workflow", stage="prepare",
    )
    job = {"index": descriptor.index, "message": body.model_dump(
        mode="json", exclude_none=True
    )}
    message = Mock(spec=function_app.func.Out)
    activity = function_app.enqueue_music_video_generation_v2._function._func
    result = activity(job, message)
    assert json.loads(message.set.call_args.args[0]) == job["message"]
    assert result["event_key"] == body.event_key
    with pytest.raises(ValidationError):
        function_app.enqueue_music_video_generation._function._func(job, message)


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


@functools.cache
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
        "run_music_video_orchestrator_v2",
        "enqueue_music_video_generation_v2",
    ):
        assert name in bindings

    for name in ("enqueue_music_video_generation", "enqueue_music_video_generation_v2"):
        queues = [
            binding["queueName"]
            for binding in bindings[name]
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
    assert properties["music_plan"]["propertyType"] == "object"
    assert properties["backgrounds"]["propertyType"] in {"object", "string"}
    assert "prompt" not in properties
    for name in ("scene_min_seconds", "scene_max_seconds", "scene_bias"):
        assert properties[name]["propertyType"] == "number"


def test_every_mcp_tool_property_uses_a_valid_json_schema_type():
    valid_types = {"string", "number", "integer", "boolean", "object"}
    tool_count = 0
    for name, bindings in registered_bindings().items():
        for binding in bindings:
            if binding.get("type") != "mcpToolTrigger":
                continue
            tool_count += 1
            for prop in json.loads(binding["toolProperties"]):
                assert prop["propertyType"] in valid_types, (name, prop)
    assert tool_count == 6
