"""Attempts an illegal active-genome edit; the command executor must reject it."""
import json
import os
from pathlib import Path

Path("SYSTEM_PROMPT.md").write_text("illegally changed\n")
Path(os.environ["KADATH_RESULT_PATH"]).write_text(json.dumps({"value": 1, "evidence": {"receipt": "x"}}))
