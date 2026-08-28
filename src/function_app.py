from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import timedelta
from typing import Any, Awaitable, Callable, List

import azure.durable_functions as df
import azure.functions as func
from AzureFunctionsMCPPydanticTool import (
    pydantic_mcp_tool_properties,
    validate_pydantic_arguments,
)
from mcp.types import ContentBlock, ResourceLink, TextContent
from pydantic import BaseModel

from models import (
    CompletedWorkflowResult,
    CreateHDVideoInput,
    FailedWorkflowResult,
    GetHDVideoResultInput,
    HDVideoWorkflowOutput,
    Ltx25Message,
    NotFoundWorkflowResult,
    Orientation,
    RunningWorkflowResult,
)
from video_access import (
    DEFAULT_VIDEO_SAS_TTL_SECONDS,
    MAX_VIDEO_SAS_TTL_SECONDS,
    generate_video_sas_uris,
)
from video_workflow import (
    aggregate_generation_results,
    build_generation,
    is_retryable_failure_event,
    serialize_ltx25_message,
)


app = df.DFApp(http_auth_level=func.AuthLevel.FUNCTION)


def _positive_int_setting(name: str, default: int) -> int:
    raw_value = os.environ.get(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} doit être un entier positif.") from exc
    if value <= 0:
        raise ValueError(f"{name} doit être un entier positif.")
    return value


MCP_WAIT_BUDGET_SECONDS = _positive_int_setting("MCP_WAIT_BUDGET_SECONDS", 20)
MCP_POLL_INTERVAL_SECONDS = _positive_int_setting("MCP_POLL_INTERVAL_SECONDS", 5)
ORCHESTRATION_TIMEOUT_SECONDS = _positive_int_setting(
    "ORCHESTRATION_TIMEOUT_SECONDS", 2 * 60 * 60
)
VIDEO_BLOB_BASE_URL = os.environ.get(
    "VIDEO_BLOB_BASE_URL",
    "https://fluxstorageaca.blob.core.windows.net/ltxavatarjob/agentvideo",
).rstrip("/")
VIDEO_SAS_TTL_SECONDS = _positive_int_setting(
    "VIDEO_SAS_TTL_SECONDS",
    DEFAULT_VIDEO_SAS_TTL_SECONDS,
)
if VIDEO_SAS_TTL_SECONDS > MAX_VIDEO_SAS_TTL_SECONDS:
    raise ValueError("VIDEO_SAS_TTL_SECONDS ne doit pas dépasser 86400.")

SasUriProvider = Callable[..., Awaitable[dict[str, str]]]


@app.orchestration_trigger(context_name="context")
def run_hd_video_orchestrator(context: df.DurableOrchestrationContext):
    orchestration_input = context.get_input()
    if not isinstance(orchestration_input, dict):
        raise ValueError("L’entrée d’orchestration doit être un objet JSON.")
    terminal_result = orchestration_input.get("terminal_result")
    if terminal_result is not None:
        return HDVideoWorkflowOutput.model_validate(terminal_result).model_dump(
            mode="json"
        )

    request = CreateHDVideoInput.model_validate(orchestration_input["request"])
    timeout_seconds = int(orchestration_input["timeout_seconds"])
    instance_id = context.instance_id
    deadline = context.current_utc_datetime + timedelta(seconds=timeout_seconds)
    timeout_task = context.create_timer(deadline)

    descriptors = []
    dispatch_tasks = []
    for index in range(len(request.prompts)):
        event_key = f"{instance_id}:{index}:{context.new_uuid()}"
        dts_event_name = f"ltx25-hd-{index}-{context.new_uuid()}"
        descriptor, message = build_generation(
            request,
            index=index,
            instance_id=instance_id,
            event_key=event_key,
            dts_event_name=dts_event_name,
        )
        descriptors.append(descriptor)
        dispatch_tasks.append(
            context.call_activity(
                "enqueue_ltx25_generation",
                {
                    "index": index,
                    "message": message.model_dump(mode="json"),
                },
            )
        )

    event_payloads: dict[int, Any] = {}
    pending_dispatches = list(enumerate(dispatch_tasks))
    while pending_dispatches:
        winner = yield context.task_any(
            [timeout_task, *[task for _, task in pending_dispatches]]
        )
        if winner == timeout_task:
            result = aggregate_generation_results(
                request=request,
                descriptors=descriptors,
                event_payloads=event_payloads,
                timed_out_indexes={
                    descriptor.index
                    for descriptor in descriptors
                    if descriptor.index not in event_payloads
                },
            )
            context.continue_as_new(
                {"terminal_result": result.model_dump(mode="json")}
            )
            return None

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

    timed_out_indexes = {index for index, _, _ in pending_events}
    result = aggregate_generation_results(
        request=request,
        descriptors=descriptors,
        event_payloads=event_payloads,
        timed_out_indexes=timed_out_indexes,
    )
    if timed_out_indexes:
        context.continue_as_new(
            {"terminal_result": result.model_dump(mode="json")}
        )
        return None
    return result.model_dump(mode="json")


@app.activity_trigger(input_name="job")
@app.service_bus_queue_output(
    arg_name="message",
    queue_name="%SERVICE_BUS_QUEUE_NAME%",
    connection="ServiceBusConnection",
)
def enqueue_ltx25_generation(job: dict, message: func.Out[str]) -> dict:
    body = Ltx25Message.model_validate(job["message"])
    message.set(serialize_ltx25_message(body))
    return {
        "index": job["index"],
        "event_key": body.event_key,
        "dts_event_name": body.dts_event_name,
    }


