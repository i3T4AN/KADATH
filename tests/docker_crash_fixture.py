"""Docker proof fixture: crash once, then resume from the same state."""
import json
import os
from pathlib import Path

state = Path(os.environ["KADATH_STATE_DIR"])
marker = state / "first-crash-recorded"
if not marker.exists():
    marker.write_text("crashed once")
    raise SystemExit(23)
(state / "workspace").mkdir(exist_ok=True)
(state / "workspace" / "recovered.txt").write_text("state survived container restart\n")
Path(os.environ["KADATH_RESULT_PATH"]).write_text(json.dumps({"value": 0, "evidence": {"recovered": True}}))
