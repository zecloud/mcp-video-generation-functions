import pytest

from models import (
    CreateHDVideoInput,
    Ltx25Message,
    MAX_PROMPT_UTF8_BYTES,
    Orientation,
    SERVICE_BUS_BODY_BUDGET_BYTES,
)
from video_workflow import (
    aggregate_generation_results,
    build_generation,
    ensure_png_when_extensionless,
    is_retryable_failure_event,
    seed_for_event_key,
    serialize_ltx25_message,
    type_prefix_for,
)


def make_request(orientation=Orientation.VERTICAL):
    return CreateHDVideoInput(
        videoid="video-42",
        ref_speaker1_filename="alice",
        ref_speaker2_filename="bob.jpeg",
        prompts=["Premier prompt", "Second prompt", "Troisième prompt"],
        orientation=orientation,
    )


def test_message_mapping_vertical_and_reference_extensions():
    descriptor, message = build_generation(
        make_request(),
        index=0,
        instance_id="instance-1",
        event_key="event-key-1",
        dts_event_name="event-name-1",
    )

    assert message.model_dump() == {
        "videoid": "video-42",
        "prompt": "Premier prompt",
        "pic1": "alice.png",
        "pic2": "bob.jpeg",
        "width": 720,
        "height": 1280,
        "type_prefix": type_prefix_for("instance-1", 0),
        "instance_id": "instance-1",
        "event_key": "event-key-1",
        "dts_event_name": "event-name-1",
        "seed": seed_for_event_key("event-key-1"),
    }
    assert descriptor.blob_path == (
        "ltxavatarjob/agentvideo/video-42/"
        f"{type_prefix_for('instance-1', 0)}-video-42.mp4"
    )


def test_message_mapping_horizontal():
    _, message = build_generation(
        make_request(Orientation.HORIZONTAL),
        index=1,
        instance_id="instance-1",
        event_key="event-key-2",
        dts_event_name="event-name-2",
    )

    assert (message.width, message.height) == (1280, 720)
    assert message.type_prefix == type_prefix_for("instance-1", 1)


def test_png_is_only_added_when_extension_is_absent():
    assert ensure_png_when_extensionless("speaker") == "speaker.png"
    assert ensure_png_when_extensionless("speaker.PNG") == "speaker.PNG"
    assert ensure_png_when_extensionless("speaker.webp") == "speaker.webp"


def test_aggregation_preserves_completed_failed_and_timeout_results():
    request = make_request()
    descriptors = [
        build_generation(
            request,
            index=index,
            instance_id="instance-1",
            event_key=f"key-{index}",
            dts_event_name=f"event-{index}",
        )[0]
        for index in range(3)
    ]

    result = aggregate_generation_results(
        request=request,
        descriptors=descriptors,
        event_payloads={
            0: {
                "status": "completed",
                "event_key": "key-0",
                "num_frames": 241,
            },
            1: {
                "status": "failed",
                "event_key": "key-1",
                "error": "GPU unavailable",
            },
        },
        timed_out_indexes={2},
    )

    assert [item.status for item in result.generations] == [
        "completed",
        "failed",
        "timeout",
    ]
    assert result.generations[0].num_frames == 241
    assert result.generations[1].error == "GPU unavailable"
    assert result.generations[2].blob_path.endswith(
        f"/{type_prefix_for('instance-1', 2)}-video-42.mp4"
    )


def test_aggregation_rejects_mismatched_event_key():
    request = make_request()
    descriptor = build_generation(
        request,
        index=0,
        instance_id="instance-1",
        event_key="expected",
        dts_event_name="event-0",
    )[0]

    result = aggregate_generation_results(
        request=request,
        descriptors=[descriptor],
        event_payloads={
            0: {"status": "completed", "event_key": "unexpected", "num_frames": 9}
        },
        timed_out_indexes=set(),
    )

    assert result.generations[0].status == "failed"
    assert "corrélation" in result.generations[0].error


def test_worker_failure_requires_explicit_retryable_signal():
    assert is_retryable_failure_event(
        {
            "status": "failed",
            "event_key": "expected",
            "error": "temporary",
            "retryable": True,
        },
        "expected",
    )
    assert not is_retryable_failure_event(
        {"status": "failed", "event_key": "expected", "error": "terminal"},
        "expected",
    )
    assert not is_retryable_failure_event(
        {"status": "failed", "event_key": "unexpected", "retryable": True},
        "expected",
    )


def test_type_prefix_is_stable_per_workflow_and_unique_between_workflows():
    assert type_prefix_for("workflow-a", 0) == type_prefix_for("workflow-a", 0)
    assert type_prefix_for("workflow-a", 0) != type_prefix_for("workflow-b", 0)
    assert type_prefix_for("workflow-a", 0) != type_prefix_for("workflow-a", 1)


def test_timeout_overrides_retryable_intermediate_failure():
    request = make_request()
    descriptor = build_generation(
        request,
        index=0,
        instance_id="instance-1",
        event_key="expected",
        dts_event_name="event-0",
    )[0]

    result = aggregate_generation_results(
        request=request,
        descriptors=[descriptor],
        event_payloads={
            0: {
                "status": "failed",
                "event_key": "expected",
                "retryable": True,
                "error": "temporary",
            }
        },
        timed_out_indexes={0},
    )

    assert result.generations[0].status == "timeout"
    assert result.generations[0].error == "Délai maximal de génération dépassé."


def test_multibyte_message_close_to_limit_is_accepted():
    prompt = "é" * ((MAX_PROMPT_UTF8_BYTES - 1024) // 2)
    request = CreateHDVideoInput(
        videoid="video-42",
        ref_speaker1_filename="alice",
        ref_speaker2_filename="bob",
        prompts=[prompt],
    )

    _, message = build_generation(
        request,
        index=0,
        instance_id="instance-1",
        event_key="event-key-1",
        dts_event_name="event-name-1",
    )
    serialized = serialize_ltx25_message(message)

    assert len(serialized.encode("utf-8")) <= SERVICE_BUS_BODY_BUDGET_BYTES
    assert len(serialized.encode("utf-8")) > len(serialized)


def test_final_serialized_message_over_service_bus_budget_is_rejected():
    message = Ltx25Message(
        videoid="video-42",
        prompt="😀" * (SERVICE_BUS_BODY_BUDGET_BYTES // 4),
        pic1="alice.png",
        pic2="bob.png",
        width=720,
        height=1280,
        type_prefix="hdvideo-token-001",
        instance_id="instance-1",
        event_key="event-key-1",
        dts_event_name="event-name-1",
        seed=42,
    )

    with pytest.raises(ValueError, match="limite Service Bus Basic"):
        serialize_ltx25_message(message)
