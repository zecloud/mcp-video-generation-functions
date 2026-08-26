# MCP HD video generation

Azure Functions Python 3.13 MCP server that starts one LTX 2.5 generation per
prompt, sends all jobs to the existing `ltx25msrjob` Service Bus queue, and
collects their Durable Task Scheduler events.

## MCP tools

- `create_hd_video` validates `CreateHDVideoInput`, starts the fan-out/fan-in
  orchestration, and waits up to `MCP_WAIT_BUDGET_SECONDS`. It returns the
  completed result inline or a `workflow_id`.
- `get_hd_video_result` accepts a required, strongly validated `workflow_id`
  and returns `running`, `completed`, `failed`, or `not_found`.

Every generation result contains its prompt/index, terminal status,
deterministic blob path, optional `num_frames`, and any error or timeout.
Vertical videos are 720x1280; horizontal videos are 1280x720.
Each queue message also carries a deterministic seed derived from its
`event_key`, so an at-least-once activity replay produces the same video at the
same blob path instead of racing with a randomly different generation.
The LTX `type_prefix` includes a short stable token derived from the Durable
instance ID and prompt index. Retries of one workflow keep the same blob path,
while concurrent workflows for the same `videoid` write distinct blobs.

## Local validation

Python 3.13 and Azure Functions Core Tools are expected.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pytest -q
```

`src/local.settings.json` contains no secret. Azurite is used for local Durable
state. The DTS emulator can instead be started on port 8080 as documented by
Durable Task Scheduler. Running the Service Bus output binding locally requires
an Azure identity that already has sender access to the existing queue; unit
tests do not access Azure.

## Configuration

| Setting | Default / purpose |
| --- | --- |
| `MCP_WAIT_BUDGET_SECONDS` | `20`, inline MCP wait budget |
| `MCP_POLL_INTERVAL_SECONDS` | `5`, suggested polling delay |
| `ORCHESTRATION_TIMEOUT_SECONDS` | `7200`, durable global timeout |
| `SERVICE_BUS_QUEUE_NAME` | `ltx25msrjob` |
| `ServiceBusConnection__fullyQualifiedNamespace` | Existing namespace endpoint |
| `ServiceBusConnection__credential` | `managedidentity` |
| `ServiceBusConnection__clientId` | Function UAMI client ID in Azure |
| `DURABLE_TASK_SCHEDULER_CONNECTION_STRING` | DTS endpoint/task hub/UAMI |
| `TASKHUB_NAME` | `default` |

The Azure configuration is assembled from the official
`functions-quickstart-python-http-azd` base plus the MCP, Durable and Service
Bus recipes. It creates the Function App, FC1 plan, storage, identity and
Application Insights, while referencing the existing Service Bus, DTS and Log
Analytics resources.

`ASSIGN_EXISTING_RESOURCE_ROLES` defaults to `false`. Enabling it would create
sender/contributor role assignments on existing Service Bus and DTS resources
and therefore requires separate explicit approval.

## Deployment gate

This repository is prepared for validation only. Do not run `azd up`,
`azd provision`, `azd deploy`, or change any Azure setting/RBAC without a new
explicit approval from the resource owner.
