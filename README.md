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

Both tools return `List[ContentBlock]`. The first block is a `TextContent`
containing the JSON status contract used for polling. A completed response also
contains one `ResourceLink` per successful generation, with
`mimeType="video/mp4"` and the generated video URL.

Every generation result contains its prompt/index, terminal status,
deterministic blob path, optional `num_frames`, and any error or timeout.
Vertical videos are 704x1280; horizontal videos are 1280x704.
Each queue message also carries a deterministic seed derived from its
`event_key`, so an at-least-once activity replay produces the same video at the
same blob path instead of racing with a randomly different generation.
The LTX `type_prefix` includes a short stable token derived from the Durable
instance ID and prompt index. Retries of one workflow keep the same blob path,
while concurrent workflows for the same `videoid` write distinct blobs.
When the global timer wins, the orchestrator uses `continue_as_new` to enter a
terminal phase without recreating pending external-event listeners, then
completes with timeout results.

Transport limits are enforced on UTF-8 bytes rather than character counts:

- at most 64 prompts per workflow;
- DTS input is capped at 960 KiB, retaining 64 KiB for the orchestration wrapper;
- each Service Bus body is capped at 252 KiB, retaining 4 KiB below the Basic
  tier's 256 KiB limit;
- prompt validation reserves an additional 16 KiB for generated correlation
  fields and the JSON envelope, and the final serialized message is checked
  again immediately before the output binding.

## Local validation

Python 3.13 and Azure Functions Core Tools are expected.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pytest -q
```

`src/local.settings.json` contains no secret. Start the Durable Task Scheduler
emulator on port 8080 before running the Function App locally. The same
`src/host.json` selects DTS locally and in every deployment path, preventing a
CI package from silently falling back to the Azure Storage provider. Running
the Service Bus output binding locally requires an Azure identity that already
has sender access to the existing queue; unit tests do not access Azure.
`local.settings.json` is excluded from both AZD and GitHub Actions deployment
packages.

The runtime pins the maintained MCP SDK 1.x line because stable
`azure-functions` 2.2 serializes its `mcp.types` content blocks natively. MCP
SDK 2.x support requires a later Azure Functions release.

## Configuration

| Setting | Default / purpose |
| --- | --- |
| `MCP_WAIT_BUDGET_SECONDS` | `20`, inline MCP wait budget |
| `MCP_POLL_INTERVAL_SECONDS` | `5`, suggested polling delay |
| `ORCHESTRATION_TIMEOUT_SECONDS` | `7200`, durable global timeout |
| `VIDEO_BLOB_BASE_URL` | Public base URL for generated LTX video links |
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
