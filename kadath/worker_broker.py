"""Kernel-owned HTTP broker through which parents request temporary workers."""
from __future__ import annotations

import json
import hashlib
import os
import secrets
import socket
import urllib.parse
import urllib.request
import urllib.error
import time
import subprocess
import uuid
from datetime import UTC, datetime
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread, Semaphore
from typing import Any

from .store import Store
from .workers import DockerWorkerPool


@dataclass(frozen=True)
class ParentWorkerScope:
    agent_id: str
    token: str
    repository: Path
    state_dir: Path
    genome_hash: str
    objective_prompt: str
    deadline: datetime
    allowed_worker_tools: tuple[str, ...] = ()
    browser_url: str | None = None
    browser_profile: Path | None = None
    browser_image: str | None = None


def _redact(value: Any, key: str = "", secrets_to_remove: tuple[str, ...] = ()) -> Any:
    if any(word in key.lower() for word in ("authorization", "api_key", "password", "secret", "token")):
        return "[REDACTED]"
    if isinstance(value, dict): return {str(k): _redact(v, str(k), secrets_to_remove) for k, v in value.items()}
    if isinstance(value, list): return [_redact(item, key, secrets_to_remove) for item in value]
    if isinstance(value, str):
        for secret in secrets_to_remove:
            if secret: value = value.replace(secret, "[REDACTED]")
        if len(value) > 200_000: return value[:200_000] + "...[truncated]"
    return value


def _file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk: break
            hasher.update(chunk)
    return hasher.hexdigest()


