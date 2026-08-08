"""Kernel-owned per-parent Playwright MCP containers and artifact scopes."""
from __future__ import annotations

import os
import re
import subprocess
import shutil
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from pathlib import Path

from .containers import docker_host_path


class BrowserFleet:
    def __init__(self, network: str, image: str | None = None):
        self.network = network
        self.image = image or os.getenv("KADATH_BROWSER_IMAGE", "mcr.microsoft.com/playwright/mcp:latest")
        self.containers: dict[str, str] = {}

    def start(self, run_id: str, agents: list[tuple[str, Path]]) -> dict[str, str]:
        def launch(item: tuple[str, Path]) -> tuple[str, str, str]:
            agent_id, state = item
            artifacts = state / "browser-artifacts"; artifacts.mkdir(parents=True, exist_ok=True); artifacts.chmod(0o777)
            profile = state / "browser-profile"; profile.mkdir(parents=True, exist_ok=True); profile.chmod(0o777)
            name = re.sub(r"[^a-zA-Z0-9_.-]", "-", f"kadath-browser-{run_id}-{agent_id}")[-63:]
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            command = [
                "docker", "run", "-d", "--name", name, "--network", self.network,
                "--label", "kadath.managed=true", "--label", f"kadath.run_id={run_id}", "--label", f"kadath.agent_id={agent_id}", "--label", "kadath.role=browser",
                "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                "--pids-limit", "256", "--memory", "1g", "--memory-swap", "1g", "--shm-size", "512m",
                "--tmpfs", "/tmp:rw,nosuid,size=256m",
                "--mount", f"type=bind,src={docker_host_path(artifacts)},dst=/artifacts",
                "--mount", f"type=bind,src={docker_host_path(profile)},dst=/browser-profile",
                "--entrypoint", "node", self.image, "/app/cli.js", "--headless", "--browser", "chromium",
                "--no-sandbox", "--user-data-dir", "/browser-profile", "--allowed-hosts=*", "--output-dir", "/artifacts",
                "--output-max-size", "1073741824", "--save-session", "--port", "8931", "--host", "0.0.0.0",
            ]
            subprocess.run(command, check=True, capture_output=True)
            try: wait_for_browser(name)
            except Exception:
                subprocess.run(["docker", "rm", "-f", name], capture_output=True); raise
            return agent_id, name, f"http://{name}:8931/mcp"

        urls: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=min(32, max(1, len(agents)))) as pool:
            futures = [pool.submit(launch, item) for item in agents]
            try:
                for future in as_completed(futures):
                    agent_id, name, url = future.result(); self.containers[agent_id] = name; urls[agent_id] = url
            except Exception:
                self.stop(); raise
        return urls

    def stop(self) -> None:
        names = list(self.containers.values()); self.containers.clear()
        if not names: return
        with ThreadPoolExecutor(max_workers=min(32, len(names))) as pool:
            list(pool.map(lambda name: subprocess.run(["docker", "rm", "-f", name], capture_output=True), names))


def copy_browser_profile(source: Path, destination: Path) -> None:
    """Snapshot durable Chromium state, using SQLite backup for live databases."""
    staging = destination.with_name(destination.name + ".snapshot-" + uuid.uuid4().hex)
    if staging.exists(): shutil.rmtree(staging)
    staging.mkdir(parents=True); staging.chmod(0o777)
    ignored_names = {"DevToolsActivePort", "LOCK"}
    if source.exists():
        for item in sorted(source.rglob("*")):
            relative = item.relative_to(source)
            if any(part.startswith("Singleton") or part.endswith((".lock", "-wal", "-shm", "-journal")) or part in ignored_names for part in relative.parts): continue
            try:
                if item.is_symlink(): continue
                target = staging / relative
                if item.is_dir(): target.mkdir(parents=True, exist_ok=True); continue
                if not item.is_file(): continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with item.open("rb") as stream: sqlite_header = stream.read(16) == b"SQLite format 3\x00"
                if sqlite_header:
                    source_uri = item.resolve().as_uri() + "?mode=ro"
                    with closing(sqlite3.connect(source_uri, uri=True, timeout=10)) as source_db, closing(sqlite3.connect(target)) as target_db:
                        source_db.backup(target_db)
                else:
                    for attempt in range(3):
                        try:
                            shutil.copy2(item, target, follow_symlinks=False)
                            break
                        except OSError:
                            if attempt == 2: raise
                            time.sleep(.05 * (attempt + 1))
            except (OSError, sqlite3.Error):
                # A transient cache file must not prevent a worker from getting
                # all other durable browser memory.
                continue
    if destination.exists(): shutil.rmtree(destination)
    staging.replace(destination)
    destination.chmod(0o777)
    for item in destination.rglob("*"):
        try: item.chmod(0o777 if item.is_dir() else 0o666)
        except OSError: pass


def start_worker_browser(run_id: str, parent_id: str, network: str, image: str, source_profile: Path, worker_dir: Path) -> tuple[str, str]:
    profile = worker_dir / "browser-profile"; artifacts = worker_dir / "browser-artifacts"
    try: copy_browser_profile(source_profile, profile)
    except (OSError, shutil.Error):
        if profile.exists(): shutil.rmtree(profile)
        profile.mkdir(parents=True); profile.chmod(0o777)
    artifacts.mkdir(parents=True, exist_ok=True); artifacts.chmod(0o777)
    name = re.sub(r"[^a-zA-Z0-9_.-]", "-", f"kadath-worker-browser-{run_id}-{parent_id}-{uuid.uuid4().hex[:8]}")[-63:]
    command = [
        "docker", "run", "-d", "--name", name, "--network", network,
        "--label", "kadath.managed=true", "--label", f"kadath.run_id={run_id}", "--label", f"kadath.agent_id={parent_id}", "--label", "kadath.role=worker-browser",
        "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--pids-limit", "256", "--memory", "1g", "--memory-swap", "1g", "--shm-size", "512m", "--tmpfs", "/tmp:rw,nosuid,size=256m",
        "--mount", f"type=bind,src={docker_host_path(artifacts)},dst=/artifacts",
        "--mount", f"type=bind,src={docker_host_path(profile)},dst=/browser-profile",
        "--entrypoint", "node", image, "/app/cli.js", "--headless", "--browser", "chromium", "--no-sandbox",
        "--user-data-dir", "/browser-profile", "--allowed-hosts=*", "--output-dir", "/artifacts", "--output-max-size", "268435456", "--save-session", "--port", "8931", "--host", "0.0.0.0",
    ]
    subprocess.run(command, check=True, capture_output=True)
    try: wait_for_browser(name)
    except Exception:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True); raise
    return name, f"http://{name}:8931/mcp"


def wait_for_browser(name: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        check = subprocess.run(["docker", "exec", name, "node", "-e", "fetch('http://127.0.0.1:8931/mcp').then(()=>process.exit(0)).catch(()=>process.exit(1))"], capture_output=True)
        if check.returncode == 0: return
        time.sleep(.25)
    raise RuntimeError(f"browser MCP container did not become ready: {name}")
