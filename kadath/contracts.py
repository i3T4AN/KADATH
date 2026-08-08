"""Stable interfaces between the non-evolving control kernel and integrations."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ExecutionRequest:
    run_id: str
    epoch: int
    agent_id: str
    genome_hash: str
    goal: str
    criterion: str
    objective_prompt: str
    worktree: Path
    state_dir: Path
    deadline: datetime
    shared_knowledge_path: Path | None = None
    adaptation_context_path: Path | None = None
    worker_broker_url: str | None = None
    worker_token: str | None = None
    browser_url: str | None = None
    attempt_id: str = ""


@dataclass(frozen=True)
class ObjectiveResult:
    value: float
    evidence: dict[str, Any]
    tie_break_value: float = 0.0
    outcome: str = "success"
