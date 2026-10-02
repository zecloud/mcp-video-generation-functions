from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, List

import azure.durable_functions as df
import azure.functions as func
from azure.core.exceptions import AzureError
from AzureFunctionsMCPPydanticTool import (
    pydantic_mcp_tool_properties,
    validate_pydantic_arguments,
)
from mcp.types import ContentBlock, ResourceLink, TextContent
from pydantic import BaseModel

from media_workflow import run_media_orchestration
from models import (
    BackgroundSpec,
    CompletedMusicVideoWorkflowResult,
    CompletedMusicWorkflowResult,
    CompletedWorkflowResult,
    CreateHDVideoInput,
    CreateMusicInput,
    CreateMusicVideoInput,
    FailedWorkflowResult,
    GenerationResult,
    GetHDVideoResultInput,
    GetMusicResultInput,
    GetMusicVideoResultInput,
    HDVideoWorkflowOutput,
    MusicMessage,
    MusicPlan,
    MusicVideoMessage,
    MusicVideoWorkflowOutput,
    MusicWorkflowOutput,
    TrackSpec,
    VideoMessage,
    NotFoundWorkflowResult,
    Orientation,
    RunningWorkflowResult,
)
from media_access import (
    DEFAULT_MEDIA_SAS_TTL_SECONDS,
    MAX_MEDIA_SAS_TTL_SECONDS,
    generate_media_sas_uris,
    upload_media_blob,
)
from music_workflow import (
    aggregate_music_results,
    build_music_generation,
    serialize_music_message,
)
from music_video_workflow import (
    MUSIC_VIDEO_GENERATION_INDEX,
    aggregate_music_video_results,
    build_music_video_generation,
    music_plan_blob_path,
    serialize_music_video_message,
)
from video_workflow import (
    aggregate_generation_results,
    build_generation,
    serialize_video_message,
)


app = df.DFApp(http_auth_level=func.AuthLevel.FUNCTION)

# azure-functions maps McpPropertyType.FLOAT to "float", which the MCP host
# copies verbatim into the tool JSON schema; clients (e.g. OpenAI) reject it.
# The valid JSON Schema / MCP toolProperties type is "number".
_MCP_PROPERTY_TYPE_FIXES = {"float": "number"}


def mcp_tool_properties(model: type[BaseModel]) -> Callable[[Any], Any]:
    pydantic_decorator = pydantic_mcp_tool_properties(app, model)

    def decorator(function: Any) -> Any:
        decorated = pydantic_decorator(function)
        target = getattr(decorated, "_function", decorated)
        target = getattr(target, "_func", target)
        for metadata in getattr(target, "__mcp_tool_properties__", {}).values():
            property_type = metadata.get("propertyType")
            if property_type in _MCP_PROPERTY_TYPE_FIXES:
                metadata["propertyType"] = _MCP_PROPERTY_TYPE_FIXES[property_type]
        return decorated

    return decorator


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
# Un clip musical demande ~29 min de GPU par minute de chanson : 4 h couvrent
# une chanson d'environ 8 minutes.
MUSIC_VIDEO_ORCHESTRATION_TIMEOUT_SECONDS = _positive_int_setting(
    "MUSIC_VIDEO_ORCHESTRATION_TIMEOUT_SECONDS", 4 * 60 * 60
)
VIDEO_BLOB_BASE_URL = os.environ.get(
    "VIDEO_BLOB_BASE_URL",
    "https://storage.example.invalid/video",
).rstrip("/")
VIDEO_SAS_TTL_SECONDS = _positive_int_setting(
    "VIDEO_SAS_TTL_SECONDS",
    DEFAULT_MEDIA_SAS_TTL_SECONDS,
)
if VIDEO_SAS_TTL_SECONDS > MAX_MEDIA_SAS_TTL_SECONDS:
    raise ValueError(
        f"VIDEO_SAS_TTL_SECONDS ne doit pas dépasser {MAX_MEDIA_SAS_TTL_SECONDS}."
    )

MediaUploader = Callable[..., Awaitable[str]]


