"""Docker adapter for production organism isolation.

The Docker daemon is used only by the orchestrator process. Organism containers
never receive its socket or control-plane credentials.
"""
from __future__ import annotations

import json
import os
import subprocess
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .contracts import ExecutionRequest, ObjectiveResult
from .executors import ExecutionError, _tree_digest


def docker_host_path(path: Path) -> Path:
    """Translate control-container run paths into paths visible to the host daemon."""
    container_root = os.environ.get("KADATH_CONTAINER_RUN_ROOT")
    host_root = os.environ.get("KADATH_HOST_RUN_ROOT")
    resolved = path.resolve()
    if not container_root or not host_root: return resolved
    try: relative = resolved.relative_to(Path(container_root).resolve())
    except ValueError: return resolved
    return Path(host_root).resolve() / relative


def make_agent_state_writable(root: Path) -> None:
    root.chmod(0o777)
    for item in root.rglob("*"):
        try: item.chmod(0o777 if item.is_dir() else 0o666)
        except FileNotFoundError: pass


class DockerExecutor:
    def __init__(self, image: str, command: list[str], network: str = "none", environment: dict[str, str] | None = None):
        if not image or not command:
            raise ValueError("docker executor requires image and command")
        self.image, self.command, self.network, self.environment = image, command, network, environment or {}

    def execute(self, request: ExecutionRequest) -> ObjectiveResult:
        result_path = request.state_dir / "result.json"
        if result_path.exists():
            result_path.unlink()
        remaining = (request.deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise ExecutionError(f"epoch deadline already elapsed: {request.agent_id}")
        before = _tree_digest(request.worktree)
        env = {
            **self.environment,
            # The mounted genome, not the immutable package baked into the
            # worker image, is the organism's active framework implementation.
            "PYTHONPATH": "/organism/src",
            "KADATH_RUN_ID": request.run_id, "KADATH_EPOCH": str(request.epoch),
            "KADATH_ATTEMPT_ID": request.attempt_id,
            "KADATH_AGENT_ID": request.agent_id, "KADATH_GENOME": request.genome_hash,
            "KADATH_GOAL": request.goal, "KADATH_CRITERION": request.criterion,
            "KADATH_TASK": request.goal,
            "KADATH_RESULT_PATH": "/state/result.json", "KADATH_STATE_DIR": "/state",
            "KADATH_DEADLINE": request.deadline.isoformat(),
            "KADATH_SHARED_KNOWLEDGE_PATH": "/state/shared-knowledge.json",
            "KADATH_WORKER_BROKER_URL": request.worker_broker_url or "",
            "KADATH_WORKER_TOKEN": request.worker_token or "",
            "KADATH_WORKERS_ENABLED": "1",
            "KADATH_PLAYWRIGHT_MCP_URL": request.browser_url or self.environment.get("KADATH_PLAYWRIGHT_MCP_URL", ""),
            "LITELLM_API_BASE": request.worker_broker_url or self.environment.get("LITELLM_API_BASE", ""),
            "LITELLM_API_KEY": request.worker_token or self.environment.get("LITELLM_API_KEY", ""),
        }
        make_agent_state_writable(request.state_dir)
        maximum_attempts = max(1, int(os.getenv("KADATH_PARENT_CRASH_RESTARTS", "3")) + 1)
        for attempt in range(1, maximum_attempts + 1):
            remaining = (request.deadline - datetime.now(UTC)).total_seconds()
            if remaining <= 0: break
            name = self._container_name(request, "parent")
            args = ["docker", "run", "--name", name, "--label", "kadath.managed=true", "--label", f"kadath.run_id={request.run_id}", "--label", f"kadath.agent_id={request.agent_id}", "--label", "kadath.role=parent", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--pids-limit", "256", "--ulimit", "nofile=1024:1024", "--ulimit", "fsize=268435456:268435456", "--cpus", "1.0", "--memory", "2g", "--memory-swap", "2g", "--user", "65532:65532", "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m", "--network", self.network, "--mount", f"type=bind,src={docker_host_path(request.worktree)},dst=/organism,readonly", "--mount", f"type=bind,src={docker_host_path(request.state_dir)},dst=/state"]
            for key, value in env.items(): args.extend(["--env", f"{key}={value}"])
            args.extend([self.image, *self.command])
            process = subprocess.Popen(args)
            finalize_at = request.deadline.timestamp() - min(30.0, max(2.0, remaining * .10))
            try:
                while process.poll() is None and datetime.now(UTC) < request.deadline:
                    if time.time() >= finalize_at: (request.state_dir / "finalize.requested").touch(exist_ok=True)
                    time.sleep(.25)
                if process.poll() is None: self._terminate(name, process)
                if result_path.is_file():
                    try:
                        maximum_result = int(os.getenv("KADATH_RESULT_BYTES", "5000000"))
                        if result_path.stat().st_size > maximum_result: raise ValueError(f"result exceeds {maximum_result}-byte limit")
                        candidate = json.loads(result_path.read_text())
                        float(candidate["value"])
                        if not isinstance(candidate["evidence"], dict): raise TypeError
                    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                        self._append_crash(request, attempt, process.returncode, "container wrote an invalid result and was restarted")
                        result_path.unlink(missing_ok=True)
                    else: break
                else:
                    self._append_crash(request, attempt, process.returncode, "container exited without a valid result")
                if attempt >= maximum_attempts or (request.deadline - datetime.now(UTC)).total_seconds() < 10:
                    raise ExecutionError(f"isolated container failed for {request.agent_id} after {attempt} attempt(s) ({process.returncode})")
            finally:
                subprocess.run(["docker", "rm", "-f", name], capture_output=True)
                (request.state_dir / "finalize.requested").unlink(missing_ok=True)
        if before != _tree_digest(request.worktree):
            raise ExecutionError(f"active genome changed during epoch: {request.agent_id}")
        try:
            maximum_result = int(os.getenv("KADATH_RESULT_BYTES", "5000000"))
            if result_path.stat().st_size > maximum_result: raise ValueError(f"result exceeds {maximum_result}-byte limit")
            payload = json.loads(result_path.read_text())
            return ObjectiveResult(float(payload["value"]), payload["evidence"])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ExecutionError(f"container did not return a valid result: {request.agent_id}") from exc

    @staticmethod
    def _append_crash(request: ExecutionRequest, attempt: int, returncode: int | None, error: str) -> None:
        with (request.state_dir / "crash-log.jsonl").open("a") as stream:
            stream.write(json.dumps({"at": datetime.now(UTC).isoformat(), "epoch": request.epoch, "epoch_attempt_id": request.attempt_id, "agent_id": request.agent_id, "genome": request.genome_hash, "container_attempt": attempt, "returncode": returncode, "error": error}, sort_keys=True) + "\n")

    def adapt(self, request: ExecutionRequest) -> None:
        before = _tree_digest(request.worktree)
        remaining = (request.deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0: raise ExecutionError(f"adaptation deadline elapsed: {request.agent_id}")
        make_agent_state_writable(request.state_dir)
        name = self._container_name(request, "adapt")
        args = ["docker", "run", "--name", name, "--label", "kadath.managed=true", "--label", f"kadath.run_id={request.run_id}", "--label", f"kadath.agent_id={request.agent_id}", "--label", "kadath.role=adaptation", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--pids-limit", "256", "--ulimit", "nofile=1024:1024", "--ulimit", "fsize=268435456:268435456", "--cpus", "1.0", "--memory", "2g", "--memory-swap", "2g", "--user", "65532:65532", "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m", "--network", self.network, "--mount", f"type=bind,src={docker_host_path(request.worktree)},dst=/organism,readonly", "--mount", f"type=bind,src={docker_host_path(request.state_dir)},dst=/state"]
        env = {**self.environment, "PYTHONPATH": "/organism/src", "KADATH_PHASE": "adaptation", "KADATH_RUN_ID": request.run_id, "KADATH_EPOCH": str(request.epoch), "KADATH_ATTEMPT_ID": request.attempt_id, "KADATH_AGENT_ID": request.agent_id, "KADATH_GOAL": request.goal, "KADATH_STATE_DIR": "/state", "KADATH_ADAPTATION_CONTEXT_PATH": "/state/adaptation-context.json", "KADATH_PLAYWRIGHT_MCP_URL": request.browser_url or "", "LITELLM_API_BASE": request.worker_broker_url or self.environment.get("LITELLM_API_BASE", ""), "LITELLM_API_KEY": request.worker_token or self.environment.get("LITELLM_API_KEY", "")}
        for key, value in env.items(): args.extend(["--env", f"{key}={value}"])
        process = subprocess.Popen([*args, self.image, *self.command])
        try:
            process.wait(timeout=remaining)
            if process.returncode != 0: raise ExecutionError(f"container adaptation failed: {request.agent_id}")
        except subprocess.TimeoutExpired:
            self._terminate(name, process)
            raise ExecutionError(f"container adaptation timed out: {request.agent_id}")
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        if before != _tree_digest(request.worktree):
            raise ExecutionError(f"active genome changed during adaptation: {request.agent_id}")

    @staticmethod
    def _container_name(request: ExecutionRequest, role: str) -> str:
        base = re.sub(r"[^a-zA-Z0-9_.-]", "-", f"kadath-{request.run_id}-{request.epoch}-{request.agent_id}-{role}")
        return f"{base[:54]}-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _terminate(name: str, process: subprocess.Popen) -> None:
        subprocess.run(["docker", "stop", "--time", "5", name], capture_output=True)
        if process.poll() is None:
            subprocess.run(["docker", "kill", name], capture_output=True)
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired: process.kill()
