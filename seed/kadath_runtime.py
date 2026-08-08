"""Organism-side access to KADATH state. It cannot access control tables."""
from __future__ import annotations

import json
import os
import ipaddress
import socket
import urllib.parse
import urllib.request
import subprocess
from pathlib import Path
from typing import Any


def search_web(query: str) -> str:
    """Search the public web through KADATH's shared SearXNG service.

    Args:
        query: The concise search query to run.
    """
    endpoint = os.environ.get("KADATH_SEARXNG_URL", "").rstrip("/")
    if not endpoint:
        raise RuntimeError("SearXNG is not configured for this run")
    query = query.strip()
    if not query or len(query) > 500:
        raise ValueError("search query must contain 1 to 500 characters")
    url = endpoint + "/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
    with urllib.request.urlopen(url, timeout=20) as response:
        payload = json.loads(response.read(1_000_000))
    results = [
        {"title": str(item.get("title", ""))[:500], "url": str(item.get("url", ""))[:2000], "snippet": str(item.get("content", ""))[:2000]}
        for item in payload.get("results", [])[:10]
    ]
    return json.dumps(results, ensure_ascii=False)


def fetch_web(url: str) -> str:
    """Retrieve bounded text from one public HTTP or HTTPS page.

    Args:
        url: The public page URL to retrieve.
    """
    _require_public_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "KADATH/0.1"})
    with urllib.request.urlopen(request, timeout=20) as response:
        final_url = response.geturl()
        _require_public_url(final_url)
        content_type = response.headers.get_content_type()
        if not (content_type.startswith("text/") or content_type in {"application/json", "application/xml", "application/xhtml+xml"}):
            raise ValueError(f"unsupported response type: {content_type}")
        return response.read(250_000).decode(response.headers.get_content_charset() or "utf-8", errors="replace")


def _require_public_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("only public HTTP and HTTPS URLs are allowed")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError("URL host could not be resolved") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("private, loopback, link-local, and reserved destinations are blocked")


def read_shared_knowledge(query: str = "", limit: int = 100) -> list[dict[str, Any]]:
    """Search the agent's inherited and population memory.

    Args:
        query: Optional case-insensitive semantic keyword filter.
        limit: Maximum records to return, from 1 to 500.
    """
    limit = max(1, min(int(limit), 500))
    broker, token, agent_id = os.environ.get("KADATH_WORKER_BROKER_URL"), os.environ.get("KADATH_WORKER_TOKEN"), os.environ.get("KADATH_AGENT_ID")
    if broker and token and agent_id:
        encoded_query = urllib.parse.urlencode({"agent_id": agent_id, "token": token, "q": query, "limit": limit})
        try:
            with urllib.request.urlopen(broker + "/knowledge?" + encoded_query, timeout=15) as response:
                return json.loads(response.read()).get("records", [])
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    path = os.environ.get("KADATH_SHARED_KNOWLEDGE_PATH")
    if not path or not Path(path).is_file(): return []
    records = json.loads(Path(path).read_text()).get("records", [])
    if query: records = [item for item in records if query.lower() in json.dumps(item).lower()]
    return records[-limit:]


def rate_knowledge(record_id: int, useful: bool = True) -> str:
    """Mark a retrieved memory as useful or misleading for population ranking."""
    broker, token, agent_id = os.environ.get("KADATH_WORKER_BROKER_URL"), os.environ.get("KADATH_WORKER_TOKEN"), os.environ.get("KADATH_AGENT_ID")
    if not broker or not token or not agent_id: raise RuntimeError("memory rating is unavailable")
    body = json.dumps({"agent_id": agent_id, "token": token, "record_id": int(record_id), "value": 1 if useful else -1}).encode()
    request = urllib.request.Request(broker + "/knowledge/rate", data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=15): pass
    return "memory usefulness recorded"


def log_activity(summary: str, outcome: str = "", next_step: str = "", shared: bool = True, strategy: str = "", evidence_refs: list[str] | None = None, framework_observation: str = "", result_type: str = "activity") -> None:
    """Append one useful big-picture record; do not log raw search chatter.

    Args:
        summary: What substantial avenue or task was investigated or performed.
        outcome: What was learned, produced, or decided.
        next_step: The best useful follow-up, if any.
        shared: Whether the population may read this record.
    """
    if not summary.strip(): raise ValueError("activity summary is required")
    if any(len(value) > 2000 for value in (summary, outcome, next_step, strategy, framework_observation)):
        raise ValueError("activity record is too long")
    record = {"summary": summary, "outcome": outcome, "next_step": next_step, "strategy": strategy, "framework_observation": framework_observation, "evidence_refs": [str(item)[:500] for item in (evidence_refs or [])][:25], "result_type": result_type, "visibility": "shared" if shared else "private"}
    broker, token, agent_id = os.environ.get("KADATH_WORKER_BROKER_URL"), os.environ.get("KADATH_WORKER_TOKEN"), os.environ.get("KADATH_AGENT_ID")
    if broker and token and agent_id:
        body = json.dumps({**record, "agent_id": agent_id, "token": token}).encode()
        request = urllib.request.Request(broker + "/knowledge", data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=15): return
        except OSError:
            pass
    state = Path(os.environ["KADATH_STATE_DIR"])
    with (state / "activity.jsonl").open("a") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")