async def _upload_music_plan(
    request: CreateMusicVideoInput,
    *,
    uploader: MediaUploader | None = None,
) -> str:
    """Upload le plan pré-calculé et rend le nom simple attendu par le worker."""

    if request.music_plan is None:
        raise ValueError("Aucun music_plan à uploader.")
    blob_path = music_plan_blob_path(request)
    await (uploader or upload_media_blob)(
        blob_path,
        request.music_plan.serialize(),
        base_url=VIDEO_BLOB_BASE_URL,
        content_type="application/json",
    )
    logging.info("Uploaded music plan %s", blob_path)
    return request.music_plan_blob_name()


SasUriProvider = Callable[..., Awaitable[dict[str, str]]]


def _describe_video(generation: GenerationResult) -> str:
    frame_description = (
        f", {generation.num_frames} images"
        if generation.num_frames is not None
        else ""
    )
    return (
        f"Vidéo HD générée pour le prompt {generation.index + 1}"
        f"{frame_description}."
    )


def _describe_music(generation: GenerationResult) -> str:
    return (
        f"Morceau généré pour la piste {generation.index + 1} "
        f"({generation.prompt})."
    )


@dataclass(frozen=True)
class ExtraResourceLink:
    blob_path: str
    description: str
    mime_type: str


@dataclass(frozen=True)
class MediaProfile:
    output_model: type[BaseModel]
    completed_model: type[BaseModel]
    result_tool: str
    mime_type: str
    describe: Callable[[GenerationResult], str]
    extra_links: Callable[[BaseModel], list[ExtraResourceLink]] | None = None


def _describe_music_video(generation: GenerationResult) -> str:
    frame_description = (
        f", {generation.num_frames} images"
        if generation.num_frames is not None
        else ""
    )
    return f"{generation.prompt}{frame_description}."


def _music_video_artifact_links(output: BaseModel) -> list[ExtraResourceLink]:
    if not isinstance(output, MusicVideoWorkflowOutput) or not any(
        generation.status == "completed" for generation in output.generations
    ):
        return []
    artifacts = output.artifacts
    return [
        ExtraResourceLink(
            blob_path=artifacts.music_plan_path,
            description=(
                "Plan musical (scènes calées sur le rythme et prompts), "
                "réutilisable avec reuse_music_plan ou music_plan."
            ),
            mime_type="application/json",
        ),
        ExtraResourceLink(
            blob_path=artifacts.scenes_srt_path,
            description="Sous-titres des paroles corrigées, scène par scène.",
            mime_type="application/x-subrip",
        ),
        ExtraResourceLink(
            blob_path=artifacts.prompts_srt_path,
            description="Prompts visuels de chaque scène au format SRT.",
            mime_type="application/x-subrip",
        ),
    ]


VIDEO_PROFILE = MediaProfile(
    output_model=HDVideoWorkflowOutput,
    completed_model=CompletedWorkflowResult,
    result_tool="get_hd_video_result",
    mime_type="video/mp4",
    describe=_describe_video,
)
MUSIC_PROFILE = MediaProfile(
    output_model=MusicWorkflowOutput,
    completed_model=CompletedMusicWorkflowResult,
    result_tool="get_music_result",
    mime_type="audio/flac",
    describe=_describe_music,
)
MUSIC_VIDEO_PROFILE = MediaProfile(
    output_model=MusicVideoWorkflowOutput,
    completed_model=CompletedMusicVideoWorkflowResult,
    result_tool="get_music_video_result",
    mime_type="video/mp4",
    describe=_describe_music_video,
    extra_links=_music_video_artifact_links,
)
_PROFILE_BY_COMPLETED_MODEL = {
    profile.completed_model: profile
    for profile in (VIDEO_PROFILE, MUSIC_PROFILE, MUSIC_VIDEO_PROFILE)
}


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
    instance_id = context.instance_id

    def build_dispatch():
        descriptors = []
        payloads = []
        for index in range(len(request.prompts)):
            event_key = f"{instance_id}:{index}:{context.new_uuid()}"
            dts_event_name = f"video-hd-{index}-{context.new_uuid()}"
            descriptor, message = build_generation(
                request,
                index=index,
                instance_id=instance_id,
                event_key=event_key,
                dts_event_name=dts_event_name,
            )
            descriptors.append(descriptor)
            payloads.append(
                {
                    "index": index,
                    "message": message.model_dump(mode="json", exclude_none=True),
                }
            )
        return descriptors, payloads

    descriptors, event_payloads, timed_out_indexes = yield from run_media_orchestration(
        context,
        timeout_seconds=int(orchestration_input["timeout_seconds"]),
        build_dispatch=build_dispatch,
        activity_name="enqueue_video_generation",
    )

    result = aggregate_generation_results(
        request=request,
        descriptors=descriptors,
        event_payloads=event_payloads,
        timed_out_indexes=timed_out_indexes,
    )
    if timed_out_indexes:
        context.continue_as_new({"terminal_result": result.model_dump(mode="json")})
        return None
    return result.model_dump(mode="json")


