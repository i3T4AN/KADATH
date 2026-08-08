"""A real subprocess organism fixture used by the command-executor test."""
import json
import os
from pathlib import Path

if os.environ.get("KADATH_PHASE") != "adaptation":
    Path(os.environ["KADATH_RESULT_PATH"]).write_text(json.dumps({
        "value": 42.0, "evidence": {"source": "fixture", "receipt": "verified-fixture-receipt"},
    }))
Path(os.environ["KADATH_STATE_DIR"], "mutation.json").write_text(json.dumps({
    "action": "mutate",
    "reason": "fixture reflection",
    "prompt_suffix": "\nUse the verified fixture strategy.",
    "files": {"agent_strategy.py": "STRATEGY = 'post-grade mutation'\n"},
}))
if os.environ.get("KADATH_PHASE") != "adaptation":
    with Path(os.environ["KADATH_STATE_DIR"], "activity.jsonl").open("a") as stream:
        stream.write(json.dumps({"summary": "Compared fixture approaches and completed the strongest one.", "outcome": "Produced a verified fixture receipt.", "next_step": "Adapt from elite evidence.", "visibility": "shared"}) + "\n")
