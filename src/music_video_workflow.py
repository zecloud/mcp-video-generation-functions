"""Fan-out LTX par scène, puis assemblage CPU et remux unique de la chanson."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable, Literal, Mapping, Sequence

from media_workflow import (
    MEDIA_BLOB_PATH_PREFIX,
    aggregate_results,
    decode_event_payload,
    run_media_orchestration,
    seed_for_event_key,
    serialize_media_message,
    type_prefix_for,
)
from media_workflow import output_blob_path as media_output_blob_path
from models import (
    CreateMusicVideoInput,
    GenerationDescriptor,
    MusicVideoArtifacts,
    MusicVideoMessage,
    MusicVideoPreparation,
    MusicVideoStageMessage,
    MusicVideoWorkflowOutput,
    music_plan_blob_name,
)
from video_workflow import ORIENTATION_DIMENSIONS, VIDEO_EXTENSION

MUSIC_VIDEO_TYPE_PREFIX_KIND = "musicvideo"
MUSIC_VIDEO_GENERATION_INDEX = 0
SCENES_SRT_SUFFIX = "scenes.srt"
PROMPTS_SRT_SUFFIX = "prompts.srt"


def output_blob_path(videoid: str, type_prefix: str) -> str:
    return media_output_blob_path(videoid, type_prefix, extension=VIDEO_EXTENSION)


def artifact_blob_path(videoid: str, blob_name: str) -> str:
    return f"{MEDIA_BLOB_PATH_PREFIX}{videoid}/{blob_name}"


def music_video_artifacts(videoid: str, music_track: str) -> MusicVideoArtifacts:
    base_name = f"{music_track}-{videoid}"
    return MusicVideoArtifacts(
        music_plan_path=artifact_blob_path(
            videoid, music_plan_blob_name(videoid, music_track)
        ),
        scenes_srt_path=artifact_blob_path(videoid, f"{base_name}.{SCENES_SRT_SUFFIX}"),
        prompts_srt_path=artifact_blob_path(
            videoid, f"{base_name}.{PROMPTS_SRT_SUFFIX}"
        ),
    )


def music_plan_blob_path(request: CreateMusicVideoInput) -> str:
    """Chemin complet du blob de plan uploadé par le serveur MCP."""

    return artifact_blob_path(request.videoid, request.music_plan_blob_name())


def serialize_music_video_message(message: MusicVideoMessage) -> str:
    return serialize_media_message(message)


def music_video_label(request: CreateMusicVideoInput) -> str:
    return f"Clip musical de {request.music_track}"


def build_music_video_generation(
    request: CreateMusicVideoInput,
    *,
    instance_id: str,
    event_key: str,
    dts_event_name: str,
    music_plan_blob: str | None = None,
) -> tuple[GenerationDescriptor, MusicVideoMessage]:
    index = MUSIC_VIDEO_GENERATION_INDEX
    width, height = ORIENTATION_DIMENSIONS[request.orientation]
    type_prefix = type_prefix_for(
        instance_id, index, kind=MUSIC_VIDEO_TYPE_PREFIX_KIND
    )
    descriptor = GenerationDescriptor(
        index=index,
        prompt=music_video_label(request),
        type_prefix=type_prefix,
        event_key=event_key,
        dts_event_name=dts_event_name,
        blob_path=output_blob_path(request.videoid, type_prefix),
    )
    options = request.worker_options()
    if music_plan_blob is not None:
        # Nom du blob uploadé par le serveur MCP avant l'orchestration ; il
        # survit au passage DTS, qui ne transporte jamais le plan lui-même.
        if request.reuse_music_plan:
            raise ValueError(
                "music_plan et reuse_music_plan sont mutuellement exclusifs."
            )
        options["music_plan"] = music_plan_blob
    message = MusicVideoMessage(
        videoid=request.videoid,
        music_track=request.music_track,
        references=request.reference_specs(),
        **options,
        width=width,
        height=height,
        type_prefix=type_prefix,
        instance_id=instance_id,
        event_key=event_key,
        dts_event_name=dts_event_name,
        seed=seed_for_event_key(event_key),
    )
    serialize_music_video_message(message)
    return descriptor, message


def aggregate_music_video_results(
    *,
    request: CreateMusicVideoInput,
    descriptors: Sequence[GenerationDescriptor],
    event_payloads: Mapping[int, Any],
    timed_out_indexes: set[int],
    completed_fields: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
) -> MusicVideoWorkflowOutput:
    return MusicVideoWorkflowOutput(
        videoid=request.videoid,
        orientation=request.orientation,
        music_track=request.music_track,
        reuse_music_plan=request.reuse_music_plan,
        artifacts=music_video_artifacts(request.videoid, request.music_track),
        generations=aggregate_results(
            descriptors=descriptors,
            event_payloads=event_payloads,
            timed_out_indexes=timed_out_indexes,
            completed_fields=completed_fields,
        ),
    )


def build_music_video_stage(
    request: CreateMusicVideoInput,
    *,
    instance_id: str,
    stage: Literal["prepare", "scene", "assemble"],
    index: int = 0,
    music_plan_blob: str | None = None,
    preparation: MusicVideoPreparation | None = None,
) -> tuple[GenerationDescriptor, MusicVideoStageMessage]:
    if MEDIA_BLOB_PATH_PREFIX != "video/":
        raise ValueError("Le fan-out musical v1 nécessite VIDEO_BLOB_PATH_PREFIX=video/.")
    event_key = f"{instance_id}:music-video:{stage}:{index}"
    descriptor, base = build_music_video_generation(
        request,
        instance_id=instance_id,
        event_key=event_key,
        dts_event_name=f"music-video-{stage}-{index}",
        music_plan_blob=music_plan_blob,
    )
    render_id = base.type_prefix
    fields = base.model_dump(mode="json", exclude_none=True)
    fields.update(
        music_video_contract=1,
        music_video_stage=stage,
        render_id=render_id,
        music_plan_source="mcp" if music_plan_blob is not None else "legacy",
    )
    if preparation is not None:
        fields.update(
            music_plan=preparation.music_plan,
            music_plan_source="mcp",
            plan_sha256=preparation.plan_sha256,
        )
    if stage == "scene":
        fields.update(
            music_scene_index=index,
            type_prefix=f"{render_id}-scene-{index:04d}",
        )
        descriptor = descriptor.model_copy(
            update={
                "index": index,
                "type_prefix": fields["type_prefix"],
                "blob_path": output_blob_path(request.videoid, fields["type_prefix"]),
            }
        )
    message = MusicVideoStageMessage.model_validate(fields)
    serialize_media_message(message)
    return descriptor, message


def run_music_video_fanout(
    context,
    *,
    request: CreateMusicVideoInput,
    timeout_seconds: int,
    music_plan_blob: str | None = None,
):
    """Chaque activité/événement est historisé par DTS, sans redispatch au replay."""
    deadline = context.current_utc_datetime + timedelta(seconds=timeout_seconds)
    final_descriptor, _ = build_music_video_generation(
        request,
        instance_id=context.instance_id,
        event_key=f"{context.instance_id}:music-video:assemble:0",
        dts_event_name="music-video-assemble-0",
    )

    def output(status: Literal["failed", "timeout"], error: str):
        result = aggregate_music_video_results(
            request=request,
            descriptors=[final_descriptor],
            event_payloads={
                0: {
                    "status": "failed",
                    "event_key": final_descriptor.event_key,
                    "error": error,
                }
            },
            timed_out_indexes={0} if status == "timeout" else set(),
        )
        result.generations[0].error = error
        return result

    def run_stage(jobs):
        descriptors = [descriptor for descriptor, _ in jobs]
        payloads = [
            {"index": descriptor.index, "message": message.model_dump(
                mode="json", exclude_none=True
            )}
            for descriptor, message in jobs
        ]
        return (yield from run_media_orchestration(
            context,
            timeout_seconds=(deadline - context.current_utc_datetime).total_seconds(),
            build_dispatch=lambda: (descriptors, payloads),
            activity_name="enqueue_music_video_generation",
        ))

    prepare_job = build_music_video_stage(
        request,
        instance_id=context.instance_id,
        stage="prepare",
        music_plan_blob=music_plan_blob,
    )
    descriptors, events, timeouts = yield from run_stage([prepare_job])
    prepared_result = aggregate_results(
        descriptors=descriptors, event_payloads=events, timed_out_indexes=timeouts
    )[0]
    if prepared_result.status != "completed":
        return output(prepared_result.status, f"Préparation : {prepared_result.error}")
    try:
        payload = decode_event_payload(events[0])
        preparation = MusicVideoPreparation.model_validate({
            name: payload.get(name) for name in MusicVideoPreparation.model_fields
        })
    except (TypeError, ValueError) as exc:
        return output("failed", f"Manifest de préparation invalide : {exc}")

    # Le tri ne modifie jamais les index du plan ni les clés de corrélation.
    jobs = [
        build_music_video_stage(
            request,
            instance_id=context.instance_id,
            stage="scene",
            index=scene.index,
            preparation=preparation,
        )
        for scene in sorted(preparation.scenes, key=lambda scene: (-scene.frames, scene.index))
    ]
    descriptors, events, timeouts = yield from run_stage(jobs)
    event_indexes = {descriptor.event_key: descriptor.index for descriptor in descriptors}

    def validate_scene_completion(payload):
        index = event_indexes[payload["event_key"]]
        if (
            type(payload.get("music_scene_index")) is not int
            or payload["music_scene_index"] != index
            or type(payload.get("num_frames")) is not int
            or payload["num_frames"] != preparation.scenes[index].frames
        ):
            raise ValueError("Index ou nombre de frames de scène inattendu.")
        return {}

    scene_results = aggregate_results(
        descriptors=descriptors, event_payloads=events, timed_out_indexes=timeouts,
        completed_fields=validate_scene_completion,
    )
    failures = [result for result in scene_results if result.status != "completed"]
    if failures:
        return output(
            "timeout" if timeouts else "failed",
            "Scènes non terminées : " + "; ".join(
                f"{result.index} ({result.status}: {result.error})"
                for result in sorted(failures, key=lambda result: result.index)
            ),
        )

    assemble_job = build_music_video_stage(
        request, instance_id=context.instance_id, stage="assemble",
        preparation=preparation,
    )
    descriptors, events, timeouts = yield from run_stage([assemble_job])

    def validate_assembly_completion(payload):
        if (
            payload.get("music_plan") != preparation.music_plan
            or type(payload.get("num_frames")) is not int
            or payload["num_frames"] != preparation.total_frames
        ):
            raise ValueError("Assemblage : plan ou nombre de frames inattendu.")
        return {}

    return aggregate_music_video_results(
        request=request, descriptors=descriptors,
        event_payloads=events, timed_out_indexes=timeouts,
        completed_fields=validate_assembly_completion,
    )