class WorkerBroker:
    def __init__(self, scopes: list[ParentWorkerScope], image: str, command: list[str], network: str = "none", environment: dict[str, str] | None = None, advertise_host: str | None = None, store: Store | None = None, run_id: str = "", epoch: int = 0, trace_root: Path | None = None, attempt_id: str = ""):
        self.scopes = {scope.agent_id: scope for scope in scopes}
        self.image, self.command, self.network = image, command, network
        self.pool = DockerWorkerPool(max_total_workers=max(1, int(os.getenv("KADATH_WORKER_GLOBAL_LIMIT", "500"))))
        self.attempt_id = attempt_id or f"attempt-{uuid.uuid4().hex}"
        self.environment = {**(environment or {}), "KADATH_RUN_ID": run_id, "KADATH_EPOCH": str(epoch), "KADATH_ATTEMPT_ID": self.attempt_id}
        self.advertise_host = advertise_host
        self.store, self.run_id, self.epoch = store, run_id, epoch
        self.trace_root = trace_root
        self.jobs: dict[tuple[str, str], Any] = {}
        self.server: ThreadingHTTPServer | None = None
        self._model_limits = {scope.agent_id: Semaphore(2) for scope in scopes}
        self._global_model_limit = Semaphore(max(1, int(os.getenv("KADATH_MODEL_GLOBAL_CONCURRENCY", "64"))))
        self._model_identities: dict[str, dict[str, Any]] = {scope.token: {"scope": scope, "worker_id": None} for scope in scopes}
        self._publish_counts: dict[str, int] = {scope.agent_id: 0 for scope in scopes}
        self._telemetry_secrets = tuple(str(value) for key, value in self.environment.items() if len(str(value)) >= 6 and any(word in key.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")))

    @staticmethod
    def token() -> str: return secrets.token_urlsafe(32)

    def start(self, host: str = "0.0.0.0", port: int = 0) -> str:
        broker = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args): pass
            def _json(self, status: int, value: dict) -> None:
                encoded = json.dumps(value).encode(); self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(encoded))); self.end_headers(); self.wfile.write(encoded)
            def _read_json(self, maximum_bytes: int) -> dict[str, Any]:
                try: length = int(self.headers.get("Content-Length", "0"))
                except ValueError as exc: raise ValueError("invalid request length") from exc
                if length <= 0 or length > maximum_bytes: raise ValueError(f"request exceeds {maximum_bytes}-byte limit")
                value = json.loads(self.rfile.read(length))
                if not isinstance(value, dict): raise ValueError("request body must be a JSON object")
                return value
            def _authorized_identity(self, agent_id: str, token: str):
                scope = broker.scopes.get(agent_id)
                if scope is None: raise PermissionError
                if secrets.compare_digest(scope.token, token): return scope, None
                identity = next((value for candidate, value in broker._model_identities.items() if secrets.compare_digest(candidate, token)), None)
                if identity is None or identity["scope"].agent_id != agent_id: raise PermissionError
                return scope, identity.get("worker_id")
            def do_POST(self):
                if self.path in {"/chat/completions", "/v1/chat/completions"}:
                    return self._model_completion()
                if self.path == "/knowledge":
                    try:
                        data = self._read_json(int(os.getenv("KADATH_KNOWLEDGE_REQUEST_BYTES", "65536")))
                        scope, worker_id = self._authorized_identity(str(data["agent_id"]), str(data["token"]))
                        if datetime.now(UTC) >= scope.deadline: raise RuntimeError("epoch deadline has closed publishing")
                        if broker.store is None: raise RuntimeError("knowledge broker is unavailable")
                        if broker._publish_counts[scope.agent_id] >= int(os.getenv("KADATH_KNOWLEDGE_RECORD_LIMIT", "200")):
                            raise RuntimeError("epoch knowledge publication limit reached")
                        summary = str(data["summary"]).strip()
                        outcome, next_step = str(data.get("outcome", "")), str(data.get("next_step", ""))
                        if not summary or any(len(value) > 2000 for value in (summary, outcome, next_step)): raise ValueError("invalid activity record")
                        evidence_refs = [str(item)[:500] for item in data.get("evidence_refs", [])][:25]
                        payload = {"summary": summary, "outcome": outcome, "next_step": next_step, "strategy": str(data.get("strategy", ""))[:2000], "framework_observation": str(data.get("framework_observation", ""))[:2000], "evidence_refs": evidence_refs, "result_type": str(data.get("result_type", "activity"))[:100], "genome": scope.genome_hash, "attempt": broker.attempt_id, "worker_id": worker_id, "visibility": "shared" if data.get("visibility") == "shared" else "private"}
                        broker.store.add_knowledge(broker.run_id, broker.epoch, scope.agent_id, "worker_activity" if worker_id else "activity", payload, datetime.now(UTC).isoformat())
                        broker._publish_counts[scope.agent_id] += 1
                        return self._json(201, {"status": "published"})
                    except Exception as exc:
                        return self._json(400, {"error": str(exc)})
                if self.path == "/knowledge/rate":
                    try:
                        data = self._read_json(16_384)
                        scope, _worker_id = self._authorized_identity(str(data["agent_id"]), str(data["token"]))
                        if broker.store is None: raise RuntimeError("knowledge broker is unavailable")
                        if not broker.store.memory_visible(broker.run_id, scope.agent_id, int(data["record_id"])): raise PermissionError("memory record is not visible to this agent")
                        broker.store.rate_knowledge(broker.run_id, int(data["record_id"]), scope.agent_id, int(data["value"]), datetime.now(UTC).isoformat())
                        return self._json(200, {"status": "rated"})
                    except Exception as exc: return self._json(400, {"error": str(exc)})
                if self.path != "/workers": return self._json(404, {"error": "not found"})
                try:
                    data = self._read_json(int(os.getenv("KADATH_WORKER_REQUEST_BYTES", "262144")))
                    scope = broker.scopes[data["agent_id"]]
                    if not secrets.compare_digest(scope.token, str(data["token"])): raise PermissionError
                    if datetime.now(UTC) >= scope.deadline: raise RuntimeError("epoch deadline has closed worker creation")
                    worker_token = WorkerBroker.token()
                    broker._model_identities[worker_token] = {"scope": scope, "worker_id": "starting"}
                    try:
                        requested_tools = data.get("tools", [])
                        if not isinstance(requested_tools, list) or any(str(item) not in scope.allowed_worker_tools for item in requested_tools): raise ValueError("worker tools must be an explicit subset of the parent's allowed tools")
                        task = data["task"]
                        if not isinstance(task, dict): raise ValueError("worker task must be a JSON object")
                        handle, future = broker.pool.spawn_container(scope.agent_id, broker.image, broker.command, scope.repository, scope.state_dir, task, broker.network, broker.environment, scope.genome_hash, scope.objective_prompt, scope.deadline, broker.public_url, worker_token, [str(item) for item in requested_tools], scope.browser_url, scope.browser_profile, scope.browser_image)
                    except Exception:
                        broker._model_identities.pop(worker_token, None)
                        raise
                    broker._model_identities[worker_token]["worker_id"] = handle.worker_id
                    broker.jobs[(scope.agent_id, handle.worker_id)] = future
                    self._json(202, {"worker_id": handle.worker_id})
                except Exception as exc:
                    self._json(400, {"error": str(exc)})
            def _model_completion(self):
                authorization = self.headers.get("Authorization", "")
                token = authorization.removeprefix("Bearer ").strip()
                identity = next((value for candidate, value in broker._model_identities.items() if secrets.compare_digest(candidate, token)), None)
                if identity is None: return self._json(403, {"error": "forbidden"})
                scope = identity["scope"]
                if datetime.now(UTC) >= scope.deadline: return self._json(408, {"error": "epoch deadline elapsed"})
                upstream = broker.environment.get("LITELLM_API_BASE", "").rstrip("/")
                upstream_key = broker.environment.get("LITELLM_API_KEY", "")
                if not upstream or not upstream_key: return self._json(503, {"error": "model gateway is not configured"})
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 5_000_000: raise ValueError("invalid model request size")
                    body = json.loads(self.rfile.read(length))
                    body["metadata"] = {**(body.get("metadata") or {}), "kadath_run_id": broker.run_id, "kadath_epoch": broker.epoch, "kadath_attempt_id": broker.attempt_id, "kadath_agent_id": scope.agent_id, "kadath_worker_id": identity.get("worker_id"), "kadath_genome": scope.genome_hash}
                    encoded = json.dumps(body).encode()
                    request = urllib.request.Request(upstream + "/v1/chat/completions", data=encoded, headers={"Content-Type": "application/json", "Authorization": f"Bearer {upstream_key}"}, method="POST")
                    started = time.monotonic(); response_data = None; last_error = None; request_id = uuid.uuid4().hex
                    with broker._global_model_limit, broker._model_limits[scope.agent_id]:
                        if datetime.now(UTC) >= scope.deadline: raise RuntimeError("epoch deadline elapsed while waiting for model capacity")
                        for attempt in range(4):
                            try:
                                with urllib.request.urlopen(request, timeout=min(180, max(1, int((scope.deadline - datetime.now(UTC)).total_seconds())))) as response:
                                    response_data = response.read(10_000_001)
                                if len(response_data) > 10_000_000: raise ValueError("model gateway response exceeded 10MB")
                                break
                            except (urllib.error.URLError, TimeoutError) as exc:
                                last_error = exc
                                if attempt < 3: time.sleep(2 ** attempt)
                    if response_data is None: raise RuntimeError(f"model gateway failed after retries: {last_error}")
                    decoded = json.loads(response_data)
                    usage = decoded.get("usage", {})
                    if broker.store:
                        trace_root = broker.trace_root or (scope.state_dir / "model-calls")
                        trace = trace_root / f"epoch-{broker.epoch:04d}" / broker.attempt_id / scope.agent_id / f"{request_id}.json"
                        trace.parent.mkdir(parents=True, exist_ok=True)
                        trace_payload = {"request_id": request_id, "run_id": broker.run_id, "epoch": broker.epoch, "attempt_id": broker.attempt_id, "agent_id": scope.agent_id, "worker_id": identity.get("worker_id"), "genome": scope.genome_hash, "requested_at": datetime.now(UTC).isoformat(), "request": _redact(body, secrets_to_remove=broker._telemetry_secrets), "response": _redact(decoded, secrets_to_remove=broker._telemetry_secrets)}
                        trace.write_text(json.dumps(trace_payload, indent=2, sort_keys=True))
                        broker.store.add_event(broker.run_id, "model_call", {"request_id": request_id, "epoch": broker.epoch, "attempt_id": broker.attempt_id, "agent_id": scope.agent_id, "worker_id": identity.get("worker_id"), "genome": scope.genome_hash, "model": body.get("model"), "requested_at": trace_payload["requested_at"], "latency_seconds": round(time.monotonic() - started, 3), "usage": usage, "trace": str(trace), "trace_sha256": _file_sha256(trace), "tool_correlation": {"request_tools": [tool.get("function", {}).get("name") for tool in body.get("tools", []) if isinstance(tool, dict)], "response_tool_calls": decoded.get("choices", [{}])[0].get("message", {}).get("tool_calls", []) if decoded.get("choices") else []}}, datetime.now(UTC).isoformat())
                    self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(response_data))); self.end_headers(); self.wfile.write(response_data)
                except Exception as exc:
                    return self._json(502, {"error": str(exc)})
            def do_GET(self):
                if self.path.startswith("/knowledge?"):
                    if broker.store is None: return self._json(503, {"error": "knowledge broker is unavailable"})
                    _, _, query = self.path.partition("?")
                    params = urllib.parse.parse_qs(query)
                    agent_id, token = params.get("agent_id", [""])[0], params.get("token", [""])[0]
                    query = params.get("q", [""])[0].lower()[:500]
                    try: limit = max(1, min(int(params.get("limit", ["100"])[0]), 500))
                    except ValueError: limit = 100
                    try: scope, _worker_id = self._authorized_identity(agent_id, token)
                    except PermissionError: return self._json(403, {"error": "forbidden"})
                    records = broker.store.ranked_memory(broker.run_id, agent_id, query, limit)
                    return self._json(200, {"records": records})
                if not self.path.startswith("/workers/"): return self._json(404, {"error": "not found"})
                path, _, query = self.path.partition("?")
                _, _, encoded = path.partition("/workers/")
                agent_id, _, worker_id = urllib.parse.unquote(encoded).partition("/")
                scope = broker.scopes.get(agent_id)
                token = urllib.parse.parse_qs(query).get("token", [""])[0]
                if scope is None or not secrets.compare_digest(scope.token, token): return self._json(403, {"error": "forbidden"})
                future = broker.jobs.get((agent_id, f"{agent_id}/{worker_id}"))
                if future is None: return self._json(404, {"error": "unknown worker"})
                if not future.done(): return self._json(200, {"status": "running"})
                try: return self._json(200, {"status": "complete", "result": future.result()})
                except Exception as exc: return self._json(200, {"status": "failed", "error": str(exc)})
        self.server = ThreadingHTTPServer((host, port), Handler)
        Thread(target=self.server.serve_forever, daemon=True).start()
        advertised = self.advertise_host
        if not advertised and os.getenv("KADATH_IN_CONTAINER") == "1":
            try:
                networks = json.loads(subprocess.check_output(["docker", "inspect", os.environ["HOSTNAME"], "--format", "{{json .NetworkSettings.Networks}}"], text=True, timeout=10))
                match = networks.get(self.network) or next((value for key, value in networks.items() if key.endswith(self.network)), None)
                advertised = match.get("IPAddress") if match else None
            except (OSError, KeyError, ValueError, subprocess.SubprocessError):
                advertised = socket.gethostbyname(socket.gethostname())
        advertised = advertised or "host.docker.internal"
        self.public_url = f"http://{advertised}:{self.server.server_port}"
        return self.public_url

    def stop(self) -> None:
        self.pool.shutdown()
        if self.store:
            for (agent_id, worker_id), future in self.jobs.items():
                try:
                    result = future.result()
                    payload = {"summary": "Temporary worker completed.", "worker_id": worker_id, "status": "complete", "genome": self.scopes[agent_id].genome_hash, "attempt": self.attempt_id, "result_summary": str(result.get("answer", result))[:4000], "returned_files": sorted((result.get("files") or {}).keys()), "visibility": "private"}
                except Exception as exc:
                    payload = {"summary": "Temporary worker failed.", "worker_id": worker_id, "status": "failed", "genome": self.scopes[agent_id].genome_hash, "attempt": self.attempt_id, "error": str(exc)[:2000], "visibility": "private"}
                self.store.add_knowledge(self.run_id, self.epoch, agent_id, "worker_completion", payload, datetime.now(UTC).isoformat())
        if self.server: self.server.shutdown(); self.server.server_close()
