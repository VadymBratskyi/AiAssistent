from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore


@dataclass(frozen=True)
class TeamConfig:
    team_id: str
    project: str | None
    jira_project_key: str | None
    notify_chat_id: int | None
    description: str | None


def load_teams(path: Path) -> dict[str, TeamConfig]:
    """Load teams.yaml. Missing file or empty content returns {}."""
    if not path.exists():
        return {}
    if yaml is None:
        raise RuntimeError(
            "teams.yaml is configured but PyYAML is not installed. "
            "Add `PyYAML` to dependencies."
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = data.get("teams") or {}
    out: dict[str, TeamConfig] = {}
    for team_id, cfg in raw.items():
        cfg = cfg or {}
        chat_raw = cfg.get("notify_chat_id")
        chat_id = int(chat_raw) if chat_raw is not None else None
        out[team_id] = TeamConfig(
            team_id=team_id,
            project=cfg.get("project"),
            jira_project_key=cfg.get("jira_project_key"),
            notify_chat_id=chat_id,
            description=cfg.get("description"),
        )
    return out
