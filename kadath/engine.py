from __future__ import annotations

import hashlib
import fnmatch
import json
import os
import re
import shutil
import stat
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import fcntl
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .artifacts import ArtifactStore, S3ArtifactStore
from .browsers import BrowserFleet
from .containers import DockerExecutor
from .contracts import ExecutionRequest, ObjectiveResult
from .executors import CommandExecutor, SimulatedExecutor
from .gitstore import GitStore
from .mutations import Mutation, MutationError
from .specialists import ArchitectAgent, BirtherAgent, GraderAgent, LiteLLMSpecialistModel, RecordingSpecialistModel, SpecialistModel, TweakerAgent, serialize_architect
from .store import Store
from .worker_broker import ParentWorkerScope, WorkerBroker


def now() -> str:
    return datetime.now(UTC).isoformat()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def hash_file(path: Path) -> tuple[int, str]:
    hasher = hashlib.sha256(); size = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk: break
            size += len(chunk); hasher.update(chunk)
    return size, hasher.hexdigest()


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    encoded = json.dumps(payload, indent=2, sort_keys=True, default=str)
    with temporary.open("w") as stream:
        stream.write(encoded); stream.flush(); os.fsync(stream.fileno())
    temporary.replace(path)


def scoped_regular_bytes(root: Path, item: Path) -> bytes:
    """Read a regular file without following organism-created symlinks."""
    root = root.absolute(); item = item.absolute(); relative = item.relative_to(root)
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        mode = cursor.lstat().st_mode
        if stat.S_ISLNK(mode): raise ValueError(f"symbolic link is not admissible evidence: {relative}")
    if not stat.S_ISREG(item.lstat().st_mode): raise ValueError(f"non-regular evidence file: {relative}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(item, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode): raise ValueError(f"non-regular evidence file: {relative}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream: return stream.read()
    finally:
        os.close(descriptor)


def copy_scoped_regular(root: Path, item: Path, destination: Path, maximum_bytes: int) -> tuple[int, str]:
    """Copy and hash one regular file without following links or loading it into memory."""
    root = root.absolute(); item = item.absolute(); relative = item.relative_to(root)
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if stat.S_ISLNK(cursor.lstat().st_mode): raise ValueError(f"symbolic link is not admissible evidence: {relative}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(item, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode): raise ValueError(f"non-regular evidence file: {relative}")
        if metadata.st_size > maximum_bytes: raise ValueError(f"evidence exceeds remaining {maximum_bytes}-byte quota: {relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        hasher = hashlib.sha256(); size = 0
        with os.fdopen(descriptor, "rb", closefd=False) as source, destination.open("wb") as target:
            while True:
                data = source.read(1024 * 1024)
                if not data: break
                size += len(data); hasher.update(data); target.write(data)
        return size, hasher.hexdigest()
    finally:
        os.close(descriptor)


def scoped_regular_hash(root: Path, item: Path) -> tuple[int, str]:
    """Hash one regular file without following links or loading it into memory."""
    root = root.absolute(); item = item.absolute(); relative = item.relative_to(root)
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if stat.S_ISLNK(cursor.lstat().st_mode): raise ValueError(f"symbolic link is not admissible evidence: {relative}")
    descriptor = os.open(item, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode): raise ValueError(f"non-regular evidence file: {relative}")
        hasher = hashlib.sha256(); size = 0
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            while True:
                data = stream.read(1024 * 1024)
                if not data: break
                size += len(data); hasher.update(data)
        return size, hasher.hexdigest()
    finally:
        os.close(descriptor)


def copy_tree_without_symlinks(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for item in sorted(source.rglob("*")):
        try: mode = item.lstat().st_mode
        except OSError: continue
        if stat.S_ISLNK(mode): continue
        target = destination / item.relative_to(source)
        if stat.S_ISDIR(mode): target.mkdir(parents=True, exist_ok=True)
        elif stat.S_ISREG(mode):
            target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(item, target, follow_symlinks=False)


def make_tree_owner_writable(root: Path) -> None:
    """Undo kernel evidence sealing before a scoped deletion or replacement."""
    if not root.exists(): return
    for directory, names, files in os.walk(root, topdown=False):
        for name in files:
            try: (Path(directory) / name).chmod(0o600)
            except OSError: pass
        for name in names:
            try: (Path(directory) / name).chmod(0o700)
            except OSError: pass
        try: Path(directory).chmod(0o700)
        except OSError: pass


class RunError(RuntimeError):
    pass


class DuplicateGenomeError(RunError):
    pass


class Kernel:
    """Fixed control plane. Organisms receive no writable path into this class."""

    def __init__(self, root: Path = Path(".kadath/runs"), specialist_model: SpecialistModel | None = None):
        self.root = root
        self.specialist_model = specialist_model
        self._stores: dict[str, Store] = {}
        self._artifact_stores: dict[str, ArtifactStore | S3ArtifactStore] = {}

    def _specialist_model(self, run_id: str | None = None) -> SpecialistModel:
        model = self.specialist_model or LiteLLMSpecialistModel()
        if not run_id: return model
        def record(identity: str, system: str, payload: dict[str, Any], response: dict[str, Any] | None, latency: float, error: str | None, telemetry: dict[str, Any]) -> None:
            call_id = uuid.uuid4().hex
            directory = self._run_dir(run_id) / "specialist-model-calls"; directory.mkdir(parents=True, exist_ok=True)
            body = {"call_id": call_id, "identity": identity, "system": system, "request": payload, "response": response, "telemetry": telemetry, "error": error, "latency_seconds": round(latency, 3), "recorded_at": now()}
            encoded = json.dumps(body, indent=2, sort_keys=True)
            runtime_path = self._run_dir(run_id) / "runtime.json"
            runtime = json.loads(runtime_path.read_text()) if runtime_path.is_file() else {}
            secrets_to_remove = [str(value) for key, value in runtime.get("agent_env", {}).items() if len(str(value)) >= 6 and any(word in key.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"))]
            secrets_to_remove.extend(str(value) for key, value in os.environ.items() if key.startswith("KADATH_GRADER_CONNECTOR_") and key.endswith("_TOKEN") and value)
            for secret in secrets_to_remove: encoded = encoded.replace(secret, "[REDACTED]")
            path = directory / f"{call_id}.json"; path.write_text(encoded)
            _trace_size, trace_sha256 = hash_file(path)
            self._store(run_id).add_event(run_id, "specialist_model_call", {"call_id": call_id, "identity": identity, "trace": str(path), "sha256": trace_sha256, "latency_seconds": round(latency, 3), "usage": telemetry.get("usage", {}), "model": telemetry.get("model"), "error": error}, now())
        return RecordingSpecialistModel(model, record)

    def _run_dir(self, run_id: str) -> Path:
        return self.root / run_id

    def _store(self, run_id: str) -> Store:
        if run_id not in self._stores:
            self._stores[run_id] = Store(self._run_dir(run_id) / "state.sqlite")
        return self._stores[run_id]

    def init(self, goal: str, criterion: str, epochs: int, population: int, epoch_seconds: int, executor: str = "simulated", command: list[str] | None = None, agent_env: dict[str, str] | None = None) -> str:
        if not goal.strip():
            raise RunError("goal is required")
        if epochs < 1 or population < 4 or epoch_seconds < 1:
            raise RunError("epochs and epoch length must be positive; population must be at least 4")
        run_id = f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        directory = self._run_dir(run_id); directory.mkdir(parents=True)
        inventory = self._environment_inventory(executor, agent_env or {})
        (directory / "environment-inventory.json").write_text(json.dumps(inventory, indent=2, sort_keys=True))
        store = self._store(run_id)
        try:
            architect = ArchitectAgent(self._specialist_model(run_id)).run(goal, criterion, epoch_seconds, population, epochs, inventory)
        except Exception:
            store.close(); self._stores.pop(run_id, None); self._artifact_stores.pop(run_id, None)
            if directory.exists(): shutil.rmtree(directory)
            raise
        # The CLI criterion is an Architect hint.  Only the returned and
        # explicitly approved measurement becomes the locked criterion.
        criterion = architect.measurement_method
        store.execute(
            "INSERT INTO runs VALUES(?,?,?,?,?,?,?,0,?)",
            (run_id, goal, criterion, epoch_seconds, epochs, population, "awaiting_approval", now()),
        )
        store.add_event(run_id, "run_created", {"status": "awaiting_approval", "population": population, "epochs": epochs}, now())
        (directory / "architect-output.json").write_text(json.dumps(serialize_architect(architect), indent=2, sort_keys=True))
        enabled = set(architect.tool_policy["enabled_capabilities"])
        epoch_tools = ["list_repository", "read_repository_file", "list_workspace", "read_workspace_file", "write_workspace_file", "delete_workspace_file", "run_workspace_command", "save_artifact", "log_activity", "read_shared_knowledge", "rate_knowledge"]
        if "web_search" in enabled: epoch_tools.append("search_web")
        if "web_fetch" in enabled: epoch_tools.append("fetch_web")
        if "browser" in enabled: epoch_tools.append("browser")
        if "workers" in enabled: epoch_tools.extend(["spawn_worker", "worker_result"])
        tools = {"phase": {"epoch": epoch_tools, "adaptation": ["list_repository", "read_repository_file", "list_workspace", "read_workspace_file", "write_workspace_file", "delete_workspace_file", "run_workspace_command", "save_artifact", "log_activity", "read_shared_knowledge", "rate_knowledge", "propose_mutation"]}, "enabled_optional_capabilities": sorted(enabled), "worker_limit_per_parent": 5, "workspace_root": "/state/workspace", "repository_root": "/organism", "repository_writable_during_epoch": False}
        (directory / "tool-manifest.json").write_text(json.dumps(tools, indent=2, sort_keys=True))
        definition = {"version": 3, "goal": goal, "criterion": criterion, "measurement_method": architect.measurement_method, "attribution_method": architect.attribution_method, "evidence_requirements": architect.evidence_requirements, "baseline": architect.baseline, "anti_fraud_checks": architect.anti_fraud_checks, "tie_breaker": architect.tie_breaker, "benchmark": architect.benchmark, "verification_plan": architect.verification_plan, "environment_inventory": inventory, "tool_policy": architect.tool_policy, "locked": False}
        definition["objective_hash"] = digest(json.dumps(definition, sort_keys=True))
        (directory / "objective-definition.json").write_text(json.dumps(definition, indent=2, sort_keys=True))
        (directory / "runtime.json").write_text(json.dumps({"executor": executor, "command": command or [], "model": os.environ.get("KADATH_MODEL", ""), "upstream_model": os.environ.get("KADATH_UPSTREAM_MODEL", ""), "worker_limit": 5, "enabled_optional_capabilities": sorted(enabled), "agent_env": agent_env or {}, "container_limits": {"cpus": 1.0, "memory": "2g", "pids": 256}}, indent=2))
        return run_id

    @staticmethod
    def _environment_inventory(executor: str, agent_env: dict[str, str]) -> dict[str, Any]:
        optional: list[str] = []
        services: list[str] = []
        if os.environ.get("KADATH_SEARXNG_URL"):
            optional.extend(["web_search", "web_fetch"]); services.append("searxng")
        if os.environ.get("KADATH_PLAYWRIGHT_MCP_URL") or os.environ.get("KADATH_BROWSER_IMAGE"):
            optional.append("browser"); services.append("playwright")
        if executor == "docker":
            optional.append("workers"); services.append("docker-worker-broker")
        connectors = sorted({match.group(1).lower().replace("_", "-") for key, value in os.environ.items() for match in [re.fullmatch(r"KADATH_GRADER_CONNECTOR_([A-Z0-9_]+)_URL", key)] if match and str(value).strip()})
        return {
            "executor": executor,
            "available_optional_capabilities": sorted(set(optional)),
            "configured_services": sorted(services),
            "grader_connectors": connectors,
            "seed_framework": {"repository": "vendored-smolagents", "entrypoint": "organism.py", "agent_class": "CodeAgent"},
            "runtime_limits": {"parent_cpus": 1.0, "parent_memory": "2g", "parent_pids": 256, "maximum_live_workers_per_parent": 5, "worker_cpus": 0.5, "worker_memory": "1g"},
            # Names establish what has actually been configured without sending
            # secret values to the Architect model.
            "agent_environment_keys": sorted(agent_env),
            "credential_like_environment_keys": sorted(key for key in agent_env if any(word in key.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"))),
        }

    def approve(self, run_id: str) -> None:
        with self._run_guard(run_id): self._approve_unlocked(run_id)

    def _approve_unlocked(self, run_id: str) -> None:
        store, run = self._store(run_id), self._require_run(run_id)
        if run["status"] != "awaiting_approval":
            raise RunError(f"run is {run['status']}, not awaiting approval")
        self._preflight_runtime(run_id)
        architect = self._architect_output(run_id)
        prompts = BirtherAgent(self._specialist_model(run_id), architect["special_agent_instructions"]["birther"]).first_birth(run["population_size"], architect["objective_prompt"], self._run_dir(run_id) / "specialist-checkpoints" / "generation-1")
        continuation_path = self._run_dir(run_id) / "continuation.json"
        continuation = json.loads(continuation_path.read_text()) if continuation_path.is_file() else None
        self._clear_generation_one(store, run_id, continuation)
        try:
            for number, variation in enumerate(prompts, 1):
                agent_id = f"agent-{number:03d}"
                prompt = f"{continuation['parent_prompt'] if continuation else architect['objective_prompt']}\n{variation}"
                parent_commit = continuation["parent_commit"] if continuation else None
                parent_genome = continuation["parent_genome"] if continuation else None
                parent_agent = continuation["parent_agent"] if continuation else None
                commit, prompt = self._materialize_agent(run_id, agent_id, agent_id, parent_commit, prompt, "continuation birth" if continuation else "generation-1 birth")
                genome = self._create_genome(store, run_id, prompt, parent_genome, 0, commit)
                store.execute("INSERT INTO agents VALUES(?,?,?,?,?,?,?,NULL)", (run_id, agent_id, "active", genome, parent_agent, agent_id, 0))
                store.execute("INSERT INTO lineage VALUES(?,?,?,?,?,?)", (run_id, agent_id, parent_agent, parent_genome, genome, 0))
                if continuation and parent_agent:
                    self._inherit_cross_run(run_id, agent_id, continuation)
        except Exception:
            self._clear_generation_one(store, run_id, continuation)
            raise
        objective_path = self._run_dir(run_id) / "objective-definition.json"
        objective = json.loads(objective_path.read_text()); objective["locked"] = True
        objective["locked_at"] = now(); objective["runtime_hash"] = digest((self._run_dir(run_id) / "runtime.json").read_text())
        objective["tool_manifest_hash"] = digest((self._run_dir(run_id) / "tool-manifest.json").read_text())
        objective["architect_output_hash"] = digest((self._run_dir(run_id) / "architect-output.json").read_text())
        atomic_write_json(objective_path, objective)
        store.execute("UPDATE runs SET status='ready' WHERE id=?", (run_id,))
        store.add_event(run_id, "objective_approved", {"population_born": run["population_size"]}, now())

    def _clear_generation_one(self, store: Store, run_id: str, continuation: dict[str, Any] | None) -> None:
        store.execute("DELETE FROM knowledge WHERE run_id=? AND epoch=0", (run_id,))
        store.execute("DELETE FROM lineage WHERE run_id=? AND epoch=0", (run_id,))
        store.execute("DELETE FROM agents WHERE run_id=?", (run_id,))
        store.execute("DELETE FROM genomes WHERE run_id=? AND created_epoch=0", (run_id,))
        for name in ("agents", "genomes", "git-repository", "seed-bootstrap"):
            path = self._run_dir(run_id) / name
            if path.exists(): shutil.rmtree(path)
        if continuation:
            source = Path(continuation["source_export"]) / "git-repository" if continuation.get("source_export") else self._run_dir(continuation["source_run"]) / "git-repository"
            if source.exists(): shutil.copytree(source, self._run_dir(run_id) / "git-repository")

    def _preflight_runtime(self, run_id: str) -> None:
        runtime_path = self._run_dir(run_id) / "runtime.json"
        runtime = json.loads(runtime_path.read_text())
        objective = json.loads((self._run_dir(run_id) / "objective-definition.json").read_text())
        for connector in objective.get("verification_plan", {}).get("external_connectors", []):
            key = "KADATH_GRADER_CONNECTOR_" + str(connector).upper().replace("-", "_") + "_URL"
            url = os.environ.get(key, "").strip()
            parsed = urllib.parse.urlparse(url)
            if not url or parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise RunError(f"approved benchmark requires a valid Grader connector URL for {connector}")
        if runtime.get("executor") != "docker": return
        image, network = runtime.get("image"), runtime.get("network")
        if not image or not runtime.get("command"): raise RunError("Docker runtime needs an organism image and command")
        try:
            runtime.setdefault("image_requested", image)
            runtime["image"] = subprocess.check_output(["docker", "image", "inspect", image, "--format", "{{.Id}}"], text=True, timeout=30).strip()
        except (OSError, subprocess.SubprocessError) as exc: raise RunError(f"Docker preflight failed: image {image} is unavailable") from exc
        targets = [(f"network {network}", ["docker", "network", "inspect", network])]
        if "browser" in objective.get("tool_policy", {}).get("enabled_capabilities", []) and os.getenv("KADATH_BROWSER_FLEET", "1") != "0":
            browser_image = runtime.get("browser_image") or os.getenv("KADATH_BROWSER_IMAGE", "mcr.microsoft.com/playwright/mcp:latest")
            try:
                runtime["browser_image_requested"] = browser_image
                runtime["browser_image"] = subprocess.check_output(["docker", "image", "inspect", browser_image, "--format", "{{.Id}}"], text=True, timeout=30).strip()
            except (OSError, subprocess.SubprocessError) as exc: raise RunError(f"Docker preflight failed: browser image {browser_image} is unavailable") from exc
        for target, command in targets:
            try: subprocess.run(command, check=True, capture_output=True, timeout=30)
            except (OSError, subprocess.SubprocessError) as exc: raise RunError(f"Docker preflight failed: {target} is unavailable") from exc
        atomic_write_json(runtime_path, runtime)

    def continue_from(self, source_run_id: str, genome_hash: str, epochs: int, population: int | None = None, epoch_seconds: int | None = None) -> str:
        source_run = self._require_run(source_run_id); source_store = self._store(source_run_id)
        genome = source_store.one("SELECT * FROM genomes WHERE run_id=? AND hash=?", (source_run_id, genome_hash))
        if not genome: raise RunError("selected genome does not belong to the source run")
        runtime = json.loads((self._run_dir(source_run_id) / "runtime.json").read_text())
        new_id = self.init(source_run["goal"], source_run["criterion"], epochs, population or source_run["population_size"], epoch_seconds or source_run["epoch_seconds"], runtime["executor"], runtime.get("command"), agent_env=runtime.get("agent_env"))
        new_runtime_path = self._run_dir(new_id) / "runtime.json"
        new_runtime = json.loads(new_runtime_path.read_text())
        for key in ("executor", "command", "image", "image_requested", "browser_image", "browser_image_requested", "network", "worker_limit", "agent_env", "container_limits"):
            if key in runtime: new_runtime[key] = runtime[key]
        new_runtime_path.write_text(json.dumps(new_runtime, indent=2, sort_keys=True))
        shutil.copytree(self._run_dir(source_run_id) / "git-repository", self._run_dir(new_id) / "git-repository")
        manifest = json.loads(genome["manifest_json"])
        parent = source_store.one("SELECT agent_id FROM agents WHERE run_id=? AND active_genome=? ORDER BY agent_id LIMIT 1", (source_run_id, genome_hash))
        (self._run_dir(new_id) / "continuation.json").write_text(json.dumps({"source_run": source_run_id, "parent_genome": genome_hash, "parent_commit": manifest["git_commit"], "parent_prompt": genome["prompt"], "parent_agent": parent["agent_id"] if parent else None, "parent_manifest": manifest}, indent=2, sort_keys=True))
        return new_id

    def continue_from_export(self, export_dir: Path, genome_hash: str, epochs: int, population: int | None = None, epoch_seconds: int | None = None) -> str:
        export_dir = export_dir.resolve(); self._verify_export(export_dir)
        report = json.loads((export_dir / "run-report.json").read_text())
        genomes = json.loads((export_dir / "genome-registry" / "records.json").read_text())
        genome = next((row for row in genomes if row["hash"] == genome_hash), None)
        if not genome: raise RunError("selected genome is not present in the export")
        source_run = report["run"]
        scored = sorted((row for row in report["scores"] if row["genome_hash"] == genome_hash), key=lambda row: (row["epoch"], row["agent_id"]), reverse=True)
        parent_agent = scored[0]["agent_id"] if scored else next((row["agent_id"] for row in report["agents"] if row["active_genome"] == genome_hash), None)
        if not parent_agent: raise RunError("export cannot attribute the selected genome to a parent agent")
        runtime = json.loads((export_dir / "runtime.json").read_text())
        new_id = self.init(source_run["goal"], source_run["criterion"], epochs, population or source_run["population_size"], epoch_seconds or source_run["epoch_seconds"], runtime["executor"], runtime.get("command"), agent_env=runtime.get("agent_env"))
        new_runtime_path = self._run_dir(new_id) / "runtime.json"; new_runtime = json.loads(new_runtime_path.read_text())
        for key in ("executor", "command", "image", "image_requested", "browser_image", "browser_image_requested", "network", "worker_limit", "agent_env", "container_limits"):
            if key in runtime: new_runtime[key] = runtime[key]
        atomic_write_json(new_runtime_path, new_runtime)
        shutil.copytree(export_dir / "git-repository", self._run_dir(new_id) / "git-repository")
        manifest = json.loads(genome["manifest_json"])
        atomic_write_json(self._run_dir(new_id) / "continuation.json", {"source_run": source_run["id"], "source_export": str(export_dir), "parent_genome": genome_hash, "parent_commit": manifest["git_commit"], "parent_prompt": genome["prompt"], "parent_agent": parent_agent, "parent_manifest": manifest})
        return new_id

    @contextmanager
    def _run_guard(self, run_id: str):
        lock_path = self._run_dir(run_id) / "control.lock"; lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock:
            try: fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc: raise RunError(f"another control process is already operating run {run_id}") from exc
            try: yield
            finally: fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def run(self, run_id: str, max_epochs: int | None = None) -> int:
        with self._run_guard(run_id): return self._run_unlocked(run_id, max_epochs)

    def _run_unlocked(self, run_id: str, max_epochs: int | None = None) -> int:
        store, run = self._store(run_id), self._require_run(run_id)
        self._remove_orphan_containers(run_id, store)
        self._verify_locks(run_id)
        if run["status"] not in {"ready", "paused"}:
            raise RunError(f"run is {run['status']}; approve it before execution")
        pending_epoch = run["current_epoch"] + 1
        pending = store.one("SELECT status FROM epochs WHERE run_id=? AND epoch=?", (run_id, pending_epoch))
        if pending and pending["status"] in {"execution_complete", "grading_interrupted"}:
            self._grade_epoch(store, run, pending_epoch)
            run = self._require_run(run_id)
        selection = store.one("SELECT status FROM epochs WHERE run_id=? AND epoch=?", (run_id, run["current_epoch"])) if run["current_epoch"] else None
        if selection and selection["status"] == "selection_pending":
            if run["current_epoch"] >= run["total_epochs"]:
                self._complete_final_epoch(store, run_id, run["current_epoch"])
                return 0
            self._rollback_selection(store, run_id, run["current_epoch"])
            try:
                self._select_and_birth(store, self._require_run(run_id))
            except Exception as exc:
                self._rollback_selection(store, run_id, run["current_epoch"])
                store.execute("UPDATE runs SET status='paused' WHERE id=?", (run_id,))
                store.add_event(run_id, "selection_failure", {"epoch": run["current_epoch"], "error": str(exc)[:4000], "rolled_back": True}, now())
                raise
            run = self._require_run(run_id)
        remaining = run["total_epochs"] - run["current_epoch"]
        count = min(remaining, max_epochs or remaining)
        completed = 0
        for _ in range(count):
            try:
                run = self._require_run(run_id)
                self._execute_epoch(store, run)
                advanced = self._require_run(run_id)
                if advanced["current_epoch"] >= advanced["total_epochs"]:
                    self._complete_final_epoch(store, run_id, advanced["current_epoch"])
                else:
                    self._select_and_birth(store, advanced)
            except Exception as exc:
                # A repeat of `kadath run` restarts this epoch from its locked
                # genome snapshot rather than pretending a partial epoch passed.
                selection = store.one("SELECT status FROM epochs WHERE run_id=? AND epoch=?", (run_id, self._require_run(run_id)["current_epoch"]))
                if selection and selection["status"] == "selection_pending":
                    self._rollback_selection(store, run_id, self._require_run(run_id)["current_epoch"])
                    store.execute("UPDATE runs SET status='paused' WHERE id=?", (run_id,))
                    store.add_event(run_id, "selection_failure", {"epoch": self._require_run(run_id)["current_epoch"], "error": str(exc)[:4000], "rolled_back": True}, now())
                else:
                    self._mark_interrupted(store, run_id)
                    store.add_event(run_id, "epoch_failure", {"epoch": self._require_run(run_id)["current_epoch"] + 1, "error": str(exc)[:4000]}, now())
                raise
            completed += 1
            if self._require_run(run_id)["status"] in {"complete", "failed"}:
                break
            pause = self._run_dir(run_id) / "pause.requested"
            if pause.exists():
                pause.unlink(); store.execute("UPDATE runs SET status='paused' WHERE id=?", (run_id,))
                store.add_event(run_id, "run_paused", {"after_epoch": self._require_run(run_id)["current_epoch"]}, now())
                break
        return completed

    @staticmethod
    def _complete_final_epoch(store: Store, run_id: str, epoch: int) -> None:
        successful = store.one("SELECT COUNT(*) AS count FROM scores WHERE run_id=? AND epoch=? AND outcome='success'", (run_id, epoch))
        status = "complete" if successful and successful["count"] else "failed"
        store.execute("UPDATE epochs SET status='complete' WHERE run_id=? AND epoch=?", (run_id, epoch))
        store.execute("UPDATE runs SET status=? WHERE id=?", (status, run_id))
        store.add_event(run_id, "run_completed" if status == "complete" else "run_failed", {"final_epoch": epoch, "selection_skipped": True, "status": status}, now())

    def pause(self, run_id: str) -> None:
        run = self._require_run(run_id)
        if run["status"] in {"complete", "failed", "awaiting_approval"}:
            raise RunError(f"run is {run['status']} and cannot be paused")
        (self._run_dir(run_id) / "pause.requested").write_text(now())

    def _verify_locks(self, run_id: str) -> None:
        objective = json.loads((self._run_dir(run_id) / "objective-definition.json").read_text())
        if not objective.get("locked"):
            raise RunError("objective is not locked")
        base = {key: objective[key] for key in ("version", "goal", "criterion", "measurement_method", "attribution_method", "evidence_requirements", "baseline", "anti_fraud_checks", "tie_breaker", "benchmark", "verification_plan", "environment_inventory", "tool_policy")}
        base["locked"] = False
        if objective.get("objective_hash") != digest(json.dumps(base, sort_keys=True)):
            raise RunError("locked objective definition was modified")
        runtime_text = (self._run_dir(run_id) / "runtime.json").read_text()
        if objective.get("runtime_hash") != digest(runtime_text):
            raise RunError("locked runtime configuration was modified")
        if objective.get("tool_manifest_hash") != digest((self._run_dir(run_id) / "tool-manifest.json").read_text()):
            raise RunError("locked tool manifest was modified")
        if objective.get("architect_output_hash") != digest((self._run_dir(run_id) / "architect-output.json").read_text()):
            raise RunError("locked Architect output was modified")
        runtime = json.loads(runtime_text)
        for environment_key, runtime_key in (("KADATH_MODEL", "model"), ("KADATH_UPSTREAM_MODEL", "upstream_model")):
            locked_model = runtime.get(runtime_key)
            if locked_model and os.environ.get(environment_key) != locked_model:
                raise RunError(f"locked model configuration changed: {environment_key}")
        if runtime.get("executor") == "docker":
            for role, image in (("organism", runtime.get("image")), ("browser", runtime.get("browser_image"))):
                if not image: continue
                try: subprocess.run(["docker", "image", "inspect", image], check=True, capture_output=True, timeout=30)
                except (OSError, subprocess.SubprocessError) as exc: raise RunError(f"locked {role} image is no longer available: {image}") from exc
            enabled = set(runtime.get("enabled_optional_capabilities", []))
            if {"web_search", "web_fetch"} & enabled and not os.environ.get("KADATH_SEARXNG_URL"):
                raise RunError("the locked tool policy requires SearXNG but KADATH_SEARXNG_URL is unavailable")
            if "browser" in enabled and not (runtime.get("browser_image") or os.environ.get("KADATH_PLAYWRIGHT_MCP_URL") or os.environ.get("KADATH_BROWSER_IMAGE")):
                raise RunError("the locked tool policy requires Playwright but KADATH_PLAYWRIGHT_MCP_URL is unavailable")
        for connector in objective.get("verification_plan", {}).get("external_connectors", []):
            key = "KADATH_GRADER_CONNECTOR_" + str(connector).upper().replace("-", "_") + "_URL"
            if not os.environ.get(key):
                raise RunError(f"the locked benchmark requires unavailable Grader connector {connector}")

    def _execute_epoch(self, store: Store, run: dict[str, Any]) -> None:
        epoch, run_id = run["current_epoch"] + 1, run["id"]
        attempt_id = f"attempt-{uuid.uuid4().hex}"
        previous = store.one("SELECT status FROM epochs WHERE run_id=? AND epoch=?", (run_id, epoch))
        if previous and previous["status"] in {"running", "interrupted"}:
            self._restore_epoch_state(run_id, epoch)
            # Discard partial measurements and activity from the failed attempt.
            # The immutable worktrees/genomes remain the restart source of truth.
            store.execute("DELETE FROM scores WHERE run_id=? AND epoch=?", (run_id, epoch))
            store.execute("DELETE FROM knowledge WHERE run_id=? AND epoch=?", (run_id, epoch))
            store.execute("DELETE FROM agent_attempts WHERE run_id=? AND epoch=?", (run_id, epoch))
        else:
            self._snapshot_epoch_state(run_id, epoch)
        active = store.rows("SELECT * FROM agents WHERE run_id=? AND status='active' ORDER BY agent_id", (run_id,))
        for agent in active:
            genome = store.one("SELECT * FROM genomes WHERE run_id=? AND hash=?", (run_id, agent["active_genome"]))
            if not genome: raise RunError(f"active genome is missing for {agent['agent_id']}")
            self._verify_agent_genome(run_id, agent, genome)
        for agent in active:
            state = self._agent_dir(run_id, agent["agent_id"]) / "state"
            for name in ("result.json", "activity.jsonl", "step-trace.jsonl", "step-trace.cursor", "worker-events.jsonl", "crash-log.jsonl", "mutation.json", "candidate-output.json", "finalize.requested"):
                (state / name).unlink(missing_ok=True)
            if (state / "workers").exists(): shutil.rmtree(state / "workers")
        if len(active) != run["population_size"]:
            raise RunError("population invariant failed before epoch")
        runtime = json.loads((self._run_dir(run_id) / "runtime.json").read_text())
        runtime_environment = self._runtime_environment(runtime)
        worker_tools = tuple(sorted(set(runtime.get("enabled_optional_capabilities", [])) - {"workers"}))
        browser_fleet: BrowserFleet | None = None; browser_urls: dict[str, str] = {}
        if runtime.get("executor") == "docker" and "browser" in runtime.get("enabled_optional_capabilities", []) and os.getenv("KADATH_BROWSER_FLEET", "1") != "0":
            browser_fleet = BrowserFleet(runtime.get("network", "none"), runtime.get("browser_image"))
            browser_urls = browser_fleet.start(run_id, [(agent["agent_id"], self._agent_dir(run_id, agent["agent_id"]) / "state") for agent in active])
        started = datetime.now(UTC)
        deadline = started + timedelta(seconds=run["epoch_seconds"])
        try:
            store.execute(
                "INSERT INTO epochs(run_id,epoch,status,started_at,completed_at) VALUES(?,?,?,?,NULL) "
                "ON CONFLICT(run_id,epoch) DO UPDATE SET status=excluded.status, started_at=excluded.started_at, completed_at=NULL",
                (run_id, epoch, "running", started.isoformat()),
            )
            store.add_event(run_id, "epoch_started", {"epoch": epoch, "attempt_id": attempt_id, "genomes_locked": True}, now())
        except Exception:
            if browser_fleet: browser_fleet.stop()
            raise
        broker: WorkerBroker | None = None
        broker_url = ""; worker_tokens: dict[str, str] = {}
        if runtime.get("executor") == "docker" and runtime.get("image") and runtime.get("command"):
            scopes = []
            for agent in active:
                token = WorkerBroker.token(); worker_tokens[agent["agent_id"]] = token
                genome = store.one("SELECT * FROM genomes WHERE hash=?", (agent["active_genome"],))
                scopes.append(ParentWorkerScope(agent["agent_id"], token, self._agent_dir(run_id, agent["agent_id"]) / "repository", self._agent_dir(run_id, agent["agent_id"]) / "state", genome["hash"], genome["prompt"], deadline, worker_tools, browser_urls.get(agent["agent_id"]) or runtime_environment.get("KADATH_PLAYWRIGHT_MCP_URL"), self._agent_dir(run_id, agent["agent_id"]) / "state" / "browser-profile", runtime.get("browser_image")))
            broker = WorkerBroker(scopes, runtime["image"], runtime["command"], runtime.get("network", "none"), runtime_environment, os.getenv("KADATH_BROKER_ADVERTISE_HOST"), store, run_id, epoch, self._run_dir(run_id) / "model-calls", attempt_id)
            try: broker_url = broker.start()
            except Exception:
                if browser_fleet: browser_fleet.stop()
                raise
        requests: list[tuple[dict[str, Any], dict[str, Any], ExecutionRequest]] = []
        for agent in active:
            genome = store.one("SELECT * FROM genomes WHERE hash=?", (agent["active_genome"],))
            state_dir = self._agent_dir(run_id, agent["agent_id"]) / "state"
            shared_path = state_dir / "shared-knowledge.json"
            shared_path.write_text(json.dumps({"records": self._agent_knowledge(store, run_id, agent["agent_id"])}, indent=2, sort_keys=True))
            # Tweaker output is Birther-only. Never leave it in a parent state
            # mount, including residue from an older interrupted run.
            (state_dir / "tweaker-report.json").unlink(missing_ok=True)
            requests.append((agent, genome, ExecutionRequest(
                run_id=run_id, epoch=epoch, agent_id=agent["agent_id"], genome_hash=genome["hash"],
                goal=run["goal"], criterion=run["criterion"], objective_prompt=genome["prompt"],
                worktree=self._agent_dir(run_id, agent["agent_id"]) / "repository",
                state_dir=state_dir,
                deadline=deadline,
                shared_knowledge_path=shared_path,
                worker_broker_url=broker_url or None,
                worker_token=worker_tokens.get(agent["agent_id"]),
                browser_url=browser_urls.get(agent["agent_id"]) or runtime_environment.get("KADATH_PLAYWRIGHT_MCP_URL"),
                attempt_id=attempt_id,
            )))
        executor = self._executor(run_id)
        for agent, genome, request in requests:
            checkpoint = {"epoch": epoch, "attempt_id": attempt_id, "genome": genome["hash"], "status": "running", "started_at": started.isoformat()}
            (request.state_dir / "checkpoint.json").write_text(json.dumps(checkpoint, indent=2))
            store.execute(
                "INSERT INTO agent_attempts(run_id,epoch,agent_id,attempt_id,status,started_at,completed_at,error) VALUES(?,?,?,?,?,?,NULL,NULL) "
                "ON CONFLICT(run_id,epoch,agent_id) DO UPDATE SET attempt_id=excluded.attempt_id,status=excluded.status, started_at=excluded.started_at, completed_at=NULL, error=NULL",
                (run_id, epoch, agent["agent_id"], attempt_id, "running", started.isoformat()),
            )
        results: list[tuple[dict[str, Any], dict[str, Any], ExecutionRequest, Any | None, str | None]] = []
        try:
            with ThreadPoolExecutor(max_workers=run["population_size"]) as pool:
                futures = {pool.submit(executor.execute, request): (agent, genome, request) for agent, genome, request in requests}
                for future in as_completed(futures):
                    agent, genome, request = futures[future]
                    try:
                        raw_result = future.result()
                    except Exception as exc:
                        store.execute("UPDATE agent_attempts SET status='failed', completed_at=?, error=? WHERE run_id=? AND epoch=? AND agent_id=?", (now(), str(exc)[:2000], run_id, epoch, agent["agent_id"]))
                        results.append((agent, genome, request, None, str(exc)))
                    else:
                        store.execute("UPDATE agent_attempts SET status='completed', completed_at=? WHERE run_id=? AND epoch=? AND agent_id=?", (now(), run_id, epoch, agent["agent_id"]))
                        results.append((agent, genome, request, raw_result, None))
        finally:
            if broker: broker.stop()
            if browser_fleet: browser_fleet.stop()
        objective = json.loads((self._run_dir(run_id) / "objective-definition.json").read_text())
        for agent, genome, request, raw_result, execution_error in results:
            self._collect_activity(store, run_id, epoch, agent["agent_id"], request.state_dir, request.attempt_id)
            self._freeze_attempt(request, raw_result, execution_error, objective["objective_hash"], store)
        store.execute("UPDATE epochs SET status='execution_complete', completed_at=? WHERE run_id=? AND epoch=?", (now(), run_id, epoch))
        store.add_event(run_id, "epoch_execution_complete", {"epoch": epoch, "attempt_id": attempt_id, "frozen_attempts": len(results)}, now())
        self._grade_epoch(store, run, epoch)

    def _freeze_attempt(self, request: ExecutionRequest, raw_result: Any | None, error: str | None, objective_hash: str, store: Store | None = None) -> dict[str, Any]:
        frozen_root = self._run_dir(request.run_id) / "frozen-attempts" / f"epoch-{request.epoch:04d}" / request.agent_id
        if frozen_root.exists(): shutil.rmtree(frozen_root)
        frozen_root.mkdir(parents=True)
        files: list[dict[str, Any]] = []
        aggregate_limit = int(os.getenv("KADATH_FROZEN_ATTEMPT_BYTES", str(512 * 1024 * 1024)))
        aggregate = 0
        source_roots = [(name, request.state_dir / name) for name in ("workspace", "artifacts", "browser-artifacts", "workers")]
        # Model traffic is captured by the kernel broker, outside editable
        # organism code, and frozen with the attempt for guaranteed activity review.
        source_roots.append(("model-calls", self._run_dir(request.run_id) / "model-calls" / f"epoch-{request.epoch:04d}" / request.attempt_id / request.agent_id))
        for relative_root, source_root in source_roots:
            if not source_root.exists(): continue
            for item in sorted(source_root.rglob("*")):
                relative_parts = item.relative_to(source_root).parts
                if relative_root == "workers" and any(part in {"browser-profile", "python-deps"} for part in relative_parts):
                    continue
                try:
                    mode = item.lstat().st_mode
                    if stat.S_ISDIR(mode): continue
                    destination = frozen_root / relative_root / item.relative_to(source_root)
                    size, checksum = copy_scoped_regular(source_root, item, destination, aggregate_limit - aggregate)
                except (OSError, ValueError) as exc:
                    error = error or f"unsafe evidence was rejected: {exc}"
                    continue
                aggregate += size
                record: dict[str, Any] = {"path": f"{relative_root}/{item.relative_to(source_root)}", "size": size, "sha256": checksum}
                if size <= 100_000:
                    try: record["text"] = destination.read_text()
                    except UnicodeDecodeError: pass
                record["artifact"] = self._artifact_store(request.run_id).put_file(request.run_id, request.epoch, request.agent_id, "frozen/" + record["path"], destination)
                files.append(record)
        candidate = None
        candidate_path = request.state_dir / "candidate-output.json"
        if candidate_path.is_file():
            try:
                if candidate_path.stat().st_size > int(os.getenv("KADATH_RESULT_BYTES", "5000000")):
                    raise ValueError("candidate output exceeded its size limit")
                candidate = json.loads(candidate_path.read_text())
            except (OSError, ValueError, json.JSONDecodeError): candidate = {"invalid": True}
        activity = []
        trace = request.state_dir / "activity.jsonl"
        if trace.is_file():
            for line in trace.read_text().splitlines()[-100:]:
                try: activity.append(json.loads(line))
                except json.JSONDecodeError: pass
        if store:
            activity = [{"kind": row["kind"], "payload": json.loads(row["payload_json"]), "published_at": str(row["published_at"])} for row in store.rows("SELECT kind,payload_json,published_at FROM knowledge WHERE run_id=? AND epoch=? AND agent_id=? AND kind IN ('activity','worker_completion','worker_activity','runtime_crash') ORDER BY id", (request.run_id, request.epoch, request.agent_id))]
        model_trace_refs = ["file:" + str(record["path"]) for record in files if str(record["path"]).startswith("model-calls/")]
        if model_trace_refs:
            payload = {"summary": f"Kernel captured {len(model_trace_refs)} model interaction trace(s) for independent activity review.", "outcome": "trace frozen", "evidence_refs": model_trace_refs, "genome": request.genome_hash, "attempt": request.attempt_id, "visibility": "shared", "source": "kernel"}
            if store: store.add_knowledge(request.run_id, request.epoch, request.agent_id, "kernel_activity_trace", payload, now())
            activity.append({"kind": "kernel_activity_trace", "payload": payload, "published_at": now()})
        tool_trace = []
        trace_path = request.state_dir / "step-trace.jsonl"
        if trace_path.is_file():
            for line in trace_path.read_text().splitlines()[-5000:]:
                try: tool_trace.append(json.loads(line))
                except json.JSONDecodeError: pass
        manifest = {"run_id": request.run_id, "epoch": request.epoch, "attempt_id": request.attempt_id, "agent_id": request.agent_id, "genome": request.genome_hash, "objective_hash": objective_hash, "frozen_at": now(), "execution_error": error, "candidate": candidate, "organism_evidence": getattr(raw_result, "evidence", None), "activity": activity, "tool_trace": tool_trace, "files": files, "aggregate_evidence_bytes": aggregate, "aggregate_evidence_limit": aggregate_limit}
        path = frozen_root / "attempt.json"; path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
        manifest["manifest_path"] = str(path)
        return manifest

    def _grade_epoch(self, store: Store, run: dict[str, Any], epoch: int) -> None:
        """Grade an immutable execution boundary; failures here are regradeable infrastructure failures."""
        run_id = run["id"]
        objective = json.loads((self._run_dir(run_id) / "objective-definition.json").read_text())
        architect = self._architect_output(run_id)
        active = store.rows("SELECT * FROM agents WHERE run_id=? AND status='active' ORDER BY agent_id", (run_id,))

        def grade(agent: dict[str, Any]):
            genome = store.one("SELECT * FROM genomes WHERE hash=?", (agent["active_genome"],))
            if not genome: raise RunError(f"active genome is missing for {agent['agent_id']}")
            path = self._run_dir(run_id) / "frozen-attempts" / f"epoch-{epoch:04d}" / agent["agent_id"] / "attempt.json"
            self._verify_frozen_attempt_seal(path.parent)
            frozen = json.loads(path.read_text()); frozen["manifest_path"] = str(path)
            request = ExecutionRequest(run_id, epoch, agent["agent_id"], genome["hash"], run["goal"], run["criterion"], genome["prompt"], self._agent_dir(run_id, agent["agent_id"]) / "repository", self._agent_dir(run_id, agent["agent_id"]) / "state", datetime.now(UTC), attempt_id=str(frozen.get("attempt_id", "")))
            self._verify_agent_genome(run_id, agent, genome)
            verification = self._kernel_verify_attempt(request, frozen, objective)
            forced_failure = frozen.get("execution_error") or ("one or more machine-checkable required outputs are missing" if not verification["checks"]["required_output_presence"] else None)
            checkpoints = self._run_dir(run_id) / "grader-checkpoints" / f"epoch-{epoch:04d}" / agent["agent_id"]
            final_checkpoint = checkpoints / "final-result.json"
            _manifest_size, manifest_sha256 = hash_file(path)
            fingerprint = digest(json.dumps({"attempt_sha256": manifest_sha256, "objective": objective, "verification": verification, "genome": genome["hash"]}, sort_keys=True, default=str))
            result = None
            if final_checkpoint.is_file():
                try:
                    saved = json.loads(final_checkpoint.read_text())
                    candidate = saved.get("result", {})
                    if saved.get("input_sha256") == fingerprint and isinstance(candidate.get("evidence"), dict) and candidate.get("outcome") in {"success", "failed"}:
                        result = ObjectiveResult(float(candidate["value"]), candidate["evidence"], float(candidate["tie_break_value"]), candidate["outcome"])
                except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError): pass
            if result is None:
                result = GraderAgent(self._specialist_model(run_id), architect["special_agent_instructions"]["grader"]).run(request, frozen, objective, verification, checkpoints, str(forced_failure) if forced_failure else None)
                atomic_write_json(final_checkpoint, {"input_sha256": fingerprint, "result": {"value": result.value, "tie_break_value": result.tie_break_value, "outcome": result.outcome, "evidence": result.evidence}})
            return agent, genome, frozen, result

        try:
            graded_rows = []
            with ThreadPoolExecutor(max_workers=min(16, run["population_size"])) as pool:
                futures = [pool.submit(grade, agent) for agent in active]
                for future in as_completed(futures): graded_rows.append(future.result())
        except Exception as exc:
            store.execute("UPDATE epochs SET status='grading_interrupted' WHERE run_id=? AND epoch=?", (run_id, epoch))
            store.execute("UPDATE runs SET status='paused' WHERE id=?", (run_id,))
            store.add_event(run_id, "grading_infrastructure_failure", {"epoch": epoch, "error": str(exc)[:4000], "agents_not_penalized": True}, now())
            raise RunError(f"grading paused without penalizing agents: {exc}") from exc

        # Only complete, regradeable attempts are sealed. A partial provider
        # outage never creates a partially accepted leaderboard.
        for agent, _genome, _frozen, _result in graded_rows:
            self._seal_frozen_attempt(store, run_id, epoch, agent["agent_id"])

        # Persist only after every Grader completed, so a partial provider outage
        # cannot create a half-ranked population.
        store.execute("DELETE FROM scores WHERE run_id=? AND epoch=?", (run_id, epoch))
        store.execute("DELETE FROM knowledge WHERE run_id=? AND epoch=? AND kind='epoch_report'", (run_id, epoch))
        for agent, genome, frozen, result in graded_rows:
            if not frozen.get("execution_error"):
                store.execute("UPDATE agent_attempts SET status='completed',error=NULL WHERE run_id=? AND epoch=? AND agent_id=?", (run_id, epoch, agent["agent_id"]))
            if result.outcome != "success":
                reason = str(result.evidence.get("failure") or result.evidence.get("grader_assessment") or "failed benchmark evidence")[:2000]
                store.execute("UPDATE agent_attempts SET status='failed', error=? WHERE run_id=? AND epoch=? AND agent_id=?", (reason, run_id, epoch, agent["agent_id"]))
            artifact = self._artifact_store(run_id).put_json(run_id, epoch, agent["agent_id"], "grader-evidence.json", result.evidence)
            evidence = {**result.evidence, "artifact": artifact, "frozen_attempt": frozen["manifest_path"]}
            store.execute("INSERT INTO scores(run_id,epoch,agent_id,genome_hash,value,tie_break_value,outcome,evidence_json) VALUES(?,?,?,?,?,?,?,?)", (run_id, epoch, agent["agent_id"], genome["hash"], result.value, result.tie_break_value, result.outcome, json.dumps(evidence, sort_keys=True)))
            store.add_knowledge(run_id, epoch, agent["agent_id"], "epoch_report", {"summary": "Completed an independently verified epoch objective attempt." if result.outcome == "success" else "Epoch objective evidence failed verification.", "score": result.value, "tie_break_score": result.tie_break_value, "outcome": result.outcome, "genome": genome["hash"], "evidence": evidence, "visibility": "shared"}, now())
            activity_summary = result.evidence.get("activity_summary")
            if isinstance(activity_summary, dict):
                store.add_knowledge(run_id, epoch, agent["agent_id"], "kernel_activity", {"summary": "Kernel-reviewed high-level epoch activity.", "investigated": activity_summary.get("investigated", []), "actions": activity_summary.get("actions", []), "outcomes": activity_summary.get("outcomes", []), "genome": genome["hash"], "attempt": str(frozen.get("attempt_id") or f"epoch-{epoch}"), "visibility": "shared", "source": "kernel"}, now())
            state = self._agent_dir(run_id, agent["agent_id"]) / "state"
            (state / "checkpoint.json").write_text(json.dumps({"epoch": epoch, "genome": genome["hash"], "status": result.outcome, "completed_at": now()}, indent=2))
        ranked = store.rows("SELECT agent_id FROM scores WHERE run_id=? AND epoch=? ORDER BY CASE outcome WHEN 'success' THEN 0 ELSE 1 END,value DESC,tie_break_value DESC,agent_id", (run_id, epoch))
        for rank, row in enumerate(ranked, 1): store.execute("UPDATE agents SET last_rank=? WHERE run_id=? AND agent_id=?", (rank, run_id, row["agent_id"]))
        store.execute("UPDATE epochs SET status='selection_pending', completed_at=? WHERE run_id=? AND epoch=?", (now(), run_id, epoch))
        store.execute("UPDATE runs SET current_epoch=?, status='paused' WHERE id=?", (epoch, run_id))
        store.add_event(run_id, "epoch_graded", {"epoch": epoch, "scores": len(ranked), "kernel_calculated_scores": True}, now())
        snapshot = self._run_dir(run_id) / "epoch-state-snapshots" / f"epoch-{epoch:04d}"
        if snapshot.exists(): shutil.rmtree(snapshot)

    def _kernel_verify_attempt(self, request: ExecutionRequest, frozen: dict[str, Any], objective: dict[str, Any]) -> dict[str, Any]:
        expected = {"run_id": request.run_id, "epoch": request.epoch, "agent_id": request.agent_id, "genome": request.genome_hash, "objective_hash": objective["objective_hash"]}
        if request.attempt_id: expected["attempt_id"] = request.attempt_id
        identity_ok = all(frozen.get(key) == value for key, value in expected.items())
        if not identity_ok: raise RunError(f"frozen attempt identity mismatch for {request.agent_id}")
        root = Path(frozen["manifest_path"]).parent
        valid_refs: list[str] = []
        integrity: list[dict[str, Any]] = []
        for record in frozen.get("files", []):
            relative = str(record["path"]); relative_path = Path(relative)
            if relative_path.is_absolute() or ".." in relative_path.parts: raise RunError(f"unsafe frozen evidence path: {request.agent_id}/{relative}")
            path = root / relative_path
            try: size, checksum = scoped_regular_hash(root, path)
            except (OSError, ValueError): size, checksum = -1, ""
            valid = size == int(record["size"]) and checksum == record["sha256"]
            integrity.append({"path": relative, "valid": valid})
            if not valid: raise RunError(f"frozen evidence integrity failure: {request.agent_id}/{relative}")
            valid_refs.append("file:" + relative)
        if frozen.get("candidate") is not None: valid_refs.append("candidate")
        if frozen.get("organism_evidence") is not None: valid_refs.append("organism_evidence")
        valid_refs.extend(f"activity:{index}" for index, _ in enumerate(frozen.get("activity", [])))
        valid_refs.extend(f"tool_trace:{index}" for index, _ in enumerate(frozen.get("tool_trace", [])))
        external = self._external_measurements(request, objective, root)
        external_path = root / "external-measurements.json"
        if external_path.is_file():
            _external_size, external_sha = scoped_regular_hash(root, external_path)
            if frozen.get("external_measurements_sha256") and frozen["external_measurements_sha256"] != external_sha:
                raise RunError(f"frozen external measurement integrity failure: {request.agent_id}")
            if not frozen.get("external_measurements_sha256"):
                frozen["external_measurements_sha256"] = external_sha
                manifest_path = Path(frozen["manifest_path"]); persisted = {key: value for key, value in frozen.items() if key != "manifest_path"}
                atomic_write_json(manifest_path, persisted)
        valid_refs.extend("external:" + name for name in external)
        required_checks = []
        for required in objective["benchmark"].get("required_outputs", []):
            reference = str(required["evidence_ref"])
            if reference == "activity": present = bool(frozen.get("activity"))
            elif "*" in reference or "?" in reference or "[" in reference: present = any(fnmatch.fnmatch(candidate, reference) for candidate in valid_refs)
            else: present = reference in valid_refs
            required_checks.append({"description": required["description"], "evidence_ref": reference, "present": present})
        output_present = bool(required_checks) and all(item["present"] for item in required_checks)
        return {"checks": {"identity_attribution": True, "artifact_integrity": True, "required_output_presence": output_present}, "required_outputs": required_checks, "valid_evidence_refs": valid_refs, "file_integrity": integrity, "independent_external_measurements": external}

    @staticmethod
    def _frozen_seal_records(root: Path) -> list[dict[str, Any]]:
        records = []
        for path in sorted(root.rglob("*")):
            if path == root / "seal.json": continue
            if path.is_symlink(): raise RunError(f"frozen attempt contains symbolic link: {path.relative_to(root)}")
            if not path.is_file(): continue
            size, checksum = scoped_regular_hash(root, path)
            records.append({"path": str(path.relative_to(root)), "size": size, "sha256": checksum})
        return records

    def _verify_frozen_attempt_seal(self, root: Path) -> None:
        seal_path = root / "seal.json"
        if not seal_path.is_file(): return
        seal = json.loads(seal_path.read_text())
        records = self._frozen_seal_records(root)
        fingerprint = digest(json.dumps(records, sort_keys=True, separators=(",", ":")))
        if seal.get("format") != 1 or seal.get("tree_sha256") != fingerprint or seal.get("files") != records:
            raise RunError(f"frozen attempt seal failed: {root}")

    def _seal_frozen_attempt(self, store: Store, run_id: str, epoch: int, agent_id: str) -> None:
        root = self._run_dir(run_id) / "frozen-attempts" / f"epoch-{epoch:04d}" / agent_id
        seal_path = root / "seal.json"
        if seal_path.exists():
            self._verify_frozen_attempt_seal(root)
            return
        records = self._frozen_seal_records(root)
        tree_hash = digest(json.dumps(records, sort_keys=True, separators=(",", ":")))
        atomic_write_json(seal_path, {"format": 1, "run_id": run_id, "epoch": epoch, "agent_id": agent_id, "sealed_at": now(), "tree_sha256": tree_hash, "files": records})
        for path in root.rglob("*"):
            try: path.chmod(0o500 if path.is_dir() else 0o400)
            except OSError: pass
        try: root.chmod(0o500)
        except OSError: pass
        store.add_event(run_id, "frozen_attempt_sealed", {"epoch": epoch, "agent_id": agent_id, "tree_sha256": tree_hash, "files": len(records)}, now())

    def _external_measurements(self, request: ExecutionRequest, objective: dict[str, Any], frozen_root: Path | None = None) -> dict[str, Any]:
        cache = frozen_root / "external-measurements.json" if frozen_root else None
        if cache and cache.is_file():
            envelope = json.loads(scoped_regular_bytes(frozen_root, cache))
            if envelope.get("objective_hash") != objective.get("objective_hash"): raise RunError("frozen external measurement objective mismatch")
            measurements = envelope.get("measurements")
            if not isinstance(measurements, dict): raise RunError("frozen external measurements are invalid")
            for connector, measurement in measurements.items(): self._validate_external_attribution(request, connector, measurement)
            return measurements
        measurements: dict[str, Any] = {}
        for connector in objective.get("verification_plan", {}).get("external_connectors", []):
            prefix = "KADATH_GRADER_CONNECTOR_" + str(connector).upper().replace("-", "_")
            url, token = os.environ.get(prefix + "_URL"), os.environ.get(prefix + "_TOKEN", "")
            if not url: raise RunError(f"external Grader connector disappeared: {connector}")
            payload = json.dumps({"run_id": request.run_id, "epoch": request.epoch, "agent_id": request.agent_id, "genome": request.genome_hash, "goal": request.goal, "benchmark": objective["benchmark"]}).encode()
            headers = {"Content-Type": "application/json"}
            if token: headers["Authorization"] = "Bearer " + token
            last_error: Exception | None = None
            for attempt in range(4):
                try:
                    with urllib.request.urlopen(urllib.request.Request(url, data=payload, headers=headers, method="POST"), timeout=60) as response:
                        measurement = json.loads(response.read(5_000_000))
                    self._validate_external_attribution(request, str(connector), measurement)
                    measurements[str(connector)] = measurement
                    break
                except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
                    last_error = exc
                    if attempt < 3: time.sleep(2 ** attempt)
            else: raise RunError(f"external Grader connector {connector} failed: {last_error}")
        if cache:
            atomic_write_json(cache, {"objective_hash": objective.get("objective_hash"), "frozen_at": now(), "measurements": measurements})
        return measurements

    @staticmethod
    def _validate_external_attribution(request: ExecutionRequest, connector: str, measurement: Any) -> None:
        attribution = measurement.get("attribution", {}) if isinstance(measurement, dict) else {}
        expected = {"run_id": request.run_id, "epoch": request.epoch, "agent_id": request.agent_id, "genome": request.genome_hash}
        if any(attribution.get(key) != value for key, value in expected.items()): raise ValueError(f"connector {connector} response attribution does not match the frozen attempt")

    @staticmethod
    def _mark_interrupted(store: Store, run_id: str) -> None:
        run = store.one("SELECT current_epoch FROM runs WHERE id=?", (run_id,))
        if not run:
            return
        selection = store.one("SELECT status FROM epochs WHERE run_id=? AND epoch=?", (run_id, run["current_epoch"])) if run["current_epoch"] else None
        if selection and selection["status"] == "selection_pending":
            return
        epoch = run["current_epoch"] + 1
        record = store.one("SELECT status FROM epochs WHERE run_id=? AND epoch=?", (run_id, epoch))
        if record and record["status"] in {"execution_complete", "grading_interrupted"}:
            store.execute("UPDATE runs SET status='paused' WHERE id=?", (run_id,))
            return
        if record and record["status"] not in {"graded", "complete"}:
            store.execute("UPDATE epochs SET status='interrupted', completed_at=? WHERE run_id=? AND epoch=?", (now(), run_id, epoch))
        store.execute("UPDATE runs SET status='paused' WHERE id=?", (run_id,))

    def _snapshot_epoch_state(self, run_id: str, epoch: int) -> None:
        destination = self._run_dir(run_id) / "epoch-state-snapshots" / f"epoch-{epoch:04d}"
        if (destination / ".complete").is_file(): return
        staging = destination.with_name(destination.name + ".staging")
        if staging.exists(): shutil.rmtree(staging)
        staging.mkdir(parents=True)
        agents = self._run_dir(run_id) / "agents"
        for agent in agents.iterdir() if agents.exists() else []:
            state = agent / "state"
            if state.exists(): shutil.copytree(state, staging / agent.name, symlinks=True)
        (staging / ".complete").write_text(now())
        if destination.exists(): shutil.rmtree(destination)
        staging.rename(destination)

    def _restore_epoch_state(self, run_id: str, epoch: int) -> None:
        source = self._run_dir(run_id) / "epoch-state-snapshots" / f"epoch-{epoch:04d}"
        if not (source / ".complete").is_file(): raise RunError(f"epoch {epoch} has no clean state snapshot for recovery")
        for saved in source.iterdir():
            if saved.name == ".complete": continue
            target = self._agent_dir(run_id, saved.name) / "state"
            if target.exists(): shutil.rmtree(target)
            shutil.copytree(saved, target, symlinks=True)

    def _select_and_birth(self, store: Store, run: dict[str, Any]) -> None:
        epoch, run_id, size = run["current_epoch"], run["id"], run["population_size"]
        ranked = store.rows("SELECT agent_id,genome_hash,value,tie_break_value,outcome FROM scores WHERE run_id=? AND epoch=? ORDER BY CASE outcome WHEN 'success' THEN 0 ELSE 1 END,value DESC,tie_break_value DESC,agent_id", (run_id, epoch))
        successful = [row for row in ranked if row["outcome"] == "success"]
        failed = [row for row in ranked if row["outcome"] != "success"]
        if not successful:
            store.execute("UPDATE epochs SET status='complete' WHERE run_id=? AND epoch=?", (run_id, epoch))
            store.execute("UPDATE runs SET status='failed' WHERE id=?", (run_id,))
            store.add_event(run_id, "run_failed", {"epoch": epoch, "reason": "every agent failed; no verified genome can reproduce"}, now())
            return
        # Re-prove every scored source immediately before evolutionary models
        # inspect it. Only the exact genome that performed may influence
        # reflection, Tweaker analysis, or reproduction.
        for row in ranked:
            source_agent = store.one("SELECT * FROM agents WHERE run_id=? AND agent_id=?", (run_id, row["agent_id"]))
            source_genome = store.one("SELECT * FROM genomes WHERE run_id=? AND hash=?", (run_id, row["genome_hash"]))
            if not source_agent or not source_genome:
                raise RunError(f"scored genome source disappeared for {row['agent_id']}")
            try:
                self._verify_agent_genome(run_id, source_agent, source_genome)
            except RunError:
                GitStore(self._run_dir(run_id), self._seed_dir()).materialize(source_agent["agent_id"], source_agent["branch_name"], self._git_commit(source_genome))
                self._verify_agent_genome(run_id, source_agent, source_genome)
                store.add_event(run_id, "scored_genome_rematerialized", {"epoch": epoch, "agent_id": source_agent["agent_id"], "genome": source_genome["hash"]}, now())
        elite_count = min(max(1, round(size * .30)), len(successful))
        target_cull = max(1, round(size * .30))
        successful_to_cull = max(0, target_cull - len(failed))
        elites = successful[:elite_count]
        remaining_success = successful[elite_count:]
        bottom_success = remaining_success[-successful_to_cull:] if successful_to_cull else []
        middle = remaining_success[:-successful_to_cull] if successful_to_cull else remaining_success
        culled = [*bottom_success, *failed]
        middle_count = len(middle)
        architect = self._architect_output(run_id)
        analysis = self._tweaker_analysis(store, run_id, epoch, ranked, elite_count)
        specialist_checkpoints = self._run_dir(run_id) / "specialist-checkpoints" / f"epoch-{epoch:04d}"
        report = TweakerAgent(self._specialist_model(run_id), architect["special_agent_instructions"]["tweaker"]).run(epoch, ranked, elite_count, middle_count, len(culled), analysis, specialist_checkpoints / "tweaker")
        self._selection_snapshot(store, run_id, epoch)
        reports = self._run_dir(run_id) / "tweaker-reports"; reports.mkdir(exist_ok=True)
        (reports / f"epoch-{epoch:04d}.json").write_text(json.dumps(report, indent=2, sort_keys=True))
        for row in culled:
            store.execute("UPDATE agents SET status='archived' WHERE run_id=? AND agent_id=?", (run_id, row["agent_id"]))
        # Middle agents adapt after grading; elites retain their exact scored genome.
        adaptation_inputs = []
        shared_for_adaptation = self._shared_knowledge(store, run_id)
        for row in middle:
            agent = store.one("SELECT * FROM agents WHERE run_id=? AND agent_id=?", (run_id, row["agent_id"]))
            old = store.one("SELECT * FROM genomes WHERE hash=?", (agent["active_genome"],))
            self._verify_agent_genome(run_id, agent, old)
            own_records = [record for record in analysis["population_records"] if record["agent_id"] == agent["agent_id"]]
            context = {"purpose": "Self-reflect on your own behavior and directly observed elite behavior. The Tweaker report is intentionally unavailable. Use read_shared_knowledge to search the complete canonical memory bank when these ranked excerpts are insufficient.", "ranking": ranked, "own_semantic_records": own_records, "elite_semantic_records": analysis["elite_records"], "population_outcome_digest": analysis["adaptation_population_records"], "elite_framework_profiles": analysis["elite_framework_profiles"], "shared_knowledge": shared_for_adaptation[:100], "own_genome": {"hash": old["hash"], "prompt": old["prompt"], "manifest": json.loads(old["manifest_json"]), "framework": self._framework_snapshot(self._agent_dir(run_id, agent["agent_id"]) / "repository", 750_000)}}
            state_dir = self._agent_dir(run_id, agent["agent_id"]) / "state"
            (state_dir / "adaptation-context.json").write_text(json.dumps(context, indent=2, sort_keys=True))
            (state_dir / "mutation.json").unlink(missing_ok=True)
            adaptation_inputs.append((agent, old, state_dir))
        adaptation_results = {}
        adaptation_pending = []
        adaptation_fingerprints: dict[str, tuple[str, Path]] = {}
        for agent, old, state_dir in adaptation_inputs:
            _context_size, context_sha256 = hash_file(state_dir / "adaptation-context.json")
            fingerprint = digest(json.dumps({"run_id": run_id, "epoch": epoch, "agent_id": agent["agent_id"], "genome": old["hash"], "context_sha256": context_sha256}, sort_keys=True))
            checkpoint = specialist_checkpoints / "adaptations" / f"{agent['agent_id']}.json"
            adaptation_fingerprints[agent["agent_id"]] = (fingerprint, checkpoint)
            try:
                saved = json.loads(checkpoint.read_text()) if checkpoint.is_file() else {}
                if saved.get("input_sha256") == fingerprint:
                    adaptation_results[agent["agent_id"]] = Mutation.from_payload(saved["mutation"])
                    continue
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError): pass
            adaptation_pending.append((agent, old, state_dir))
        with ThreadPoolExecutor(max_workers=max(1, min(16, len(adaptation_inputs)))) as pool:
            futures = {pool.submit(self._organism_adapt, run, agent, old, state_dir): (agent, old) for agent, old, state_dir in adaptation_pending}
            for future in as_completed(futures):
                agent, old = futures[future]
                try:
                    mutation = future.result(); adaptation_results[agent["agent_id"]] = mutation
                    fingerprint, checkpoint = adaptation_fingerprints[agent["agent_id"]]
                    atomic_write_json(checkpoint, {"input_sha256": fingerprint, "mutation": {"action": mutation.action, "reason": mutation.reason, "prompt_suffix": mutation.prompt_suffix, "files": mutation.files or {}, "delete_files": list(mutation.delete_files)}})
                except Exception as exc:
                    adaptation_results[agent["agent_id"]] = Mutation(reason=f"Adaptation failed; retained scored genome: {exc}")
                    store.add_knowledge(run_id, epoch, agent["agent_id"], "adaptation_crash", {"summary": "Post-epoch self-reflection crashed; the scored genome was retained for safety.", "outcome": "failed", "error": str(exc)[:4000], "genome": old["hash"], "visibility": "shared"}, now())
        for agent, old, _state_dir in adaptation_inputs:
            proposal = adaptation_results[agent["agent_id"]]
            if not proposal.changed:
                failed = proposal.reason.startswith("Adaptation failed;")
                store.add_knowledge(run_id, epoch, agent["agent_id"], "adaptation", {"summary": "Post-epoch adaptation failed and retained the scored genome." if failed else "Reviewed the epoch and explicitly kept the scored genome unchanged.", "outcome": "failed" if failed else "unchanged", "reason": proposal.reason, "genome": old["hash"], "visibility": "shared"}, now())
                continue
            prompt = old["prompt"] + ("\n" + proposal.prompt_suffix.strip() if proposal.prompt_suffix.strip() else "")
            commit = None
            try:
                commit, prompt = self._materialize_agent(run_id, agent["agent_id"], agent["branch_name"], self._git_commit(old), prompt, f"adaptation epoch {epoch}", proposal)
                child = self._create_genome(store, run_id, prompt, old["hash"], epoch, commit)
            except DuplicateGenomeError:
                self._discard_unregistered_commit(run_id, agent["branch_name"], commit, self._git_commit(old))
                GitStore(self._run_dir(run_id), self._seed_dir()).materialize(agent["agent_id"], agent["branch_name"], self._git_commit(old))
                store.add_knowledge(run_id, epoch, agent["agent_id"], "adaptation", {"summary": "Proposed a genome already present in the population and therefore remained unchanged.", "outcome": "unchanged", "reason": "duplicate genome", "genome": old["hash"], "visibility": "shared"}, now())
                continue
            except MutationError as exc:
                if commit: self._discard_unregistered_commit(run_id, agent["branch_name"], commit, self._git_commit(old))
                GitStore(self._run_dir(run_id), self._seed_dir()).materialize(agent["agent_id"], agent["branch_name"], self._git_commit(old))
                store.add_knowledge(run_id, epoch, agent["agent_id"], "adaptation", {"summary": "Rejected an invalid self-mutation and retained the scored genome.", "outcome": "unchanged", "reason": str(exc), "genome": old["hash"], "visibility": "shared"}, now())
                continue
            store.execute("UPDATE agents SET active_genome=? WHERE run_id=? AND agent_id=?", (child, run_id, agent["agent_id"]))
            store.execute("INSERT INTO lineage VALUES(?,?,?,?,?,?)", (run_id, agent["agent_id"], agent["agent_id"], old["hash"], child, epoch))
            store.add_knowledge(run_id, epoch, agent["agent_id"], "adaptation", {"summary": "Independently adapted after comparing its work with elite behavior.", "outcome": "mutated", "reason": proposal.reason, "mutation": {"prompt_suffix": proposal.prompt_suffix, "files": list((proposal.files or {}).keys()), "deleted": list(proposal.delete_files)}, "old_genome": old["hash"], "new_genome": child, "visibility": "shared"}, now())
        existing_count = store.one("SELECT COUNT(*) AS count FROM agents WHERE run_id=?", (run_id,))["count"]
        slots = [f"agent-{existing_count + index:03d}" for index in range(1, len(culled) + 1)]
        births: list[dict[str, str]] = []
        birth_inputs = []
        assigned_parents = [agent_id for agent_id, count in report["reproduction_assignments"].items() for _ in range(int(count))]
        for index, slot in enumerate(slots):
            selected = assigned_parents[index]
            parent = next(row for row in elites if row["agent_id"] == selected)
            parent_genome = store.one("SELECT * FROM genomes WHERE hash=?", (parent["genome_hash"],))
            parent_agent = store.one("SELECT * FROM agents WHERE run_id=? AND agent_id=?", (run_id, parent["agent_id"]))
            self._verify_agent_genome(run_id, parent_agent, parent_genome)
            parent_context = {"agent_id": parent["agent_id"], "genome": parent_genome["hash"], "prompt": parent_genome["prompt"], "manifest": json.loads(parent_genome["manifest_json"]), "framework": self._framework_snapshot(self._agent_dir(run_id, parent["agent_id"]) / "repository", int(os.getenv("KADATH_BIRTHER_SOURCE_CHARS", "150000"))), "tweaker_parent_brief": report["parent_briefs"][parent["agent_id"]]}
            birth_inputs.append((slot, parent, parent_genome, parent_context))
        initial_births = {}
        with ThreadPoolExecutor(max_workers=max(1, min(16, len(birth_inputs)))) as pool:
            futures = {pool.submit(BirtherAgent(self._specialist_model(run_id), architect["special_agent_instructions"]["birther"]).run, epoch, slot, parent_context, {**report, "parent_briefs": {parent_context["agent_id"]: parent_context["tweaker_parent_brief"]}}, [], specialist_checkpoints / "births" / f"{slot}-attempt-0.json"): slot for slot, _parent, _genome, parent_context in birth_inputs}
            for future in as_completed(futures): initial_births[futures[future]] = future.result()
        for slot, parent, parent_genome, parent_context in birth_inputs:
            rejected: list[str] = []
            for attempt in range(5):
                compact_report = {**report, "parent_briefs": {parent["agent_id"]: report["parent_briefs"][parent["agent_id"]]}}
                mutation = initial_births[slot] if attempt == 0 else BirtherAgent(self._specialist_model(run_id), architect["special_agent_instructions"]["birther"]).run(epoch, slot, parent_context, compact_report, rejected, specialist_checkpoints / "births" / f"{slot}-attempt-{attempt}.json")
                try:
                    proposal = Mutation.from_payload({"action": "mutate", "reason": mutation["mutation_brief"], "prompt_suffix": mutation["prompt_suffix"], "files": mutation.get("files", {}), "delete_files": mutation.get("delete_files", [])})
                    prompt = parent_genome["prompt"] + ("\n" + proposal.prompt_suffix.strip() if proposal.prompt_suffix.strip() else "")
                    commit, prompt = self._materialize_agent(run_id, slot, slot, self._git_commit(parent_genome), prompt, f"descendant birth epoch {epoch}", proposal)
                    child = self._create_genome(store, run_id, prompt, parent_genome["hash"], epoch, commit)
                    break
                except DuplicateGenomeError:
                    self._discard_unregistered_commit(run_id, slot, commit, self._git_commit(parent_genome))
                    rejected.append(mutation["mutation_brief"])
                except MutationError as exc:
                    GitStore(self._run_dir(run_id), self._seed_dir()).materialize(slot, slot, self._git_commit(parent_genome))
                    rejected.append(f"{mutation['mutation_brief']}: {exc}")
            else:
                raise RunError(f"Birther could not create a distinct genome for {slot} after 5 attempts")
            store.execute("INSERT INTO agents VALUES(?,?,?,?,?,?,?,NULL)", (run_id, slot, "active", child, parent["agent_id"], slot, epoch))
            store.execute("INSERT INTO lineage VALUES(?,?,?,?,?,?)", (run_id, slot, parent["agent_id"], parent_genome["hash"], child, epoch))
            self._inherit_knowledge(store, run_id, epoch, parent["agent_id"], slot)
            self._inherit_state(run_id, parent["agent_id"], run_id, slot)
            births.append({"child_agent_id": slot, "parent_agent_id": parent["agent_id"], "parent_genome": parent_genome["hash"], "child_genome": child, "mutation": mutation["mutation_brief"]})
        birth_reports = self._run_dir(run_id) / "birther-reports"; birth_reports.mkdir(exist_ok=True)
        (birth_reports / f"epoch-{epoch:04d}.json").write_text(json.dumps({"epoch": epoch, "births": births}, indent=2, sort_keys=True))
        next_status = "complete" if epoch >= run["total_epochs"] else "ready"
        store.execute("UPDATE epochs SET status='complete' WHERE run_id=? AND epoch=?", (run_id, epoch))
        store.execute("UPDATE runs SET status=? WHERE id=?", (next_status, run_id))
        store.add_event(run_id, "selection_completed", {"epoch": epoch, "elites": len(elites), "adapted": len(middle), "culled": len(culled), "born": len(births), "next_status": next_status}, now())
        (self._run_dir(run_id) / "selection-snapshots" / f"epoch-{epoch:04d}.json").unlink(missing_ok=True)
        state_snapshot = self._run_dir(run_id) / "selection-snapshots" / f"epoch-{epoch:04d}-states"
        if state_snapshot.exists(): shutil.rmtree(state_snapshot)

    def _tweaker_analysis(self, store: Store, run_id: str, epoch: int, ranked: list[dict[str, Any]], elite_count: int) -> dict[str, Any]:
        elite_ids = {row["agent_id"] for row in ranked[:elite_count]}
        ratings = store.knowledge_rating_scores(run_id)
        per_agent: dict[str, list[tuple[float, dict[str, Any]]]] = {}
        all_records: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for row in store.rows("SELECT id,agent_id,kind,payload_json,content_hash FROM knowledge WHERE run_id=? AND epoch=? ORDER BY id", (run_id, epoch)):
            fingerprint = row.get("content_hash") or hashlib.sha256(row["payload_json"].encode()).hexdigest()
            owner_fingerprint = (row["agent_id"], fingerprint)
            if owner_fingerprint in seen: continue
            seen.add(owner_fingerprint)
            payload = json.loads(row["payload_json"])
            kind_weight = {"epoch_report": 50, "kernel_activity": 40, "adaptation": 30, "runtime_crash": 25, "kernel_activity_trace": 20, "worker_completion": 15, "activity": 10}.get(row["kind"], 0)
            semantic_weight = 5 if payload.get("evidence_refs") else 0
            record = {"record_id": row["id"], "agent_id": row["agent_id"], "kind": row["kind"], "usefulness": ratings.get(int(row["id"]), 0), "payload": payload}
            all_records.append(record)
            per_agent.setdefault(row["agent_id"], []).append((kind_weight + semantic_weight + ratings.get(int(row["id"]), 0) * 8, record))
        population_records = []
        for agent_id, candidates in per_agent.items():
            candidates.sort(key=lambda item: item[0], reverse=True)
            population_records.extend(record for _, record in candidates[:15])
        records = [record for record in population_records if record["agent_id"] in elite_ids]
        profiles = []
        tweaker_profiles = []
        lineage = store.rows("SELECT child_agent_id,parent_agent_id,parent_genome,child_genome,epoch FROM lineage WHERE run_id=? ORDER BY epoch,child_agent_id", (run_id,))
        for row in ranked:
            agent = store.one("SELECT active_genome FROM agents WHERE run_id=? AND agent_id=?", (run_id, row["agent_id"]))
            genome = store.one("SELECT prompt,manifest_json FROM genomes WHERE hash=?", (row["genome_hash"],))
            base = {"agent_id": row["agent_id"], "ranked_outcome": row, "prompt": genome["prompt"] if genome else "", "manifest": json.loads(genome["manifest_json"]) if genome else {}, "active_genome": agent["active_genome"] if agent else None}
            profiles.append({**base, "framework": self._framework_snapshot(self._agent_dir(run_id, row["agent_id"]) / "repository", 10_000)})
            tweaker_profiles.append({**base, "framework": self._framework_snapshot(self._agent_dir(run_id, row["agent_id"]) / "repository", int(os.getenv("KADATH_TWEAKER_SOURCE_CHARS", "100000")))})
        digest_records = [record for record in population_records if record["kind"] in {"epoch_report", "kernel_activity", "kernel_activity_trace", "adaptation", "adaptation_crash", "runtime_crash", "worker_completion"}]
        dossiers = []
        for profile in tweaker_profiles:
            agent_id = profile["agent_id"]
            dossiers.append({"agent_id": agent_id, "profile": profile, "semantic_records": [record for record in all_records if record["agent_id"] == agent_id], "lineage": [edge for edge in lineage if edge["child_agent_id"] == agent_id or edge["parent_agent_id"] == agent_id]})
        return {"elite_records": records, "population_records": population_records, "adaptation_population_records": digest_records, "ranked_outcomes": ranked, "elite_framework_profiles": profiles[:elite_count], "tweaker_dossiers": dossiers, "memory_ranking": "all epoch records are supplied to Tweaker in bounded chunks; parent excerpts use per-agent verified-outcome, evidence, credibility, and usefulness ranking; complete bank remains searchable"}

    @staticmethod
    def _framework_snapshot(repository: Path, text_budget: int) -> dict[str, Any]:
        """Bounded source view so evolution agents never mutate a framework blind."""
        files: list[dict[str, Any]] = []
        if not repository.exists(): return {"files": files, "text_budget": text_budget}
        priority = {"organism.py": 0, "kadath_runtime.py": 1, "SYSTEM_PROMPT.md": 2, "pyproject.toml": 3, "requirements.txt": 4}
        candidates = [item for item in repository.rglob("*") if item.is_file() and ".git" not in item.parts]
        candidates.sort(key=lambda item: (priority.get(str(item.relative_to(repository)), 10), str(item.relative_to(repository))))
        used = 0
        for item in candidates:
            size, checksum = hash_file(item); relative = str(item.relative_to(repository))
            record: dict[str, Any] = {"path": relative, "size": size, "sha256": checksum}
            if used < text_budget and size <= 500_000:
                try:
                    text = item.read_text(); remaining = text_budget - used
                    record["text"] = text[:remaining]; used += len(record["text"].encode())
                    if len(record["text"]) < len(text): record["text_truncated"] = True
                except UnicodeDecodeError: pass
            files.append(record)
        return {"files": files, "text_budget": text_budget, "text_bytes_included": used}

    @staticmethod
    def _inherit_knowledge(store: Store, run_id: str, epoch: int, parent_id: str, child_id: str) -> None:
        ids = {int(row["id"]) for row in store.rows("SELECT id FROM knowledge WHERE run_id=? AND agent_id=?", (run_id, parent_id))}
        ids.update(int(row["knowledge_id"]) for row in store.rows("SELECT knowledge_id FROM memory_links WHERE run_id=? AND agent_id=?", (run_id, parent_id)))
        for knowledge_id in sorted(ids):
            store.link_memory(run_id, child_id, knowledge_id, parent_id, epoch, now())

    def _inherit_cross_run(self, target_run: str, child_id: str, continuation: dict[str, Any]) -> None:
        if continuation.get("source_export"):
            self._inherit_from_export(target_run, child_id, continuation)
            return
        source_run, parent_id = continuation["source_run"], continuation["parent_agent"]
        source_store, target_store = self._store(source_run), self._store(target_run)
        source_ids = {int(row["id"]) for row in source_store.rows("SELECT id FROM knowledge WHERE run_id=? AND agent_id=?", (source_run, parent_id))}
        source_ids.update(int(row["knowledge_id"]) for row in source_store.rows("SELECT knowledge_id FROM memory_links WHERE run_id=? AND agent_id=?", (source_run, parent_id)))
        bank_owner = f"memory-bank/{source_run}/{parent_id}"
        for source_id in sorted(source_ids):
            row = source_store.one("SELECT id,epoch,agent_id,kind,payload_json,published_at FROM knowledge WHERE run_id=? AND id=?", (source_run, source_id))
            if not row: continue
            imported_id = target_store.add_knowledge(target_run, 0, bank_owner, row["kind"], {"record": json.loads(row["payload_json"]), "source_run": source_run, "source_record_id": source_id, "original_epoch": row["epoch"], "original_agent_id": row["agent_id"], "parent_genome": continuation["parent_genome"], "parent_commit": continuation["parent_commit"], "visibility": "private"}, now())
            target_store.link_memory(target_run, child_id, imported_id, parent_id, 0, now())
        self._inherit_state(source_run, parent_id, target_run, child_id)
        self._artifact_store(target_run).inherit_from(self._artifact_store(source_run), source_run, parent_id, target_run, child_id)
        source_frozen = self._run_dir(source_run) / "frozen-attempts"
        target_frozen = self._run_dir(target_run) / "inherited-frozen-attempts" / source_run / child_id
        for epoch in source_frozen.glob("epoch-*") if source_frozen.exists() else []:
            parent = epoch / parent_id
            if parent.exists(): shutil.copytree(parent, target_frozen / epoch.name, dirs_exist_ok=True)

    def _inherit_from_export(self, target_run: str, child_id: str, continuation: dict[str, Any]) -> None:
        export_dir = Path(continuation["source_export"]); report = json.loads((export_dir / "run-report.json").read_text())
        parent_id, source_run = continuation["parent_agent"], continuation["source_run"]
        own_ids = {int(row["id"]) for row in report["knowledge"] if row["agent_id"] == parent_id}
        own_ids.update(int(row["knowledge_id"]) for row in report.get("memory_links", []) if row["agent_id"] == parent_id)
        target_store = self._store(target_run); bank_owner = f"memory-bank/{source_run}/{parent_id}"
        by_id = {int(row["id"]): row for row in report["knowledge"]}
        for source_id in sorted(own_ids):
            row = by_id.get(source_id)
            if not row: continue
            imported_id = target_store.add_knowledge(target_run, 0, bank_owner, row["kind"], {"record": json.loads(row["payload_json"]), "source_run": source_run, "source_record_id": source_id, "original_epoch": row["epoch"], "original_agent_id": row["agent_id"], "parent_genome": continuation["parent_genome"], "parent_commit": continuation["parent_commit"], "visibility": "private"}, now())
            target_store.link_memory(target_run, child_id, imported_id, parent_id, 0, now())
        source_state = export_dir / "agent-states" / parent_id
        target_state = self._agent_dir(target_run, child_id) / "state"
        if source_state.exists(): copy_tree_without_symlinks(source_state, target_state)
        inherited = target_state / "inherited-state.json"
        atomic_write_json(inherited, {"source_run": source_run, "source_export": str(export_dir), "parent_agent": parent_id, "framework_inherited_from_scored_commit": True, "copied_at": now()})
        source_frozen = export_dir / "frozen-attempts"; target_frozen = self._run_dir(target_run) / "inherited-frozen-attempts" / source_run / child_id
        for epoch in source_frozen.glob("epoch-*") if source_frozen.exists() else []:
            parent = epoch / parent_id
            if parent.exists(): shutil.copytree(parent, target_frozen / epoch.name, dirs_exist_ok=True, symlinks=True)

    def _inherit_state(self, source_run: str, parent_id: str, target_run: str, child_id: str) -> None:
        source = self._agent_dir(source_run, parent_id) / "state"
        target = self._agent_dir(target_run, child_id) / "state"
        transient = {"result.json", "activity.jsonl", "mutation.json", "adaptation-context.json", "shared-knowledge.json", "finalize.requested", "checkpoint.json", "worker-events.jsonl", "workers", "crash-log.jsonl"}
        copied = []
        for item in source.iterdir() if source.exists() else []:
            if item.name in transient: continue
            destination = target / item.name
            if item.is_symlink():
                continue
            if item.is_dir(): copy_tree_without_symlinks(item, destination)
            elif stat.S_ISREG(item.lstat().st_mode): shutil.copy2(item, destination, follow_symlinks=False)
            else: continue
            copied.append(item.name)
        inherited = target / "inherited-state.json"
        inherited.write_text(json.dumps({"source_run": source_run, "parent_agent": parent_id, "copied": sorted(copied), "framework_inherited_from_scored_commit": True, "copied_at": now()}, indent=2, sort_keys=True))

    def _selection_snapshot(self, store: Store, run_id: str, epoch: int) -> None:
        directory = self._run_dir(run_id) / "selection-snapshots"; directory.mkdir(exist_ok=True)
        path = directory / f"epoch-{epoch:04d}.json"
        if not path.exists():
            repo = self._run_dir(run_id) / "git-repository"
            refs = {}
            if repo.exists():
                output = subprocess.check_output(["git", f"--git-dir={repo}", "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/tags/genome"], text=True)
                refs = dict(line.split(" ", 1) for line in output.splitlines() if " " in line)
            maximum = store.one("SELECT MAX(id) AS value FROM knowledge WHERE run_id=?", (run_id,))
            state_snapshot = directory / f"epoch-{epoch:04d}-states"
            staging_states = directory / f".epoch-{epoch:04d}-states.staging"
            if staging_states.exists(): shutil.rmtree(staging_states)
            staging_states.mkdir()
            for agent in (self._run_dir(run_id) / "agents").iterdir():
                if (agent / "state").exists(): shutil.copytree(agent / "state", staging_states / agent.name, dirs_exist_ok=True, symlinks=True)
            (staging_states / ".complete").write_text(now())
            if state_snapshot.exists(): shutil.rmtree(state_snapshot)
            staging_states.replace(state_snapshot)
            snapshot = {"agents": store.rows("SELECT * FROM agents WHERE run_id=? ORDER BY agent_id", (run_id,)), "genomes": [row["hash"] for row in store.rows("SELECT hash FROM genomes WHERE run_id=?", (run_id,))], "knowledge_max_id": maximum["value"] or 0, "memory_links": store.rows("SELECT * FROM memory_links WHERE run_id=?", (run_id,)), "knowledge_ratings": store.rows("SELECT * FROM knowledge_ratings WHERE run_id=?", (run_id,)), "refs": refs}
            atomic_write_json(path, snapshot)

    def _rollback_selection(self, store: Store, run_id: str, epoch: int) -> None:
        path = self._run_dir(run_id) / "selection-snapshots" / f"epoch-{epoch:04d}.json"
        if not path.is_file():
            return
        snapshot = json.loads(path.read_text())
        agents = snapshot if isinstance(snapshot, list) else snapshot["agents"]
        original_ids = {agent["agent_id"] for agent in agents}
        for row in store.rows("SELECT agent_id FROM agents WHERE run_id=?", (run_id,)):
            if row["agent_id"] not in original_ids:
                store.execute("DELETE FROM knowledge WHERE run_id=? AND agent_id=?", (run_id, row["agent_id"]))
        store.execute("DELETE FROM agents WHERE run_id=?", (run_id,))
        for agent in agents:
            store.execute("INSERT INTO agents VALUES(?,?,?,?,?,?,?,?)", (agent["run_id"], agent["agent_id"], agent["status"], agent["active_genome"], agent["parent_agent_id"], agent["branch_name"], agent["birth_epoch"], agent["last_rank"]))
        store.execute("DELETE FROM lineage WHERE run_id=? AND epoch=?", (run_id, epoch))
        if isinstance(snapshot, dict):
            store.execute("DELETE FROM knowledge WHERE run_id=? AND id>?", (run_id, snapshot.get("knowledge_max_id", 0)))
            store.execute("DELETE FROM memory_links WHERE run_id=?", (run_id,))
            for link in snapshot.get("memory_links", []):
                store.execute("INSERT INTO memory_links(run_id,agent_id,knowledge_id,inherited_from_agent_id,inherited_at_epoch,created_at) VALUES(?,?,?,?,?,?)", (link["run_id"], link["agent_id"], link["knowledge_id"], link["inherited_from_agent_id"], link["inherited_at_epoch"], link["created_at"]))
            store.execute("DELETE FROM knowledge_ratings WHERE run_id=?", (run_id,))
            for rating in snapshot.get("knowledge_ratings", []):
                store.execute("INSERT INTO knowledge_ratings(run_id,knowledge_id,agent_id,value,created_at) VALUES(?,?,?,?,?)", (rating["run_id"], rating["knowledge_id"], rating["agent_id"], rating["value"], rating["created_at"]))
            original_genomes = set(snapshot.get("genomes", []))
            for genome in store.rows("SELECT hash FROM genomes WHERE run_id=?", (run_id,)):
                if genome["hash"] not in original_genomes: store.execute("DELETE FROM genomes WHERE run_id=? AND hash=?", (run_id, genome["hash"]))
            genome_root = self._run_dir(run_id) / "genomes"
            for directory in genome_root.iterdir() if genome_root.exists() else []:
                if directory.is_dir() and directory.name not in original_genomes: shutil.rmtree(directory)
            repo = self._run_dir(run_id) / "git-repository"
            current = {}
            if repo.exists():
                output = subprocess.check_output(["git", f"--git-dir={repo}", "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/tags/genome"], text=True)
                current = dict(line.split(" ", 1) for line in output.splitlines() if " " in line)
                for ref in set(current) - set(snapshot.get("refs", {})): subprocess.run(["git", f"--git-dir={repo}", "update-ref", "-d", ref], check=True)
                for ref, commit in snapshot.get("refs", {}).items(): subprocess.run(["git", f"--git-dir={repo}", "update-ref", ref, commit], check=True)
        else:
            store.execute("DELETE FROM genomes WHERE run_id=? AND created_epoch=?", (run_id, epoch))
        for child_dir in (self._run_dir(run_id) / "agents").iterdir():
            if child_dir.is_dir() and child_dir.name not in original_ids: shutil.rmtree(child_dir)
        for agent in agents:
            genome = store.one("SELECT manifest_json,prompt FROM genomes WHERE run_id=? AND hash=?", (run_id, agent["active_genome"]))
            if genome:
                GitStore(self._run_dir(run_id), self._seed_dir()).materialize(agent["agent_id"], agent["branch_name"], json.loads(genome["manifest_json"])["git_commit"])
        state_snapshot = path.with_name(f"epoch-{epoch:04d}-states")
        if state_snapshot.exists() and (state_snapshot / ".complete").is_file():
            for saved in state_snapshot.iterdir():
                if saved.name == ".complete": continue
                target = self._agent_dir(run_id, saved.name) / "state"
                if target.exists(): shutil.rmtree(target)
                shutil.copytree(saved, target, symlinks=True)
            shutil.rmtree(state_snapshot)
        path.unlink(missing_ok=True)

    def _create_genome(self, store: Store, run_id: str, prompt: str, parent: str | None, epoch: int, git_commit: str) -> str:
        runtime = json.loads((self._run_dir(run_id) / "runtime.json").read_text())
        repository = self._run_dir(run_id) / "git-repository"
        tree_hash = subprocess.check_output(["git", f"--git-dir={repository}", "rev-parse", f"{git_commit}^{{tree}}"], text=True).strip()
        runtime_hash = digest(json.dumps(runtime, sort_keys=True))
        content_signature = digest(json.dumps({"tree": tree_hash, "prompt": prompt, "runtime": runtime_hash}, sort_keys=True))
        existing = store.one("SELECT hash FROM genomes WHERE run_id=? AND manifest_json LIKE ?", (run_id, f'%"content_signature": "{content_signature}"%'))
        if existing:
            raise DuplicateGenomeError(f"genome duplicates {existing['hash']}")
        genome = digest(f"{run_id}:{content_signature}")
        manifest = {"runtime": runtime, "entrypoint": "organism.py", "worker_limit": 5, "immutable_during_epoch": True, "git_commit": git_commit, "tree_hash": tree_hash, "content_signature": content_signature}
        store.execute("INSERT INTO genomes VALUES(?,?,?,?,?,?)", (genome, run_id, parent, prompt, json.dumps(manifest, sort_keys=True), epoch))
        directory = self._run_dir(run_id) / "genomes" / genome
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
        (directory / "SYSTEM_PROMPT.md").write_text(prompt + "\n")
        return genome

    def _verify_agent_genome(self, run_id: str, agent: dict[str, Any], genome: dict[str, Any]) -> None:
        """Prove the mounted repository is the registered genome before it performs."""
        repository = self._agent_dir(run_id, agent["agent_id"]) / "repository"
        manifest = json.loads(genome["manifest_json"])
        try:
            head = subprocess.check_output(["git", "-C", str(repository), "rev-parse", "HEAD"], text=True).strip()
            tree = subprocess.check_output(["git", "-C", str(repository), "rev-parse", "HEAD^{tree}"], text=True).strip()
            dirty = subprocess.check_output(["git", "-C", str(repository), "status", "--porcelain", "--untracked-files=all"], text=True)
        except (OSError, subprocess.SubprocessError) as exc: raise RunError(f"cannot verify active genome for {agent['agent_id']}") from exc
        if head != manifest.get("git_commit") or tree != manifest.get("tree_hash") or dirty.strip():
            raise RunError(f"active repository does not match the scored genome for {agent['agent_id']}")
        runtime = json.loads((self._run_dir(run_id) / "runtime.json").read_text())
        signature = digest(json.dumps({"tree": tree, "prompt": genome["prompt"], "runtime": digest(json.dumps(runtime, sort_keys=True))}, sort_keys=True))
        if signature != manifest.get("content_signature"):
            raise RunError(f"active genome signature mismatch for {agent['agent_id']}")
        prompt_file = repository / "SYSTEM_PROMPT.md"
        if not prompt_file.is_file() or prompt_file.read_text() != genome["prompt"] + "\n":
            raise RunError(f"active genome prompt mismatch for {agent['agent_id']}")

    @staticmethod
    def _seed_dir() -> Path:
        return Path(os.environ.get("KADATH_SEED_DIR", Path(__file__).resolve().parents[1] / "seed" / "smolagents"))

    def _discard_unregistered_commit(self, run_id: str, branch: str, commit: str, restore_commit: str) -> None:
        repo = self._run_dir(run_id) / "git-repository"
        subprocess.run(["git", f"--git-dir={repo}", "update-ref", "-d", f"refs/tags/genome/{commit}"], check=True)
        subprocess.run(["git", f"--git-dir={repo}", "update-ref", f"refs/heads/{branch}", restore_commit], check=True)

    def _executor(self, run_id: str):
        runtime = json.loads((self._run_dir(run_id) / "runtime.json").read_text())
        if runtime["executor"] == "simulated":
            return SimulatedExecutor()
        if runtime["executor"] == "command":
            return CommandExecutor(runtime["command"], environment=self._runtime_environment(runtime))
        if runtime["executor"] == "docker":
            return DockerExecutor(runtime["image"], runtime["command"], runtime.get("network", "none"), self._runtime_environment(runtime))
        raise RunError(f"unknown executor: {runtime['executor']}")

    @staticmethod
    def _runtime_environment(runtime: dict[str, Any]) -> dict[str, str]:
        inherited = {key: value for key, value in {
                "KADATH_PLAYWRIGHT_MCP_URL": os.environ.get("KADATH_PLAYWRIGHT_MCP_URL"),
                "KADATH_SEARXNG_URL": os.environ.get("KADATH_SEARXNG_URL"),
                "LITELLM_API_BASE": os.environ.get("LITELLM_API_BASE"),
                "LITELLM_API_KEY": os.environ.get("LITELLM_API_KEY"),
                "KADATH_MODEL": runtime.get("model") or os.environ.get("KADATH_MODEL"),
            }.items() if value}
        return {**inherited, "KADATH_ENABLED_OPTIONAL_TOOLS": ",".join(runtime.get("enabled_optional_capabilities", [])), **runtime.get("agent_env", {})}

    def _architect_output(self, run_id: str) -> dict[str, Any]:
        try:
            return json.loads((self._run_dir(run_id) / "architect-output.json").read_text())
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise RunError("architect output is missing or invalid") from exc

    def _organism_adapt(self, run: dict[str, Any], agent: dict[str, Any], genome: dict[str, Any], state_dir: Path) -> Mutation:
        deadline = datetime.now(UTC) + timedelta(seconds=min(run["epoch_seconds"], 300))
        runtime = json.loads((self._run_dir(run["id"]) / "runtime.json").read_text())
        token = WorkerBroker.token()
        scope = ParentWorkerScope(agent["agent_id"], token, self._agent_dir(run["id"], agent["agent_id"]) / "repository", state_dir, genome["hash"], genome["prompt"], deadline)
        broker: WorkerBroker | None = None
        broker_url: str | None = None
        if runtime.get("executor") == "docker":
            adaptation_attempt = f"adaptation-{run['current_epoch']:04d}-{agent['agent_id']}-{uuid.uuid4().hex}"
            broker = WorkerBroker([scope], runtime["image"], runtime["command"], runtime.get("network", "none"), self._runtime_environment(runtime), os.getenv("KADATH_BROKER_ADVERTISE_HOST"), self._store(run["id"]), run["id"], run["current_epoch"], self._run_dir(run["id"]) / "model-calls", adaptation_attempt)
            broker_url = broker.start()
        request = ExecutionRequest(run["id"], run["current_epoch"], agent["agent_id"], genome["hash"], run["goal"], run["criterion"], genome["prompt"], self._agent_dir(run["id"], agent["agent_id"]) / "repository", state_dir, deadline, adaptation_context_path=state_dir / "adaptation-context.json", worker_broker_url=broker_url, worker_token=token if broker_url else None, attempt_id=broker.attempt_id if broker else f"adaptation-{run['current_epoch']:04d}-{agent['agent_id']}")
        executor = self._executor(run["id"])
        try:
            if hasattr(executor, "adapt"):
                executor.adapt(request)
        finally:
            if broker: broker.stop()
        return Mutation.from_state(state_dir)

    @staticmethod
    def _shared_knowledge(store: Store, run_id: str) -> list[dict[str, Any]]:
        return store.ranked_memory(run_id, "__population_reader__", "", 500)

    @staticmethod
    def _agent_knowledge(store: Store, run_id: str, agent_id: str) -> list[dict[str, Any]]:
        return store.ranked_memory(run_id, agent_id, "", 500)

    @staticmethod
    def _collect_activity(store: Store, run_id: str, epoch: int, agent_id: str, state_dir: Path, attempt_id: str = "") -> None:
        agent = store.one("SELECT active_genome FROM agents WHERE run_id=? AND agent_id=?", (run_id, agent_id))
        genome_hash = agent["active_genome"] if agent else ""
        outbox = state_dir / "activity.jsonl"
        if outbox.is_file():
            lines = outbox.read_text().splitlines()[-100:]
            outbox.unlink()
            for line in lines:
                try:
                    activity = json.loads(line)
                    summary = activity["summary"]
                    if not isinstance(summary, str) or not summary.strip() or len(summary) > 2000: raise ValueError
                except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                    continue
                payload = {"summary": summary, "outcome": str(activity.get("outcome", ""))[:2000], "next_step": str(activity.get("next_step", ""))[:2000], "strategy": str(activity.get("strategy", ""))[:2000], "framework_observation": str(activity.get("framework_observation", ""))[:2000], "evidence_refs": [str(item)[:500] for item in activity.get("evidence_refs", [])][:25], "result_type": str(activity.get("result_type", "activity"))[:100], "genome": genome_hash, "attempt": attempt_id or f"epoch-{epoch}", "visibility": "shared" if activity.get("visibility") == "shared" else "private"}
                store.add_knowledge(run_id, epoch, agent_id, "activity", payload, now())
        workers = state_dir / "worker-events.jsonl"
        if workers.is_file():
            lines = workers.read_text().splitlines()[-100:]; workers.unlink()
            for line in lines:
                try:
                    event = json.loads(line); summary = str(event["summary"]).strip()
                    if not summary or len(summary) > 2000: raise ValueError
                except (ValueError, KeyError, TypeError, json.JSONDecodeError): continue
                store.add_knowledge(run_id, epoch, agent_id, "worker_activity", {"summary": summary, "worker_id": str(event.get("worker_id", ""))[:200], "status": str(event.get("status", ""))[:100], "genome": genome_hash, "attempt": attempt_id or f"epoch-{epoch}", "visibility": "private"}, now())
        crashes = state_dir / "crash-log.jsonl"
        if crashes.is_file():
            attempt_status = store.one("SELECT status FROM agent_attempts WHERE run_id=? AND epoch=? AND agent_id=?", (run_id, epoch, agent_id))
            for line in crashes.read_text().splitlines()[-100:]:
                try: crash = json.loads(line)
                except json.JSONDecodeError: continue
                store.add_knowledge(run_id, epoch, agent_id, "runtime_crash", {"summary": "Parent container crashed and the kernel attempted recovery.", "outcome": "restarted" if attempt_status and attempt_status["status"] == "completed" else "failed", "crash": crash, "attempt": attempt_id or f"epoch-{epoch}", "visibility": "shared"}, now())

    def _artifact_store(self, run_id: str):
        if run_id in self._artifact_stores: return self._artifact_stores[run_id]
        endpoint = os.environ.get("KADATH_S3_ENDPOINT")
        if endpoint:
            access = os.environ.get("AWS_ACCESS_KEY_ID")
            secret = os.environ.get("AWS_SECRET_ACCESS_KEY")
            if not access or not secret:
                raise RunError("S3 artifact storage needs AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY")
            store: ArtifactStore | S3ArtifactStore = S3ArtifactStore(endpoint, os.environ.get("KADATH_S3_BUCKET", "kadath"), access, secret)
        else:
            store = ArtifactStore(self._run_dir(run_id) / "artifacts")
        self._artifact_stores[run_id] = store
        return store

    def _agent_dir(self, run_id: str, agent_id: str) -> Path:
        directory = self._run_dir(run_id) / "agents" / agent_id
        (directory / "state").mkdir(parents=True, exist_ok=True)
        (directory / "browser-profile").mkdir(parents=True, exist_ok=True)
        return directory

    def _materialize_agent(self, run_id: str, agent_id: str, branch: str, source_commit: str | None, prompt: str, message: str, mutation: Mutation | None = None) -> tuple[str, str]:
        seed = Path(os.environ.get("KADATH_SEED_DIR", Path(__file__).resolve().parents[1] / "seed" / "smolagents"))
        if not seed.is_dir():
            raise RunError("seed/smolagents is unavailable; set KADATH_SEED_DIR to the vendored framework directory")
        repo = GitStore(self._run_dir(run_id), seed).materialize(agent_id, branch, source_commit)
        (repo / "SYSTEM_PROMPT.md").write_text(prompt + "\n")
        if mutation:
            mutation.apply(repo)
        prompt_path = repo / "SYSTEM_PROMPT.md"
        if not prompt_path.is_file():
            raise MutationError("mutation removed SYSTEM_PROMPT.md")
        try: effective_prompt = prompt_path.read_text().rstrip("\n")
        except UnicodeDecodeError as exc: raise MutationError("SYSTEM_PROMPT.md must be UTF-8 text") from exc
        maximum = int(os.getenv("KADATH_SYSTEM_PROMPT_BYTES", str(2 * 1024 * 1024)))
        if not effective_prompt.strip() or len(effective_prompt.encode()) > maximum:
            raise MutationError(f"system prompt must be non-empty and no larger than {maximum} bytes")
        # SYSTEM_PROMPT.md is the evolvable source of truth. Register exactly
        # what the mutation produced instead of keeping a stale database prompt.
        prompt_path.write_text(effective_prompt + "\n")
        return GitStore.commit(repo, message), effective_prompt

    @staticmethod
    def _git_commit(genome: dict[str, Any]) -> str:
        return json.loads(genome["manifest_json"])["git_commit"]

    def status(self, run_id: str) -> dict[str, Any]:
        store, run = self._store(run_id), self._require_run(run_id)
        scores = store.rows("SELECT agent_id,value,tie_break_value,outcome,last_rank FROM scores JOIN agents USING(run_id,agent_id) WHERE scores.run_id=? AND epoch=? ORDER BY CASE outcome WHEN 'success' THEN 0 ELSE 1 END,value DESC,tie_break_value DESC,agent_id LIMIT 10", (run_id, run["current_epoch"])) if run["current_epoch"] else []
        epochs = store.rows("SELECT epoch,status,started_at,completed_at FROM epochs WHERE run_id=? ORDER BY epoch DESC LIMIT 5", (run_id,))
        attempt_epoch = epochs[0]["epoch"] if epochs else None
        attempts = store.rows("SELECT status,COUNT(*) AS count FROM agent_attempts WHERE run_id=? AND epoch=? GROUP BY status", (run_id, attempt_epoch)) if attempt_epoch else []
        recent_activity = store.rows(
            "SELECT epoch,agent_id,kind,payload_json,published_at FROM knowledge "
            "WHERE run_id=? AND kind IN ('activity','worker_activity','worker_completion','kernel_activity','epoch_report','adaptation','adaptation_crash','runtime_crash') "
            "ORDER BY id DESC LIMIT 8",
            (run_id,),
        )
        operations = {
            "model_calls": store.one("SELECT COUNT(*) AS count FROM events WHERE run_id=? AND event_type IN ('model_call','specialist_model_call')", (run_id,))["count"],
            "runtime_crashes": store.one("SELECT COUNT(*) AS count FROM knowledge WHERE run_id=? AND kind IN ('runtime_crash','adaptation_crash')", (run_id,))["count"],
            "worker_records": store.one("SELECT COUNT(*) AS count FROM knowledge WHERE run_id=? AND kind IN ('worker_activity','worker_completion')", (run_id,))["count"],
        }
        return {"run": run, "population": store.rows("SELECT status,COUNT(*) AS count FROM agents WHERE run_id=? GROUP BY status", (run_id,)), "leaders": scores, "epochs": epochs, "attempt_epoch": attempt_epoch, "attempts": attempts, "operations": operations, "recent_activity": recent_activity}

    def export(self, run_id: str) -> Path:
        with self._run_guard(run_id):
            return self._export_unlocked(run_id)

    def _export_unlocked(self, run_id: str) -> Path:
        run = self._require_run(run_id); store = self._store(run_id)
        if run["status"] not in {"complete", "failed"}:
            raise RunError("export is available only after the run has reached a terminal state")
        final_target = self.root.parent / "exports" / run_id
        final_target.parent.mkdir(parents=True, exist_ok=True)
        target = final_target.with_name("." + run_id + ".exporting")
        if target.exists(): make_tree_owner_writable(target); shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        agents = store.rows("SELECT * FROM agents WHERE run_id=? ORDER BY agent_id", (run_id,))
        genomes = store.rows("SELECT * FROM genomes WHERE run_id=? ORDER BY created_epoch,hash", (run_id,))
        epochs = store.rows("SELECT * FROM epochs WHERE run_id=? ORDER BY epoch", (run_id,))
        scores = store.rows("SELECT * FROM scores WHERE run_id=? ORDER BY epoch,CASE outcome WHEN 'success' THEN 0 ELSE 1 END,value DESC,tie_break_value DESC,agent_id", (run_id,))
        attempts = store.rows("SELECT * FROM agent_attempts WHERE run_id=? ORDER BY epoch, agent_id", (run_id,))
        events = store.rows("SELECT * FROM events WHERE run_id=? ORDER BY id", (run_id,))
        lineage = store.rows("SELECT * FROM lineage WHERE run_id=?", (run_id,))
        knowledge = store.rows("SELECT * FROM knowledge WHERE run_id=?", (run_id,))
        memory_links = store.rows("SELECT * FROM memory_links WHERE run_id=?", (run_id,))
        knowledge_ratings = store.rows("SELECT * FROM knowledge_ratings WHERE run_id=?", (run_id,))
        payload = {"run": run, "agents": agents, "genomes": genomes, "epochs": epochs, "scores": scores, "attempts": attempts, "events": events, "lineage": lineage, "knowledge": knowledge, "memory_links": memory_links, "knowledge_ratings": knowledge_ratings}
        (target / "run-report.json").write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
        shutil.copy2(self._run_dir(run_id) / "objective-definition.json", target / "objective-definition.json")
        for name, data in {"genome-registry": genomes, "epochs": epochs, "leaderboards": scores, "lineage": lineage, "shared-knowledge": knowledge, "memory-links": memory_links, "knowledge-ratings": knowledge_ratings}.items():
            directory = target / name; directory.mkdir(exist_ok=True)
            (directory / "records.json").write_text(json.dumps(data, indent=2, sort_keys=True, default=str))
        final = target / "final-population"; archived = target / "archived-agents"
        final.mkdir(exist_ok=True); archived.mkdir(exist_ok=True)
        states = target / "agent-states"; states.mkdir(exist_ok=True)
        for agent in agents:
            source = self._agent_dir(run_id, agent["agent_id"]) / "repository"
            manifest = {"agent": agent, "repository_present": source.exists()}
            destination = final if agent["status"] == "active" else archived
            (destination / f"{agent['agent_id']}.json").write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str))
            if source.exists() and agent["status"] == "active":
                shutil.copytree(source, final / agent["agent_id"], dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git"))
            elif source.exists():
                shutil.copytree(source, archived / agent["agent_id"], dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git"))
            source_state = self._agent_dir(run_id, agent["agent_id"]) / "state"
            destination_state = states / agent["agent_id"]
            destination_state.mkdir(exist_ok=True)
            transient = {"result.json", "activity.jsonl", "mutation.json", "adaptation-context.json", "shared-knowledge.json", "finalize.requested", "checkpoint.json", "worker-events.jsonl", "workers", "crash-log.jsonl"}
            for item in source_state.iterdir() if source_state.exists() else []:
                if item.name in transient or item.is_symlink(): continue
                destination = destination_state / item.name
                if item.is_dir(): copy_tree_without_symlinks(item, destination)
                elif stat.S_ISREG(item.lstat().st_mode): shutil.copy2(item, destination, follow_symlinks=False)
        champions = []
        for epoch in sorted({score["epoch"] for score in scores}):
            champion = next(score for score in scores if score["epoch"] == epoch)
            champions.append(champion)
        champions_dir = target / "epoch-champions"; champions_dir.mkdir(exist_ok=True)
        (champions_dir / "records.json").write_text(json.dumps(champions, indent=2, sort_keys=True, default=str))
        top_dir = target / "top-historical-genomes"; top_dir.mkdir(exist_ok=True)
        top = []
        seen_genomes: set[str] = set()
        for score in sorted(scores, key=lambda item: (item["outcome"] != "success", -item["value"], -item["tie_break_value"], item["epoch"], item["agent_id"])):
            if score["genome_hash"] in seen_genomes: continue
            seen_genomes.add(score["genome_hash"]); top.append(score)
            if len(top) >= 100: break
        (top_dir / "records.json").write_text(json.dumps(top, indent=2, sort_keys=True, default=str))
        raw = target / "raw-measurements"; raw.mkdir(exist_ok=True)
        (raw / "records.json").write_text(json.dumps(scores, indent=2, sort_keys=True, default=str))
        source_git = self._run_dir(run_id) / "git-repository"
        if source_git.exists():
            shutil.copytree(source_git, target / "git-repository", dirs_exist_ok=True)
            diff_dir = target / "framework-diffs"; diff_dir.mkdir(exist_ok=True)
            seed_commit = subprocess.check_output(["git", f"--git-dir={source_git}", "rev-list", "--max-parents=0", "HEAD"], text=True).strip().splitlines()[0]
            for agent in agents:
                if agent["status"] != "active": continue
                genome = store.one("SELECT manifest_json FROM genomes WHERE hash=?", (agent["active_genome"],))
                commit = json.loads(genome["manifest_json"])["git_commit"]
                patch = subprocess.check_output(["git", f"--git-dir={source_git}", "diff", "--binary", seed_commit, commit], text=True)
                (diff_dir / f"{agent['agent_id']}.patch").write_text(patch)
        self._artifact_store(run_id).export_run(run_id, target / "artifacts")
        for name in ("architect-output.json", "environment-inventory.json", "runtime.json", "tool-manifest.json"):
            source = self._run_dir(run_id) / name
            if source.exists(): shutil.copy2(source, target / name)
        source_genomes = self._run_dir(run_id) / "genomes"
        if source_genomes.exists(): shutil.copytree(source_genomes, target / "genome-manifests", dirs_exist_ok=True, symlinks=True)
        for name in ("tweaker-reports", "birther-reports", "model-calls", "specialist-model-calls", "specialist-checkpoints", "grader-checkpoints", "frozen-attempts", "inherited-frozen-attempts"):
            source = self._run_dir(run_id) / name
            if source.exists(): shutil.copytree(source, target / name, dirs_exist_ok=True)
        # Browser evidence is exported only from each agent's scoped state and
        # frozen attempt. Never mix a global MCP output directory into a run.
        files = []
        for item in sorted(target.rglob("*")):
            if item.is_symlink() or not item.is_file(): continue
            size, checksum = hash_file(item)
            files.append({"path": str(item.relative_to(target)), "size": size, "sha256": checksum})
        atomic_write_json(target / "export-manifest.json", {"format": 1, "run_id": run_id, "created_at": now(), "files": files})
        if final_target.exists(): make_tree_owner_writable(final_target); shutil.rmtree(final_target)
        target.replace(final_target)
        return final_target

    @staticmethod
    def _verify_export(export_dir: Path) -> None:
        manifest_path = export_dir / "export-manifest.json"
        if not manifest_path.is_file(): raise RunError("export manifest is missing")
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("format") != 1 or not isinstance(manifest.get("files"), list): raise RunError("export manifest is invalid")
        expected_paths = set()
        for record in manifest["files"]:
            relative = Path(str(record.get("path", "")))
            if relative.is_absolute() or ".." in relative.parts: raise RunError("export manifest contains an unsafe path")
            expected_paths.add(str(relative))
            path = export_dir / relative
            if path.is_symlink() or not path.is_file():
                raise RunError(f"export integrity check failed: {relative}")
            size, checksum = hash_file(path)
            if size != int(record["size"]) or checksum != record["sha256"]:
                raise RunError(f"export integrity check failed: {relative}")
        actual_paths = set()
        for path in export_dir.rglob("*"):
            if path.is_symlink(): raise RunError(f"export contains an untrusted symbolic link: {path.relative_to(export_dir)}")
            if path.is_file() and path != manifest_path: actual_paths.add(str(path.relative_to(export_dir)))
        if actual_paths != expected_paths: raise RunError("export contains files not covered by its integrity manifest")

    def reset(self, run_id: str) -> None:
        self._require_run(run_id)
        with self._run_guard(run_id): self._reset_unlocked(run_id)

    def cleanup_completed(self, older_than_days: int | None = None) -> dict[str, list[str]]:
        if older_than_days is not None and older_than_days < 0:
            raise RunError("cleanup age must be zero or greater")
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days) if older_than_days is not None else None
        removed: list[str] = []
        protected: list[str] = []
        if not self.root.exists():
            return {"removed": removed, "protected": protected}
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir() or directory.name.startswith("."):
                continue
            run_id = directory.name
            try:
                run = self._require_run(run_id)
            except RunError:
                continue
            if run["status"] not in {"complete", "failed"}:
                protected.append(run_id)
                continue
            created_at = datetime.fromisoformat(str(run["created_at"]).replace("Z", "+00:00"))
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            if cutoff is not None and created_at > cutoff:
                continue
            self.reset(run_id)
            removed.append(run_id)
        return {"removed": removed, "protected": protected}

    def _reset_unlocked(self, run_id: str) -> None:
        directory = self._run_dir(run_id)
        if directory.parent.resolve() != self.root.resolve():
            raise RunError("refusing to remove an unknown run")
        store = self._store(run_id)
        if not store.one("SELECT id FROM runs WHERE id=?", (run_id,)):
            raise RunError("refusing to remove an unknown run")
        self._remove_orphan_containers(run_id, store)
        staged = directory.with_name(f".{run_id}.resetting")
        if staged.exists():
            raise RunError("a previous reset requires manual inspection")
        if directory.exists():
            directory.rename(staged)
            if not store.is_postgres:
                store.path = staged / "state.sqlite"
        try:
            self._artifact_store(run_id).delete_run(run_id)
            store.delete_run(run_id)
        except Exception:
            if staged.exists(): staged.rename(directory)
            if not store.is_postgres: store.path = directory / "state.sqlite"
            raise
        if staged.exists(): make_tree_owner_writable(staged); shutil.rmtree(staged)
        store.close()
        self._stores.pop(run_id, None)
        self._artifact_stores.pop(run_id, None)

    def _remove_orphan_containers(self, run_id: str, store: Store | None = None) -> int:
        """Remove only Docker resources carrying this run's kernel label."""
        try:
            output = subprocess.check_output(["docker", "ps", "-aq", "--filter", f"label=kadath.run_id={run_id}", "--filter", "label=kadath.managed=true"], text=True, timeout=20, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError): return 0
        identifiers = [line.strip() for line in output.splitlines() if line.strip()]
        if identifiers:
            subprocess.run(["docker", "rm", "-f", *identifiers], check=True, capture_output=True, timeout=60)
            if store: store.add_event(run_id, "orphan_containers_removed", {"count": len(identifiers)}, now())
        return len(identifiers)

    def _require_run(self, run_id: str) -> dict[str, Any]:
        run = self._store(run_id).one("SELECT * FROM runs WHERE id=?", (run_id,))
        if not run:
            raise RunError(f"run not found: {run_id}")
        return run
