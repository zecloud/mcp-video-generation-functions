import json

import pytest
from pydantic import ValidationError

from models import (
    CreateMusicInput,
    MusicLora,
    MusicMessage,
    SERVICE_BUS_BODY_BUDGET_BYTES,
    TrackSpec,
)
from music_workflow import (
    MUSIC_LABEL_MAX_LENGTH,
    aggregate_music_results,
    build_music_generation,
    output_blob_path,
    serialize_music_message,
    track_label,
)
from media_workflow import seed_for_event_key, type_prefix_for
from video_workflow import type_prefix_for as video_type_prefix_for


def make_request():
    return CreateMusicInput(
        videoid="video-42",
        tracks=[
            {"style": "epic orchestral trailer", "lyrics": "[instrumental]"},
            {
                "style": "industrial rock",
                "lyrics": "[verse] acier et fumée",
                "lora": "industrial_rock",
            },
        ],
    )


def test_message_mapping_carries_track_fields_and_correlation():
    descriptor, message = build_music_generation(
        make_request(),
        index=1,
        instance_id="instance-1",
        event_key="event-key-1",
        dts_event_name="music-1-uuid",
    )

    type_prefix = type_prefix_for("instance-1", 1, kind="music")
    assert message.model_dump(mode="json", exclude_none=True) == {
        "videoid": "video-42",
        "style": "industrial rock",
        "lyrics": "[verse] acier et fumée",
        "lora": "industrial_rock",
        "type_prefix": type_prefix,
        "instance_id": "instance-1",
        "event_key": "event-key-1",
        "dts_event_name": "music-1-uuid",
        "seed": seed_for_event_key("event-key-1"),
    }
    assert descriptor.prompt == "industrial rock (industrial_rock)"
    assert descriptor.blob_path == f"video/video-42/{type_prefix}-video-42.flac"


def test_optional_lora_is_omitted_from_serialized_message():
    _, message = build_music_generation(
        make_request(),
        index=0,
        instance_id="instance-1",
        event_key="event-key-0",
        dts_event_name="music-0-uuid",
    )

    assert message.lora is None
    assert "lora" not in json.loads(serialize_music_message(message))


@pytest.mark.parametrize("track_fields", [{}, {"lora": None}])
def test_missing_and_null_lora_keep_worker_default(track_fields):
    request = CreateMusicInput(
        videoid="video-42",
        tracks=[{"style": "pop", "lyrics": "[instrumental]", **track_fields}],
    )
    _, message = build_music_generation(
        request, index=0, instance_id="instance-1",
        event_key="event-key-0", dts_event_name="music-0-uuid",
    )

    assert message.lora is None
    assert "lora" not in json.loads(serialize_music_message(message))


@pytest.mark.parametrize("lora", ["none", MusicLora.NONE])
def test_explicit_no_lora_is_preserved_in_queue_message(lora):
    request = CreateMusicInput(
        videoid="video-42",
        tracks=[{"style": "acoustic pop", "lyrics": "[instrumental]", "lora": lora}],
    )
    descriptor, message = build_music_generation(
        request, index=0, instance_id="instance-1",
        event_key="event-key-0", dts_event_name="music-0-uuid",
    )

    assert request.tracks[0].lora is MusicLora.NONE
    assert message.lora is MusicLora.NONE
    assert json.loads(serialize_music_message(message))["lora"] == "none"
    assert descriptor.prompt == "acoustic pop (none)"
    assert json.loads(request.model_dump_json(exclude_none=True))["tracks"][0]["lora"] == "none"


def test_music_schema_advertises_explicit_no_lora():
    schema = CreateMusicInput.model_json_schema()
    assert set(schema["$defs"]["MusicLora"]["enum"]) == {
        "none", "two_steps_from_hell", "industrial_rock",
    }


def test_music_output_shares_video_working_folder_with_flac_extension():
    assert output_blob_path("video-42", "music-token-001") == (
        "video/video-42/music-token-001-video-42.flac"
    )


def test_music_and_video_type_prefixes_never_collide():
    assert type_prefix_for("instance-1", 0, kind="music") != video_type_prefix_for(
        "instance-1", 0
    )
    assert type_prefix_for("instance-1", 0, kind="music").startswith("music-")


def test_track_label_truncates_long_styles():
    label = track_label(TrackSpec(style="a" * 200, lyrics="[instrumental]"))

    assert len(label) == MUSIC_LABEL_MAX_LENGTH
    assert label.endswith("…")


def test_unknown_lora_is_rejected():
    with pytest.raises(ValidationError):
        TrackSpec(style="rock", lyrics="[verse]", lora="synthwave")

    assert TrackSpec(
        style="rock", lyrics="[verse]", lora="two_steps_from_hell"
    ).lora is MusicLora.TWO_STEPS_FROM_HELL


def test_tracks_accept_json_string_payload_from_mcp_trigger():
    request = CreateMusicInput(
        videoid="video-42",
        tracks=json.dumps(
            [{"style": "ambient", "lyrics": "[instrumental]"}],
            ensure_ascii=False,
        ),
    )

    assert request.tracks[0].style == "ambient"


