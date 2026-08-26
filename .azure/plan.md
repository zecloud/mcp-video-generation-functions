# Azure Deployment Plan

> **Status:** Approved
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
| `prompts` | `List[str]` | One parallel generation per non-empty prompt |
| `orientation` | `Orientation` | `Vertical` = 720x1280; `Horizontal` = 1280x720 |

MCP tools:

1. `create_hd_video`: validates input, starts the durable orchestration, waits for a short configurable MCP budget, then returns either completed results or a `workflow_id`.
2. `get_hd_video_result`: accepts a strongly typed required `workflow_id` and returns `running`, `completed`, `failed`, or `not_found`.

### Durable workflow

1. Start one orchestration for the complete request.
2. Fan out one `enqueue_ltx25_generation` activity per prompt.
3. Each activity sends one JSON message to the existing `ltx25msrjob` queue:
   - `videoid`, `prompt`, `pic1`, `pic2`, `width`, `height`
   - deterministic `type_prefix`
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

- [ ] Initialize the official Python Azure Functions AZD template
- [ ] Compose MCP source/storage settings
- [ ] Compose Durable Functions with existing DTS resources
- [ ] Compose Service Bus sender integration with existing queue
- [ ] Implement Pydantic models and MCP decorators
- [ ] Implement budgeted start/poll MCP tools
- [ ] Implement fan-out queue activities and parallel external-event waits
- [ ] Implement deterministic 2-hour durable timeout
- [ ] Add tests for validation, message mapping, orchestration results, failures, and timeouts
- [ ] Add local development configuration and README
- [ ] Run targeted tests, lint/type checks already provided by the project, and Functions metadata validation
- [ ] Update plan status to `Ready for Validation`

### Phase 3: Validation

- [ ] Invoke `azure-validate`
- [ ] Record validation proof
- [ ] Update plan status to `Validated`

### Phase 4: Deployment

- [ ] Ask the user for explicit deployment approval
- [ ] Invoke `azure-deploy` only after approval
- [ ] Ask separately before changing settings or RBAC on existing `agentvideo` resources
- [ ] Record deployed endpoint URLs and update status to `Deployed`

---

## 8. Validation Proof

| Check | Command Run | Result | Timestamp |
|-------|-------------|--------|-----------|
| Pending | Pending execution | Pending | Pending |

**Validated by:** Pending `azure-validate`

---

## 9. Files to Generate

| File | Purpose | Status |
|------|---------|--------|
| `.azure/plan.md` | Approved architecture and execution source of truth | Complete |
| `azure.yaml` | AZD service and deployment hooks | Pending |
| `infra/` | Secure Bicep composition and existing-resource references | Pending |
| `src/function_app.py` | MCP tools, orchestrator, activities | Pending |
| `src/models.py` | Pydantic input/result models and enums | Pending |
| `src/host.json` | MCP and DTS extension configuration | Pending |
| `src/requirements.txt` | Runtime dependencies | Pending |
| `tests/` | Unit and orchestration contract tests | Pending |
| `README.md` | Local use, MCP tools, configuration, and deployment gate | Pending |

---

## 10. Next Step

Current phase: Approved for implementation.

Start the coding sub-session. Do not deploy or modify Azure resources without a new explicit user approval.