@app.orchestration_trigger(context_name="context")
def run_music_orchestrator(context: df.DurableOrchestrationContext):
    orchestration_input = context.get_input()
    if not isinstance(orchestration_input, dict):
        raise ValueError("L’entrée d’orchestration doit être un objet JSON.")
    terminal_result = orchestration_input.get("terminal_result")
    if terminal_result is not None:
        return MusicWorkflowOutput.model_validate(terminal_result).model_dump(
            mode="json"
        )

    request = CreateMusicInput.model_validate(orchestration_input["request"])
    instance_id = context.instance_id

    def build_dispatch():
        descriptors = []
        payloads = []
        for index in range(len(request.tracks)):
            event_key = f"{instance_id}:{index}:{context.new_uuid()}"
            dts_event_name = f"music-{index}-{context.new_uuid()}"
            descriptor, message = build_music_generation(
                request,
                index=index,
                instance_id=instance_id,
                event_key=event_key,
                dts_event_name=dts_event_name,
            )
            descriptors.append(descriptor)
            payloads.append(
                {
                    "index": index,
                    "message": message.model_dump(mode="json", exclude_none=True),
                }
            )
        return descriptors, payloads

    descriptors, event_payloads, timed_out_indexes = yield from run_media_orchestration(
        context,
        timeout_seconds=int(orchestration_input["timeout_seconds"]),
        build_dispatch=build_dispatch,
        activity_name="enqueue_music_generation",
    )

    result = aggregate_music_results(
        request=request,
        descriptors=descriptors,
        event_payloads=event_payloads,
        timed_out_indexes=timed_out_indexes,
    )
    if timed_out_indexes:
        context.continue_as_new({"terminal_result": result.model_dump(mode="json")})
        return None
    return result.model_dump(mode="json")


@app.orchestration_trigger(context_name="context")
def run_music_video_orchestrator(context: df.DurableOrchestrationContext):
    orchestration_input = context.get_input()
    if not isinstance(orchestration_input, dict):
        raise ValueError("L’entrée d’orchestration doit être un objet JSON.")
    terminal_result = orchestration_input.get("terminal_result")
    if terminal_result is not None:
        return MusicVideoWorkflowOutput.model_validate(terminal_result).model_dump(
            mode="json"
        )

    request = CreateMusicVideoInput.model_validate(orchestration_input["request"])
    music_plan_blob = orchestration_input.get("music_plan_blob")
    instance_id = context.instance_id

    def build_dispatch():
        index = MUSIC_VIDEO_GENERATION_INDEX
        event_key = f"{instance_id}:{index}:{context.new_uuid()}"
        dts_event_name = f"music-video-{index}-{context.new_uuid()}"
        descriptor, message = build_music_video_generation(
            request,
            instance_id=instance_id,
            event_key=event_key,
            dts_event_name=dts_event_name,
            music_plan_blob=music_plan_blob,
        )
        return [descriptor], [
            {
                "index": index,
                "message": message.model_dump(mode="json", exclude_none=True),
            }
        ]

    descriptors, event_payloads, timed_out_indexes = yield from run_media_orchestration(
        context,
        timeout_seconds=int(orchestration_input["timeout_seconds"]),
        build_dispatch=build_dispatch,
        activity_name="enqueue_music_video_generation",
    )

    result = aggregate_music_video_results(
        request=request,
        descriptors=descriptors,
        event_payloads=event_payloads,
        timed_out_indexes=timed_out_indexes,
    )
    if timed_out_indexes:
        context.continue_as_new({"terminal_result": result.model_dump(mode="json")})
        return None
    return result.model_dump(mode="json")


