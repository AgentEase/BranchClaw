"""CLI commands for BranchClaw."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from branchclaw import __version__
from branchclaw.board import serve as serve_board
from branchclaw.board import summarize_run
from branchclaw.config import get_data_dir, load_config
from branchclaw.daemon import (
    BranchClawDaemonClient,
    daemon_root,
    daemon_socket_path,
    read_saved_daemon_status,
    run_daemon_server,
    start_daemon_process,
    stop_orphaned_daemon_process,
)
from branchclaw.mcp_server import run_server as run_mcp_server
from branchclaw.runtime import launch_supervised_worker
from branchclaw.service import BranchClawService
from branchclaw.storage import EventStore

app = typer.Typer(name="branchclaw", help="BranchClaw v1 CLI", no_args_is_help=True)
console = Console()
service = BranchClawService()

_json_output = False
_live_worker_statuses = {"starting", "running", "stale"}
_unhealthy_worker_statuses = {"stale", "blocked", "failed"}


def _data_dir_str() -> str:
    return str(get_data_dir().resolve())


@app.callback()
def main(
    version: bool = typer.Option(False, "--version", callback=lambda value: _show_version(value), is_eager=True),
    json_output: bool = typer.Option(False, "--json", help="Output JSON"),
):
    """BranchClaw v1 CLI."""
    global _json_output
    _json_output = json_output


def _show_version(value: bool):
    if value:
        console.print(f"branchclaw v{__version__}")
        raise typer.Exit()


def _output(data, human=None):
    if _json_output:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return
    if human:
        human(data)
        return
    console.print_json(json.dumps(data, ensure_ascii=False))


def _fail(message: str):
    if _json_output:
        print(json.dumps({"error": message}, ensure_ascii=False))
    else:
        console.print(f"[red]{message}[/red]")
    raise typer.Exit(1)


def _read_text_or_file(value: str) -> str:
    try:
        path = Path(value)
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8")
    except OSError:
        return value
    return value


def _read_json_or_file(value: str) -> dict:
    try:
        path = Path(value)
        raw = path.read_text(encoding="utf-8") if path.exists() and path.is_file() else value
    except OSError:
        raw = value
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object for worker result payload")
    return payload


def _run_summary(projection) -> dict:
    return {
        "id": projection.run.id,
        "name": projection.run.name,
        "status": projection.run.status.value,
        "projectProfile": projection.run.project_profile.value,
        "repoRoot": projection.run.repo_root,
        "baseRef": projection.run.base_ref,
        "direction": projection.run.direction,
        "integrationRef": projection.run.integration_ref,
        "maxActiveFeatures": projection.run.max_active_features,
        "currentStageId": projection.run.current_stage_id,
        "activePlanId": projection.run.active_plan_id,
        "constraints": len(projection.constraints),
        "workers": len(projection.workers),
        "archives": len(projection.archives),
        "features": len(getattr(projection, "features", {})),
        "batches": len(getattr(projection, "batches", {})),
        "readyFeatures": getattr(projection.stats, "ready_feature_count", 0),
        "openBatches": getattr(projection.stats, "open_batch_count", 0),
        "needsReplan": projection.run.needs_replan,
        "dirtyReason": projection.run.dirty_reason,
        "pendingRecovery": projection.stats.pending_recovery_count,
        "openInterventions": projection.stats.open_intervention_count,
        "pendingApprovals": sum(
            1 for gate in projection.approvals.values() if gate.status.value == "pending"
        ),
    }


def _short_text(value: str, limit: int = 56) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return "-"
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _format_heartbeat(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    return f"{seconds:.1f}s"


def _format_exit_state(item: dict) -> str:
    if item.get("failure_reason"):
        return item["failure_reason"]
    if item.get("exit_code") is not None:
        return str(item["exit_code"])
    return "-"


def _worker_result(item: dict) -> dict:
    result = item.get("result")
    return result if isinstance(result, dict) else {}


def _worker_preview(item: dict) -> str:
    result = _worker_result(item)
    return (
        result.get("preview_url")
        or result.get("backend_url")
        or item.get("discoveredUrl")
        or _short_text(result.get("output_snippet", ""), 48)
    )


def _render_workers_table(
    workers: list[dict],
    *,
    title: str = "Workers",
    show_branch: bool = False,
    show_task: bool = False,
    show_workspace: bool = False,
) -> None:
    if not workers:
        console.print("[dim]No workers[/dim]")
        return
    table = Table(title=title)
    table.add_column("Name", style="cyan")
    table.add_column("Status")
    table.add_column("Backend")
    table.add_column("Heartbeat", justify="right")
    table.add_column("PIDs", justify="right")
    table.add_column("Exit")
    table.add_column("MCP")
    table.add_column("Mgmt")
    table.add_column("Tool")
    table.add_column("Result")
    table.add_column("Preview / Output")
    if show_branch:
        table.add_column("Branch")
    if show_task:
        table.add_column("Task")
    if show_workspace:
        table.add_column("Workspace")
    for item in workers:
        row = [
            item["worker_name"],
            item["status"],
            item["backend"],
            _format_heartbeat(item.get("heartbeatAgeSeconds")),
            f"{item.get('supervisor_pid', 0)}/{item.get('child_pid', item.get('pid', 0))}",
            _short_text(_format_exit_state(item), 48),
            "on" if item.get("mcpEnabled") else "-",
            _short_text(
                (
                    f"daemon:{item.get('daemonPid')}"
                    if item.get("managedByDaemon")
                    else "-"
                ),
                24,
            ),
            _short_text(
                f"{item.get('lastToolName') or '-'}:{item.get('lastToolStatus') or '-'}",
                40,
            ),
            _worker_result(item).get("status", "-"),
            _short_text(_worker_preview(item), 48),
        ]
        if show_branch:
            row.append(item["branch"])
        if show_task:
            row.append(_short_text(item.get("task", ""), 48))
        if show_workspace:
            row.append(item["workspace_path"])
        table.add_row(*row)
    console.print(table)


def _render_worker_reports(workers: list[dict]) -> None:
    console.print("[bold]Worker Reports[/bold]")
    reported = [item for item in workers if _worker_result(item)]
    if not reported:
        console.print("[dim]No worker results reported[/dim]")
        return
    for item in reported:
        result = _worker_result(item)
        console.print(
            f"[cyan]{item['worker_name']}[/cyan] "
            f"status={result.get('status', '-')}"
            f" stack={result.get('stack') or '-'}"
            f" runtime={result.get('runtime') or '-'}"
        )
        if item.get("reportSource"):
            console.print(f"Source: {item['reportSource']}")
        if result.get("preview_url"):
            console.print(f"Preview: {result['preview_url']}")
        elif item.get("discoveredUrl"):
            console.print(f"Discovered Preview: {item['discoveredUrl']}")
        if result.get("backend_url"):
            console.print(f"Backend: {result['backend_url']}")
        if result.get("changed_surface_summary"):
            console.print(f"Surface: {_short_text(result['changed_surface_summary'], 160)}")
        if result.get("architecture_summary"):
            console.print(_short_text(result["architecture_summary"], 240))
        warnings = result.get("warnings") or []
        blockers = result.get("blockers") or []
        if warnings:
            console.print(f"Warnings: {_short_text('; '.join(warnings), 160)}")
        if blockers:
            console.print(f"Blockers: {_short_text('; '.join(blockers), 160)}")


def _render_pending_approvals(approvals: list[dict]) -> None:
    console.print("[bold]Pending Approvals[/bold]")
    if not approvals:
        console.print("[dim]No pending approvals[/dim]")
        return
    table = Table()
    table.add_column("Gate", style="cyan")
    table.add_column("Type")
    table.add_column("Target")
    table.add_column("Created")
    for approval in approvals:
        table.add_row(
            approval["id"],
            approval["gate_type"],
            approval["target_id"],
            approval["created_at"],
        )
    console.print(table)


def _render_archives(archives: list[dict]) -> None:
    console.print("[bold]Archives[/bold]")
    if not archives:
        console.print("[dim]No archives[/dim]")
        return
    table = Table()
    table.add_column("ID", style="cyan")
    table.add_column("Stage")
    table.add_column("Status")
    table.add_column("Label")
    for archive in archives:
        table.add_row(
            archive["id"],
            archive["stage_id"],
            archive["status"],
            archive["label"] or "-",
        )
    console.print(table)


def _render_interventions(interventions: list[dict]) -> None:
    console.print("[bold]Interventions[/bold]")
    open_items = [item for item in interventions if item.get("status") == "open"]
    if not open_items:
        console.print("[dim]No open interventions[/dim]")
        return
    table = Table()
    table.add_column("ID", style="cyan")
    table.add_column("Worker")
    table.add_column("Action")
    table.add_column("Reason")
    table.add_column("Attempts", justify="right")
    for item in open_items:
        attempts = f"{item.get('remediation_attempts', 0)}/{item.get('restart_attempts', 0)}"
        table.add_row(
            item["id"],
            item.get("worker_name", "-"),
            item.get("recommended_action", "-"),
            _short_text(item.get("reason", ""), 84),
            attempts,
        )
    console.print(table)


def _render_daemon_status(data: dict) -> None:
    console.print("[bold cyan]BranchClaw Daemon[/bold cyan]")
    details = Table.grid(padding=(0, 2))
    details.add_row("Running", "yes" if data.get("running") else "no")
    details.add_row("PID", str(data.get("daemon_pid", 0) or "-"))
    details.add_row("Socket", data.get("socket_path") or str(daemon_socket_path()))
    details.add_row("Root", str(daemon_root()))
    details.add_row("Started", data.get("started_at") or "-")
    details.add_row(
        "Dashboard",
        (
            f"{data.get('dashboard_url')} "
            f"({data.get('dashboard_host')}:{data.get('dashboard_port')})"
            if data.get("dashboard_running")
            else "stopped"
        ),
    )
    details.add_row("Managed Data Dirs", str(len(data.get("data_dirs", []))))
    details.add_row("Managed Processes", str(len(data.get("processes", []))))
    console.print(details)


def _render_daemon_processes(data: dict) -> None:
    _render_daemon_status(data)
    processes = data.get("processes", [])
    if not processes:
        console.print("[dim]No managed processes[/dim]")
        return
    table = Table(title="Managed Processes")
    table.add_column("ID", style="cyan")
    table.add_column("Kind")
    table.add_column("Data Dir")
    table.add_column("Run/Worker")
    table.add_column("PID")
    table.add_column("Endpoint")
    table.add_column("Status")
    for item in processes:
        endpoint = item.get("socket") or (
            f"{item.get('host')}:{item.get('port')}"
            if item.get("port")
            else "-"
        )
        owner = item.get("run_id", "")
        if item.get("worker_name"):
            owner = f"{owner}/{item['worker_name']}" if owner else item["worker_name"]
        table.add_row(
            item["id"],
            item["process_kind"],
            _short_text(item["data_dir"], 48),
            owner or "-",
            str(item.get("supervisor_pid") or item.get("pid") or "-"),
            endpoint,
            item.get("status", "-"),
        )
    console.print(table)


def _render_constraints(constraints: list[dict]) -> None:
    console.print("[bold]Constraints[/bold]")
    if not constraints:
        console.print("[dim]No constraints[/dim]")
        return
    for constraint in constraints:
        console.print(
            f"[cyan]{constraint['id']}[/cyan] {_short_text(constraint['content'], 120)}"
        )


def _format_counter(counter: dict[str, int]) -> str:
    if not counter:
        return "-"
    return ", ".join(f"{key}={value}" for key, value in sorted(counter.items()))


def _render_worktree_track(track: dict) -> None:
    console.print("[bold]Working Tree Track[/bold]")
    summary = track.get("summary", {})
    console.print(
        "Summary: "
        f"{summary.get('trackedWorkers', 0)} tracked worker(s) / "
        f"{summary.get('currentWorktrees', 0)} current / "
        f"{summary.get('restoredWorktrees', 0)} restored / "
        f"{summary.get('archivedSnapshots', 0)} archived snapshots / "
        f"{summary.get('reportedWorktrees', 0)} current reported / "
        f"{summary.get('acceptedEntries', 0)} accepted entries"
    )
    console.print(
        "Tree States: "
        f"current={_format_counter(track.get('currentStatusCounts', {}))}  "
        f"archive={_format_counter(track.get('archiveStatusCounts', {}))}  "
        f"acceptance={_format_counter(track.get('resultStatusCounts', {}))}"
    )
    tracks = track.get("tracks", [])
    if not tracks:
        console.print("[dim]No tracked worktrees[/dim]")
        return
    for worker_track in tracks:
        console.print(f"[cyan]{worker_track['workerName']}[/cyan]")
        entries = worker_track.get("entries", [])
        for index, entry in enumerate(entries):
            prefix = "`-" if index == len(entries) - 1 else "|-"
            label = entry.get("kind", "current")
            status = entry.get("status", "-")
            archive_id = entry.get("archiveId", "")
            archive_suffix = f" archive={archive_id}" if archive_id else ""
            console.print(
                f"  {prefix} {label} [{status}] stage={entry.get('stageId', '-')}{archive_suffix}"
            )
            console.print(
                f"  {'  ' if prefix == '`-' else '| '} path={entry.get('relativePath') or entry.get('workspacePath')}"
            )
            console.print(
                f"  {'  ' if prefix == '`-' else '| '} branch={entry.get('branch', '-')}"
            )
            console.print(
                f"  {'  ' if prefix == '`-' else '| '} head={entry.get('headSha', '-') or '-'}"
            )
            if entry.get("resultStatus"):
                summary_line = (
                    f"  {'  ' if prefix == '`-' else '| '} acceptance={entry['resultStatus']}"
                )
                preview = entry.get("previewUrl") or entry.get("backendUrl")
                if preview:
                    summary_line += f" target={preview}"
                elif entry.get("outputSnippet"):
                    summary_line += f" output={_short_text(entry['outputSnippet'], 72)}"
                console.print(summary_line)
            if entry.get("changedSurfaceSummary"):
                console.print(
                    f"  {'  ' if prefix == '`-' else '| '} surface={_short_text(entry['changedSurfaceSummary'], 120)}"
                )
            warnings = entry.get("warnings") or []
            blockers = entry.get("blockers") or []
            if warnings:
                console.print(
                    f"  {'  ' if prefix == '`-' else '| '} warnings={_short_text('; '.join(warnings), 120)}"
                )
            if blockers:
                console.print(
                    f"  {'  ' if prefix == '`-' else '| '} blockers={_short_text('; '.join(blockers), 120)}"
                )


def _render_run_dashboard(data: dict, *, show_workspace: bool) -> None:
    run = data["run"]
    workers = data["workers"]
    worktree_track = data["worktreeTrack"]
    live_workers = sum(1 for worker in workers if worker["status"] in _live_worker_statuses)
    unhealthy_workers = sum(
        1 for worker in workers if worker["status"] in _unhealthy_worker_statuses
    )

    console.print(f"[bold cyan]{run['name']}[/bold cyan] [{run['id']}]")
    details = Table.grid(padding=(0, 2))
    details.add_row("Status", run["status"])
    details.add_row("Project Profile", run["projectProfile"])
    details.add_row("Stage", run["currentStageId"])
    details.add_row("Active Plan", run["activePlanId"] or "(none)")
    details.add_row("Repo", f"{run['repoRoot']} @ {run['baseRef']}")
    details.add_row("Owner Data Dir", run.get("ownerDataDir", "-"))
    if run.get("daemonPid"):
        details.add_row("Daemon PID", str(run["daemonPid"]))
    if run["needsReplan"]:
        details.add_row(
            "Replan",
            f"dirty ({run['dirtyReason'] or 'constraint'} since {run['dirtySince']})",
        )
    else:
        details.add_row("Replan", "clean")
    details.add_row(
        "Worker Health",
        f"{len(workers)} total / {live_workers} live / {unhealthy_workers} unhealthy",
    )
    details.add_row(
        "MCP",
        f"{sum(1 for worker in workers if worker.get('mcpEnabled'))} enabled / "
        f"{sum(1 for worker in workers if worker.get('lastToolStatus') == 'failed')} tool failures",
    )
    details.add_row(
        "Flow",
        f"{len(data['approvals'])} pending approvals / "
        f"{len(data['archives'])} archives / "
        f"{len(data.get('features', []))} features / "
        f"{len(data.get('batches', []))} batches / "
        f"{len([item for item in data.get('interventions', []) if item.get('status') == 'open'])} open interventions / "
        f"{data['stats']['pending_recovery_count']} pending recovery",
    )
    details.add_row(
        "Feature Queue",
        f"{run.get('readyFeatureCount', 0)} ready / "
        f"{run.get('openBatchCount', 0)} open batches / "
        f"max active {run.get('maxActiveFeatures', 0)}",
    )
    details.add_row("Integration Ref", run.get("integrationRef", "-"))
    if run.get("direction"):
        details.add_row("Direction", _short_text(run["direction"], 120))
    details.add_row(
        "Worktrees",
        f"{worktree_track['summary']['currentWorktrees']} current / "
        f"{worktree_track['summary']['restoredWorktrees']} restored / "
        f"{worktree_track['summary']['archivedSnapshots']} archived snapshots",
    )
    console.print(details)
    console.print(
        f"Constraints={len(data['constraints'])}  "
        f"LatestConstraint={run['latestConstraintId'] or '(none)'}  "
        f"LastEvent={data['lastEventAt'] or '(none)'}"
    )
    _render_pending_approvals(data["approvals"])
    _render_interventions(data.get("interventions", []))
    if data.get("features"):
        console.print("[bold]Feature Queue[/bold]")
        for feature in data["features"]:
            console.print(
                f"- [cyan]{feature['id']}[/cyan] {feature['title']} "
                f"[{feature['status']}] "
                f"priority={feature.get('priority', 0)} "
                f"worker={feature.get('worker_name') or '-'} "
                f"areas={','.join(feature.get('claimed_areas', [])) or '-'}"
            )
    if data.get("batches"):
        console.print("[bold]Batch Review[/bold]")
        for batch in data["batches"]:
            console.print(
                f"- [cyan]{batch['id']}[/cyan] [{batch['status']}] "
                f"features={len(batch.get('feature_ids', []))} "
                f"integration={batch.get('integration_ref') or '-'} "
                f"validation={batch.get('validation_status') or '-'}"
            )
    _render_workers_table(
        workers,
        title="Workers",
        show_branch=True,
        show_task=True,
        show_workspace=show_workspace,
    )
    _render_worker_reports(workers)
    _render_worktree_track(worktree_track)
    _render_archives(data["archives"])
    _render_constraints(data["constraints"])


run_app = typer.Typer(help="Run lifecycle commands")
planner_app = typer.Typer(help="Planner and approval commands")
worker_app = typer.Typer(help="Worker runtime commands")
feature_app = typer.Typer(help="Feature backlog commands")
batch_app = typer.Typer(help="Batch review commands")
constraint_app = typer.Typer(help="Constraint commands")
archive_app = typer.Typer(help="Archive and rollback commands")
event_app = typer.Typer(help="Event stream commands")
board_app = typer.Typer(help="Board commands")
mcp_app = typer.Typer(help="MCP server commands")
daemon_app = typer.Typer(help="Global daemon commands")

app.add_typer(run_app, name="run")
app.add_typer(planner_app, name="planner")
app.add_typer(worker_app, name="worker")
app.add_typer(feature_app, name="feature")
app.add_typer(batch_app, name="batch")
app.add_typer(constraint_app, name="constraint")
app.add_typer(archive_app, name="archive")
app.add_typer(event_app, name="event")
app.add_typer(board_app, name="board")
app.add_typer(mcp_app, name="mcp")
app.add_typer(daemon_app, name="daemon")


@run_app.command("create")
def run_create(
    name: str = typer.Argument(..., help="Run name"),
    description: str = typer.Option("", "--description", "-d", help="Run description"),
    project_profile: str = typer.Option("backend", "--project-profile", help="Project profile: web, fullstack, or backend"),
    spec: str = typer.Option("", "--spec", help="Spec text or file"),
    rules: str = typer.Option("", "--rules", help="Rules text or file"),
    repo: Optional[str] = typer.Option(None, "--repo", help="Git repository path"),
    direction: str = typer.Option("", "--direction", help="High-level direction for the long-lived run"),
    integration_ref: str = typer.Option("", "--integration-ref", help="Integration branch used for batch merges"),
    max_active_features: int = typer.Option(2, "--max-active-features", help="Automatic feature dispatch concurrency"),
):
    """Create a new BranchClaw run."""
    try:
        projection = service.create_run(
            name,
            description=description,
            project_profile=project_profile,
            spec_content=_read_text_or_file(spec),
            rules_content=_read_text_or_file(rules),
            repo=repo,
            direction=direction,
            integration_ref=integration_ref,
            max_active_features=max_active_features,
        )
    except Exception as exc:
        _fail(str(exc))
        return

    data = _run_summary(projection)
    _output(
        data,
        lambda item: console.print(
            f"[green]OK[/green] Created run [cyan]{item['id']}[/cyan] "
            f"({item['status']})"
        ),
    )


@run_app.command("list")
def run_list():
    """List all BranchClaw runs."""
    runs = [_run_summary(item) for item in service.list_runs()]

    def _human(items):
        if not items:
            console.print("[dim]No runs found[/dim]")
            return
        table = Table(title="BranchClaw Runs")
        table.add_column("ID", style="cyan")
        table.add_column("Name")
        table.add_column("Status")
        table.add_column("Project Profile")
        table.add_column("Replan")
        table.add_column("Workers", justify="right")
        table.add_column("Interventions", justify="right")
        table.add_column("Approvals", justify="right")
        for item in items:
            table.add_row(
                item["id"],
                item["name"],
                item["status"],
                item["projectProfile"],
                "dirty" if item["needsReplan"] else "-",
                str(item["workers"]),
                str(item.get("openInterventions", 0)),
                str(item["pendingApprovals"]),
            )
        console.print(table)

    _output(runs, _human)


@run_app.command("show")
def run_show(run_id: str = typer.Argument(..., help="Run ID")):
    """Show the full projection for a run."""
    try:
        service.get_run(run_id)
    except Exception as exc:
        _fail(str(exc))
        return
    data = summarize_run(run_id, service)

    def _human(item):
        _render_run_dashboard(item, show_workspace=True)

    _output(data, _human)


@run_app.command("merge-request")
def run_merge_request(
    run_id: str = typer.Argument(..., help="Run ID"),
    archive_id: str = typer.Option("", "--archive-id", help="Approved archive to merge"),
    batch_id: str = typer.Option("", "--batch-id", help="Ready batch to merge into integration"),
    actor: str = typer.Option("", "--actor", help="Human actor requesting merge"),
):
    """Request merge promotion for an approved archive."""
    try:
        gate = service.request_merge(run_id, archive_id=archive_id, batch_id=batch_id, actor=actor)
    except Exception as exc:
        _fail(str(exc))
        return

    data = json.loads(gate.model_dump_json())
    _output(
        data,
        lambda item: console.print(
            f"[green]OK[/green] Merge gate [cyan]{item['id']}[/cyan] created"
        ),
    )


@run_app.command("promote-request")
def run_promote_request(
    run_id: str = typer.Argument(..., help="Run ID"),
    batch_id: str = typer.Option(..., "--batch-id", help="Integrated batch to promote to base ref"),
    actor: str = typer.Option("", "--actor", help="Human actor requesting promote"),
):
    """Request promote from integration_ref to the main branch."""
    try:
        gate = service.request_promote(run_id, batch_id=batch_id, actor=actor)
    except Exception as exc:
        _fail(str(exc))
        return

    data = json.loads(gate.model_dump_json())
    _output(
        data,
        lambda item: console.print(
            f"[green]OK[/green] Promote gate [cyan]{item['id']}[/cyan] created"
        ),
    )


@run_app.command("migrate-clawteam")
def run_migrate_clawteam(
    team_name: str = typer.Argument(..., help="Legacy ClawTeam team name"),
    run_name: str = typer.Option("", "--run-name", help="Override imported run name"),
    repo: Optional[str] = typer.Option(None, "--repo", help="Repository path"),
    clawteam_data_dir: Optional[str] = typer.Option(None, "--clawteam-data-dir", help="Legacy data root"),
):
    """Import a legacy ClawTeam team into BranchClaw."""
    try:
        projection = service.migrate_from_clawteam(
            team_name,
            new_run_name=run_name,
            clawteam_data_dir=clawteam_data_dir,
            repo=repo,
        )
    except Exception as exc:
        _fail(str(exc))
        return
    data = _run_summary(projection)
    _output(
        data,
        lambda item: console.print(
            f"[green]OK[/green] Imported into run [cyan]{item['id']}[/cyan]"
        ),
    )


@planner_app.command("propose")
def planner_propose(
    run_id: str = typer.Argument(..., help="Run ID"),
    plan: str = typer.Argument(..., help="Plan text or file"),
    summary: str = typer.Option("", "--summary", help="Plan summary"),
    author: str = typer.Option("", "--author", help="Author"),
):
    """Propose a planner output and open a plan gate."""
    try:
        proposal, gate = service.propose_plan(
            run_id,
            _read_text_or_file(plan),
            summary=summary,
            author=author,
        )
    except Exception as exc:
        _fail(str(exc))
        return

    data = {
        "planId": proposal.id,
        "gateId": gate.id,
        "status": proposal.status.value,
    }
    _output(
        data,
        lambda item: console.print(
            f"[green]OK[/green] Proposed plan [cyan]{item['planId']}[/cyan], "
            f"gate [cyan]{item['gateId']}[/cyan] pending"
        ),
    )


@planner_app.command("approve")
def planner_approve(
    run_id: str = typer.Argument(..., help="Run ID"),
    gate_id: str = typer.Argument(..., help="Gate ID"),
    actor: str = typer.Option("", "--actor", help="Approver"),
    feedback: str = typer.Option("", "--feedback", help="Optional feedback"),
):
    """Approve a BranchClaw gate."""
    try:
        projection = service.approve_gate(run_id, gate_id, actor=actor, feedback=feedback)
    except Exception as exc:
        _fail(str(exc))
        return

    data = _run_summary(projection)
    _output(
        data,
        lambda item: console.print(
            f"[green]OK[/green] Approved gate [cyan]{gate_id}[/cyan], run now "
            f"[cyan]{item['status']}[/cyan]"
        ),
    )


@planner_app.command("reject")
def planner_reject(
    run_id: str = typer.Argument(..., help="Run ID"),
    gate_id: str = typer.Argument(..., help="Gate ID"),
    actor: str = typer.Option("", "--actor", help="Rejector"),
    feedback: str = typer.Option("", "--feedback", help="Feedback"),
):
    """Reject a BranchClaw gate."""
    try:
        projection = service.reject_gate(run_id, gate_id, actor=actor, feedback=feedback)
    except Exception as exc:
        _fail(str(exc))
        return
    data = _run_summary(projection)
    _output(
        data,
        lambda item: console.print(
            f"[green]OK[/green] Rejected gate [cyan]{gate_id}[/cyan], run now "
            f"[cyan]{item['status']}[/cyan]"
        ),
    )


@planner_app.command("resume")
def planner_resume(
    run_id: str = typer.Argument(..., help="Run ID"),
    actor: str = typer.Option("", "--actor", help="Actor"),
    note: str = typer.Option("", "--note", help="Planner note"),
):
    """Recompile the effective planner execution bundle."""
    try:
        bundle = service.resume_planner(run_id, actor=actor, note=note)
    except Exception as exc:
        _fail(str(exc))
        return
    _output({"runId": run_id, "bundle": bundle}, lambda item: console.print(item["bundle"]))


@worker_app.command("spawn")
def worker_spawn(
    run_id: str = typer.Argument(..., help="Run ID"),
    worker_name: str = typer.Argument(..., help="Worker name"),
    command: list[str] = typer.Argument(None, help="Command to run (default: claude)"),
    backend: str = typer.Option("subprocess", "--backend", help="subprocess or tmux"),
    task: str = typer.Option("", "--task", help="Worker task text"),
    feature_id: str = typer.Option("", "--feature-id", help="Attach this worker to a feature record"),
    skip_permissions: bool | None = typer.Option(
        None,
        "--skip-permissions/--no-skip-permissions",
        help="Skip Claude/Codex approval prompts",
    ),
):
    """Spawn a worker with an isolated Git workspace."""
    final_command = list(command) if command else ["claude"]
    try:
        worker = service.spawn_worker(
            run_id,
            worker_name,
            command=final_command,
            backend=backend,
            task=task,
            feature_id=feature_id,
            skip_permissions=skip_permissions,
        )
    except Exception as exc:
        _fail(str(exc))
        return
    data = json.loads(worker.model_dump_json())
    _output(
        data,
        lambda item: console.print(
            f"[green]OK[/green] Spawned worker [cyan]{item['worker_name']}[/cyan] "
            f"in {item['workspace_path']} via supervisor {item['supervisor_pid']}"
        ),
    )


@worker_app.command("list")
def worker_list(run_id: str = typer.Argument(..., help="Run ID")):
    """List workers for a run."""
    try:
        projection = summarize_run(run_id, service)
    except Exception as exc:
        _fail(str(exc))
        return
    workers = projection["workers"]

    def _human(items):
        console.print(
            f"[bold cyan]{projection['run']['name']}[/bold cyan] [{projection['run']['id']}]"
        )
        console.print(
            f"Status={projection['run']['status']}  "
            f"ProjectProfile={projection['run']['projectProfile']}  "
            f"PendingApprovals={len(projection['approvals'])}  "
            f"OpenInterventions={len([item for item in projection.get('interventions', []) if item.get('status') == 'open'])}  "
            f"PendingRecovery={projection['stats']['pending_recovery_count']}"
        )
        console.print(
            "Workers="
            + (", ".join(worker["worker_name"] for worker in items) if items else "(none)")
        )
        _render_workers_table(
            items,
            title=f"Workers for {run_id}",
            show_task=True,
            show_workspace=True,
        )
        _render_interventions(projection.get("interventions", []))
        _render_worker_reports(items)

    _output(workers, _human)


@worker_app.command("checkpoint")
def worker_checkpoint(
    run_id: str = typer.Argument(..., help="Run ID"),
    worker_name: str = typer.Argument(..., help="Worker name"),
    message: str = typer.Option("", "--message", help="Commit message"),
):
    """Checkpoint a worker workspace into its branch."""
    try:
        worker = service.checkpoint_worker(run_id, worker_name, message=message)
    except Exception as exc:
        _fail(str(exc))
        return
    data = json.loads(worker.model_dump_json())
    _output(
        data,
        lambda item: console.print(
            f"[green]OK[/green] Checkpointed [cyan]{item['worker_name']}[/cyan] @ {item['head_sha'][:12]}"
        ),
    )


@worker_app.command("stop")
def worker_stop(
    run_id: str = typer.Argument(..., help="Run ID"),
    worker_name: str = typer.Argument(..., help="Worker name"),
):
    """Stop a worker process or tmux window."""
    try:
        worker = service.stop_worker(run_id, worker_name)
    except Exception as exc:
        _fail(str(exc))
        return
    data = json.loads(worker.model_dump_json())
    _output(
        data,
        lambda item: console.print(
            f"[green]OK[/green] Stopped [cyan]{item['worker_name']}[/cyan] "
            f"({item['status']})"
        ),
    )


@worker_app.command("restart")
def worker_restart(
    run_id: str = typer.Argument(..., help="Run ID"),
    worker_name: str = typer.Argument(..., help="Worker name"),
):
    """Restart a stopped, failed, or blocked worker."""
    try:
        worker = service.restart_worker(run_id, worker_name)
    except Exception as exc:
        _fail(str(exc))
        return
    data = json.loads(worker.model_dump_json())
    _output(
        data,
        lambda item: console.print(
            f"[green]OK[/green] Restarted [cyan]{item['worker_name']}[/cyan] "
            f"({item['status']})"
        ),
    )


@worker_app.command("report")
def worker_report(
    run_id: str = typer.Argument(..., help="Run ID"),
    worker_name: str = typer.Argument(..., help="Worker name"),
    result_file: str = typer.Option("", "--result-file", help="Result JSON file"),
    status: str = typer.Option("", "--status", help="Result status"),
    stack: str = typer.Option("", "--stack", help="Detected stack"),
    runtime: str = typer.Option("", "--runtime", help="Detected runtime"),
    package_manager: str = typer.Option("", "--package-manager", help="Detected package manager"),
    install_command: str = typer.Option("", "--install-command", help="Install command used"),
    start_command: str = typer.Option("", "--start-command", help="Start command used"),
    preview_url: str = typer.Option("", "--preview-url", help="Frontend preview URL"),
    backend_url: str = typer.Option("", "--backend-url", help="Backend base URL"),
    output_snippet: str = typer.Option("", "--output-snippet", help="Relevant runtime output"),
    changed_surface_summary: str = typer.Option("", "--changed-surface-summary", help="Changed surface summary"),
    architecture_summary: str = typer.Option("", "--architecture-summary", help="Architecture change markdown"),
    warnings: list[str] = typer.Option(None, "--warning", help="Warning text (repeatable)"),
    blockers: list[str] = typer.Option(None, "--blocker", help="Blocker text (repeatable)"),
    source: str = typer.Option("operator", "--source", help="Report source: agent, fallback, or operator"),
):
    """Publish a structured worker result into the run projection."""
    try:
        payload = _read_json_or_file(result_file) if result_file else {}
        payload.update(
            {
                key: value
                for key, value in {
                    "status": status,
                    "stack": stack,
                    "runtime": runtime,
                    "package_manager": package_manager,
                    "install_command": install_command,
                    "start_command": start_command,
                    "preview_url": preview_url,
                    "backend_url": backend_url,
                    "output_snippet": output_snippet,
                    "changed_surface_summary": changed_surface_summary,
                    "architecture_summary": architecture_summary,
                }.items()
                if value
            }
        )
        if warnings:
            payload["warnings"] = warnings
        if blockers:
            payload["blockers"] = blockers
        worker = service.report_worker_result(run_id, worker_name, payload, source=source)
    except Exception as exc:
        _fail(str(exc))
        return
    data = json.loads(worker.model_dump_json())
    _output(
        data,
        lambda item: console.print(
            f"[green]OK[/green] Reported result for [cyan]{item['worker_name']}[/cyan]"
        ),
    )


@worker_app.command("reconcile")
def worker_reconcile(run_id: str = typer.Argument(..., help="Run ID")):
    """Sweep worker runtime state and repair stale projections."""
    try:
        service.reconcile_workers(run_id)
    except Exception as exc:
        _fail(str(exc))
        return
    data = summarize_run(run_id, service)
    _output(
        data,
        lambda item: console.print(
            f"[green]OK[/green] Reconciled [cyan]{run_id}[/cyan] "
            f"(pending recovery: {item['stats']['pending_recovery_count']})"
        ),
    )


@feature_app.command("list")
def feature_list(run_id: str = typer.Argument(..., help="Run ID")):
    """List planner-generated features for a run."""
    try:
        items = [json.loads(item.model_dump_json()) for item in service.list_features(run_id)]
    except Exception as exc:
        _fail(str(exc))
        return

    def _human(rows):
        if not rows:
            console.print("[dim]No features[/dim]")
            return
        table = Table(title=f"Features for {run_id}")
        table.add_column("ID", style="cyan")
        table.add_column("Title")
        table.add_column("Status")
        table.add_column("Priority", justify="right")
        table.add_column("Worker")
        table.add_column("Areas")
        table.add_column("Validation")
        for row in rows:
            table.add_row(
                row["id"],
                row["title"],
                row["status"],
                str(row.get("priority", 0)),
                row.get("worker_name", "") or "-",
                ", ".join(row.get("claimed_areas", [])) or "-",
                row.get("validation_status", "-"),
            )
        console.print(table)

    _output(items, _human)


@feature_app.command("show")
def feature_show(
    run_id: str = typer.Argument(..., help="Run ID"),
    feature_id: str = typer.Argument(..., help="Feature ID"),
):
    """Show a single feature record."""
    try:
        feature = service.get_feature(run_id, feature_id)
    except Exception as exc:
        _fail(str(exc))
        return
    _output(json.loads(feature.model_dump_json()))


@batch_app.command("list")
def batch_list(run_id: str = typer.Argument(..., help="Run ID")):
    """List batch review records for a run."""
    try:
        items = [json.loads(item.model_dump_json()) for item in service.list_batches(run_id)]
    except Exception as exc:
        _fail(str(exc))
        return

    def _human(rows):
        if not rows:
            console.print("[dim]No batches[/dim]")
            return
        table = Table(title=f"Batches for {run_id}")
        table.add_column("ID", style="cyan")
        table.add_column("Status")
        table.add_column("Features", justify="right")
        table.add_column("Integration Ref")
        table.add_column("Validation")
        for row in rows:
            table.add_row(
                row["id"],
                row["status"],
                str(len(row.get("feature_ids", []))),
                row.get("integration_ref", "-"),
                row.get("validation_status", "-"),
            )
        console.print(table)

    _output(items, _human)


@batch_app.command("show")
def batch_show(
    run_id: str = typer.Argument(..., help="Run ID"),
    batch_id: str = typer.Argument(..., help="Batch ID"),
):
    """Show a single batch record."""
    try:
        batch = service.get_batch(run_id, batch_id)
    except Exception as exc:
        _fail(str(exc))
        return
    _output(json.loads(batch.model_dump_json()))


@worker_app.command("supervise", hidden=True)
def worker_supervise(
    run_id: str = typer.Argument(..., help="Run ID"),
    worker_name: str = typer.Argument(..., help="Worker name"),
):
    """Internal supervisor entrypoint."""
    raise typer.Exit(launch_supervised_worker(run_id, worker_name))


@daemon_app.command("start")
def daemon_start(
    host: str = typer.Option("", "--host", help="Dashboard host override"),
    port: int = typer.Option(0, "--port", help="Dashboard port override"),
):
    """Start the global BranchClaw daemon."""
    try:
        data = start_daemon_process(host=host or None, port=port or None)
    except Exception as exc:
        _fail(str(exc))
        return
    _output(
        data,
        lambda item: console.print(
            f"[green]OK[/green] BranchClaw daemon ready at [cyan]{item.get('socket_path') or daemon_socket_path()}[/cyan] "
            f"dashboard [cyan]{item.get('dashboard_url') or '-'}[/cyan]"
        ),
    )


@daemon_app.command("status")
def daemon_status():
    """Show daemon status."""
    try:
        client = BranchClawDaemonClient.optional()
        data = client.status() if client is not None else read_saved_daemon_status().model_dump(mode="json")
    except Exception as exc:
        _fail(str(exc))
        return
    _output(data, _render_daemon_status)


@daemon_app.command("ps")
def daemon_ps():
    """List daemon-managed processes."""
    try:
        client = BranchClawDaemonClient.optional()
        data = client.ps() if client is not None else read_saved_daemon_status().model_dump(mode="json")
    except Exception as exc:
        _fail(str(exc))
        return
    _output(data, _render_daemon_processes)


@daemon_app.command("stop")
def daemon_stop():
    """Stop the daemon and all managed processes."""
    try:
        client = BranchClawDaemonClient.optional()
        data = client.stop() if client is not None else stop_orphaned_daemon_process()
    except Exception as exc:
        _fail(str(exc))
        return
    _output(
        data,
        lambda item: console.print(
            "[green]OK[/green] Stopped BranchClaw daemon"
            if item.get("stopped")
            else "[dim]BranchClaw daemon not running[/dim]"
        ),
    )


@daemon_app.command("stop-service")
def daemon_stop_service(
    process_id: str = typer.Argument(..., help="Managed process ID"),
):
    """Stop a single daemon-managed service."""
    try:
        client = BranchClawDaemonClient.require_running()
        data = client.stop_service(process_id)
    except Exception as exc:
        _fail(str(exc))
        return
    _output(
        data,
        lambda item: console.print(
            f"[green]OK[/green] Stopped managed process [cyan]{item['processId']}[/cyan]"
        ),
    )


@daemon_app.command("serve", hidden=True)
def daemon_serve(
    socket_path: str = typer.Option(str(daemon_socket_path()), "--socket", help="Unix socket path"),
    host: str = typer.Option("", "--host", help="Dashboard host"),
    port: int = typer.Option(0, "--port", help="Dashboard port"),
):
    """Internal daemon entrypoint."""
    run_daemon_server(socket_path, host=host, port=port)


@mcp_app.command("serve")
def mcp_serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Host to bind the MCP server to"),
    port: int = typer.Option(8765, "--port", help="Port for the MCP server"),
):
    """Request the daemon to start the MCP server for the current data dir."""
    try:
        client = BranchClawDaemonClient.require_running()
        data = client.ensure_mcp_server(data_dir=_data_dir_str())
    except Exception as exc:
        _fail(str(exc))
        return
    _output(
        data,
        lambda item: console.print(
            f"[green]OK[/green] MCP server ready at [cyan]{item['base_url']}[/cyan] "
            f"({item['managedStatus']})"
        ),
    )


@mcp_app.command("stop")
def mcp_stop():
    """Stop the daemon-managed MCP server for the current data dir."""
    try:
        client = BranchClawDaemonClient.require_running()
        data = client.stop_mcp_server(data_dir=_data_dir_str())
    except Exception as exc:
        _fail(str(exc))
        return
    _output(
        data,
        lambda item: console.print(
            "[green]OK[/green] Stopped MCP server"
            if item.get("stopped")
            else "[dim]No MCP server running for this data dir[/dim]"
        ),
    )


@mcp_app.command("serve-local", hidden=True)
def mcp_serve_local(
    host: str = typer.Option("127.0.0.1", "--host", help="Host to bind the MCP server to"),
    port: int = typer.Option(8765, "--port", help="Port for the MCP server"),
):
    """Internal MCP server entrypoint."""
    run_mcp_server(host, port)


@constraint_app.command("add")
def constraint_add(
    run_id: str = typer.Argument(..., help="Run ID"),
    content: str = typer.Argument(..., help="Constraint text or file"),
    author: str = typer.Option("", "--author", help="Author"),
):
    """Append a constraint patch."""
    try:
        constraint = service.add_constraint(run_id, _read_text_or_file(content), author=author)
    except Exception as exc:
        _fail(str(exc))
        return
    data = json.loads(constraint.model_dump_json())
    _output(
        data,
        lambda item: console.print(
            f"[green]OK[/green] Added constraint [cyan]{item['id']}[/cyan]"
        ),
    )


@constraint_app.command("list")
def constraint_list(run_id: str = typer.Argument(..., help="Run ID")):
    """List approved constraints."""
    try:
        items = [json.loads(item.model_dump_json()) for item in service.list_constraints(run_id)]
    except Exception as exc:
        _fail(str(exc))
        return
    _output(
        items,
        lambda rows: [console.print(f"[cyan]{row['id']}[/cyan] {row['content']}") for row in rows] or None,
    )


@archive_app.command("create")
def archive_create(
    run_id: str = typer.Argument(..., help="Run ID"),
    label: str = typer.Option("", "--label", help="Archive label"),
    summary: str = typer.Option("", "--summary", help="Archive summary"),
    actor: str = typer.Option("", "--actor", help="Requester"),
):
    """Create a stage archive request and archive approval gate."""
    try:
        archive, gate = service.create_archive(run_id, label=label, summary=summary, actor=actor)
    except Exception as exc:
        _fail(str(exc))
        return
    data = {"archiveId": archive.id, "gateId": gate.id, "status": archive.status.value}
    _output(
        data,
        lambda item: console.print(
            f"[green]OK[/green] Archive [cyan]{item['archiveId']}[/cyan] requested, "
            f"gate [cyan]{item['gateId']}[/cyan] pending"
        ),
    )


@archive_app.command("list")
def archive_list(run_id: str = typer.Argument(..., help="Run ID")):
    """List run archives."""
    try:
        items = [json.loads(item.model_dump_json()) for item in service.list_archives(run_id)]
    except Exception as exc:
        _fail(str(exc))
        return

    def _human(rows):
        if not rows:
            console.print("[dim]No archives[/dim]")
            return
        table = Table(title=f"Archives for {run_id}")
        table.add_column("ID", style="cyan")
        table.add_column("Stage")
        table.add_column("Status")
        table.add_column("Label")
        for row in rows:
            table.add_row(row["id"], row["stage_id"], row["status"], row["label"])
        console.print(table)

    _output(items, _human)


@archive_app.command("restore")
def archive_restore(
    run_id: str = typer.Argument(..., help="Run ID"),
    archive_id: str = typer.Argument(..., help="Archive ID"),
    actor: str = typer.Option("", "--actor", help="Requester"),
):
    """Request a rollback to an approved archive."""
    try:
        gate = service.request_restore(run_id, archive_id, actor=actor)
    except Exception as exc:
        _fail(str(exc))
        return
    data = json.loads(gate.model_dump_json())
    _output(
        data,
        lambda item: console.print(
            f"[green]OK[/green] Rollback gate [cyan]{item['id']}[/cyan] created"
        ),
    )


@event_app.command("export")
def event_export(
    run_id: str = typer.Argument(..., help="Run ID"),
    out: Optional[str] = typer.Option(None, "--out", help="Write JSON to file"),
    include_heartbeats: bool = typer.Option(
        False,
        "--include-heartbeats",
        help="Include worker.heartbeat events in the exported JSON",
    ),
):
    """Export the raw event stream and current projection."""
    try:
        data = service.export_events(run_id, include_heartbeats=include_heartbeats)
    except Exception as exc:
        _fail(str(exc))
        return
    if out:
        Path(out).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    _output(
        data,
        lambda item: console.print(
            f"[green]OK[/green] Exported {len(item['events'])} event(s) for [cyan]{run_id}[/cyan]"
        ),
    )


@event_app.command("tail")
def event_tail(
    run_id: str = typer.Argument(..., help="Run ID"),
    limit: int = typer.Option(20, "--limit", help="Max events"),
    follow: bool = typer.Option(False, "--follow", help="Follow new events"),
    poll_interval: float = typer.Option(1.0, "--poll-interval", help="Follow interval seconds"),
):
    """Tail the event stream for a run."""
    store = EventStore()
    shown = 0

    def _print_events():
        nonlocal shown
        events = store.list_events(run_id)
        fresh = events[shown:]
        if not fresh:
            return
        for event in fresh[-limit:]:
            if _json_output:
                print(event.model_dump_json())
            else:
                console.print(
                    f"[{event.sequence:04d}] [cyan]{event.event_type}[/cyan] "
                    f"{json.dumps(event.payload, ensure_ascii=False)}"
                )
        shown = len(events)

    try:
        _print_events()
        while follow:
            time.sleep(poll_interval)
            _print_events()
    except KeyboardInterrupt:
        raise typer.Exit()


@board_app.command("show")
def board_show(run_id: str = typer.Argument(..., help="Run ID")):
    """Show a compact board summary for a run."""
    try:
        item = summarize_run(run_id, service)
    except Exception as exc:
        _fail(str(exc))
        return

    def _human(data):
        _render_run_dashboard(data, show_workspace=False)

    _output(item, _human)


@board_app.command("serve")
def board_serve(
    host: str = typer.Option("", "--host", help="Deprecated compatibility option; dashboard bind is daemon-owned."),
    port: int = typer.Option(0, "--port", help="Deprecated compatibility option; dashboard bind is daemon-owned."),
    interval: float = typer.Option(2.0, "--interval", help="SSE polling interval"),
):
    """Return the daemon-hosted dashboard URL."""
    try:
        client = BranchClawDaemonClient.require_running()
        data = client.status()
    except Exception as exc:
        _fail(str(exc))
        return
    if not data.get("dashboard_running"):
        _fail("Daemon dashboard is not running")
        return
    _output(
        data,
        lambda item: console.print(
            f"[green]OK[/green] Dashboard ready at [cyan]{item['dashboard_url']}[/cyan]"
        ),
    )


@board_app.command("stop")
def board_stop():
    """Show dashboard stop guidance."""
    client = BranchClawDaemonClient.optional()
    data = client.status() if client is not None else read_saved_daemon_status().model_dump(mode="json")
    _output(
        data,
        lambda item: console.print(
            "[yellow]Dashboard is hosted by the daemon[/yellow]. "
            "Stop it with [cyan]branchclaw daemon stop[/cyan]"
            + (
                f" (current URL: [cyan]{item['dashboard_url']}[/cyan])"
                if item.get("dashboard_url")
                else ""
            )
        ),
    )


@board_app.command("serve-local", hidden=True)
def board_serve_local(
    host: str = typer.Option("", "--host", help="Host"),
    port: int = typer.Option(0, "--port", help="Port"),
    interval: float = typer.Option(2.0, "--interval", help="SSE polling interval"),
):
    """Internal board server entrypoint."""
    config = load_config()
    serve_board(
        host=host or config.board_host,
        port=port or config.board_port,
        poll_interval=interval,
    )
