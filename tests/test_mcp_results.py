import asyncio
import json
from dataclasses import dataclass

from azure.functions import mcp as functions_mcp
from function_app import (
    _to_content_blocks,
    _wait_for_workflow,
    _workflow_response,
)
from mcp.types import ResourceLink, TextContent


@dataclass
class RuntimeStatus:
    name: str


@dataclass
class DurableStatus:
    runtime_status: RuntimeStatus | None
    instance_id: str
    output: object = None


class FakeClient:
    def __init__(self, statuses):
        self.statuses = list(statuses)

    async def get_status(self, _workflow_id):
        return self.statuses.pop(0)


def workflow_output():
    return {
        "videoid": "video-42",
        "orientation": "Vertical",
        "generations": [
            {
                "index": 0,
                "prompt": "Prompt",
                "status": "completed",
                "blob_path": (
                    "ltxavatarjob/agentvideo/video-42/"
                    "hdvideo-001-video-42.mp4"
                ),
                "num_frames": 121,
            }
        ],
    }


def mixed_workflow_output():
    output = workflow_output()
    output["generations"].append(
        {
            "index": 1,
            "prompt": "Prompt en échec",
            "status": "failed",
            "blob_path": (
                "ltxavatarjob/agentvideo/video-42/"
                "hdvideo-failed-video-42.mp4"
            ),
            "error": "GPU unavailable",
        }
    )
    return output


async def fake_sas_uri_provider(blob_paths, *, base_url, ttl_seconds):
    assert base_url == (
        "https://fluxstorageaca.blob.core.windows.net/"
        "ltxavatarjob/agentvideo"
    )
    assert ttl_seconds == 3600
    return {
        path: (
            f"{base_url}/{path.removeprefix('ltxavatarjob/agentvideo/')}"
            "?sp=r&sig=test-signature"
        )
        for path in blob_paths
    }


def test_completed_polling_result_is_strongly_typed():
    result = _workflow_response(
        DurableStatus(RuntimeStatus("Completed"), "workflow-1", workflow_output()),
        "workflow-1",
    )

    assert result.status == "completed"
    assert result.result.generations[0].num_frames == 121


def test_failed_and_not_found_polling_results():
    failed = _workflow_response(
        DurableStatus(RuntimeStatus("Failed"), "workflow-1", "worker failure"),
        "workflow-1",
    )
    missing = _workflow_response(None, "workflow-missing")

    assert failed.status == "failed"
    assert failed.error == "worker failure"
    assert missing.status == "not_found"


def test_budgeted_wait_returns_inline_terminal_result():
    client = FakeClient(
        [DurableStatus(RuntimeStatus("Completed"), "workflow-1", workflow_output())]
    )

    result = asyncio.run(
        _wait_for_workflow(
            client,
            "workflow-1",
            wait_budget_seconds=1,
            poll_interval_seconds=1,
        )
    )

    assert result.status == "completed"


def test_budgeted_wait_returns_poll_handle_when_budget_expires():
    result = asyncio.run(
        _wait_for_workflow(
            FakeClient([]),
            "workflow-1",
            wait_budget_seconds=0,
            poll_interval_seconds=1,
        )
    )

    assert result.status == "running"
    assert result.workflow_id == "workflow-1"


def test_completed_result_returns_text_and_video_resource_links():
    result = _workflow_response(
        DurableStatus(
            RuntimeStatus("Completed"),
            "workflow-1",
            mixed_workflow_output(),
        ),
        "workflow-1",
    )

    blocks = asyncio.run(
        _to_content_blocks(result, sas_uri_provider=fake_sas_uri_provider)
    )

    assert len(blocks) == 2
    assert isinstance(blocks[0], TextContent)
    assert '"status":"completed"' in blocks[0].text
    assert isinstance(blocks[1], ResourceLink)
    assert blocks[1].mimeType == "video/mp4"
    assert str(blocks[1].uri) == (
        "https://fluxstorageaca.blob.core.windows.net/"
        "ltxavatarjob/agentvideo/video-42/hdvideo-001-video-42.mp4"
        "?sp=r&sig=test-signature"
    )
    assert blocks[1].name == "hdvideo-001-video-42.mp4"


def test_azure_functions_serializes_rich_content_block_list():
    result = _workflow_response(
        DurableStatus(
            RuntimeStatus("Completed"),
            "workflow-1",
            workflow_output(),
        ),
        "workflow-1",
    )

    blocks = asyncio.run(
        _to_content_blocks(result, sas_uri_provider=fake_sas_uri_provider)
    )
    encoded = functions_mcp._MCPToolTriggerConverter.encode(blocks)
    payload = json.loads(encoded.value)

    assert payload[0]["type"] == "text"
    assert payload[1] == {
        "name": "hdvideo-001-video-42.mp4",
        "uri": (
            "https://fluxstorageaca.blob.core.windows.net/"
            "ltxavatarjob/agentvideo/video-42/hdvideo-001-video-42.mp4"
            "?sp=r&sig=test-signature"
        ),
        "description": "Vidéo HD générée pour le prompt 1, 121 images.",
        "mimeType": "video/mp4",
        "type": "resource_link",
    }


def test_running_result_returns_json_text_without_resource_link():
    blocks = asyncio.run(
        _to_content_blocks(
            _workflow_response(
                DurableStatus(RuntimeStatus("Running"), "workflow-1"),
                "workflow-1",
            )
        )
    )

    assert len(blocks) == 1
    assert isinstance(blocks[0], TextContent)
    assert '"status":"running"' in blocks[0].text