@app.mcp_tool()
@pydantic_mcp_tool_properties(app, CreateHDVideoInput)
@app.durable_client_input(client_name="client")
@validate_pydantic_arguments(CreateHDVideoInput, strict=True)
async def create_hd_video(
    client: df.DurableOrchestrationClient,
    videoid: str,
    ref_speaker1_filename: str,
    ref_speaker2_filename: str,
    prompts: List[str],
    orientation: Orientation = Orientation.VERTICAL,
) -> List[ContentBlock]:
    """Démarre les générations vidéo HD en parallèle et retourne le résultat ou un workflow_id."""
    request = CreateHDVideoInput(
        videoid=videoid,
        ref_speaker1_filename=ref_speaker1_filename,
        ref_speaker2_filename=ref_speaker2_filename,
        prompts=prompts,
        orientation=orientation,
    )
    instance_id = await client.start_new(
        "run_hd_video_orchestrator",
        client_input={
            "request": request.model_dump(mode="json"),
            "timeout_seconds": ORCHESTRATION_TIMEOUT_SECONDS,
        },
    )
    logging.info("Started HD video orchestration %s", instance_id)
    result = await _wait_for_workflow(
        client,
        instance_id,
        wait_budget_seconds=MCP_WAIT_BUDGET_SECONDS,
        poll_interval_seconds=MCP_POLL_INTERVAL_SECONDS,
    )
    return await _to_content_blocks(result)


@app.mcp_tool()
@pydantic_mcp_tool_properties(app, GetHDVideoResultInput)
@app.durable_client_input(client_name="client")
@validate_pydantic_arguments(GetHDVideoResultInput, strict=True)
async def get_hd_video_result(
    client: df.DurableOrchestrationClient,
    workflow_id: str,
) -> List[ContentBlock]:
    """Retourne l'état et le résultat d'un workflow create_hd_video."""
    status = await client.get_status(workflow_id)
    return await _to_content_blocks(_workflow_response(status, workflow_id))


async def _wait_for_workflow(
    client: df.DurableOrchestrationClient,
    workflow_id: str,
    *,
    wait_budget_seconds: int,
    poll_interval_seconds: int,
) -> BaseModel:
    deadline = time.monotonic() + wait_budget_seconds
    while time.monotonic() < deadline:
        status = await client.get_status(workflow_id)
        if status is not None and _is_terminal(status.runtime_status):
            return _workflow_response(status, workflow_id)
        await asyncio.sleep(poll_interval_seconds)
    return _running_result(workflow_id)


def _workflow_response(status: Any, workflow_id: str) -> BaseModel:
    if status is None or status.runtime_status is None:
        return NotFoundWorkflowResult(
            workflow_id=workflow_id,
            error=f'Aucun workflow trouvé avec l’identifiant "{workflow_id}".',
        )

    runtime_name = getattr(status.runtime_status, "name", str(status.runtime_status))
    instance_id = status.instance_id or workflow_id
    if runtime_name == "Completed":
        output = status.output
        try:
            if isinstance(output, str):
                output = json.loads(output)
            workflow_output = HDVideoWorkflowOutput.model_validate(output)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return FailedWorkflowResult(
                workflow_id=instance_id,
                error=f"Résultat d’orchestration invalide : {exc}",
            )
        return CompletedWorkflowResult(
            workflow_id=instance_id,
            result=workflow_output,
        )
    if runtime_name in {"Failed", "Terminated", "Canceled"}:
        detail = status.output
        error = (
            detail
            if isinstance(detail, str) and detail.strip()
            else json.dumps(detail, ensure_ascii=False)
            if detail is not None
            else "L’orchestration Durable a échoué."
        )
        return FailedWorkflowResult(workflow_id=instance_id, error=error)
    return _running_result(instance_id)


def _running_result(workflow_id: str) -> RunningWorkflowResult:
    return RunningWorkflowResult(
        workflow_id=workflow_id,
        poll_after_seconds=MCP_POLL_INTERVAL_SECONDS,
        next=(
            f'Appelez get_hd_video_result avec workflow_id "{workflow_id}" '
            f"dans environ {MCP_POLL_INTERVAL_SECONDS} secondes."
        ),
    )


def _is_terminal(runtime_status: Any) -> bool:
    runtime_name = getattr(runtime_status, "name", None)
    return runtime_name in {"Completed", "Failed", "Terminated", "Canceled"}


def _serialize(result: BaseModel) -> str:
    return result.model_dump_json(exclude_none=True)


async def _to_content_blocks(
    result: BaseModel,
    *,
    sas_uri_provider: SasUriProvider | None = None,
) -> List[ContentBlock]:
    blocks: List[ContentBlock] = [
        TextContent(type="text", text=_serialize(result))
    ]
    if not isinstance(result, CompletedWorkflowResult):
        return blocks

    completed_generations = [
        generation
        for generation in result.result.generations
        if generation.status == "completed"
    ]
    provider = sas_uri_provider or generate_video_sas_uris
    sas_uris = await provider(
        [generation.blob_path for generation in completed_generations],
        base_url=VIDEO_BLOB_BASE_URL,
        ttl_seconds=VIDEO_SAS_TTL_SECONDS,
    )

    for generation in completed_generations:
        filename = generation.blob_path.rsplit("/", 1)[-1]
        frame_description = (
            f", {generation.num_frames} images"
            if generation.num_frames is not None
            else ""
        )
        blocks.append(
            ResourceLink(
                type="resource_link",
                uri=sas_uris[generation.blob_path],
                name=filename,
                description=(
                    f"Vidéo HD générée pour le prompt {generation.index + 1}"
                    f"{frame_description}."
                ),
                mimeType="video/mp4",
            )
        )
    return blocks
