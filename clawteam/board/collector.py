"""Aggregates team snapshots plus unified event stream data for rendering."""

from __future__ import annotations

import json

from clawteam.events import EventStore, EventTypes
from clawteam.team.manager import TeamManager
from clawteam.team.tasks import TaskStore


class BoardCollector:
    """Aggregates team/task/inbox/event data into plain dicts."""

    def collect_team(self, team_name: str, *, event_limit: int = 200) -> dict:
        """Collect full board data for a single team."""
        config = TeamManager.get_team(team_name)
        if not config:
            raise ValueError(f"Team '{team_name}' not found")

        event_store = EventStore(team_name)
        store = TaskStore(team_name)

        members = []
        for m in config.members:
            inbox_name = f"{m.user}_{m.name}" if m.user else m.name
            entry = {
                "name": m.name,
                "agentId": m.agent_id,
                "agentType": m.agent_type,
                "joinedAt": m.joined_at,
                "inboxCount": self._pending_inbox_count(team_name, inbox_name),
            }
            if m.user:
                entry["user"] = m.user
            members.append(entry)

        all_tasks = store.list_tasks()
        grouped: dict[str, list[dict]] = {
            "pending": [],
            "in_progress": [],
            "completed": [],
            "blocked": [],
        }
        for task in all_tasks:
            grouped[task.status.value].append(
                json.loads(task.model_dump_json(by_alias=True, exclude_none=True))
            )

        summary = {status: len(grouped[status]) for status in grouped}
        summary["total"] = len(all_tasks)

        leader_name = ""
        for member in config.members:
            if member.agent_id == config.lead_agent_id:
                leader_name = member.name
                break

        events = event_store.list_events(limit=event_limit)
        event_dicts = [event.model_dump() for event in events]
        messages = []
        for event in events:
            if event.event_type != EventTypes.MESSAGE_SENT:
                continue
            message = event.payload.get("message", event.payload)
            if isinstance(message, dict):
                messages.append(message)

        cost_data = {}
        try:
            from clawteam.team.costs import CostStore

            cost_store = CostStore(team_name)
            cost_summary = cost_store.summary()
            cost_data = {
                "totalCostCents": cost_summary.total_cost_cents,
                "totalInputTokens": cost_summary.total_input_tokens,
                "totalOutputTokens": cost_summary.total_output_tokens,
                "eventCount": cost_summary.event_count,
                "byAgent": cost_summary.by_agent,
            }
        except Exception:
            pass

        return {
            "team": {
                "name": config.name,
                "description": config.description,
                "leadAgentId": config.lead_agent_id,
                "leaderName": leader_name,
                "createdAt": config.created_at,
                "budgetCents": config.budget_cents,
            },
            "members": members,
            "tasks": grouped,
            "taskSummary": summary,
            "messages": messages,
            "events": event_dicts,
            "cost": cost_data,
        }

    def collect_event_stream(
        self,
        team_name: str,
        *,
        run_id: str = "",
        stage_id: str = "",
        worker_name: str = "",
        correlation_id: str = "",
        limit: int = 200,
    ) -> dict:
        """Collect a filtered event stream without rendering the full team board."""
        if not TeamManager.get_team(team_name):
            raise ValueError(f"Team '{team_name}' not found")
        events = EventStore(team_name).list_events(
            run_id=run_id,
            stage_id=stage_id,
            worker_name=worker_name,
            correlation_id=correlation_id,
            limit=limit,
        )
        return {
            "team": team_name,
            "filters": {
                "run_id": run_id,
                "stage_id": stage_id,
                "worker_name": worker_name,
                "correlation_id": correlation_id,
                "limit": limit,
            },
            "events": [event.model_dump() for event in events],
        }

    def collect_overview(self) -> list[dict]:
        """Collect summary data for all teams."""
        teams_meta = TeamManager.discover_teams()
        result = []
        for meta in teams_meta:
            name = meta["name"]
            try:
                data = self.collect_team(name)
                total_inbox = sum(m["inboxCount"] for m in data["members"])
                leader = data["team"].get("leaderName", "")
                result.append({
                    "name": name,
                    "description": meta.get("description", ""),
                    "leader": leader,
                    "members": len(data["members"]),
                    "tasks": data["taskSummary"]["total"],
                    "pendingMessages": total_inbox,
                })
            except Exception:
                result.append({
                    "name": name,
                    "description": meta.get("description", ""),
                    "leader": "",
                    "members": meta.get("memberCount", 0),
                    "tasks": 0,
                    "pendingMessages": 0,
                })
        return result

    @staticmethod
    def _pending_inbox_count(team_name: str, inbox_name: str) -> int:
        from clawteam.team.models import get_data_dir

        inbox_dir = get_data_dir() / "teams" / team_name / "inboxes" / inbox_name
        return len(list(inbox_dir.glob("msg-*.json"))) if inbox_dir.exists() else 0
