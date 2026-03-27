"""Git-first runtime adapter for BranchClaw workers."""

from __future__ import annotations

import os
import re
from hashlib import sha1
from pathlib import Path

from branchclaw.config import get_data_dir
from branchclaw.models import ArchiveWorkspace, WorkerResult
from branchclaw.workspace.base import WorkspaceRuntimeAdapter
from clawteam.workspace import git


def _slug(value: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-._")
    return safe or "item"


class GitWorkspaceRuntimeAdapter(WorkspaceRuntimeAdapter):
    """Git-backed worktree adapter for worker isolation."""

    def __init__(self, repo_path: str | None = None):
        cwd = Path(repo_path) if repo_path else Path.cwd()
        self._repo_root = git.repo_root(cwd)
        self._base_ref = git.current_branch(self._repo_root)

    def repo_root(self) -> str:
        return str(self._repo_root)

    def default_base_ref(self) -> str:
        return self._base_ref

    def create_workspace(self, run_id: str, stage_id: str, worker_name: str) -> ArchiveWorkspace:
        workspaces_root = get_data_dir() / "workspaces" / run_id / stage_id
        workspaces_root.mkdir(parents=True, exist_ok=True)
        workspace_path = workspaces_root / _slug(worker_name)
        branch = f"branchclaw/{_slug(run_id)}/{_slug(stage_id)}/{_slug(worker_name)}"

        if workspace_path.exists():
            try:
                git.remove_worktree(self._repo_root, workspace_path)
            except Exception:
                pass
        try:
            git.delete_branch(self._repo_root, branch)
        except Exception:
            pass

        git.create_worktree(self._repo_root, workspace_path, branch, base_ref=self._base_ref)
        return self.snapshot_workspace(
            worker_name=worker_name,
            stage_id=stage_id,
            feature_id="",
            workspace_path=str(workspace_path),
            branch=branch,
            base_ref=self._base_ref,
        )

    def checkpoint(self, workspace_path: str, message: str) -> bool:
        return git.commit_all(Path(workspace_path), message)

    def head_sha(self, workspace_path: str) -> str:
        return git._run(["rev-parse", "HEAD"], cwd=Path(workspace_path))

    def diff_signature(self, workspace_path: str) -> str:
        status = git._run(["status", "--porcelain"], cwd=Path(workspace_path))
        if not status.strip():
            return ""
        return sha1(status.encode("utf-8")).hexdigest()

    def snapshot_workspace(
        self,
        worker_name: str,
        stage_id: str,
        feature_id: str,
        workspace_path: str,
        branch: str,
        base_ref: str,
        backend: str = "",
        task: str = "",
        result: WorkerResult | None = None,
    ) -> ArchiveWorkspace:
        return ArchiveWorkspace(
            worker_name=worker_name,
            stage_id=stage_id,
            feature_id=feature_id,
            workspace_path=workspace_path,
            branch=branch,
            base_ref=base_ref,
            head_sha=self.head_sha(workspace_path),
            backend=backend,
            task=task,
            result=result,
        )

    def restore_workspace(
        self,
        run_id: str,
        archive_id: str,
        snapshot: ArchiveWorkspace,
    ) -> ArchiveWorkspace:
        restore_root = get_data_dir() / "workspaces" / run_id / "restored" / archive_id
        restore_root.mkdir(parents=True, exist_ok=True)
        suffix = snapshot.head_sha[:7]
        workspace_path = restore_root / f"{_slug(snapshot.worker_name)}-{suffix}"
        branch = (
            f"branchclaw/{_slug(run_id)}/{_slug(snapshot.stage_id)}/"
            f"{_slug(snapshot.worker_name)}-restore-{suffix}"
        )

        if workspace_path.exists():
            try:
                git.remove_worktree(self._repo_root, workspace_path)
            except Exception:
                pass
        try:
            git.delete_branch(self._repo_root, branch)
        except Exception:
            pass

        git.create_worktree(self._repo_root, workspace_path, branch, base_ref=snapshot.head_sha)
        return self.snapshot_workspace(
            worker_name=snapshot.worker_name,
            stage_id=snapshot.stage_id,
            feature_id=snapshot.feature_id,
            workspace_path=str(workspace_path),
            branch=branch,
            base_ref=snapshot.base_ref,
            backend=snapshot.backend,
            task=snapshot.task,
            result=snapshot.result,
        )

    def promote_workspace(
        self,
        snapshot: ArchiveWorkspace,
        target_ref: str | None = None,
    ) -> tuple[bool, str]:
        target = target_ref or snapshot.base_ref or self._base_ref
        cwd_before = os.getcwd()
        try:
            os.chdir(self._repo_root)
            return git.merge_branch(self._repo_root, snapshot.branch, target)
        finally:
            os.chdir(cwd_before)

    def prepare_branch(self, branch: str, source_ref: str) -> None:
        git._run(["checkout", source_ref], cwd=self._repo_root)
        git._run(["checkout", "-B", branch, source_ref], cwd=self._repo_root)

    def checkout_ref(self, ref: str) -> None:
        git._run(["checkout", ref], cwd=self._repo_root)

    def promote_workspace_branch(
        self,
        source_branch: str,
        target_ref: str,
    ) -> tuple[bool, str]:
        cwd_before = os.getcwd()
        try:
            os.chdir(self._repo_root)
            return git.merge_branch(self._repo_root, source_branch, target_ref)
        finally:
            os.chdir(cwd_before)
