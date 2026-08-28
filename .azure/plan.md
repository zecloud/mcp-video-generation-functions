# Azure Deployment Plan

> **Status:** Validated
>
> **Deployment gate:** Never provision, deploy, or modify Azure resources without the user's explicit approval immediately before the operation.

Generated: 2026-08-26T15:51:00+02:00

---

## 1. Project Overview

**Goal:** Build a Python Azure Functions MCP server that starts parallel LTX 2.5 HD video generations through Service Bus, waits for their Durable Task Scheduler events, and returns terminal generation results.

**Path:** New Project

**Repository:** `https://github.com/zecloud/mcp-video-generation-functions` (private)

---

## 2. Requirements

| Attribute | Value |
|-----------|-------|
| Classification | Development / internal tooling |
| Scale | Serverless, event-driven; initial small workload |
| Budget | Cost-optimized |
| Compliance | No specific requirement |
| Subscription | Microsoft Azure Sponsorship (`5459e31a-a44f-4526-847e-73352604bc98`) |
| Location | `westus3` |
| Runtime | Python 3.13, Azure Functions v2 programming model |
| Hosting | Flex Consumption (FC1), scale to zero |
| Orchestration timeout | 2 hours, configurable through an app setting |

---

## 3. Components

| Component | Type | Technology | Path |
|-----------|------|------------|------|
| MCP tools | API | Azure Functions MCP trigger + Pydantic v2 | `src/function_app.py` |
| Video orchestrator | Workflow | Durable Functions fan-out/fan-in + external events | `src/function_app.py` |
| Queue activity | Worker | Durable activity + Service Bus output binding | `src/function_app.py` |
| LTX 2.5 integration | Existing worker | `agentvideo-namespace/ltx25msrjob` | Existing `func_tts_eurovibe/ltx25` app |
| Durable backend | Existing service | DTS scheduler `agentvideo`, task hub `default` | Resource group `agentsdcpp` |
| Tests | Validation | pytest | `tests/` |

---

## 4. Recipe Selection

**Selected:** AZD with Bicep.

**Rationale:**

- Start from the official Python Azure Functions AZD base template.
- Compose the MCP, Durable Task Scheduler, and Service Bus recipes.
- Reuse the existing Service Bus namespace/queue and existing DTS scheduler/task hub; declare them as existing resources or deployment parameters rather than recreating them.
- Use user-assigned managed identity and RBAC only; do not add Service Bus connection strings or storage account keys.

---

## 5. Architecture

**Stack:** Serverless Azure Functions on Flex Consumption.

### MCP contract

`CreateHDVideoInput` is exposed through `pydantic_mcp_tool_properties` and validated at runtime with `validate_pydantic_arguments` from:

`git+https://github.com/zecloud/azurefunctionsmcpydantic.git`

Fields:

| Field | Type | Behavior |
|-------|------|----------|
| `videoid` | `str` | Existing video job folder identifier |
| `ref_speaker1_filename` | `str` | Extensionless name; `.png` is appended for LTX `pic1` |
| `ref_speaker2_filename` | `str` | Extensionless name; `.png` is appended for LTX `pic2` |
| `prompts` | `List[str]` | 1–64 non-empty prompts; UTF-8 byte budgets protect DTS and Service Bus limits |
| `orientation` | `Orientation` | `Vertical` = 704x1280; `Horizontal` = 1280x704 |

MCP tools:

1. `create_hd_video`: validates input, starts the durable orchestration, waits for a short configurable MCP budget, then returns either completed results or a `workflow_id`.
2. `get_hd_video_result`: accepts a strongly typed required `workflow_id` and returns `running`, `completed`, `failed`, or `not_found`.
3. Both tools return `List[ContentBlock]`: a JSON `TextContent` status and, for every successful terminal generation, a `ResourceLink` with `mimeType="video/mp4"` built from `VIDEO_BLOB_BASE_URL`.

### Durable workflow

1. Start one orchestration for the complete request.
2. Fan out one `enqueue_ltx25_generation` activity per prompt.
3. Each activity sends one JSON message to the existing `ltx25msrjob` queue:
   - `videoid`, `prompt`, `pic1`, `pic2`, `width`, `height`
   - deterministic per-orchestration `type_prefix` derived from `instance_id`
   - orchestration `instance_id`
   - unique `event_key`
   - unique `dts_event_name`
4. The orchestrator waits in parallel for all matching DTS external events raised by LTX 2.5.
5. A durable timer enforces the configurable 2-hour timeout.
6. The result contains every terminal generation, including prompt/index, status, output blob path, frame count when available, and error details when failed or timed out.

No HTTP callback is created or used.

### Existing LTX 2.5 event contract

The merged LTX 2.5 code already accepts `instance_id`, `event_key`, and `dts_event_name`, then calls `DurableTaskSchedulerClient.raise_orchestration_event`. No LTX source change is planned.

Before any future Azure deployment, the existing LTX Function App must be confirmed to have:

- `DTS_EVENT_ENABLED=true`
- `DTS_ENDPOINT=https://agentvideo-atgnfafbfvdrg.westus3.durabletask.io`
- `DTS_TASKHUB=default`
- managed identity permission to raise events in the target DTS scheduler

