"""No-model fixture used to prove the isolated Docker execution contract."""
import json
import os
from pathlib import Path

state = Path(os.environ["KADATH_STATE_DIR"])
(state / "workspace").mkdir(exist_ok=True)
(state / "workspace" / "proof.txt").write_text("docker isolation works\n")
Path(os.environ["KADATH_RESULT_PATH"]).write_text(json.dumps({"value": 999999, "evidence": {"candidate": "docker-proof", "self_score_is_ignored": True}}))
