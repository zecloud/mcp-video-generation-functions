import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_host_always_uses_durable_task_scheduler():
    host = json.loads((ROOT / "src" / "host.json").read_text(encoding="utf-8"))

    durable = host["extensions"]["durableTask"]
    assert durable["hubName"] == "%TASKHUB_NAME%"
    assert durable["storageProvider"] == {
        "type": "azureManaged",
        "connectionStringName": "DURABLE_TASK_SCHEDULER_CONNECTION_STRING",
    }


def test_no_alternate_deployment_host_configuration_exists():
    assert not (ROOT / "src" / "host.dts.json").exists()
    azure_yaml = (ROOT / "azure.yaml").read_text(encoding="utf-8")
    assert "host.dts.json" not in azure_yaml
    assert "prepackage" not in azure_yaml


def test_github_package_excludes_local_settings():
    workflow = (
        ROOT / ".github" / "workflows" / "master_mcpvideoworkflow.yml"
    ).read_text(encoding="utf-8")

    assert "--exclude='local.settings.json'" in workflow
    assert 'test ! -e "${{ env.AZURE_FUNCTIONAPP_PACKAGE_PATH }}/local.settings.json"' in workflow