def _safe_relative(relative: str) -> Path:
    path = Path(relative)
    if not relative or path.is_absolute() or ".." in path.parts:
        raise ValueError("path must stay inside its scoped root")
    return path


def list_repository(relative: str = ".") -> str:
    """List files in the immutable organism repository.

    Args:
        relative: Repository-relative directory, or a glob pattern.
    """
    root = Path("/organism")
    relative_path = Path(".") if relative == "." else _safe_relative(relative)
    target = root / relative_path
    items = target.glob("*") if target.is_dir() else root.glob(relative)
    return json.dumps([str(item.relative_to(root)) + ("/" if item.is_dir() else "") for item in sorted(items) if ".git" not in item.parts][:1000])


def read_repository_file(relative: str) -> str:
    """Read a UTF-8 file from the immutable organism repository.

    Args:
        relative: Repository-relative file path.
    """
    target = Path("/organism") / _safe_relative(relative)
    if not target.is_file() or ".git" in target.parts: raise ValueError("repository file does not exist")
    if target.stat().st_size > 500_000: raise ValueError("repository file is too large")
    return target.read_text(errors="replace")


def list_workspace(relative: str = ".") -> str:
    """List files in the agent's persistent writable workspace.

    Args:
        relative: Workspace-relative directory, or a glob pattern.
    """
    root = Path(os.environ["KADATH_STATE_DIR"]) / "workspace"; root.mkdir(parents=True, exist_ok=True)
    relative_path = Path(".") if relative == "." else _safe_relative(relative)
    target = root / relative_path
    items = target.glob("*") if target.is_dir() else root.glob(relative)
    return json.dumps([str(item.relative_to(root)) + ("/" if item.is_dir() else "") for item in sorted(items)][:1000])


def read_workspace_file(relative: str) -> str:
    """Read a UTF-8 file from the agent's persistent workspace.

    Args:
        relative: Workspace-relative file path.
    """
    root = Path(os.environ["KADATH_STATE_DIR"]) / "workspace"
    target = root / _safe_relative(relative)
    if not target.is_file() or target.stat().st_size > 1_000_000: raise ValueError("workspace file is missing or too large")
    return target.read_text(errors="replace")


def write_workspace_file(relative: str, content: str) -> str:
    """Write a UTF-8 file to the persistent workspace.

    Args:
        relative: Workspace-relative file path.
        content: Complete file content, at most one megabyte.
    """
    if len(content.encode()) > 1_000_000: raise ValueError("workspace write is too large")
    root = Path(os.environ["KADATH_STATE_DIR"]) / "workspace"; root.mkdir(parents=True, exist_ok=True)
    target = root / _safe_relative(relative); target.parent.mkdir(parents=True, exist_ok=True); target.write_text(content)
    return f"wrote {target.relative_to(root)}"


def delete_workspace_file(relative: str) -> str:
    """Delete one file from the persistent workspace.

    Args:
        relative: Workspace-relative file path.
    """
    root = Path(os.environ["KADATH_STATE_DIR"]) / "workspace"
    target = root / _safe_relative(relative)
    if target.is_dir(): raise ValueError("directory deletion is not allowed")
    target.unlink(missing_ok=True)
    return f"deleted {target.relative_to(root)}"


def run_workspace_command(command: str, timeout_seconds: int = 30) -> str:
    """Run a bounded shell command inside the writable workspace.

    Args:
        command: Shell command to execute. The workspace is the working directory.
        timeout_seconds: Timeout from 1 to 120 seconds.
    """
    if not command.strip() or len(command) > 4000: raise ValueError("invalid command")
    root = Path(os.environ["KADATH_STATE_DIR"]) / "workspace"; root.mkdir(parents=True, exist_ok=True)
    timeout_seconds = max(1, min(int(timeout_seconds), 120))
    completed = subprocess.run(["/bin/sh", "-lc", command], cwd=root, text=True, capture_output=True, timeout=timeout_seconds)
    output = (completed.stdout + completed.stderr)[-100_000:]
    return json.dumps({"exit_code": completed.returncode, "output": output})


