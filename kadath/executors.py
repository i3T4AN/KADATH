"""Organism execution adapters. Only the kernel writes scores after an adapter returns."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import signal
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import ExecutionRequest, ObjectiveResult


class ExecutionError(RuntimeError):
    pass


class SimulatedExecutor:
    """Deterministic proof adapter, intentionally only for local lifecycle tests."""

    def execute(self, request: ExecutionRequest) -> ObjectiveResult:
        token = f"{request.goal}|{request.genome_hash}|{request.epoch}"
        signal = int(hashlib.sha256(token.encode()).hexdigest()[:12], 16)
        value = round((signal % 10_000) / 100, 2)
        return ObjectiveResult(value, {
            "adapter": "simulated-proof", "criterion": request.criterion,
            "receipt": hashlib.sha256(f"{request.agent_id}:{request.epoch}:{value}".encode()).hexdigest(),
        })


class CommandExecutor:
    """Runs a user-supplied organism command in its dedicated worktree.

    The command receives KADATH_RESULT_PATH and must write a JSON object with
    numeric `value` and an `evidence` object. This keeps objective measurement
    explicit and makes it possible to wire a real smolagents/LiteLLM runtime
    without giving that runtime database or control-plane access.
    """

    def __init__(self, command: list[str], timeout_grace_seconds: int = 10, environment: dict[str, str] | None = None):
        if not command:
            raise ValueError("command executor needs a command")
        self.command = command
        self.timeout_grace_seconds = timeout_grace_seconds
        self.environment = environment or {}

    def execute(self, request: ExecutionRequest) -> ObjectiveResult:
        result_path = request.state_dir / "result.json"
        if result_path.exists():
            result_path.unlink()
        seconds = (request.deadline - datetime.now(UTC)).total_seconds()
        if seconds <= 0:
            raise ExecutionError(f"epoch deadline already elapsed: {request.agent_id}")
        inherited_pythonpath = os.environ.get("PYTHONPATH", "")
        source_pythonpath = str(request.worktree / "src")
        env = {
            **self.environment,
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": source_pythonpath + (os.pathsep + inherited_pythonpath if inherited_pythonpath else ""),
            "KADATH_RUN_ID": request.run_id,
            "KADATH_EPOCH": str(request.epoch),
            "KADATH_ATTEMPT_ID": request.attempt_id,
            "KADATH_AGENT_ID": request.agent_id,
            "KADATH_GENOME": request.genome_hash,
            "KADATH_GOAL": request.goal,
            "KADATH_TASK": request.goal,
            "KADATH_CRITERION": request.criterion,
            "KADATH_RESULT_PATH": str(result_path),
            "KADATH_STATE_DIR": str(request.state_dir),
            "KADATH_DEADLINE": request.deadline.isoformat(),
            "KADATH_SHARED_KNOWLEDGE_PATH": str(request.shared_knowledge_path or ""),
            "KADATH_WORKER_BROKER_URL": request.worker_broker_url or "",
            "KADATH_WORKER_TOKEN": request.worker_token or "",
            "KADATH_PLAYWRIGHT_MCP_URL": request.browser_url or self.environment.get("KADATH_PLAYWRIGHT_MCP_URL", ""),
            "KADATH_WORKERS_ENABLED": "1",
            "LITELLM_API_BASE": request.worker_broker_url or self.environment.get("LITELLM_API_BASE", ""),
            "LITELLM_API_KEY": request.worker_token or self.environment.get("LITELLM_API_KEY", ""),
        }
        before = _tree_digest(request.worktree)
        process = subprocess.Popen(self.command, cwd=request.worktree, env=env, start_new_session=True)
        finalize_at = request.deadline.timestamp() - min(30.0, max(2.0, seconds * .10))
        while process.poll() is None and datetime.now(UTC) < request.deadline:
            if time.time() >= finalize_at: (request.state_dir / "finalize.requested").touch(exist_ok=True)
            time.sleep(.1)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try: process.wait(timeout=self.timeout_grace_seconds)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL); process.wait()
        (request.state_dir / "finalize.requested").unlink(missing_ok=True)
        if process.returncode != 0 and not result_path.is_file():
            raise ExecutionError(f"agent command failed ({process.returncode}): {request.agent_id}")
        if before != _tree_digest(request.worktree):
            raise ExecutionError(f"active genome changed during epoch: {request.agent_id}")
        if not result_path.is_file():
            raise ExecutionError(f"agent did not write result file: {request.agent_id}")
        try:
            maximum_result = int(os.getenv("KADATH_RESULT_BYTES", "5000000"))
            if result_path.stat().st_size > maximum_result: raise ValueError(f"result exceeds {maximum_result}-byte limit")
            payload: dict[str, Any] = json.loads(result_path.read_text())
            value = float(payload["value"])
            evidence = payload["evidence"]
            if not isinstance(evidence, dict):
                raise TypeError("evidence is not an object")
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ExecutionError(f"invalid result file from {request.agent_id}") from exc
        return ObjectiveResult(value, evidence)

    def adapt(self, request: ExecutionRequest) -> None:
        before = _tree_digest(request.worktree)
        remaining = (request.deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0: raise ExecutionError(f"adaptation deadline elapsed: {request.agent_id}")
        inherited_pythonpath = os.environ.get("PYTHONPATH", "")
        source_pythonpath = str(request.worktree / "src")
        env = {**self.environment, "PATH": os.environ.get("PATH", ""), "PYTHONPATH": source_pythonpath + (os.pathsep + inherited_pythonpath if inherited_pythonpath else ""),
               "KADATH_PHASE": "adaptation", "KADATH_RUN_ID": request.run_id, "KADATH_EPOCH": str(request.epoch),
               "KADATH_AGENT_ID": request.agent_id, "KADATH_GOAL": request.goal,
               "KADATH_STATE_DIR": str(request.state_dir),
               "KADATH_ADAPTATION_CONTEXT_PATH": str(request.adaptation_context_path or ""), "KADATH_ATTEMPT_ID": request.attempt_id, "KADATH_WORKER_BROKER_URL": request.worker_broker_url or "", "KADATH_WORKER_TOKEN": request.worker_token or "", "KADATH_PLAYWRIGHT_MCP_URL": request.browser_url or "", "LITELLM_API_BASE": request.worker_broker_url or self.environment.get("LITELLM_API_BASE", ""), "LITELLM_API_KEY": request.worker_token or self.environment.get("LITELLM_API_KEY", "")}
        process = subprocess.Popen(self.command, cwd=request.worktree, env=env, start_new_session=True)
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try: process.wait(timeout=self.timeout_grace_seconds)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL); process.wait()
            raise ExecutionError(f"adaptation command timed out: {request.agent_id}")
        if process.returncode != 0: raise ExecutionError(f"adaptation command failed: {request.agent_id}")
        if before != _tree_digest(request.worktree):
            raise ExecutionError(f"active genome changed during adaptation: {request.agent_id}")
def _tree_digest(root: Path) -> str:
    hasher = hashlib.sha256()
    for item in sorted(root.rglob("*")):
        if ".git" in item.parts or not item.is_file():
            continue
        hasher.update(str(item.relative_to(root)).encode())
        with item.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk: break
                hasher.update(chunk)
    return hasher.hexdigest()
