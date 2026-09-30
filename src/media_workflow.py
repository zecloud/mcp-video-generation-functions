"""Noyau partagé par les pipelines de génération vidéo et musique.

Les deux pipelines suivent le même principe : un message est déposé sur une
queue Azure Service Bus, puis le worker signale la fin de la génération via un
événement externe Durable Task corrélé par ``event_key``.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import timedelta
from typing import Any, Callable, Mapping, Sequence

from pydantic import BaseModel

from models import (
    GenerationDescriptor,
    GenerationResult,
    SERVICE_BUS_BODY_BUDGET_BYTES,
    SERVICE_BUS_MESSAGE_LIMIT_BYTES,
)

MEDIA_BLOB_PATH_PREFIX = (
    os.environ.get("VIDEO_BLOB_PATH_PREFIX", "video/").strip("/") + "/"
)


def type_prefix_for(instance_id: str, index: int, *, kind: str = "hdvideo") -> str:
    token = hashlib.sha256(f"{instance_id}:{index}".encode("utf-8")).hexdigest()[:10]
    return f"{kind}-{token}-{index + 1:03d}"


def output_blob_path(videoid: str, type_prefix: str, *, extension: str) -> str:
    return f"{MEDIA_BLOB_PATH_PREFIX}{videoid}/{type_prefix}-{videoid}.{extension}"


def seed_for_event_key(event_key: str) -> int:
    digest = hashlib.sha256(event_key.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFF_FFFF


def serialize_media_message(message: BaseModel) -> str:
    body = message.model_dump_json(exclude_none=True)
    body_size = len(body.encode("utf-8"))
    if body_size > SERVICE_BUS_BODY_BUDGET_BYTES:
        raise ValueError(
            f"Le message occupe {body_size} octets UTF-8 ; "
            f"le budget est {SERVICE_BUS_BODY_BUDGET_BYTES} octets "
            f"(limite Service Bus Basic : {SERVICE_BUS_MESSAGE_LIMIT_BYTES} octets)."
        )
    return body


def decode_event_payload(payload: Any) -> Mapping[str, Any]:
    if isinstance(payload, Mapping):
        return payload
    if isinstance(payload, str):
        decoded = json.loads(payload)
        if isinstance(decoded, Mapping):
            return decoded
    raise ValueError("Le résultat de génération doit être un objet JSON.")


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


def aggregate_results(
    *,
    descriptors: Sequence[GenerationDescriptor],
    event_payloads: Mapping[int, Any],
    timed_out_indexes: set[int],
) -> list[GenerationResult]:
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
                error = str(payload.get("error") or "La génération a échoué.")
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
                    error=f"Résultat de génération invalide : {exc}",
                )
            )

    return results


DispatchBuilder = Callable[
    [], tuple[Sequence[GenerationDescriptor], Sequence[dict[str, Any]]]
]


def run_media_orchestration(
    context,
    *,
    timeout_seconds: int,
    build_dispatch: DispatchBuilder,
    activity_name: str,
):
    """Dépose les messages, attend les événements worker et gère le délai.

    Générateur Durable destiné à être délégué avec ``yield from``. Retourne le
    triplet ``(descriptors, event_payloads, timed_out_indexes)``.
    """
    deadline = context.current_utc_datetime + timedelta(seconds=timeout_seconds)
    timeout_task = context.create_timer(deadline)

    descriptors, dispatch_payloads = build_dispatch()
    dispatch_tasks = [
        context.call_activity(activity_name, payload)
        for payload in dispatch_payloads
    ]

    event_payloads: dict[int, Any] = {}
    pending_dispatches = list(enumerate(dispatch_tasks))
    while pending_dispatches:
        winner = yield context.task_any(
            [timeout_task, *[task for _, task in pending_dispatches]]
        )
        if winner == timeout_task:
            return (
                descriptors,
                event_payloads,
                {
                    descriptor.index
                    for descriptor in descriptors
                    if descriptor.index not in event_payloads
                },
            )

        for position, (index, dispatch_task) in enumerate(pending_dispatches):
            if winner == dispatch_task:
                state_name = getattr(getattr(winner, "state", None), "name", None)
                if state_name == "FAILED" or isinstance(winner.result, Exception):
                    error = winner.result
                    event_payloads[index] = {
                        "status": "failed",
                        "event_key": descriptors[index].event_key,
                        "error": f"Envoi Service Bus impossible : {error}",
                    }
                pending_dispatches.pop(position)
                break
        else:
            raise RuntimeError("Durable task_any a retourné une activité inconnue.")

    pending_events = [
        (
            descriptor.index,
            descriptor.dts_event_name,
            context.wait_for_external_event(descriptor.dts_event_name),
        )
        for descriptor in descriptors
        if descriptor.index not in event_payloads
    ]

    while pending_events:
        winner = yield context.task_any(
            [timeout_task, *[task for _, _, task in pending_events]]
        )
        if winner == timeout_task:
            break

        for position, (index, event_name, event_task) in enumerate(pending_events):
            if winner == event_task:
                event_payloads[index] = event_task.result
                if is_retryable_failure_event(
                    event_task.result,
                    descriptors[index].event_key,
                ):
                    pending_events[position] = (
                        index,
                        event_name,
                        context.wait_for_external_event(event_name),
                    )
                else:
                    pending_events.pop(position)
                break
        else:
            raise RuntimeError("Durable task_any a retourné une tâche inconnue.")

    if not pending_events:
        timeout_task.cancel()

    return descriptors, event_payloads, {index for index, _, _ in pending_events}