def save_artifact(name: str, content: str) -> str:
    """Save a named text artifact for grading and export.

    Args:
        name: Simple artifact filename or relative path.
        content: Text artifact content, at most two megabytes.
    """
    if len(content.encode()) > 2_000_000: raise ValueError("artifact is too large")
    root = Path(os.environ["KADATH_STATE_DIR"]) / "artifacts"; root.mkdir(parents=True, exist_ok=True)
    target = root / _safe_relative(name); target.parent.mkdir(parents=True, exist_ok=True); target.write_text(content)
    return f"saved {target.relative_to(root)}"


def propose_mutation(action: str, reason: str, prompt_suffix: str = "", files: dict[str, str] | None = None, delete_files: list[str] | None = None) -> None:
    """Propose a post-epoch framework change, or explicitly stay unchanged.

    Args:
        action: Either 'mutate' or 'unchanged'.
        reason: Concise reflection explaining the choice.
        prompt_suffix: Optional system-prompt addition for a mutation.
        files: Complete repository file replacements for a mutation.
        delete_files: Repository-relative files to delete for a mutation.
    """
    if action not in {"mutate", "unchanged"}: raise ValueError("action must be mutate or unchanged")
    path = Path(os.environ["KADATH_STATE_DIR"]) / "mutation.json"
    path.write_text(json.dumps({"action": action, "reason": reason, "prompt_suffix": prompt_suffix, "files": files or {}, "delete_files": delete_files or []}, sort_keys=True))


def spawn_worker(task: dict[str, Any], tools: list[str] | None = None) -> str:
    """Start one bounded temporary worker for an independent subproblem.

    Args:
        task: Structured task description, optionally including timeout_seconds.
    """
    url, token, agent_id = os.environ.get("KADATH_WORKER_BROKER_URL"), os.environ.get("KADATH_WORKER_TOKEN"), os.environ.get("KADATH_AGENT_ID")
    if not url or not token or not agent_id: raise RuntimeError("temporary workers are not configured for this run")
    request = urllib.request.Request(url + "/workers", data=json.dumps({"agent_id": agent_id, "token": token, "task": task, "tools": tools or []}).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=15) as response:
        worker_id = json.loads(response.read())["worker_id"]
    _worker_event({"summary": "Delegated a bounded subproblem to a temporary worker.", "worker_id": worker_id, "visibility": "private"})
    return worker_id


def worker_result(worker_id: str) -> dict[str, Any]:
    """Poll or collect a temporary worker's structured result.

    Args:
        worker_id: Identifier returned by spawn_worker.
    """
    url, token = os.environ["KADATH_WORKER_BROKER_URL"], os.environ["KADATH_WORKER_TOKEN"]
    with urllib.request.urlopen(url + "/workers/" + worker_id.replace("/", "%2F") + "?" + urllib.parse.urlencode({"token": token}), timeout=15) as response:
        result = json.loads(response.read())
    if result.get("status") in {"complete", "failed"}:
        _worker_event({"summary": "Collected a temporary worker result.", "worker_id": worker_id, "status": result["status"], "visibility": "private"})
    return result


def _worker_event(event: dict[str, Any]) -> None:
    state = Path(os.environ["KADATH_STATE_DIR"])
    with (state / "worker-events.jsonl").open("a") as stream:
        stream.write(json.dumps(event, sort_keys=True) + "\n")


def capture_step(step: Any, agent: Any = None) -> None:
    """Keep local detail, then emit one useful summary at the end of a pass."""
    state = Path(os.environ["KADATH_STATE_DIR"])
    tool_names = [getattr(call, "name", "tool") for call in (getattr(step, "tool_calls", None) or [])]
    observation = str(getattr(step, "observations", "") or "").replace("\n", " ")[:500]
    record = {"step": getattr(step, "step_number", None), "tools": tool_names, "tool_call_ids": [str(getattr(call, "id", ""))[:200] for call in (getattr(step, "tool_calls", None) or [])], "observation": observation, "error": str(getattr(step, "error", "") or "")[:300]}
    with (state / "step-trace.jsonl").open("a") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")


def flush_activity(final_answer: str) -> None:
    state = Path(os.environ["KADATH_STATE_DIR"])
    trace = state / "step-trace.jsonl"
    lines = trace.read_text().splitlines() if trace.is_file() else []
    cursor_path = state / "step-trace.cursor"
    try: cursor = int(cursor_path.read_text()) if cursor_path.is_file() else 0
    except ValueError: cursor = 0
    steps = [json.loads(line) for line in lines[cursor:]]
    cursor_path.write_text(str(len(lines)))
    tools = sorted({tool for step in steps for tool in step.get("tools", [])})
    observations = [step["observation"] for step in steps if step.get("observation")]
    summary = f"Worked through {len(steps)} reasoning steps"
    if tools: summary += f" using {', '.join(tools[:8])}"
    outcome = (observations[-1] if observations else str(final_answer))[:1000]
    log_activity(summary + ".", outcome, "Continue from the saved evidence and improve the next approach.", strategy="fresh-context execution pass", result_type="iteration_summary")
