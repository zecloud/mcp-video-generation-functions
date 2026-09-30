from __future__ import annotations

from typing import Any, Mapping, Sequence

from media_workflow import (
    aggregate_results,
    seed_for_event_key,
    serialize_media_message,
    type_prefix_for,
)
from media_workflow import output_blob_path as media_output_blob_path
from models import (
    CreateMusicInput,
    GenerationDescriptor,
    MusicMessage,
    MusicWorkflowOutput,
)

MUSIC_EXTENSION = "flac"
MUSIC_TYPE_PREFIX_KIND = "music"
MUSIC_LABEL_MAX_LENGTH = 120


def output_blob_path(videoid: str, type_prefix: str) -> str:
    return media_output_blob_path(videoid, type_prefix, extension=MUSIC_EXTENSION)


def serialize_music_message(message: MusicMessage) -> str:
    return serialize_media_message(message)


def track_label(track) -> str:
    """Libellé court et lisible d'un morceau, reporté dans les résultats."""
    label = track.style if track.lora is None else f"{track.style} ({track.lora.value})"
    if len(label) <= MUSIC_LABEL_MAX_LENGTH:
        return label
    return f"{label[: MUSIC_LABEL_MAX_LENGTH - 1]}…"


def build_music_generation(
    request: CreateMusicInput,
    *,
    index: int,
    instance_id: str,
    event_key: str,
    dts_event_name: str,
) -> tuple[GenerationDescriptor, MusicMessage]:
    track = request.tracks[index]
    type_prefix = type_prefix_for(instance_id, index, kind=MUSIC_TYPE_PREFIX_KIND)
    descriptor = GenerationDescriptor(
        index=index,
        prompt=track_label(track),
        type_prefix=type_prefix,
        event_key=event_key,
        dts_event_name=dts_event_name,
        blob_path=output_blob_path(request.videoid, type_prefix),
    )
    message = MusicMessage(
        videoid=request.videoid,
        style=track.style,
        lyrics=track.lyrics,
        lora=track.lora,
        type_prefix=type_prefix,
        instance_id=instance_id,
        event_key=event_key,
        dts_event_name=dts_event_name,
        seed=seed_for_event_key(event_key),
    )
    serialize_music_message(message)
    return descriptor, message


def aggregate_music_results(
    *,
    request: CreateMusicInput,
    descriptors: Sequence[GenerationDescriptor],
    event_payloads: Mapping[int, Any],
    timed_out_indexes: set[int],
) -> MusicWorkflowOutput:
    return MusicWorkflowOutput(
        videoid=request.videoid,
        generations=aggregate_results(
            descriptors=descriptors,
            event_payloads=event_payloads,
            timed_out_indexes=timed_out_indexes,
        ),
    )