Changing these settings is an Azure modification and requires separate explicit approval.

### Service Mapping

| Component | Azure Service | SKU / Existing resource |
|-----------|---------------|-------------------------|
| MCP + orchestrator + activities | Azure Functions | Flex Consumption FC1 |
| Host/MCP state and deployment package | Storage Account | Standard LRS |
| Durable state | Durable Task Scheduler | Existing Consumption scheduler `agentvideo/default` |
| Generation commands | Azure Service Bus | Existing Basic namespace `agentvideo-namespace`, queue `ltx25msrjob` |
| Telemetry | Application Insights | Workspace-based |
| Logs | Log Analytics | Existing `workspaceagentsdcpp8688` |
| Service authentication | User-assigned managed identity | RBAC-only |

### Security

- Function and MCP endpoint use function-level authentication initially.
- Service Bus access uses `Azure Service Bus Data Sender` at queue or namespace scope.
- DTS access uses `Durable Task Data Contributor`.
- Storage access uses managed identity and data-plane RBAC.
- Secrets are excluded from source and local settings.
- Input validation rejects empty identifiers, empty prompt lists, and blank prompts.

---

## 6. Provisioning Limit Checklist

Quota checks used Azure CLI quota commands first. Unsupported providers use Azure Resource Graph plus official Microsoft limits.

| Resource Type | Number to Deploy | Total After Deployment | Limit / Quota | Notes |
|---------------|------------------|------------------------|---------------|-------|
| `Microsoft.Web/sites` | 1 | 7 in `westus3` | 5,000 Function Apps per subscription | Existing count: 6. Microsoft.Web quota API reports quota as not applicable; official Functions limit used. |
| `Microsoft.Web/serverfarms` (FC1) | 1 | 7 in `westus3` | 250 regional cores / 512,000 MB | Existing plan count: 6. New app will cap at 10 x 2,048 MB instances = 10 cores maximum. |
| `Microsoft.Storage/storageAccounts` | 1 | 11 in `westus3` | 250 | Azure quota CLI: current 10, limit 250. |
| `Microsoft.DurableTask/schedulers` | 0 | 1 in `westus3` | 10 Consumption schedulers | Reuse existing `agentvideo`; quota API unsupported, official DTS limit used. |
| `Microsoft.DurableTask/schedulers/taskHubs` | 0 | 1 in scheduler | 5 task hubs for Consumption SKU | Reuse existing `default`; current count verified with `az durabletask taskhub list`. |
| `Microsoft.ServiceBus/namespaces` | 0 | 1 used | Not applicable to this deployment | Reuse `agentvideo-namespace`; queue `ltx25msrjob` is active. |
| `Microsoft.Insights/components` | 1 | 11 in `westus3` | No resource-count provisioning quota exposed | Existing count: 10. Data ingestion limits are plan-based. |
| `Microsoft.OperationalInsights/workspaces` | 0 | 3 in `westus3` | No new workspace required | Reuse existing `workspaceagentsdcpp8688`. |

**Status:** All planned resources are within verified limits.

---

## 7. Execution Checklist

### Phase 1: Planning

- [x] Analyze workspace and create private repository
- [x] Gather requirements
- [x] Confirm subscription and location
- [x] Scan LTX 2.5, Pydantic MCP helper, and long-running MCP sample
- [x] Select AZD/Bicep recipe
- [x] Plan MCP, Service Bus, and DTS architecture
- [x] Validate provisioning limits
- [x] User approved this plan

### Phase 2: Execution

- [x] Initialize the official Python Azure Functions AZD template
- [x] Compose MCP source/storage settings
- [x] Compose Durable Functions with existing DTS resources
- [x] Compose Service Bus sender integration with existing queue
- [x] Implement Pydantic models and MCP decorators
- [x] Implement budgeted start/poll MCP tools
- [x] Implement fan-out queue activities and parallel external-event waits
- [x] Implement deterministic 2-hour durable timeout
- [x] Return MCP SDK `TextContent` and `ResourceLink` blocks for polling and completed videos
- [x] Use one DTS-enabled `host.json` for AZD and GitHub Actions deployments
- [x] Complete timed-out event waits through a listener-free `continue_as_new` phase
- [x] Add tests for validation, message mapping, orchestration results, failures, and timeouts
- [x] Add local development configuration and README
- [x] Run targeted tests, lint/type checks already provided by the project, and Functions metadata validation
- [x] Update plan status to `Ready for Validation`

### Phase 3: Validation

- [x] Invoke `azure-validate`
- [x] All validation checks pass
  - [x] AZD installation and `azure.yaml` schema
  - [x] AZD environment, authentication, subscription, and location
  - [x] Bicep compilation and lint
  - [x] Read-only AZD provision preview
  - [x] Python 3.13 build and targeted tests
  - [x] AZD package validation
  - [x] Read-only Azure Policy review
- [x] Record validation proof
- [x] Update plan status to `Validated`

### Phase 4: Deployment

