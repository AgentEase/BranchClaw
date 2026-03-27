"""Workspace runtime adapter base classes."""

from __future__ import annotations

from abc import ABC, abstractmethod

from branchclaw.models import ArchiveWorkspace, WorkerResult


class WorkspaceRuntimeAdapter(ABC):
    """Abstract Git-first workspace runtime adapter."""

    @abstractmethod
    def repo_root(self) -> str:
        """Return repository root for the adapter."""

    @abstractmethod
    def default_base_ref(self) -> str:
        """Return the default base ref used for new workspaces."""

    @abstractmethod
    def create_workspace(self, run_id: str, stage_id: str, worker_name: str) -> ArchiveWorkspace:
        """Create a fresh isolated workspace for a worker."""

    @abstractmethod
    def checkpoint(self, workspace_path: str, message: str) -> bool:
        """Checkpoint all staged and unstaged changes."""

    @abstractmethod
    def head_sha(self, workspace_path: str) -> str:
        """Return the workspace HEAD SHA."""

    @abstractmethod
    def diff_signature(self, workspace_path: str) -> str:
        """Return a stable signature for the current uncommitted diff state."""

    @abstractmethod
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
        """Capture current Git metadata for a workspace."""

    @abstractmethod
    def restore_workspace(
        self,
        run_id: str,
        archive_id: str,
        snapshot: ArchiveWorkspace,
    ) -> ArchiveWorkspace:
        """Restore a workspace from an archived Git point."""

    @abstractmethod
    def promote_workspace(
        self,
        snapshot: ArchiveWorkspace,
        target_ref: str | None = None,
    ) -> tuple[bool, str]:
        """Merge a workspace branch back to the target ref."""
