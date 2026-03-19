from __future__ import annotations

from typer.testing import CliRunner

from clawteam.cli.commands import app
from clawteam.config import load_config
from clawteam.team.manager import TeamManager


def test_config_cli_supports_all_keys_and_bool_values(tmp_path):
    runner = CliRunner()
    env = {
        "HOME": str(tmp_path),
        "CLAWTEAM_DATA_DIR": str(tmp_path / ".clawteam"),
    }

    result = runner.invoke(app, ["config", "set", "skip_permissions", "false"], env=env)
    assert result.exit_code == 0
    assert load_config().skip_permissions is False

    result = runner.invoke(app, ["config", "set", "workspace", "never"], env=env)
    assert result.exit_code == 0
    assert load_config().workspace == "never"

    result = runner.invoke(app, ["config", "get", "workspace"], env=env)
    assert result.exit_code == 0
    assert "workspace = never" in result.output


def test_team_approve_join_errors_when_request_missing(tmp_path):
    runner = CliRunner()
    env = {
        "HOME": str(tmp_path),
        "CLAWTEAM_DATA_DIR": str(tmp_path / ".clawteam"),
        "CLAWTEAM_AGENT_ID": "leader001",
        "CLAWTEAM_AGENT_NAME": "leader",
    }

    TeamManager.create_team(
        name="demo",
        leader_name="leader",
        leader_id="leader001",
    )

    result = runner.invoke(app, ["team", "approve-join", "demo", "missing-req"], env=env)

    assert result.exit_code == 1
    assert "No join request found with id 'missing-req'" in result.output
    assert [member.name for member in TeamManager.list_members("demo")] == ["leader"]
