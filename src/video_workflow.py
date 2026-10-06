from __future__ import annotations

from pathlib import PurePath
from typing import Any, Mapping, Sequence

from media_workflow import (
    MEDIA_BLOB_PATH_PREFIX,
    aggregate_results,
    decode_event_payload,
    is_retryable_failure_event,
    seed_for_event_key,
    serialize_media_message,
    type_prefix_for,
)
from media_workflow import output_blob_path as media_output_blob_path
from models import (
    REFERENCE_FIELDS,
    CreateHDVideoInput,
    GenerationDescriptor,
    HDVideoWorkflowOutput,
    VideoMessage,
    Orientation,
    ReferenceSpec,
)

__all__ = [
    "ORIENTATION_DIMENSIONS",
    "VIDEO_BLOB_PATH_PREFIX",
    "VIDEO_EXTENSION",
    "VIDEO_TYPE_PREFIX_KIND",
    "aggregate_generation_results",
    "build_generation",
    "decode_event_payload",
    "ensure_png_when_extensionless",
    "is_retryable_failure_event",
    "output_blob_path",
    "seed_for_event_key",
    "serialize_video_message",
    "type_prefix_for",
]

VIDEO_BLOB_PATH_PREFIX = MEDIA_BLOB_PATH_PREFIX
VIDEO_EXTENSION = "mp4"
VIDEO_TYPE_PREFIX_KIND = "hdvideo"


ORIENTATION_DIMENSIONS: dict[Orientation, tuple[int, int]] = {
    Orientation.VERTICAL: (704, 1280),
    Orientation.HORIZONTAL: (1280, 704),
}


def ensure_png_when_extensionless(filename: str) -> str:
    return filename if PurePath(filename).suffix else f"{filename}.png"


def output_blob_path(videoid: str, type_prefix: str) -> str:
    return media_output_blob_path(videoid, type_prefix, extension=VIDEO_EXTENSION)


def serialize_video_message(message: VideoMessage) -> str:
    return serialize_media_message(message)


def build_generation(
    request: CreateHDVideoInput,
    *,
    index: int,
    instance_id: str,
    event_key: str,
    dts_event_name: str,
) -> tuple[GenerationDescriptor, VideoMessage]:
    prompt = request.prompts[index]
    width, height = ORIENTATION_DIMENSIONS[request.orientation]
    type_prefix = type_prefix_for(instance_id, index, kind=VIDEO_TYPE_PREFIX_KIND)
    descriptor = GenerationDescriptor(
        index=index,
        prompt=prompt,
        type_prefix=type_prefix,
        event_key=event_key,
        dts_event_name=dts_event_name,
        blob_path=output_blob_path(request.videoid, type_prefix),
    )
    legacy_keys = ("pic1", "pic2", "pic3", "pic4", "background")
    references: list[ReferenceSpec] | None = None
    legacy_pics: dict[str, str] = {}
    uses_references = any(
        getattr(request, prompt_field) is not None
        for _, prompt_field, _ in REFERENCE_FIELDS
    )
    if uses_references:
        # Le validateur garantit la cohérence filename/prompt pour chaque référence.
        references = [
            ReferenceSpec(
                file=ensure_png_when_extensionless(getattr(request, filename_field)),
                prompt=getattr(request, prompt_field),
                is_background=is_background,
                audio_ref=(
                    getattr(request, f"audio_ref{slot}") if slot in (1, 2) else None
                ),
            )
            for slot, (filename_field, prompt_field, is_background) in enumerate(
                REFERENCE_FIELDS, 1
            )
            if getattr(request, filename_field) is not None
        ]
    else:
        for legacy_key, (filename_field, _, _) in zip(legacy_keys, REFERENCE_FIELDS):
            filename = getattr(request, filename_field)
            if filename is not None:
                legacy_pics[legacy_key] = ensure_png_when_extensionless(filename)
        for slot in (1, 2):
            audio_ref = getattr(request, f"audio_ref{slot}")
            if audio_ref is not None:
                legacy_pics[f"audio_ref{slot}"] = audio_ref

    message = VideoMessage(
        videoid=request.videoid,
        prompt=prompt,
        **legacy_pics,
        references=references,
        width=width,
        height=height,
        type_prefix=type_prefix,
        instance_id=instance_id,
        event_key=event_key,
        dts_event_name=dts_event_name,
        seed=seed_for_event_key(event_key),
    )
    serialize_video_message(message)
    return descriptor, message


def aggregate_generation_results(
    *,
    request: CreateHDVideoInput,
    descriptors: Sequence[GenerationDescriptor],
    event_payloads: Mapping[int, Any],
    timed_out_indexes: set[int],
) -> HDVideoWorkflowOutput:
    return HDVideoWorkflowOutput(
        videoid=request.videoid,
        orientation=request.orientation,
        generations=aggregate_results(
            descriptors=descriptors,
            event_payloads=event_payloads,
            timed_out_indexes=timed_out_indexes,
        ),
    )
