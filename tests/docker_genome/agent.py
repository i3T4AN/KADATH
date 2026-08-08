"""Verifies that an evolved dependency manifest becomes active."""
import json
import os
from pathlib import Path

import tomli_w

Path(os.environ["KADATH_RESULT_PATH"]).write_text(json.dumps({"value": 0, "evidence": {"dependency": tomli_w.dumps({"active": True}).strip()}}))
