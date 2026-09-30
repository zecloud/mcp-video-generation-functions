"""Clip musical : une chanson create_music mise en images par le worker ltx25.

Le message part sur la même queue que create_hd_video ; le worker bascule en
mode « music video » dès que ``music_track`` est présent.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from media_workflow import (
    MEDIA_BLOB_PATH_PREFIX,
    aggregate_results,
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
    MusicVideoWorkflowOutput,
)
from video_workflow import ORIENTATION_DIMENSIONS, VIDEO_EXTENSION

MUSIC_VIDEO_TYPE_PREFIX_KIND = "musicvideo"
MUSIC_VIDEO_GENERATION_INDEX = 0
MUSIC_PLAN_SUFFIX = "musicplan.json"
SCENES_SRT_SUFFIX = "scenes.srt"
PROMPTS_SRT_SUFFIX = "prompts.srt"


def output_blob_path(videoid: str, type_prefix: str) -> str:
    return media_output_blob_path(videoid, type_prefix, extension=VIDEO_EXTENSION)


def artifact_blob_path(videoid: str, blob_name: str) -> str:
    return f"{MEDIA_BLOB_PATH_PREFIX}{videoid}/{blob_name}"


def music_video_artifacts(videoid: str, music_track: str) -> MusicVideoArtifacts:
    base_name = f"{music_track}-{videoid}"
    return MusicVideoArtifacts(
        music_plan_path=artifact_blob_path(videoid, f"{base_name}.{MUSIC_PLAN_SUFFIX}"),
        scenes_srt_path=artifact_blob_path(videoid, f"{base_name}.{SCENES_SRT_SUFFIX}"),
        prompts_srt_path=artifact_blob_path(
            videoid, f"{base_name}.{PROMPTS_SRT_SUFFIX}"
        ),
    )


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
    message = MusicVideoMessage(
        videoid=request.videoid,
        music_track=request.music_track,
        references=request.reference_specs(),
        **request.worker_options(),
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
        ),
    )