def test_lyrics_over_service_bus_budget_are_rejected():
    with pytest.raises(ValidationError, match="enveloppe Service Bus"):
        CreateMusicInput(
            videoid="video-42",
            tracks=[
                {
                    "style": "rock",
                    "lyrics": "😀" * (SERVICE_BUS_BODY_BUDGET_BYTES // 4),
                }
            ],
        )


def test_final_serialized_message_over_service_bus_budget_is_rejected():
    message = MusicMessage(
        videoid="video-42",
        style="rock",
        lyrics="😀" * (SERVICE_BUS_BODY_BUDGET_BYTES // 4),
        type_prefix="music-token-001",
        instance_id="instance-1",
        event_key="event-key-1",
        dts_event_name="music-0-uuid",
        seed=42,
    )

    with pytest.raises(ValueError, match="limite Service Bus Basic"):
        serialize_music_message(message)


def test_aggregation_preserves_completed_failed_and_timeout_results():
    request = CreateMusicInput(
        videoid="video-42",
        tracks=[
            {"style": f"style {index}", "lyrics": "[instrumental]"}
            for index in range(3)
        ],
    )
    descriptors = [
        build_music_generation(
            request,
            index=index,
            instance_id="instance-1",
            event_key=f"key-{index}",
            dts_event_name=f"music-{index}-uuid",
        )[0]
        for index in range(3)
    ]

    result = aggregate_music_results(
        request=request,
        descriptors=descriptors,
        event_payloads={
            0: {"status": "completed", "event_key": "key-0"},
            1: {
                "status": "failed",
                "event_key": "key-1",
                "error": "GPU unavailable",
            },
        },
        timed_out_indexes={2},
    )

    assert result.videoid == "video-42"
    assert [item.status for item in result.generations] == [
        "completed",
        "failed",
        "timeout",
    ]
    assert result.generations[1].error == "GPU unavailable"
    assert result.generations[2].blob_path.endswith(
        f"/{type_prefix_for('instance-1', 2, kind='music')}-video-42.flac"
    )
    # type_prefix is the value create_music_video expects as music_track.
    assert [item.type_prefix for item in result.generations] == [
        type_prefix_for("instance-1", index, kind="music") for index in range(3)
    ]
    assert all(item.music_plan is None for item in result.generations)
    assert all(item.analysis_status is None for item in result.generations)


def _single_music_descriptor():
    request = CreateMusicInput(
        videoid="video-42",
        tracks=[{"style": "pop", "lyrics": "[verse]\nla la"}],
    )
    descriptor = build_music_generation(
        request,
        index=0,
        instance_id="instance-1",
        event_key="key-0",
        dts_event_name="music-0-uuid",
    )[0]
    return request, descriptor


def test_aggregation_returns_completed_music_analysis():
    request, descriptor = _single_music_descriptor()
    name = f"{descriptor.type_prefix}-video-42.musicanalysis.json"

    result = aggregate_music_results(
        request=request,
        descriptors=[descriptor],
        event_payloads={
            0: {
                "status": "completed",
                "event_key": "key-0",
                "analysis_status": "completed",
                "music_analysis": name,
                "analysis_scenes": 12,
                "analysis_seconds": 31.4,
            }
        },
        timed_out_indexes=set(),
    )

    generation = result.generations[0]
    assert generation.analysis_status == "completed"
    assert generation.music_analysis == name
    assert generation.music_analysis_path == (
        f"{descriptor.blob_path.rsplit('/', 1)[0]}/{name}"
    )
    assert generation.analysis_scenes == 12
    assert generation.analysis_error is None
    dumped = result.model_dump(exclude_none=True)["generations"][0]
    assert dumped["music_analysis"] == name


def test_aggregation_returns_failed_analysis_without_failing_song():
    request, descriptor = _single_music_descriptor()

    result = aggregate_music_results(
        request=request,
        descriptors=[descriptor],
        event_payloads={
            0: {
                "status": "completed",
                "event_key": "key-0",
                "analysis_status": "failed",
                "analysis_error": "whisper unavailable",
            }
        },
        timed_out_indexes=set(),
    )

    generation = result.generations[0]
    assert generation.status == "completed"
    assert generation.analysis_status == "failed"
    assert generation.analysis_error == "whisper unavailable"
    assert generation.music_analysis is None
    assert generation.music_analysis_path is None


@pytest.mark.parametrize(
    "name",
    [None, "", "dir/x.musicanalysis.json", "x.json", " x.musicanalysis.json"],
)
def test_aggregation_rejects_invalid_music_analysis_name(name):
    request, descriptor = _single_music_descriptor()

    result = aggregate_music_results(
        request=request,
        descriptors=[descriptor],
        event_payloads={
            0: {
                "status": "completed",
                "event_key": "key-0",
                "analysis_status": "completed",
                "music_analysis": name,
            }
        },
        timed_out_indexes=set(),
    )

    generation = result.generations[0]
    assert generation.status == "completed"
    assert generation.analysis_status == "failed"
    assert generation.music_analysis is None
    assert "music_analysis" in generation.analysis_error
