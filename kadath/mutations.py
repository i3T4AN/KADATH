"""Applies post-grade organism mutations without allowing kernel-file writes."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


class MutationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Mutation:
    action: str = "unchanged"
    reason: str = "No valid change proposed."
    prompt_suffix: str = ""
    files: dict[str, str] | None = None
    delete_files: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return self.action == "mutate" and bool(self.prompt_suffix.strip() or self.files or self.delete_files)

    @classmethod
    def from_state(cls, state_dir: Path) -> "Mutation":
        proposal = state_dir / "mutation.json"
        if not proposal.exists():
            return cls(reason="Agent chose to continue unchanged.")
        try:
            data = json.loads(proposal.read_text())
            return cls.from_payload(data)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise MutationError("invalid mutation proposal") from exc

    @classmethod
    def from_payload(cls, data: dict) -> "Mutation":
        action = data.get("action", "mutate")
        reason = data.get("reason", "")
        suffix = data.get("prompt_suffix", "")
        files = data.get("files", {})
        deletes = data.get("delete_files", [])
        if action not in {"mutate", "unchanged"} or not isinstance(reason, str) or not isinstance(suffix, str) or not isinstance(files, dict) or not isinstance(deletes, list) or not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items()) or not all(isinstance(item, str) for item in deletes):
            raise MutationError("invalid mutation proposal")
        maximum_operations = int(os.getenv("KADATH_MUTATION_OPERATIONS", "512"))
        maximum_file_bytes = int(os.getenv("KADATH_MUTATION_BYTES", str(32 * 1024 * 1024)))
        maximum_prompt_bytes = int(os.getenv("KADATH_PROMPT_MUTATION_BYTES", str(2 * 1024 * 1024)))
        if len(files) + len(deletes) > maximum_operations or sum(len(value.encode()) for value in files.values()) > maximum_file_bytes:
            raise MutationError("mutation proposal exceeds limits")
        if len(suffix.encode()) > maximum_prompt_bytes:
            raise MutationError("prompt mutation exceeds limits")
        for relative in [*files, *deletes]:
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts or ".git" in path.parts:
                raise MutationError("mutation writes outside organism repository")
        if action == "unchanged":
            return cls("unchanged", reason or "Agent chose to continue unchanged.")
        if not suffix.strip() and not files and not deletes:
            raise MutationError("mutate action contains no changes")
        return cls(action, reason or "Agent proposed a bounded self-change.", suffix, files, tuple(deletes))

    def apply(self, repository: Path) -> None:
        if not self.changed:
            return
        for relative, content in (self.files or {}).items():
            target = repository / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        for relative in self.delete_files:
            target = repository / relative
            if target.is_dir():
                raise MutationError("mutation cannot recursively delete directories")
            target.unlink(missing_ok=True)
