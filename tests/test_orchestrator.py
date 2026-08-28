from dataclasses import dataclass
from datetime import datetime, timezone
import inspect

import function_app


@dataclass
class FakeState:
    name: str


class FakeTask:
    def __init__(self, kind, name, state="RUNNING", result=None):
        self.kind = kind
        self.name = name
        self.state = FakeState(state)
        self.result = result
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


@dataclass
class TaskAnyRequest:
    tasks: list


class FakeContext:
    instance_id = "workflow-1"
    is_replaying = False
    current_utc_datetime = datetime(2026, 8, 26, tzinfo=timezone.utc)

    def __init__(self, orchestration_input=None):
        self.uuid_counter = 0
        self.orchestration_input = orchestration_input
        self.continued_input = None
        self.dispatch_tasks = [
            FakeTask("dispatch", "dispatch-0", "FAILED", RuntimeError("queue down")),
            FakeTask("dispatch", "dispatch-1", "SUCCEEDED", {"index": 1}),
        ]
        self.event_tasks = []
        self.timer = None

    def get_input(self):
        if self.orchestration_input is not None:
            return self.orchestration_input
        return {
            "request": {
                "videoid": "video-42",
                "ref_speaker1_filename": "alice",
                "ref_speaker2_filename": "bob",
                "prompts": ["Prompt 0", "Prompt 1"],
                "orientation": "Vertical",
            },
            "timeout_seconds": 7200,
        }

    def new_uuid(self):
        self.uuid_counter += 1
        return f"uuid-{self.uuid_counter}"

    def call_activity(self, _name, _payload):
        return self.dispatch_tasks.pop(0)

    def task_any(self, tasks):
        return TaskAnyRequest(tasks)

    def wait_for_external_event(self, event_name):
        task = FakeTask("event", event_name)
        self.event_tasks.append(task)
        return task

    def create_timer(self, _deadline):
        self.timer = FakeTask("timer", "timeout")
        return self.timer

    def continue_as_new(self, orchestration_input):
        self.continued_input = orchestration_input


def orchestrator_callable():
    function = getattr(function_app.run_hd_video_orchestrator, "_function")
    durable_wrapper = getattr(function, "_func")
    return inspect.getclosurevars(durable_wrapper).nonlocals["fn"]


def complete_continued_orchestration(context):
    assert context.continued_input is not None
    continuation = orchestrator_callable()(
        FakeContext(orchestration_input=context.continued_input)
    )
    try:
        next(continuation)
    except StopIteration as completed:
        return completed.value
    raise AssertionError("The terminal continuation should complete immediately.")


def test_orchestrator_keeps_successful_dispatch_and_completes_worker_failure():
    context = FakeContext()
    generator = orchestrator_callable()(context)

    request = next(generator)
    assert len(request.tasks) == 3
    failed_dispatch = next(
        task for task in request.tasks if task.name == "dispatch-0"
    )
    request = generator.send(failed_dispatch)
    assert len(request.tasks) == 2

    successful_dispatch = next(
        task for task in request.tasks if task.name == "dispatch-1"
    )
    request = generator.send(successful_dispatch)
    first_event = next(task for task in request.tasks if task.kind == "event")
    first_event.result = {
        "status": "failed",
        "event_key": "workflow-1:1:uuid-3",
        "error": "terminal GPU error",
    }

    try:
        generator.send(first_event)
    except StopIteration as completed:
        output = completed.value
    else:
        raise AssertionError("The orchestration should have completed.")

    assert [item["status"] for item in output["generations"]] == [
        "failed",
        "failed",
    ]
    assert "Service Bus" in output["generations"][0]["error"]
    assert output["generations"][1]["error"] == "terminal GPU error"
    assert len(context.event_tasks) == 1
    assert context.timer.cancelled


def test_orchestration_timeout_includes_dispatch_phase():
    context = FakeContext()
    generator = orchestrator_callable()(context)

    request = next(generator)
    try:
        generator.send(context.timer)
    except StopIteration:
        output = complete_continued_orchestration(context)
    else:
        raise AssertionError("The orchestration should have timed out.")

    assert [item["status"] for item in output["generations"]] == [
        "timeout",
        "timeout",
    ]


def test_retryable_failure_then_timer_returns_timeout():
    context = FakeContext()
    context.dispatch_tasks = [
        FakeTask("dispatch", "dispatch-0", "SUCCEEDED", {"index": 0}),
        FakeTask("dispatch", "dispatch-1", "SUCCEEDED", {"index": 1}),
    ]
    generator = orchestrator_callable()(context)

    request = next(generator)
    request = generator.send(
        next(task for task in request.tasks if task.name == "dispatch-0")
    )
    request = generator.send(
        next(task for task in request.tasks if task.name == "dispatch-1")
    )
    retryable_event = next(task for task in request.tasks if task.kind == "event")
    retryable_event.result = {
        "status": "failed",
        "event_key": "workflow-1:0:uuid-1",
        "retryable": True,
        "error": "temporary",
    }

    request = generator.send(retryable_event)
    try:
        generator.send(context.timer)
    except StopIteration:
        output = complete_continued_orchestration(context)
    else:
        raise AssertionError("The orchestration should have timed out.")

    assert output["generations"][0]["status"] == "timeout"
    assert output["generations"][0]["error"] == (
        "Délai maximal de génération dépassé."
    )


def test_no_external_event_timeout_completes_through_fresh_execution():
    context = FakeContext()
    context.dispatch_tasks = [
        FakeTask("dispatch", "dispatch-0", "SUCCEEDED", {"index": 0}),
        FakeTask("dispatch", "dispatch-1", "SUCCEEDED", {"index": 1}),
    ]
    generator = orchestrator_callable()(context)

    request = next(generator)
    request = generator.send(
        next(task for task in request.tasks if task.name == "dispatch-0")
    )
    request = generator.send(
        next(task for task in request.tasks if task.name == "dispatch-1")
    )
    assert len([task for task in request.tasks if task.kind == "event"]) == 2

    try:
        generator.send(context.timer)
    except StopIteration as completed:
        assert completed.value is None
    else:
        raise AssertionError("The timed-out execution should continue as new.")

    output = complete_continued_orchestration(context)
    assert [item["status"] for item in output["generations"]] == [
        "timeout",
        "timeout",
    ]