@app.activity_trigger(input_name="job")
@app.service_bus_queue_output(
    arg_name="message",
    queue_name="%VIDEO_SERVICE_BUS_QUEUE_NAME%",
    connection="ServiceBusConnection",
)
def enqueue_video_generation(job: dict, message: func.Out[str]) -> dict:
    body = VideoMessage.model_validate(job["message"])
    message.set(serialize_video_message(body))
    return {
        "index": job["index"],
        "event_key": body.event_key,
        "dts_event_name": body.dts_event_name,
    }


@app.activity_trigger(input_name="job")
@app.service_bus_queue_output(
    arg_name="message",
    queue_name="%MUSIC_SERVICE_BUS_QUEUE_NAME%",
    connection="ServiceBusConnection",
)
def enqueue_music_generation(job: dict, message: func.Out[str]) -> dict:
    body = MusicMessage.model_validate(job["message"])
    message.set(serialize_music_message(body))
    return {
        "index": job["index"],
        "event_key": body.event_key,
        "dts_event_name": body.dts_event_name,
    }


@app.activity_trigger(input_name="job")
@app.service_bus_queue_output(
    arg_name="message",
    queue_name="%VIDEO_SERVICE_BUS_QUEUE_NAME%",
    connection="ServiceBusConnection",
)
def enqueue_music_video_generation(job: dict, message: func.Out[str]) -> dict:
    body = MusicVideoMessage.model_validate(job["message"])
    message.set(serialize_music_video_message(body))
    return {
        "index": job["index"],
        "event_key": body.event_key,
        "dts_event_name": body.dts_event_name,
    }


