"""Bounded temporary worker orchestration owned by the control kernel."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import json
import os
import subprocess
import shutil
import re
import uuid
import stat
import math
from datetime import UTC, datetime
from threading import Lock
from pathlib import Path
from typing import Callable

from .containers import docker_host_path
from .browsers import start_worker_browser


class WorkerLimitError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkerHandle:
    parent_id: str
    worker_id: str


class WorkerPool:
    """Enforces a five-worker limit per parent independent of agent code."""

    def __init__(self, max_workers_per_parent: int = 5, max_total_workers: int = 500):
        self.limit = max_workers_per_parent
        self._pool = ThreadPoolExecutor(max_workers=max_total_workers, thread_name_prefix="kadath-worker")
        self._active: dict[str, int] = {}
        self._serial: dict[str, int] = {}
        self._lock = Lock()

    def spawn(self, parent_id: str, task: Callable[[], object]) -> tuple[WorkerHandle, Future[object]]:
        return self.spawn_with_handle(parent_id, lambda _handle: task())

    def spawn_with_handle(self, parent_id: str, task: Callable[[WorkerHandle], object]) -> tuple[WorkerHandle, Future[object]]:
        with self._lock:
            active = self._active.get(parent_id, 0)
            if active >= self.limit:
                raise WorkerLimitError(f"{parent_id} already has {self.limit} live workers")
            self._active[parent_id] = active + 1
            self._serial[parent_id] = self._serial.get(parent_id, 0) + 1
            handle = WorkerHandle(parent_id, f"{parent_id}/worker-{self._serial[parent_id]}")

        def wrapped() -> object:
            try:
                return task(handle)
            finally:
                with self._lock:
                    self._active[parent_id] -= 1

        return handle, self._pool.submit(wrapped)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=True)


class DockerWorkerPool(WorkerPool):
    """Runs temporary workers in separate capability-reduced containers.

    The orchestrator calls this API after validating a parent task request. The
    parent has no Docker socket and receives only the worker's result file.
    """

    def spawn_container(
        self, parent_id: str, image: str, command: list[str], parent_repository: Path,
        parent_state: Path, task: dict[str, object], network: str = "none",
        environment: dict[str, str] | None = None, genome_hash: str = "", objective_prompt: str = "", deadline: datetime | None = None,
        model_proxy_url: str = "", model_token: str = "", selected_tools: list[str] | None = None,
        browser_url: str | None = None, browser_profile: Path | None = None, browser_image: str | None = None,
    ) -> tuple[WorkerHandle, Future[dict[str, object]]]:
        worker_root = parent_state / "workers"
        worker_root.mkdir(parents=True, exist_ok=True)

        def run(handle: WorkerHandle) -> dict[str, object]:
            # The handle is assigned before this closure begins; the task file
            # is isolated from the parent state except for returned output.
            worker_dir = worker_root / handle.worker_id.rsplit("-", 1)[-1]
            if worker_dir.exists(): shutil.rmtree(worker_dir)
            worker_dir.mkdir(); worker_dir.chmod(0o777)
            (worker_dir / "task.json").write_text(json.dumps(task, sort_keys=True))
            name = re.sub(r"[^a-zA-Z0-9_.-]", "-", f"kadath-{handle.worker_id}-{uuid.uuid4().hex[:8]}")[-63:]
            run_id = str((environment or {}).get("KADATH_RUN_ID", ""))
            worker_browser_name: str | None = None
            isolated_browser_url: str | None = None
            if "browser" in (selected_tools or []) and browser_profile and browser_image:
                worker_browser_name, isolated_browser_url = start_worker_browser(run_id, parent_id, network, browser_image, browser_profile, worker_dir)
            args = [
                "docker", "run", "--name", name, "--read-only", "--cap-drop", "ALL",
                "--label", "kadath.managed=true", "--label", f"kadath.run_id={run_id}", "--label", f"kadath.agent_id={parent_id}", "--label", "kadath.role=worker",
                "--security-opt", "no-new-privileges:true", "--pids-limit", "128", "--ulimit", "nofile=512:512", "--ulimit", "fsize=134217728:134217728", "--cpus", ".5", "--memory", "1g", "--memory-swap", "1g", "--user", "65532:65532", "--tmpfs", "/tmp:rw,noexec,nosuid,size=128m",
                "--network", network,
                "--mount", f"type=bind,src={docker_host_path(parent_repository)},dst=/organism,readonly",
                "--mount", f"type=bind,src={docker_host_path(worker_dir)},dst=/worker",
                "--env", "KADATH_WORKER_TASK=/worker/task.json",
                "--env", "KADATH_STATE_DIR=/worker",
                "--env", f"KADATH_TASK={json.dumps(task, sort_keys=True)}",
                "--env", f"KADATH_PARENT_ID={parent_id}", "--env", f"KADATH_WORKER_ID={handle.worker_id}",
            ]
            worker_env = {key: value for key, value in (environment or {}).items() if key not in {"LITELLM_API_BASE", "LITELLM_API_KEY"}}
            worker_env.update({"KADATH_AGENT_ID": parent_id, "KADATH_GENOME": genome_hash, "LITELLM_API_BASE": model_proxy_url, "LITELLM_API_KEY": model_token})
            worker_env["KADATH_ENABLED_OPTIONAL_TOOLS"] = ",".join(selected_tools or [])
            if isolated_browser_url: worker_env["KADATH_PLAYWRIGHT_MCP_URL"] = isolated_browser_url
            elif browser_url and "browser" in (selected_tools or []) and not browser_profile: worker_env["KADATH_PLAYWRIGHT_MCP_URL"] = browser_url
            else: worker_env.pop("KADATH_PLAYWRIGHT_MCP_URL", None)
            worker_env["PYTHONPATH"] = "/organism/src"
            for key, value in worker_env.items(): args.extend(["--env", f"{key}={value}"])
            args.extend([image, *command])
            timeout = float(task.get("timeout_seconds", 300))
            if not math.isfinite(timeout) or timeout <= 0: raise ValueError("worker timeout must be finite and positive")
            timeout = min(timeout, float(os.getenv("KADATH_WORKER_MAX_SECONDS", "1800")))
            if deadline is not None: timeout = min(timeout, max(1.0, (deadline - datetime.now(UTC)).total_seconds()))
            process: subprocess.Popen | None = None
            try:
                process = subprocess.Popen(args)
                process.wait(timeout=timeout)
                if process.returncode != 0 and not (worker_dir / "result.json").is_file(): raise RuntimeError(f"worker container exited {process.returncode}")
            except subprocess.TimeoutExpired:
                subprocess.run(["docker", "stop", "--time", "5", name], capture_output=True)
                if process and process.poll() is None: subprocess.run(["docker", "kill", name], capture_output=True)
                raise RuntimeError("worker exceeded its deadline")
            finally:
                subprocess.run(["docker", "rm", "-f", name], capture_output=True)
                if worker_browser_name: subprocess.run(["docker", "rm", "-f", worker_browser_name], capture_output=True)
            result = worker_dir / "result.json"
            if not result.is_file():
                raise RuntimeError("worker did not produce result.json")
            maximum_result = int(os.getenv("KADATH_WORKER_RESULT_BYTES", "2000000"))
            if result.stat().st_size > maximum_result: raise RuntimeError(f"worker result exceeded {maximum_result}-byte limit")
            payload = json.loads(result.read_text())
            if not isinstance(payload, dict): raise RuntimeError("worker result must be a JSON object")
            files: dict[str, str] = {}
            total = 0
            for item in sorted(worker_dir.rglob("*")):
                if item.name in {"task.json", "result.json"}: continue
                if item.relative_to(worker_dir).parts[0] in {"python-deps", "browser-profile", "browser-artifacts"}: continue
                try:
                    if not stat.S_ISREG(item.lstat().st_mode) or item.is_symlink() or not item.resolve().is_relative_to(worker_dir.resolve()): continue
                    if item.stat().st_size > 1_000_000 - total: continue
                    data = item.read_bytes()
                except OSError: continue
                if len(files) >= 32 or total + len(data) > 1_000_000: break
                try: files[str(item.relative_to(worker_dir))] = data.decode()
                except UnicodeDecodeError: continue
                total += len(data)
            if files: payload["files"] = files
            return payload

        return self.spawn_with_handle(parent_id, run)
