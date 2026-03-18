"""WorkspaceManager — creates / checkpoints / merges / cleans up git worktrees."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from clawteam.worker_runtime import WorkerRuntimeStore, WorkerState
from clawteam.workspace import git
from clawteam.workspace.models import WorkspaceInfo

logger = logging.getLogger(__name__)


def _workspaces_root() -> Path:
    from clawteam.team.models import get_data_dir

    p = get_data_dir() / "workspaces"
    p.mkdir(parents=True, exist_ok=True)
    return p


class WorkspaceManager:
    """Manages git worktree-based isolated workspaces for agents."""

    def __init__(self, repo_path: Path | None = None):
        cwd = repo_path or Path.cwd()
        self.repo_root = git.repo_root(cwd)
        self.base_branch = git.current_branch(self.repo_root)

    def _runtime(self, team_name: str) -> WorkerRuntimeStore:
        return WorkerRuntimeStore(team_name)

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create_workspace(
        self,
        team_name: str,
        agent_name: str,
        agent_id: str,
    ) -> WorkspaceInfo:
        branch = f"clawteam/{team_name}/{agent_name}"
        wt_path = _workspaces_root() / team_name / agent_name

        if wt_path.exists():
            try:
                git.remove_worktree(self.repo_root, wt_path)
            except git.GitError:
                pass
            try:
                git.delete_branch(self.repo_root, branch)
            except git.GitError:
                pass

        git.create_worktree(
            self.repo_root, wt_path, branch, base_ref=self.base_branch,
        )

        info = WorkspaceInfo(
            agent_name=agent_name,
            agent_id=agent_id,
            team_name=team_name,
            branch_name=branch,
            worktree_path=str(wt_path),
            repo_root=str(self.repo_root),
            base_branch=self.base_branch,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        self._runtime(team_name).bind_workspace(
            worker_name=agent_name,
            branch=branch,
            worktree_path=str(wt_path),
            repo_root=str(self.repo_root),
            base_branch=self.base_branch,
            state=WorkerState.ready,
        )
        self._runtime(team_name).ensure_worker(agent_name, worker_id=agent_id, state=WorkerState.ready)
        return info

    # ------------------------------------------------------------------
    # Checkpoint
    # ------------------------------------------------------------------

    def checkpoint(
        self,
        team_name: str,
        agent_name: str,
        message: str | None = None,
    ) -> bool:
        info = self._find(team_name, agent_name)
        if info is None:
            return False
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        msg = message or f"[clawteam] checkpoint: {agent_name} @ {ts}"
        return git.commit_all(Path(info.worktree_path), msg)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup_workspace(
        self,
        team_name: str,
        agent_name: str,
        auto_checkpoint: bool = True,
    ) -> bool:
        info = self._find(team_name, agent_name)
        if info is None:
            return False

        if auto_checkpoint:
            try:
                self.checkpoint(team_name, agent_name, f"[clawteam] final checkpoint: {agent_name}")
            except Exception:
                pass

        try:
            git.remove_worktree(self.repo_root, Path(info.worktree_path))
        except git.GitError as e:
            logger.warning("worktree remove failed: %s", e)
        try:
            git.delete_branch(self.repo_root, info.branch_name)
        except git.GitError as e:
            logger.warning("branch delete failed: %s", e)

        runtime = self._runtime(team_name)
        runtime.clear_workspace(agent_name)
        runtime.update_worker(agent_name, current_stage="workspace_cleaned")
        return True

    def cleanup_team(self, team_name: str) -> int:
        count = 0
        for ws in list(self.list_workspaces(team_name)):
            if self.cleanup_workspace(team_name, ws.agent_name):
                count += 1
        return count

    # ------------------------------------------------------------------
    # Merge
    # ------------------------------------------------------------------

    def merge_workspace(
        self,
        team_name: str,
        agent_name: str,
        target_branch: str | None = None,
        cleanup_after: bool = True,
    ) -> tuple[bool, str]:
        info = self._find(team_name, agent_name)
        if info is None:
            return False, f"No workspace found for {agent_name}"

        self.checkpoint(team_name, agent_name, f"[clawteam] pre-merge checkpoint: {agent_name}")

        target = target_branch or info.base_branch
        success, output = git.merge_branch(
            self.repo_root, info.branch_name, target,
        )

        if success and cleanup_after:
            self.cleanup_workspace(team_name, agent_name, auto_checkpoint=False)

        return success, output

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def list_workspaces(self, team_name: str) -> list[WorkspaceInfo]:
        items: list[WorkspaceInfo] = []
        for worker in self._runtime(team_name).list_workers():
            if worker.workspace is None:
                continue
            items.append(
                WorkspaceInfo(
                    agent_name=worker.worker_name,
                    agent_id=worker.worker_id,
                    team_name=worker.team_name,
                    branch_name=worker.workspace.branch,
                    worktree_path=worker.workspace.worktree_path,
                    repo_root=worker.workspace.repo_root,
                    base_branch=worker.workspace.base_branch,
                    created_at=worker.workspace.bound_at,
                )
            )
        return items

    def get_workspace(self, team_name: str, agent_name: str) -> WorkspaceInfo | None:
        return self._find(team_name, agent_name)

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @staticmethod
    def try_create(repo_path: Path | None = None) -> WorkspaceManager | None:
        """Return a WorkspaceManager if inside a git repo, else None."""
        try:
            return WorkspaceManager(repo_path)
        except git.GitError:
            return None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _find(self, team_name: str, agent_name: str) -> WorkspaceInfo | None:
        worker = self._runtime(team_name).load(agent_name)
        if worker is None or worker.workspace is None:
            return None
        return WorkspaceInfo(
            agent_name=worker.worker_name,
            agent_id=worker.worker_id,
            team_name=worker.team_name,
            branch_name=worker.workspace.branch,
            worktree_path=worker.workspace.worktree_path,
            repo_root=worker.workspace.repo_root,
            base_branch=worker.workspace.base_branch,
            created_at=worker.workspace.bound_at,
        )
