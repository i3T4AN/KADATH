"""Git-backed organism worktrees; kernel state stays outside every worktree."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class GitStore:
    def __init__(self, run_dir: Path, seed_dir: Path):
        self.run_dir, self.seed_dir = run_dir, seed_dir
        self.repo = run_dir / "git-repository"

    def initialize(self) -> None:
        if self.repo.exists():
            return
        # Build a fresh run-local repository from the vendored framework. The
        # project does not need to carry smolagents' nested .git directory.
        bootstrap = self.run_dir / "seed-bootstrap"
        if bootstrap.exists():
            shutil.rmtree(bootstrap)
        shutil.copytree(
            self.seed_dir,
            bootstrap,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
        )
        subprocess.run(["git", "init", "-b", "main", str(bootstrap)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(bootstrap), "config", "user.email", "kadath@local"], check=True)
        subprocess.run(["git", "-C", str(bootstrap), "config", "user.name", "KADATH Seed"], check=True)
        subprocess.run(["git", "-C", str(bootstrap), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(bootstrap), "commit", "-m", "Initialize vendored smolagents seed"], check=True, capture_output=True)
        for filename in ("organism.py", "kadath_runtime.py"):
            shutil.copy2(self.seed_dir.parent / filename, bootstrap / filename)
        subprocess.run(["git", "-C", str(bootstrap), "add", "organism.py", "kadath_runtime.py"], check=True)
        subprocess.run(["git", "-C", str(bootstrap), "commit", "-m", "Add KADATH organism runtime entry points"], check=True, capture_output=True)
        subprocess.run(["git", "clone", "--bare", str(bootstrap), str(self.repo)], check=True, capture_output=True)
        shutil.rmtree(bootstrap)

    def materialize(self, agent_id: str, branch: str, commit: str | None = None) -> Path:
        self.initialize()
        target = self.run_dir / "agents" / agent_id / "repository"
        if target.exists():
            shutil.rmtree(target)
        command = ["git", "clone", "--shared", str(self.repo), str(target)]
        subprocess.run(command, check=True, capture_output=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "kadath@local"], check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "KADATH Birther"], check=True)
        subprocess.run(
            ["git", "-C", str(target), "checkout", "-B", branch, commit or "HEAD"],
            check=True, capture_output=True,
        )
        return target

    @staticmethod
    def commit(worktree: Path, message: str) -> str:
        subprocess.run(["git", "-C", str(worktree), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(worktree), "commit", "-m", message], check=True, capture_output=True)
        commit = subprocess.check_output(["git", "-C", str(worktree), "rev-parse", "HEAD"], text=True).strip()
        branch = subprocess.check_output(["git", "-C", str(worktree), "branch", "--show-current"], text=True).strip()
        subprocess.run(["git", "-C", str(worktree), "push", "origin", f"HEAD:refs/tags/genome/{commit}"], check=True, capture_output=True)
        # Agent branches are human navigation names and may be reassigned after culling.
        # Canonical commits are retained forever in the bare repository.
        subprocess.run(["git", "-C", str(worktree), "push", "--force", "origin", f"HEAD:refs/heads/{branch}"], check=True, capture_output=True)
        return commit