- [ ] Ask the user for explicit deployment approval
- [ ] Invoke `azure-deploy` only after approval
- [ ] Ask separately before changing settings or RBAC on existing `agentvideo` resources
- [ ] Record deployed endpoint URLs and update status to `Deployed`

---

## 8. Validation Proof

| Check | Command Run | Result | Timestamp |
|-------|-------------|--------|-----------|
| Rich MCP result tests | `.venv\Scripts\python.exe -m pytest tests\test_mcp_results.py tests\test_video_workflow.py -q` | 18 passed, including Azure Functions content-block serialization | 2026-08-26T21:20:00+02:00 |
| DTS host deployment package | `.venv\Scripts\python.exe -m pytest -q`; `azd package api ...`; inspect packaged `host.json` | 36 passed; package uses `azureManaged`; `local.settings.json` absent | 2026-08-27T17:40:00+02:00 |
| No-event orchestration timeout | `.venv\Scripts\python.exe -m pytest tests\test_orchestrator.py -q`; `.venv\Scripts\python.exe -m pytest -q` | 4 targeted and 37 total passed; timeout continues into a listener-free terminal execution | 2026-08-28T09:35:00+02:00 |
| Python 3.13 tests | `.venv\Scripts\python.exe -m pytest -q` | 33 passed | 2026-08-26T21:20:00+02:00 |
| Python compilation | `.venv\Scripts\python.exe -m compileall -q src tests` | Passed | 2026-08-26T21:20:00+02:00 |
| Functions metadata | Import `function_app.app.get_functions()` under Python 3.13 | Four functions and expected MCP/Durable/Service Bus bindings discovered | 2026-08-26T21:20:00+02:00 |
| Bicep compilation | `az bicep build --file infra\main.bicep --stdout` | Passed | 2026-08-26T21:20:00+02:00 |
| Bicep lint | `az bicep lint --file infra\main.bicep` | Passed without source warnings | 2026-08-26T21:20:00+02:00 |
| ARM validation | `az deployment sub validate ... assignExistingResourceRoles=false` | Passed with `error: null` | 2026-08-26T21:20:00+02:00 |
| AZD authentication/context | `azd auth login --check-status`; `az account show` | Logged in; Microsoft Azure Sponsorship / `westus3` confirmed | 2026-08-26T21:20:00+02:00 |
| AZD preview | `azd provision --preview --no-prompt` | Passed; five creates, no existing-resource or RBAC modification | 2026-08-26T21:20:00+02:00 |
| AZD package | `azd package api --output-path <session-artifact> --no-prompt` | Passed with DTS host config | 2026-08-26T21:20:00+02:00 |
| Azure Policy review | `az policy assignment list --disable-scope-strict-match` | Only enforced Security Center default assignment; no deployment restriction found | 2026-08-26T21:20:00+02:00 |
| Core Tools host | `func start --python --verbose` with Python 3.13 | Local worker in Core Tools 4.13.0 rejects Python 3.13; static metadata validation above passed | 2026-08-26T16:35:00+02:00 |

**Validated by:** `azure-validate` on 2026-08-26

---

## 9. Files to Generate

| File | Purpose | Status |
|------|---------|--------|
| `.azure/plan.md` | Approved architecture and execution source of truth | Complete |
| `azure.yaml` | AZD service and deployment hooks | Complete |
| `infra/` | Secure Bicep composition and existing-resource references | Complete |
| `src/function_app.py` | MCP tools, orchestrator, activities | Complete |
| `src/models.py` | Pydantic input/result models and enums | Complete |
| `src/host.json` | MCP and DTS extension configuration | Complete |
| `src/requirements.txt` | Runtime dependencies | Complete |
| `tests/` | Unit and orchestration contract tests | Complete |
| `README.md` | Local use, MCP tools, configuration, and deployment gate | Complete |

---

## 10. Research Summary

- Base project: official `Azure-Samples/functions-quickstart-python-http-azd`
  template, preserving the Flex Consumption/UAMI/AVM composition.
- MCP/Durable pattern: official
  `Azure-Samples/mcp-functions-long-running-tools-python` sample, including the
  preview extension bundle, budgeted wait, polling contract and DTS packaging
  swap.
- Typed MCP metadata and validation:
  `zecloud/azurefunctionsmcpydantic` decorators, with
  `@app.mcp_tool()` outermost and strict Pydantic validation.
- Service Bus: Python v2 output binding with the identity-based
  `fullyQualifiedNamespace`, `credential` and `clientId` settings and the
  `Azure Service Bus Data Sender` role.
- LTX event payload verified against
  `zecloud/func_tts_eurovibe/ltx25/function_app.py`: terminal events expose
  `status`, `event_key`, `type_prefix`, optional `num_frames`, and `error`.
- Existing Service Bus, DTS/task hub and Log Analytics resources are declared
  with Bicep `existing`; their RBAC assignments are gated off by default.

---

## 11. Next Step

Current phase: Validated after the rich MCP video result update; deployment
remains blocked by the explicit approval gate.

Do not invoke `azure-deploy`, provision, deploy, or modify Azure resources
without a new explicit user approval. Existing-resource RBAC requires separate
approval even after deployment approval.
