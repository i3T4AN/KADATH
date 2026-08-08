"""Kernel-owned launcher that activates dependencies declared by an evolved genome."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path


def digest_inputs(repository: Path) -> str:
    hasher = hashlib.sha256()
    for name in ("pyproject.toml", "requirements.txt"):
        path = repository / name
        if path.is_file():
            hasher.update(name.encode()); hasher.update(path.read_bytes())
    return hasher.hexdigest()


def main() -> None:
    if len(sys.argv) < 2: raise SystemExit("organism command is required")
    repository = Path("/organism")
    state = Path(os.environ.get("KADATH_STATE_DIR", "/state"))
    state.mkdir(parents=True, exist_ok=True)
    current = digest_inputs(repository)
    base = Path("/opt/kadath/base-dependencies.sha256").read_text().strip()
    deps = state / "python-deps"
    marker = state / "python-deps.json"
    active = base
    if current != base:
        recorded_payload = json.loads(marker.read_text()) if marker.is_file() else {}
        recorded, recorded_epoch = recorded_payload.get("digest"), recorded_payload.get("epoch")
        epoch = os.environ.get("KADATH_EPOCH", "worker-or-adaptation")
        if recorded != current or recorded_epoch != epoch or not deps.is_dir():
            staging = state / "python-deps.staging"
            if staging.exists(): shutil.rmtree(staging)
            staging.mkdir()
            command = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-cache-dir", "--target", str(staging)]
            specifications: list[str] = []
            if (repository / "pyproject.toml").is_file():
                project = tomllib.loads((repository / "pyproject.toml").read_text()).get("project", {})
                specifications.extend(str(item) for item in project.get("dependencies", []))
            if specifications: subprocess.run([*command, *specifications], check=True)
            if (repository / "requirements.txt").is_file(): subprocess.run([*command, "-r", str(repository / "requirements.txt")], check=True)
            if deps.exists(): shutil.rmtree(deps)
            staging.rename(deps)
            marker.write_text(json.dumps({"digest": current, "epoch": epoch}, sort_keys=True))
        active = current
    if current != base and deps.is_dir() and active == current:
        os.environ["PYTHONPATH"] = str(deps) + os.pathsep + os.environ.get("PYTHONPATH", "")
    os.execvpe(sys.argv[1], sys.argv[1:], os.environ)


if __name__ == "__main__": main()
