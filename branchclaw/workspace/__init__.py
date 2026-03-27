"""Workspace runtime adapters for BranchClaw."""

from branchclaw.workspace.base import WorkspaceRuntimeAdapter
from branchclaw.workspace.git import GitWorkspaceRuntimeAdapter

__all__ = ["GitWorkspaceRuntimeAdapter", "WorkspaceRuntimeAdapter"]
