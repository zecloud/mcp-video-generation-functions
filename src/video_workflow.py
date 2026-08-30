from __future__ import annotations

import hashlib
import json
from pathlib import PurePath
from typing import Any, Mapping, Sequence

from models import (
    REFERENCE_FIELDS,
    CreateHDVideoInput,
    GenerationDescriptor,
    GenerationResult,
    HDVideoWorkflowOutput,
    Ltx25Message,
    Orientation,
    ReferenceSpec,
    SERVICE_BUS_BODY_BUDGET_BYTES,
    SERVICE_BUS_MESSAGE_LIMIT_BYTES,
)

VIDEO_BLOB_PATH_PREFIX = "ltxavatarjob/agentvideo/"


ORIENTATION_DIMENSIONS: dict[Orientation, tuple[int, int]] = {
    Orientation.VERTICAL: (704, 1280),
    Orientation.HORIZONTAL: (1280, 704),
}


def ensure_png_when_extensionless(filename: str) -> str:
    return filename if PurePath(filename).suffix else f"{filename}.png"


def type_prefix_for(instance_id: str, index: int) -> str:
    token = hashlib.sha256(f"{instance_id}:{index}".encode("utf-8")).hexdigest()[:10]
    return f"hdvideo-{token}-{index + 1:03d}"


def output_blob_path(videoid: str, type_prefix: str) -> str:
    return f"{VIDEO_BLOB_PATH_PREFIX}{videoid}/{type_prefix}-{videoid}.mp4"


def seed_for_event_key(event_key: str) -> int:
    digest = hashlib.sha256(event_key.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFF_FFFF


def serialize_ltx25_message(message: Ltx25Message) -> str:
    body = message.model_dump_json(exclude_none=True)
    body_size = len(body.encode("utf-8"))
    if body_size > SERVICE_BUS_BODY_BUDGET_BYTES:
        raise ValueError(
            f"Le message LTX 2.5 occupe {body_size} octets UTF-8 ; "
            f"le budget est {SERVICE_BUS_BODY_BUDGET_BYTES} octets "
            f"(limite Service Bus Basic : {SERVICE_BUS_MESSAGE_LIMIT_BYTES} octets)."
        )
    return body


def build_generation(
    request: CreateHDVideoInput,
    *,
    index: int,
    instance_id: str,
    event_key: str,
    dts_event_name: str,
) -> tuple[GenerationDescriptor, Ltx25Message]:
    prompt = request.prompts[index]
    width, height = ORIENTATION_DIMENSIONS[request.orientation]
    type_prefix = type_prefix_for(instance_id, index)
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
            )
            for filename_field, prompt_field, is_background in REFERENCE_FIELDS
            if getattr(request, filename_field) is not None
        ]
    else:
        for legacy_key, (filename_field, _, _) in zip(legacy_keys, REFERENCE_FIELDS):
            filename = getattr(request, filename_field)
            if filename is not None:
                legacy_pics[legacy_key] = ensure_png_when_extensionless(filename)

    message = Ltx25Message(
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
    serialize_ltx25_message(message)
    return descriptor, message


def decode_event_payload(payload: Any) -> Mapping[str, Any]:
    if isinstance(payload, Mapping):
        return payload
    if isinstance(payload, str):
        decoded = json.loads(payload)
        if isinstance(decoded, Mapping):
            return decoded
    raise ValueError("Le résultat LTX 2.5 doit être un objet JSON.")


def is_retryable_failure_event(payload: Any, expected_event_key: str) -> bool:
    try:
        decoded = decode_event_payload(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return (
        decoded.get("event_key") == expected_event_key
        and str(decoded.get("status", "")).strip().lower() == "failed"
        and decoded.get("retryable") is True
    )


def aggregate_generation_results(
    *,
    request: CreateHDVideoInput,
    descriptors: Sequence[GenerationDescriptor],
    event_payloads: Mapping[int, Any],
    timed_out_indexes: set[int],
) -> HDVideoWorkflowOutput:
    results: list[GenerationResult] = []

    for descriptor in descriptors:
        if descriptor.index in timed_out_indexes:
            results.append(
                GenerationResult(
                    index=descriptor.index,
                    prompt=descriptor.prompt,
                    status="timeout",
                    blob_path=descriptor.blob_path,
                    error="Délai maximal de génération dépassé.",
                )
            )
            continue

        raw_payload = event_payloads.get(descriptor.index)
        try:
            payload = decode_event_payload(raw_payload)
            if payload.get("event_key") != descriptor.event_key:
                raise ValueError("Clé de corrélation DTS inattendue.")

            status = str(payload.get("status", "")).strip().lower()
            if status == "completed":
                results.append(
                    GenerationResult(
                        index=descriptor.index,
                        prompt=descriptor.prompt,
                        status="completed",
                        blob_path=descriptor.blob_path,
                        num_frames=payload.get("num_frames"),
                    )
                )
            else:
                error = str(payload.get("error") or "La génération LTX 2.5 a échoué.")
                results.append(
                    GenerationResult(
                        index=descriptor.index,
                        prompt=descriptor.prompt,
                        status="failed",
                        blob_path=descriptor.blob_path,
                        error=error,
                    )
                )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            results.append(
                GenerationResult(
                    index=descriptor.index,
                    prompt=descriptor.prompt,
                    status="failed",
                    blob_path=descriptor.blob_path,
                    error=f"Résultat LTX 2.5 invalide : {exc}",
                )
            )

    return HDVideoWorkflowOutput(
        videoid=request.videoid,
        orientation=request.orientation,
        generations=results,
    )