@app.mcp_tool()
@mcp_tool_properties(CreateHDVideoInput)
@app.durable_client_input(client_name="client")
@validate_pydantic_arguments(CreateHDVideoInput, strict=True)
async def create_hd_video(
    client: df.DurableOrchestrationClient,
    videoid: str,
    ref_speaker1_filename: str,
    ref_speaker2_filename: str,
    prompts: List[str],
    orientation: Orientation = Orientation.VERTICAL,
    ref_speaker1_prompt: str | None = None,
    ref_speaker2_prompt: str | None = None,
    ref_speaker3_filename: str | None = None,
    ref_speaker3_prompt: str | None = None,
    ref_speaker4_filename: str | None = None,
    ref_speaker4_prompt: str | None = None,
    background_filename: str | None = None,
    background_prompt: str | None = None,
) -> List[ContentBlock]:
    """Démarre les générations vidéo HD en parallèle et retourne le résultat ou un workflow_id."""
    request = CreateHDVideoInput(
        videoid=videoid,
        ref_speaker1_filename=ref_speaker1_filename,
        ref_speaker2_filename=ref_speaker2_filename,
        ref_speaker3_filename=ref_speaker3_filename,
        ref_speaker4_filename=ref_speaker4_filename,
        background_filename=background_filename,
        prompts=prompts,
        orientation=orientation,
        ref_speaker1_prompt=ref_speaker1_prompt,
        ref_speaker2_prompt=ref_speaker2_prompt,
        ref_speaker3_prompt=ref_speaker3_prompt,
        ref_speaker4_prompt=ref_speaker4_prompt,
        background_prompt=background_prompt,
    )
    instance_id = await client.start_new(
        "run_hd_video_orchestrator",
        client_input={
            "request": request.model_dump(mode="json", exclude_none=True),
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
@mcp_tool_properties(GetHDVideoResultInput)
@app.durable_client_input(client_name="client")
@validate_pydantic_arguments(GetHDVideoResultInput, strict=True)
async def get_hd_video_result(
    client: df.DurableOrchestrationClient,
    workflow_id: str,
) -> List[ContentBlock]:
    """Retourne l'état et le résultat d'un workflow create_hd_video."""
    status = await client.get_status(workflow_id)
    return await _to_content_blocks(_workflow_response(status, workflow_id))


@app.mcp_tool()
@mcp_tool_properties(CreateMusicInput)
@app.durable_client_input(client_name="client")
@validate_pydantic_arguments(CreateMusicInput, strict=True)
async def create_music(
    client: df.DurableOrchestrationClient,
    videoid: str,
    tracks: List[TrackSpec],
) -> List[ContentBlock]:
    """Démarre les générations musicales en parallèle et retourne le résultat ou un workflow_id."""
    request = CreateMusicInput(videoid=videoid, tracks=tracks)
    instance_id = await client.start_new(
        "run_music_orchestrator",
        client_input={
            "request": request.model_dump(mode="json", exclude_none=True),
            "timeout_seconds": ORCHESTRATION_TIMEOUT_SECONDS,
        },
    )
    logging.info("Started music orchestration %s", instance_id)
    result = await _wait_for_workflow(
        client,
        instance_id,
        wait_budget_seconds=MCP_WAIT_BUDGET_SECONDS,
        poll_interval_seconds=MCP_POLL_INTERVAL_SECONDS,
        profile=MUSIC_PROFILE,
    )
    return await _to_content_blocks(result)


@app.mcp_tool()
@mcp_tool_properties(GetMusicResultInput)
@app.durable_client_input(client_name="client")
@validate_pydantic_arguments(GetMusicResultInput, strict=True)
async def get_music_result(
    client: df.DurableOrchestrationClient,
    workflow_id: str,
) -> List[ContentBlock]:
    """Retourne l'état et le résultat d'un workflow create_music."""
    status = await client.get_status(workflow_id)
    return await _to_content_blocks(
        _workflow_response(status, workflow_id, profile=MUSIC_PROFILE)
    )


@app.mcp_tool()
@mcp_tool_properties(CreateMusicVideoInput)
@app.durable_client_input(client_name="client")
@validate_pydantic_arguments(CreateMusicVideoInput, strict=True)
async def create_music_video(
    client: df.DurableOrchestrationClient,
    videoid: str,
    music_track: str,
    ref_speaker1_filename: str,
    ref_speaker1_prompt: str,
    ref_speaker2_filename: str | None = None,
    ref_speaker2_prompt: str | None = None,
    ref_speaker3_filename: str | None = None,
    ref_speaker3_prompt: str | None = None,
    ref_speaker4_filename: str | None = None,
    ref_speaker4_prompt: str | None = None,
    background_filename: str | None = None,
    background_prompt: str | None = None,
    backgrounds: List[BackgroundSpec] | None = None,
    orientation: Orientation = Orientation.VERTICAL,
    lyrics: str | None = None,
    theme_style: str | None = None,
    story: str | None = None,
    scene_min_seconds: float | None = None,
    scene_max_seconds: float | None = None,
    scene_bias: float | None = None,
    whisper_language: str | None = None,
    music_plan: MusicPlan | None = None,
    reuse_music_plan: bool = False,
) -> List[ContentBlock]:
    """Démarre le clip musical d'une chanson create_music (music_track = son type_prefix) et retourne le résultat ou un workflow_id. Compter ~29 min de calcul GPU par minute de chanson."""
    request = CreateMusicVideoInput(
        videoid=videoid,
        music_track=music_track,
        ref_speaker1_filename=ref_speaker1_filename,
        ref_speaker1_prompt=ref_speaker1_prompt,
        ref_speaker2_filename=ref_speaker2_filename,
        ref_speaker2_prompt=ref_speaker2_prompt,
        ref_speaker3_filename=ref_speaker3_filename,
        ref_speaker3_prompt=ref_speaker3_prompt,
        ref_speaker4_filename=ref_speaker4_filename,
        ref_speaker4_prompt=ref_speaker4_prompt,
        background_filename=background_filename,
        background_prompt=background_prompt,
        backgrounds=backgrounds,
        orientation=orientation,
        lyrics=lyrics,
        theme_style=theme_style,
        story=story,
        scene_min_seconds=scene_min_seconds,
        scene_max_seconds=scene_max_seconds,
        scene_bias=scene_bias,
        whisper_language=whisper_language,
        music_plan=music_plan,
        reuse_music_plan=reuse_music_plan,
    )
    client_input: dict[str, Any] = {
        "request": request.model_dump(
            mode="json", exclude_none=True, exclude={"music_plan"}
        ),
        "timeout_seconds": MUSIC_VIDEO_ORCHESTRATION_TIMEOUT_SECONDS,
    }
    if request.music_plan is not None:
        # Le plan part en blob avant l'orchestration : DTS et Service Bus ne
        # transportent que son nom simple.
        client_input["music_plan_blob"] = await _upload_music_plan(request)
    instance_id = await client.start_new(
        "run_music_video_orchestrator",
        client_input=client_input,
    )
    logging.info("Started music video orchestration %s", instance_id)
    result = await _wait_for_workflow(
        client,
        instance_id,
        wait_budget_seconds=MCP_WAIT_BUDGET_SECONDS,
        poll_interval_seconds=MCP_POLL_INTERVAL_SECONDS,
        profile=MUSIC_VIDEO_PROFILE,
    )
    return await _to_content_blocks(result)


@app.mcp_tool()
@mcp_tool_properties(GetMusicVideoResultInput)
@app.durable_client_input(client_name="client")
@validate_pydantic_arguments(GetMusicVideoResultInput, strict=True)
async def get_music_video_result(
    client: df.DurableOrchestrationClient,
    workflow_id: str,
) -> List[ContentBlock]:
    """Retourne l'état et le résultat d'un workflow create_music_video."""
    status = await client.get_status(workflow_id)
    return await _to_content_blocks(
        _workflow_response(status, workflow_id, profile=MUSIC_VIDEO_PROFILE)
    )


async def _wait_for_workflow(
    client: df.DurableOrchestrationClient,
    workflow_id: str,
    *,
    wait_budget_seconds: int,
    poll_interval_seconds: int,
    profile: MediaProfile = VIDEO_PROFILE,
) -> BaseModel:
    deadline = time.monotonic() + wait_budget_seconds
    while time.monotonic() < deadline:
        status = await client.get_status(workflow_id)
        if status is not None and _is_terminal(status.runtime_status):
            return _workflow_response(status, workflow_id, profile=profile)
        await asyncio.sleep(poll_interval_seconds)
    return _running_result(workflow_id, profile=profile)


def _workflow_response(
    status: Any,
    workflow_id: str,
    *,
    profile: MediaProfile = VIDEO_PROFILE,
) -> BaseModel:
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
            workflow_output = profile.output_model.model_validate(output)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return FailedWorkflowResult(
                workflow_id=instance_id,
                error=f"Résultat d’orchestration invalide : {exc}",
            )
        return profile.completed_model(
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
    return _running_result(instance_id, profile=profile)


def _running_result(
    workflow_id: str,
    *,
    profile: MediaProfile = VIDEO_PROFILE,
) -> RunningWorkflowResult:
    return RunningWorkflowResult(
        workflow_id=workflow_id,
        poll_after_seconds=MCP_POLL_INTERVAL_SECONDS,
        next=(
            f'Appelez {profile.result_tool} avec workflow_id "{workflow_id}" '
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
    profile = _PROFILE_BY_COMPLETED_MODEL.get(type(result))
    if profile is None:
        return blocks

    completed_generations = [
        generation
        for generation in result.result.generations
        if generation.status == "completed"
    ]
    extra_links = (
        profile.extra_links(result.result) if profile.extra_links is not None else []
    )
    provider = sas_uri_provider or generate_media_sas_uris
    try:
        sas_uris = await provider(
            [generation.blob_path for generation in completed_generations]
            + [link.blob_path for link in extra_links],
            base_url=VIDEO_BLOB_BASE_URL,
            ttl_seconds=VIDEO_SAS_TTL_SECONDS,
        )
    except AzureError:
        logging.exception(
            "Échec de génération des SAS média; retour uniquement du statut."
        )
        return blocks

    for generation in completed_generations:
        filename = generation.blob_path.rsplit("/", 1)[-1]
        blocks.append(
            ResourceLink(
                type="resource_link",
                uri=sas_uris[generation.blob_path],
                name=filename,
                description=profile.describe(generation),
                mimeType=profile.mime_type,
            )
        )
    for link in extra_links:
        blocks.append(
            ResourceLink(
                type="resource_link",
                uri=sas_uris[link.blob_path],
                name=link.blob_path.rsplit("/", 1)[-1],
                description=link.description,
                mimeType=link.mime_type,
            )
        )
    return blocks
