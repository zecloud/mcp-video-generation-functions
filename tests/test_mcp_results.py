import asyncio
from dataclasses import dataclass

from function_app import (
    _wait_for_workflow,
    _workflow_response,
)


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

