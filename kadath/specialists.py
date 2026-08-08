"""Model-driven special agents outside the evolvable organism population."""
from __future__ import annotations

import json
import hashlib
import codecs
import base64
import io
import math
import mimetypes
import os
import subprocess
import urllib.error
import urllib.request
import time
from dataclasses import asdict, dataclass
from typing import Any, Protocol
from pathlib import Path

from .contracts import ExecutionRequest, ObjectiveResult


class SpecialistError(RuntimeError):
    pass


class SpecialistModel(Protocol):
    def complete_json(self, identity: str, system: str, payload: dict[str, Any]) -> dict[str, Any]: ...


def repairing_json(model: SpecialistModel, identity: str, system: str, payload: dict[str, Any], validator, attempts: int = 3):
    """Retry schema-valid JSON that violates a specialist's semantic contract."""
    request_payload = payload
    last_error: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        response = model.complete_json(identity, system, request_payload)
        try:
            return validator(response)
        except (SpecialistError, KeyError, TypeError, ValueError) as exc:
            last_error = exc
            if attempt >= attempts: break
            request_payload = {
                **payload,
                "contract_repair": {
                    "attempt": attempt + 1,
                    "validation_error": str(exc),
                    "previous_response": response,
                    "instruction": "Correct the previous response and return the complete required JSON object. Do not omit already valid fields.",
                },
            }
    raise SpecialistError(f"{identity} violated its required contract after {attempts} attempts: {last_error}") from last_error


def checkpointed_json(model: SpecialistModel, identity: str, system: str, payload: dict[str, Any], checkpoint: Path | None, validator):
    """Reuse one validated model result only when its complete input is unchanged."""
    fingerprint = hashlib.sha256(json.dumps({"identity": identity, "system": system, "payload": payload}, sort_keys=True, default=str).encode()).hexdigest()
    response = None
    if checkpoint and checkpoint.is_file():
        try:
            saved = json.loads(checkpoint.read_text())
            if saved.get("input_sha256") == fingerprint: response = saved.get("response")
        except (OSError, ValueError, json.JSONDecodeError): pass
    if isinstance(response, dict):
        try: validated = validator(response)
        except (SpecialistError, KeyError, TypeError, ValueError): response = None
    if not isinstance(response, dict): validated = repairing_json(model, identity, system, payload, validator)
    if checkpoint:
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        temporary = checkpoint.with_suffix(checkpoint.suffix + ".tmp")
        temporary.write_text(json.dumps({"input_sha256": fingerprint, "response": validated}, indent=2, sort_keys=True, default=str))
        temporary.replace(checkpoint)
    return validated


class RecordingSpecialistModel:
    """Records specialist prompts and outputs without changing model behavior."""
    def __init__(self, inner: SpecialistModel, recorder): self.inner, self.recorder = inner, recorder

    def complete_json(self, identity: str, system: str, payload: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        try:
            response = self.inner.complete_json(identity, system, payload)
        except Exception as exc:
            self.recorder(identity, system, payload, None, time.monotonic() - started, str(exc), getattr(self.inner, "last_telemetry", {})); raise
        self.recorder(identity, system, payload, response, time.monotonic() - started, None, getattr(self.inner, "last_telemetry", {}))
        return response


class LiteLLMSpecialistModel:
    """OpenAI-compatible LiteLLM gateway used by the control-plane agents."""

    def __init__(self, base_url: str | None = None, model: str | None = None, api_key: str | None = None):
        self.base_url = (base_url or os.getenv("KADATH_LITELLM_URL", "")).rstrip("/")
        self.model = model or os.getenv("KADATH_MODEL", "")
        self.api_key = api_key or os.getenv("LITELLM_API_KEY", os.getenv("LITELLM_MASTER_KEY", ""))
        if not self.base_url or not self.model:
            raise SpecialistError("special agents require KADATH_LITELLM_URL and KADATH_MODEL")
        self.last_telemetry: dict[str, Any] = {}

    def complete_json(self, identity: str, system: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.last_telemetry = {}
        clean_payload = {key: value for key, value in payload.items() if key != "_multimodal"}
        user_content: str | list[dict[str, Any]] = json.dumps({"identity": identity, **clean_payload})
        attachments = payload.get("_multimodal", [])
        if attachments:
            user_content = [{"type": "text", "text": user_content}]
            for attachment in attachments:
                label, part = self._attachment_part(attachment)
                user_content.append({"type": "text", "text": label})
                user_content.append(part)
        body = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.0 if identity.startswith("grader") else 0.15,
            "metadata": {"kadath_identity": identity},
        }).encode()
        maximum_request = int(os.getenv("KADATH_SPECIALIST_REQUEST_BYTES", "50000000"))
        if len(body) > maximum_request:
            raise SpecialistError(f"{identity} request exceeded the {maximum_request}-byte specialist limit")
        request = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions", data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}, method="POST",
        )
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    encoded_response = response.read(5_000_001)
                    if len(encoded_response) > 5_000_000: raise ValueError("specialist response exceeded 5MB")
                    response_body = json.loads(encoded_response)
                self.last_telemetry = {"model": response_body.get("model", self.model), "response_id": response_body.get("id"), "usage": response_body.get("usage", {})}
                return json.loads(response_body["choices"][0]["message"]["content"])
            except (urllib.error.URLError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(2 ** attempt)
        raise SpecialistError(f"{identity} did not return valid JSON after 4 attempts") from last_error

    @staticmethod
    def _attachment_part(attachment: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        path = Path(str(attachment["path"]))
        kind = str(attachment["kind"]); reference = str(attachment["evidence_ref"])
        if not path.is_file(): raise SpecialistError(f"multimodal evidence disappeared: {reference}")
        maximum_dimension = max(512, int(os.getenv("KADATH_GRADER_IMAGE_DIMENSION", "1800")))
        if kind in {"image", "pdf_page", "video_frame"}:
            try:
                from PIL import Image
            except ImportError as exc:
                raise SpecialistError("multimodal grading requires Pillow") from exc
            if kind == "image":
                image = Image.open(path); locator = "image"
            elif kind == "pdf_page":
                try: import pymupdf
                except ImportError as exc: raise SpecialistError("PDF grading requires PyMuPDF") from exc
                document = pymupdf.open(path)
                try:
                    page_number = int(attachment["page"]); page = document.load_page(page_number)
                    scale = min(2.0, maximum_dimension / max(page.rect.width, page.rect.height, 1))
                    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
                    image = Image.open(io.BytesIO(pixmap.tobytes("png"))); locator = f"PDF page {page_number + 1}"
                finally: document.close()
            else:
                timestamp = float(attachment["timestamp"])
                command = ["ffmpeg", "-v", "error", "-ss", str(timestamp), "-i", str(path), "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"]
                try: encoded_frame = subprocess.check_output(command, timeout=60)
                except (OSError, subprocess.SubprocessError) as exc: raise SpecialistError(f"cannot extract video frame for {reference}") from exc
                image = Image.open(io.BytesIO(encoded_frame)); locator = f"video frame at {timestamp:.2f}s"
            try:
                image = image.convert("RGB"); image.thumbnail((maximum_dimension, maximum_dimension))
                output = io.BytesIO(); image.save(output, format="JPEG", quality=85, optimize=True)
                encoded = base64.b64encode(output.getvalue()).decode()
            finally: image.close()
            return f"{reference} — {locator}", {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}", "detail": "high"}}
        if kind == "audio":
            start, duration = float(attachment.get("start", 0)), float(attachment["duration"])
            command = ["ffmpeg", "-v", "error", "-ss", str(start), "-i", str(path), "-t", str(duration), "-vn", "-acodec", "libmp3lame", "-b:a", "64k", "-f", "mp3", "-"]
            try: audio = subprocess.check_output(command, timeout=max(60, int(duration) + 30))
            except (OSError, subprocess.SubprocessError) as exc: raise SpecialistError(f"cannot extract audio segment for {reference}") from exc
            encoded = base64.b64encode(audio).decode()
            return f"{reference} — audio segment {start:.2f}s to {start + duration:.2f}s", {"type": "input_audio", "input_audio": {"data": encoded, "format": "mp3"}}
        raise SpecialistError(f"unsupported multimodal attachment kind: {kind}")


@dataclass(frozen=True)
class ArchitectOutput:
    objective_prompt: str
    measurement_method: str
    attribution_method: str
    evidence_requirements: list[str]
    baseline: str
    anti_fraud_checks: list[str]
    tie_breaker: str
    benchmark: dict[str, Any]
    verification_plan: dict[str, Any]
    tool_policy: dict[str, Any]
    special_agent_instructions: dict[str, str]


class ArchitectAgent:
    def __init__(self, model: SpecialistModel): self.model = model

    def run(self, goal: str, criterion: str, epoch_seconds: int, population: int, epochs: int = 1, environment_inventory: dict[str, Any] | None = None) -> ArchitectOutput:
        repair: dict[str, Any] | None = None
        last_error: Exception | None = None
        for attempt in range(1, 4):
            captured: dict[str, Any] = {}
            outer = self
            class RepairModel:
                def complete_json(self, identity: str, system: str, payload: dict[str, Any]) -> dict[str, Any]:
                    request_payload = {**payload, **({"contract_repair": repair} if repair else {})}
                    response = outer.model.complete_json(identity, system, request_payload)
                    captured["response"] = response
                    return response
            try:
                return self._run_once(goal, criterion, epoch_seconds, population, epochs, environment_inventory, RepairModel())
            except SpecialistError as exc:
                last_error = exc
                repair = {"attempt": attempt + 1, "validation_error": str(exc), "previous_response": captured.get("response"), "instruction": "Correct the previous response and return the complete required JSON object."}
        raise SpecialistError(f"Architect violated its required contract after 3 attempts: {last_error}") from last_error

    def _run_once(self, goal: str, criterion: str, epoch_seconds: int, population: int, epochs: int = 1, environment_inventory: dict[str, Any] | None = None, model: SpecialistModel | None = None) -> ArchitectOutput:
        inventory = environment_inventory or {}
        optional = list(inventory.get("available_optional_capabilities", []))
        capabilities = {
            "repository": ["list", "read"],
            "workspace": ["list", "read", "write", "delete", "run_command"],
            "optional": optional,
            "collaboration": ["publish_activity", "read_population_memory"] + (["spawn_up_to_5_workers"] if "workers" in optional else []),
            "artifacts": ["save_artifact"],
        }
        response = (model or self.model).complete_json(
            "architect",
            "You are KADATH Architect. Turn the user's goal into a complete, executable benchmark. Use only capabilities and named external measurement connectors explicitly present in the supplied environment inventory; never invent an account, credential, connector, or service. If the requested real-world outcome cannot be independently verified with that inventory, define the strongest honestly measurable proxy and state that limitation. The model Grader extracts supported facts, but the kernel verifies evidence references and computes the final numeric score. Define an unambiguous weighted rubric, numeric tie-break inputs, required evidence, and automatic failure conditions. It must not depend on a user-authored benchmark file or script. Do not propose organism strategy. Return only the required JSON fields.",
            {"goal": goal, "requested_criterion": criterion, "epoch_seconds": epoch_seconds, "epochs": epochs,
             "population": population, "available_capabilities": capabilities, "environment_inventory": inventory,
             "benchmark_contract": {
                 "score_range": "two finite numbers [minimum, maximum], higher is better",
                 "scoring_rubric": "ordered list of criteria {id, criterion, weight, measurement}. measurement is either {type: binary}, {type: numeric_linear, minimum, target, direction: higher|lower}, or {type: levels, levels: [{id, description, fraction}]}; the kernel maps extracted facts/level ids to points",
                 "tie_break_rubric": "non-empty list of {id, description, direction: higher|lower, minimum, maximum, weight}; the kernel normalizes extracted numeric facts",
                 "required_outputs": "non-empty list of objects {description, evidence_ref}; evidence_ref is candidate, organism_evidence, activity, file:<glob>, or external:<configured-connector>",
                 "failure_conditions": "non-empty list of {id, condition}; every condition is assessed explicitly",
                 "grader_rules": "rules the Grader must enforce consistently across all agents",
             },
             "verification_plan_contract": {"kernel_checks": ["identity_attribution", "artifact_integrity", "required_output_presence"], "external_connectors": "list selected only from environment_inventory.grader_connectors", "limitations": "honest list of outcomes the available environment cannot independently verify"},
             "tool_policy_contract": {"enabled_capabilities": f"list chosen only from {optional}"},
             "required_fields": ["objective_prompt", "measurement_method", "attribution_method", "evidence_requirements", "baseline", "anti_fraud_checks", "tie_breaker", "benchmark", "verification_plan", "tool_policy", "special_agent_instructions"]},
        )
        try:
            output = ArchitectOutput(
                objective_prompt=str(response["objective_prompt"]), measurement_method=str(response["measurement_method"]),
                attribution_method=str(response["attribution_method"]), evidence_requirements=[str(item) for item in response["evidence_requirements"]],
                baseline=str(response["baseline"]), anti_fraud_checks=[str(item) for item in response["anti_fraud_checks"]], tie_breaker=str(response["tie_breaker"]),
                benchmark=dict(response["benchmark"]), verification_plan=dict(response["verification_plan"]), tool_policy=dict(response["tool_policy"]),
                special_agent_instructions={str(key): str(value) for key, value in response["special_agent_instructions"].items()},
            )
        except (KeyError, TypeError) as exc:
            raise SpecialistError("Architect output does not match the required contract") from exc
        for agent in ("grader", "tweaker", "birther"):
            if not output.special_agent_instructions.get(agent):
                raise SpecialistError(f"Architect omitted {agent} instructions")
        required_benchmark = {"score_range", "scoring_rubric", "tie_break_rubric", "required_outputs", "failure_conditions", "grader_rules"}
        if not required_benchmark.issubset(output.benchmark):
            raise SpecialistError("Architect benchmark is incomplete")
        score_range = output.benchmark.get("score_range")
        if not isinstance(score_range, list) or len(score_range) != 2 or not all(isinstance(item, (int, float)) and math.isfinite(float(item)) for item in score_range) or score_range[0] >= score_range[1]:
            raise SpecialistError("Architect benchmark score_range is invalid")
        rubric = output.benchmark.get("scoring_rubric")
        if not isinstance(rubric, list) or not rubric:
            raise SpecialistError("Architect benchmark scoring_rubric is empty")
        try: weights = [float(item["weight"]) for item in rubric]
        except (KeyError, TypeError, ValueError) as exc: raise SpecialistError("Architect benchmark rubric weights are invalid") from exc
        if any(not math.isfinite(weight) or weight <= 0 for weight in weights):
            raise SpecialistError("Architect benchmark rubric weights must be finite and positive")
        total_weight = sum(weights)
        if abs(total_weight - 100.0) > .001:
            raise SpecialistError("Architect benchmark rubric weights must total 100")
        used_ids: set[str] = set()
        for index, item in enumerate(rubric, 1):
            if not isinstance(item, dict) or not str(item.get("criterion", "")).strip():
                raise SpecialistError("Architect benchmark rubric criterion is invalid")
            criterion_id = str(item.get("id") or f"criterion-{index}").strip()
            if criterion_id in used_ids:
                raise SpecialistError("Architect benchmark rubric ids must be unique")
            item["id"] = criterion_id; used_ids.add(criterion_id)
            measurement = item.get("measurement")
            if not isinstance(measurement, dict) or measurement.get("type") not in {"binary", "numeric_linear", "levels"}:
                raise SpecialistError("Architect rubric criterion has no supported kernel scoring method")
            if measurement["type"] == "numeric_linear":
                try: minimum, target = float(measurement["minimum"]), float(measurement["target"])
                except (KeyError, TypeError, ValueError) as exc: raise SpecialistError("numeric rubric bounds are invalid") from exc
                if not math.isfinite(minimum) or not math.isfinite(target) or minimum == target or measurement.get("direction") not in {"higher", "lower"}:
                    raise SpecialistError("numeric rubric bounds or direction are invalid")
                if (measurement["direction"] == "higher" and target <= minimum) or (measurement["direction"] == "lower" and target >= minimum):
                    raise SpecialistError("numeric rubric target contradicts its direction")
            if measurement["type"] == "levels":
                levels = measurement.get("levels")
                if not isinstance(levels, list) or len(levels) < 2:
                    raise SpecialistError("level rubric needs at least two locked levels")
                level_ids = set()
                for level in levels:
                    try: level_id, fraction = str(level["id"]), float(level["fraction"])
                    except (KeyError, TypeError, ValueError) as exc: raise SpecialistError("level rubric is invalid") from exc
                    if not level_id or level_id in level_ids or not 0 <= fraction <= 1 or not str(level.get("description", "")).strip():
                        raise SpecialistError("level rubric ids, descriptions, and fractions must be valid")
                    level_ids.add(level_id)
        for key in ("tie_break_rubric", "required_outputs", "failure_conditions", "grader_rules"):
            if not isinstance(output.benchmark.get(key), list) or not output.benchmark[key]:
                raise SpecialistError(f"Architect benchmark {key} is empty")
        tie_ids = set()
        for item in output.benchmark["tie_break_rubric"]:
            if not isinstance(item, dict): raise SpecialistError("tie-break rubric must be machine-readable")
            try:
                item_id = str(item["id"]); minimum = float(item["minimum"]); maximum = float(item["maximum"]); weight = float(item["weight"])
            except (KeyError, TypeError, ValueError) as exc: raise SpecialistError("tie-break rubric is invalid") from exc
            if not item_id or item_id in tie_ids or item.get("direction") not in {"higher", "lower"} or not all(math.isfinite(value) for value in (minimum, maximum, weight)) or maximum <= minimum or weight <= 0:
                raise SpecialistError("tie-break bounds, direction, ids, and weights must be valid")
            tie_ids.add(item_id)
        failure_ids = set()
        for item in output.benchmark["failure_conditions"]:
            if not isinstance(item, dict) or not str(item.get("id", "")).strip() or not str(item.get("condition", "")).strip() or str(item["id"]) in failure_ids:
                raise SpecialistError("failure conditions must have unique ids and explicit conditions")
            failure_ids.add(str(item["id"]))
        required_external_connectors: set[str] = set()
        for required in output.benchmark["required_outputs"]:
            if not isinstance(required, dict) or not str(required.get("description", "")).strip() or not str(required.get("evidence_ref", "")).strip():
                raise SpecialistError("Architect benchmark required_outputs must contain machine-checkable evidence references")
            reference = str(required["evidence_ref"])
            if not (reference in {"candidate", "organism_evidence", "activity"} or reference.startswith("file:") or reference.startswith("external:")):
                raise SpecialistError("Architect benchmark required output evidence_ref is unsupported")
            if reference.startswith("file:") and not reference.removeprefix("file:").strip():
                raise SpecialistError("Architect benchmark required output file reference is empty")
            if reference.startswith("external:"):
                connector = reference.removeprefix("external:").strip()
                if not connector:
                    raise SpecialistError("Architect benchmark required output external connector is empty")
                required_external_connectors.add(connector)
        if not output.objective_prompt.strip() or not output.measurement_method.strip() or not output.attribution_method.strip():
            raise SpecialistError("Architect objective and measurement fields must be non-empty")
        raw_anti_fraud = response.get("anti_fraud_checks")
        if not isinstance(raw_anti_fraud, list) or not raw_anti_fraud or any(not isinstance(item, str) or not item.strip() for item in raw_anti_fraud):
            raise SpecialistError("Architect anti-fraud checks must be a non-empty list of explicit rules")
        enabled = output.tool_policy.get("enabled_capabilities")
        allowed = set(optional)
        if not isinstance(enabled, list) or any(item not in allowed for item in enabled):
            raise SpecialistError("Architect tool_policy enabled_capabilities is invalid")
        plan = output.verification_plan
        required_checks = {"identity_attribution", "artifact_integrity", "required_output_presence"}
        checks = plan.get("kernel_checks")
        connectors = plan.get("external_connectors")
        available_connectors = set(inventory.get("grader_connectors", []))
        if not isinstance(checks, list) or not required_checks.issubset(checks):
            raise SpecialistError("Architect verification_plan omitted required kernel checks")
        if not isinstance(connectors, list) or any(not isinstance(item, str) or item not in available_connectors for item in connectors):
            raise SpecialistError("Architect selected an unavailable external measurement connector")
        if not required_external_connectors.issubset(set(connectors)):
            raise SpecialistError("Architect required output references an external connector not selected in the verification plan")
        if not isinstance(plan.get("limitations", []), list):
            raise SpecialistError("Architect verification_plan limitations must be a list")
        return output


class GraderAgent:
    def __init__(self, model: SpecialistModel, instruction: str): self.model, self.instruction = model, instruction

    @staticmethod
    def _evidence_chunks(frozen: dict[str, Any], maximum_chars: int):
        def encoded_entries(reference: str, content: Any):
            encoded = json.dumps(content, sort_keys=True, default=str, separators=(",", ":"))
            fragment_size = max(1_000, maximum_chars // 2)
            if len(encoded) <= fragment_size:
                yield {"evidence_ref": reference, "fragment_id": f"{reference}#0", "content": content}
                return
            overlap = min(512, max(0, fragment_size // 20))
            step = max(1, fragment_size - overlap)
            for part, offset in enumerate(range(0, len(encoded), step)):
                yield {
                    "evidence_ref": reference,
                    "fragment_id": f"{reference}#{part}@{offset}",
                    "content_fragment": encoded[offset:offset + fragment_size],
                    "encoding": "json",
                    "character_offset": offset,
                    "fragment_overlap": overlap,
                }

        def frozen_file_entries(record: dict[str, Any]):
            reference = "file:" + str(record.get("path", ""))
            if record.get("text") is not None:
                yield from encoded_entries(reference, str(record["text"]))
                return
            manifest = frozen.get("manifest_path")
            relative = Path(str(record.get("path", "")))
            if not manifest or relative.is_absolute() or ".." in relative.parts:
                yield from encoded_entries(reference, {key: value for key, value in record.items() if key != "text"})
                return
            root = Path(manifest).parent
            path = root / relative
            is_utf8 = True
            try:
                expected_size, expected_hash = int(record["size"]), str(record["sha256"])
                hasher = hashlib.sha256(); size = 0; decoder = codecs.getincrementaldecoder("utf-8")("strict")
                with path.open("rb") as stream:
                    while True:
                        data = stream.read(1024 * 1024)
                        if not data: break
                        size += len(data); hasher.update(data)
                        if is_utf8:
                            try: decoder.decode(data, final=False)
                            except UnicodeDecodeError: is_utf8 = False
                    if is_utf8:
                        try: decoder.decode(b"", final=True)
                        except UnicodeDecodeError: is_utf8 = False
                if size != expected_size or hasher.hexdigest() != expected_hash:
                    raise SpecialistError(f"frozen evidence changed before grading: {relative}")
            except (OSError, KeyError, TypeError, ValueError) as exc:
                raise SpecialistError(f"cannot read frozen evidence: {relative}") from exc
            if not is_utf8:
                mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                metadata = {"path": str(relative), "size": expected_size, "sha256": expected_hash, "mime_type": mime}
                if mime.startswith("image/"):
                    yield {"evidence_ref": reference, "fragment_id": f"{reference}#image", "content": {**metadata, "media_inspection": "attached image"}, "_attachment": {"kind": "image", "path": str(path), "evidence_ref": reference}}
                    return
                if mime == "application/pdf":
                    try: import pymupdf
                    except ImportError as exc: raise SpecialistError("PDF grading requires PyMuPDF") from exc
                    document = pymupdf.open(path)
                    try:
                        for page_number in range(document.page_count):
                            page_text = document.load_page(page_number).get_text("text")
                            page_content = {**metadata, "page": page_number + 1, "page_count": document.page_count, "extracted_text": page_text}
                            for part, entry in enumerate(encoded_entries(reference, page_content)):
                                entry["fragment_id"] = f"{reference}#page-{page_number + 1}-part-{part}"
                                if part == 0: entry["_attachment"] = {"kind": "pdf_page", "path": str(path), "page": page_number, "evidence_ref": reference}
                                yield entry
                    finally: document.close()
                    return
                if mime.startswith("video/"):
                    try:
                        duration_text = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)], text=True, timeout=30).strip()
                        duration = max(0.0, float(duration_text))
                    except (OSError, subprocess.SubprocessError, ValueError) as exc:
                        raise SpecialistError(f"cannot inspect video evidence: {relative}") from exc
                    points = sorted({0.0, duration * .25, duration * .5, duration * .75, max(0.0, duration - .05)})
                    for part, timestamp in enumerate(points):
                        yield {"evidence_ref": reference, "fragment_id": f"{reference}#frame-{part}", "content": {**metadata, "duration_seconds": duration, "frame_timestamp": timestamp}, "_attachment": {"kind": "video_frame", "path": str(path), "timestamp": timestamp, "evidence_ref": reference}}
                    return
                if mime.startswith("audio/"):
                    try:
                        duration_text = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)], text=True, timeout=30).strip()
                        duration = max(0.0, float(duration_text))
                    except (OSError, subprocess.SubprocessError, ValueError) as exc:
                        raise SpecialistError(f"cannot inspect audio evidence: {relative}") from exc
                    segment_seconds = max(30, int(os.getenv("KADATH_GRADER_AUDIO_SEGMENT_SECONDS", "300")))
                    for part, start in enumerate(range(0, max(1, math.ceil(duration)), segment_seconds)):
                        segment_duration = min(float(segment_seconds), max(0.01, duration - start))
                        yield {"evidence_ref": reference, "fragment_id": f"{reference}#audio-{part}", "content": {**metadata, "duration_seconds": duration, "segment_start": start, "segment_duration": segment_duration}, "_attachment": {"kind": "audio", "path": str(path), "start": start, "duration": segment_duration, "evidence_ref": reference}}
                    return
                yield from encoded_entries(reference, {**metadata, "media_inspection": "unsupported binary format; content was not claimed as reviewed"})
                return
            fragment_size = max(1_000, maximum_chars // 2)
            overlap = min(512, max(0, fragment_size // 20)); offset = 0; part = 0; carry = ""
            with path.open("r", encoding="utf-8") as stream:
                while True:
                    fresh = stream.read(max(1, fragment_size - len(carry)))
                    if not fresh: break
                    content = carry + fresh
                    yield {"evidence_ref": reference, "fragment_id": f"{reference}#{part}@{offset}", "content_fragment": content, "encoding": "utf-8", "character_offset": offset, "fragment_overlap": overlap}
                    part += 1; offset += max(1, len(content) - overlap); carry = content[-overlap:] if overlap else ""

        def entries():
            if frozen.get("candidate") is not None: yield from encoded_entries("candidate", frozen["candidate"])
            if frozen.get("organism_evidence") is not None: yield from encoded_entries("organism_evidence", frozen["organism_evidence"])
            for index, record in enumerate(frozen.get("activity", [])): yield from encoded_entries(f"activity:{index}", record)
            for record in frozen.get("files", []): yield from frozen_file_entries(record)
            for index, record in enumerate(frozen.get("tool_trace", [])): yield from encoded_entries(f"tool_trace:{index}", record)
            for connector, measurement in sorted((frozen.get("external_measurements") or {}).items()):
                yield from encoded_entries("external:" + str(connector), measurement)

        current: list[dict[str, Any]] = []; used = 0; media_count = 0
        emitted = False
        for entry in entries():
            size = len(json.dumps(entry, default=str))
            is_media = "_attachment" in entry
            if current and (used + size > maximum_chars or len(current) >= 24 or (is_media and media_count >= 4)):
                emitted = True; yield current; current = []; used = 0; media_count = 0
            current.append(entry); used += size; media_count += int(is_media)
        if current or not emitted: yield current

    @staticmethod
    def _bounded_review(response: dict[str, Any], valid_refs: set[str], valid_fragments: set[str] | None = None) -> dict[str, Any]:
        """Validate a complete review without silently discarding model output."""
        for name in ("evidence_notes", "contradictions", "fraud_signals"):
            if not isinstance(response.get(name, []), list): raise SpecialistError("Grader checkpoint output is invalid")
        notes = []
        for raw in response.get("evidence_notes", []):
            if not isinstance(raw, dict): raise SpecialistError("Grader evidence note is invalid")
            reference = str(raw.get("evidence_ref", ""))
            if reference not in valid_refs: raise SpecialistError("Grader checkpoint cited evidence outside its input")
            notes.append(raw)
        reviewed = response.get("reviewed_fragment_ids", [])
        if valid_fragments is not None:
            if not isinstance(reviewed, list) or len(reviewed) != len(set(map(str, reviewed))) or set(map(str, reviewed)) != valid_fragments:
                raise SpecialistError("Grader checkpoint did not account for every evidence fragment exactly once")
        return {"reviewed_fragment_ids": [str(item) for item in reviewed], "evidence_notes": notes, "contradictions": response.get("contradictions", []), "fraud_signals": response.get("fraud_signals", [])}

    @staticmethod
    def _coverage(values: list[str] | set[str]) -> dict[str, Any]:
        ordered = sorted(map(str, values))
        encoded = json.dumps(ordered, separators=(",", ":")).encode()
        return {"count": len(ordered), "sha256": hashlib.sha256(encoded).hexdigest()}

    @staticmethod
    def _merged_coverage(reviews: list[dict[str, Any]]) -> dict[str, Any]:
        children = sorted((str(review["review_id"]), int(review["coverage"]["count"]), str(review["coverage"]["sha256"])) for review in reviews)
        encoded = json.dumps(children, separators=(",", ":")).encode()
        return {"count": sum(count for _review_id, count, _digest in children), "sha256": hashlib.sha256(encoded).hexdigest()}

    @staticmethod
    def _pack_summaries(items: list[dict[str, Any]], maximum_chars: int) -> list[list[dict[str, Any]]]:
        batches: list[list[dict[str, Any]]] = []; current: list[dict[str, Any]] = []; used = 0
        for item in items:
            size = len(json.dumps(item, default=str))
            if current and used + size > maximum_chars:
                batches.append(current); current = []; used = 0
            current.append(item); used += size
        if current: batches.append(current)
        return batches

    def _consolidate_reviews(self, request: ExecutionRequest, objective: dict[str, Any], reviews: list[dict[str, Any]], maximum: int, checkpoint_dir: Path | None) -> list[dict[str, Any]]:
        """Hierarchically reduce checkpoint notes until the final prompt is bounded."""
        current = reviews
        round_number = 0
        while len(json.dumps(current, default=str)) > maximum:
            round_number += 1
            if round_number > 12: raise SpecialistError("Grader review consolidation did not converge")
            reduced: list[dict[str, Any]] = []
            batches = self._pack_summaries(current, max(4_000, maximum // 2))
            for batch_number, batch in enumerate(batches, 1):
                allowed_refs = {str(note["evidence_ref"]) for review in batch for note in review.get("evidence_notes", []) if isinstance(note, dict) and note.get("evidence_ref")}
                source_ids = {str(review["review_id"]) for review in batch}
                payload = {"goal": request.goal, "benchmark": objective["benchmark"], "review_summaries": batch, "required_fields": ["reviewed_summary_ids", "evidence_notes", "contradictions", "fraud_signals"]}
                checkpoint = checkpoint_dir / f"reduce-{round_number:02d}-{batch_number:04d}.json" if checkpoint_dir else None
                def validate(response: dict[str, Any]):
                    reviewed = response.get("reviewed_summary_ids")
                    if not isinstance(reviewed, list) or len(reviewed) != len(set(map(str, reviewed))) or set(map(str, reviewed)) != source_ids:
                        raise SpecialistError("Grader consolidation did not account for every supplied summary exactly once")
                    bounded = self._bounded_review(response, allowed_refs)
                    bounded.pop("reviewed_fragment_ids", None)
                    return {"reviewed_summary_ids": [str(item) for item in reviewed], **bounded}
                bounded = checkpointed_json(
                    self.model,
                    f"grader-reduce/{request.run_id}/{request.epoch}/{request.agent_id}/{round_number}/{batch_number}",
                    "You are a KADATH Grader consolidation pass. Merge duplicate checkpoint notes into a concise evidence map. Return every supplied review_id exactly once in reviewed_summary_ids. Preserve valid evidence_ref values exactly. Do not score, add facts, or drop contradictions or fraud signals.",
                    payload, checkpoint, validate,
                )
                coverage = self._merged_coverage(batch)
                review_id = f"reduce:{round_number}:{batch_number}:{coverage['sha256']}"
                reduced.append({"review_id": review_id, "coverage": coverage, "consolidation_round": round_number, "batch": batch_number, **bounded})
            if len(reduced) >= len(current) and len(json.dumps(reduced, default=str)) >= len(json.dumps(current, default=str)):
                # Force a smaller next pass instead of silently rebuilding an
                # oversized final prompt from verbose model summaries.
                maximum = max(8_000, maximum // 2)
            current = reduced
        return current

    def _review_chunks(self, request: ExecutionRequest, frozen_attempt: dict[str, Any], objective: dict[str, Any], checkpoint_dir: Path | None) -> list[dict[str, Any]]:
        token_budget = max(2_500, min(int(os.getenv("KADATH_GRADER_CHUNK_TOKENS", "12000")), 50_000))
        maximum = token_budget * 4  # conservative tokenizer-independent approximation
        reviews = []
        for index, chunk in enumerate(self._evidence_chunks(frozen_attempt, maximum), 1):
            attachments = [entry["_attachment"] for entry in chunk if "_attachment" in entry]
            visible_chunk = [{key: value for key, value in entry.items() if key != "_attachment"} for entry in chunk]
            payload = {"goal": request.goal, "benchmark": objective["benchmark"], "chunk_number": index, "evidence_entries": visible_chunk, "required_fields": ["reviewed_fragment_ids", "evidence_notes", "contradictions", "fraud_signals"]}
            if attachments: payload["_multimodal"] = attachments
            chunk_refs = {str(entry["evidence_ref"]) for entry in chunk}
            chunk_fragments = {str(entry["fragment_id"]) for entry in chunk}
            checkpoint = checkpoint_dir / f"chunk-{index:04d}.json" if checkpoint_dir else None
            response = checkpointed_json(
                self.model,
                f"grader-checkpoint/{request.run_id}/{request.epoch}/{request.agent_id}/{index}",
                "You are a KADATH Grader evidence-review pass. Review every supplied fragment and return every fragment_id exactly once in reviewed_fragment_ids. Extract concise evidence-backed notes only from this bounded chunk. Preserve the supplied evidence_ref on every note. Do not score, accept, reject, or infer facts not present in the chunk.",
                payload, checkpoint, lambda result: self._bounded_review(result, chunk_refs, chunk_fragments),
            )
            coverage = self._coverage(chunk_fragments)
            compact = {key: response[key] for key in ("evidence_notes", "contradictions", "fraud_signals")}
            reviews.append({"review_id": f"leaf:{index}:{coverage['sha256']}", "coverage": coverage, "chunk": index, **compact})
        return reviews

    def run(self, request: ExecutionRequest, frozen_attempt: dict[str, Any], objective: dict[str, Any], kernel_verification: dict[str, Any], checkpoint_dir: Path | None = None, forced_failure: str | None = None) -> ObjectiveResult:
        benchmark = objective["benchmark"]
        review_source = {**frozen_attempt, "external_measurements": kernel_verification.get("independent_external_measurements", {})}
        reviews = self._review_chunks(request, review_source, objective, checkpoint_dir)
        reviewed_fragments = sum(int(review["coverage"]["count"]) for review in reviews)
        token_budget = max(2_500, min(int(os.getenv("KADATH_GRADER_CHUNK_TOKENS", "12000")), 50_000))
        reviews = self._consolidate_reviews(request, objective, reviews, token_budget * 4, checkpoint_dir)
        if sum(int(review["coverage"]["count"]) for review in reviews) != reviewed_fragments:
            raise SpecialistError("Grader consolidation coverage invariant failed")
        frozen_summary = {key: value for key, value in frozen_attempt.items() if key not in {"candidate", "organism_evidence", "activity", "tool_trace", "files"}}
        verification_summary = {key: value for key, value in kernel_verification.items() if key not in {"valid_evidence_refs", "file_integrity", "independent_external_measurements"}}
        valid_refs = set(kernel_verification.get("valid_evidence_refs", []))
        expected = {str(item["id"]): item for item in benchmark["scoring_rubric"]}
        expected_failures = {str(item["id"]) for item in benchmark["failure_conditions"]}
        expected_fraud = {f"anti-fraud-{index}" for index, _ in enumerate(objective.get("anti_fraud_checks", []), 1)}
        tie_rules = {str(item["id"]): item for item in benchmark["tie_break_rubric"]}
        def validate_final(response: dict[str, Any]):
            activity = response.get("activity_summary")
            if not isinstance(activity, dict) or any(not isinstance(activity.get(key), list) for key in ("investigated", "actions", "outcomes")):
                raise SpecialistError("Grader omitted the kernel activity summary")
            if forced_failure: return response
            assessments = response.get("criterion_facts")
            if not isinstance(assessments, list) or len(assessments) != len(expected) or {str(item.get("criterion_id")) for item in assessments if isinstance(item, dict)} != set(expected):
                raise SpecialistError("Grader did not assess every locked rubric criterion exactly once")
            for assessment in assessments:
                criterion_id = str(assessment["criterion_id"]); measurement = expected[criterion_id]["measurement"]
                refs = [str(item) for item in assessment.get("evidence_refs", [])]
                if any(ref not in valid_refs for ref in refs): raise SpecialistError("Grader cited invalid evidence")
                fraction = 0.0
                if measurement["type"] == "binary":
                    if not isinstance(assessment.get("measured_value"), bool): raise SpecialistError("binary rubric fact is not boolean")
                    fraction = 1.0 if assessment["measured_value"] else 0.0
                if measurement["type"] == "numeric_linear":
                    value = float(assessment["measured_value"])
                    if not math.isfinite(value): raise SpecialistError("numeric rubric fact is not finite")
                    minimum, target = float(measurement["minimum"]), float(measurement["target"])
                    fraction = (value - minimum) / (target - minimum) if measurement["direction"] == "higher" else (minimum - value) / (minimum - target)
                    fraction = max(0.0, min(1.0, fraction))
                if measurement["type"] == "levels":
                    levels = {str(item["id"]): float(item["fraction"]) for item in measurement["levels"]}
                    level_id = str(assessment.get("level_id", ""))
                    if level_id not in levels: raise SpecialistError("Grader selected an unknown locked rubric level")
                    fraction = levels[level_id]
                if fraction > 0 and not refs: raise SpecialistError("Grader credited a rubric criterion without evidence")
            failures = response.get("failure_assessments")
            if not isinstance(failures, list) or len(failures) != len(expected_failures) or {str(item.get("failure_id")) for item in failures if isinstance(item, dict)} != expected_failures:
                raise SpecialistError("Grader did not assess every locked failure condition exactly once")
            fraud = response.get("anti_fraud_assessments")
            if not isinstance(fraud, list) or len(fraud) != len(expected_fraud) or {str(item.get("check_id")) for item in fraud if isinstance(item, dict)} != expected_fraud:
                raise SpecialistError("Grader anti-fraud assessment is incomplete or duplicated")
            ties = response.get("tie_break_facts")
            if not isinstance(ties, list) or len(ties) != len(tie_rules) or {str(item.get("tie_break_id")) for item in ties if isinstance(item, dict)} != set(tie_rules):
                raise SpecialistError("Grader tie-break facts are incomplete or duplicated")
            for collection in (failures, fraud, ties):
                for item in collection:
                    refs = [str(ref) for ref in item.get("evidence_refs", [])]
                    if any(ref not in valid_refs for ref in refs): raise SpecialistError("Grader contract cited invalid evidence")
            for item in failures:
                if not isinstance(item.get("triggered"), bool): raise SpecialistError("Grader failure assessment is not boolean")
                if item["triggered"] and not item.get("evidence_refs"):
                    raise SpecialistError("Grader triggered a failure condition without evidence")
            for item in fraud:
                if not isinstance(item.get("passed"), bool) or not item.get("evidence_refs"):
                    raise SpecialistError("Grader anti-fraud assessment is invalid")
            for item in ties:
                value = float(item["measured_value"])
                if not math.isfinite(value) or not item.get("evidence_refs"): raise SpecialistError("Grader tie-break fact or evidence is invalid")
            return response
        final_payload = {"goal": request.goal, "criterion": request.criterion, "objective_definition": objective,
             "benchmark": benchmark, "frozen_attempt_summary": frozen_summary, "evidence_checkpoint_reviews": reviews, "kernel_verification": verification_summary,
             "anti_fraud_rules": [{"id": f"anti-fraud-{index}", "rule": rule} for index, rule in enumerate(objective.get("anti_fraud_checks", []), 1)],
             "required_fields": ["reason", "criterion_facts", "tie_break_facts", "failure_assessments", "anti_fraud_assessments", "activity_summary"]}
        response = repairing_json(
            self.model,
            f"grader/{request.run_id}/{request.epoch}/{request.agent_id}",
            f"You are KADATH Grader. {self.instruction} Extract only the typed facts or locked level ids requested by the benchmark from the checkpoint reviews and independent kernel measurements. Do not choose fractions or scores: the kernel applies every formula. Never trust a self-reported score. Assess every locked failure and anti-fraud rule exactly once with evidence references. Also summarize the agent's high-level investigated areas, actions, and outcomes from the kernel-captured trace.",
            final_payload, validate_final,
        )
        activity_summary = response.get("activity_summary")
        if not isinstance(activity_summary, dict) or any(not isinstance(activity_summary.get(key), list) for key in ("investigated", "actions", "outcomes")):
            raise SpecialistError("Grader omitted the kernel activity summary")
        if forced_failure:
            return ObjectiveResult(float(benchmark["score_range"][0]), {"failure": forced_failure, "grader_assessment": str(response.get("reason", "kernel-detected failure")), "activity_summary": activity_summary, "kernel_verification": kernel_verification, "raw_grader_output": response, "checkpoint_reviews": reviews}, float("-1e308"), "failed")
        assessments = response.get("criterion_facts")
        if not isinstance(assessments, list) or len(assessments) != len(expected) or {str(item.get("criterion_id")) for item in assessments if isinstance(item, dict)} != set(expected):
            raise SpecialistError("Grader did not assess every locked rubric criterion exactly once")
        failure_assessments = response.get("failure_assessments")
        if not isinstance(failure_assessments, list) or len(failure_assessments) != len(expected_failures) or {str(item.get("failure_id")) for item in failure_assessments if isinstance(item, dict)} != expected_failures:
            raise SpecialistError("Grader did not assess every locked failure condition exactly once")
        for assessment in failure_assessments:
            refs = [str(item) for item in assessment.get("evidence_refs", [])]
            if any(ref not in valid_refs for ref in refs) or (assessment.get("triggered") is True and not refs): raise SpecialistError("failure assessment cited invalid evidence")
            if assessment.get("triggered") is True:
                return ObjectiveResult(float(benchmark["score_range"][0]), {"grader_assessment": str(response.get("reason", "automatic failure")), "activity_summary": activity_summary, "failure_assessments": failure_assessments, "kernel_verification": kernel_verification, "raw_grader_output": response, "checkpoint_reviews": reviews}, float("-1e308"), "failed")
        fraud = response.get("anti_fraud_assessments")
        if not isinstance(fraud, list) or len(fraud) != len(expected_fraud) or {str(item.get("check_id")) for item in fraud if isinstance(item, dict)} != expected_fraud:
            raise SpecialistError("Grader anti-fraud assessment is incomplete or duplicated")
        for check in fraud:
            refs = [str(item) for item in check.get("evidence_refs", [])]
            if any(ref not in valid_refs for ref in refs) or not refs: raise SpecialistError("Grader anti-fraud check cited invalid evidence")
            if check.get("passed") is not True:
                return ObjectiveResult(float(benchmark["score_range"][0]), {"grader_assessment": str(response.get("reason", "anti-fraud rejection")), "activity_summary": activity_summary, "rubric_assessment": assessments, "anti_fraud_assessment": fraud, "failure_assessments": failure_assessments, "kernel_verification": kernel_verification, "raw_grader_output": response, "checkpoint_reviews": reviews}, float("-1e308"), "failed")
        weighted = 0.0
        for assessment in assessments:
            try:
                criterion_id = str(assessment["criterion_id"])
                refs = [str(item) for item in assessment.get("evidence_refs", [])]
            except (KeyError, TypeError, ValueError) as exc:
                raise SpecialistError("Grader criterion assessment is invalid") from exc
            measurement = expected[criterion_id]["measurement"]
            if measurement["type"] == "binary":
                if not isinstance(assessment.get("measured_value"), bool): raise SpecialistError("binary rubric fact is not boolean")
                fraction = 1.0 if assessment["measured_value"] else 0.0
            elif measurement["type"] == "numeric_linear":
                try: value = float(assessment["measured_value"])
                except (KeyError, TypeError, ValueError) as exc: raise SpecialistError("numeric rubric fact is invalid") from exc
                if not math.isfinite(value): raise SpecialistError("numeric rubric fact is not finite")
                minimum, target = float(measurement["minimum"]), float(measurement["target"])
                fraction = (value - minimum) / (target - minimum) if measurement["direction"] == "higher" else (minimum - value) / (minimum - target)
                fraction = max(0.0, min(1.0, fraction))
            else:
                levels = {str(item["id"]): float(item["fraction"]) for item in measurement["levels"]}
                level_id = str(assessment.get("level_id", ""))
                if level_id not in levels: raise SpecialistError("Grader selected an unknown locked rubric level")
                fraction = levels[level_id]
            if (fraction > 0 and not refs) or any(ref not in valid_refs for ref in refs): raise SpecialistError("Grader cited invalid evidence")
            weighted += float(expected[criterion_id]["weight"]) * fraction
        components = response.get("tie_break_facts")
        if not isinstance(components, list) or len(components) != len(tie_rules) or {str(item.get("tie_break_id")) for item in components if isinstance(item, dict)} != set(tie_rules): raise SpecialistError("Grader tie-break facts are incomplete or duplicated")
        tie_break = 0.0
        for component in components:
            rule = tie_rules[str(component["tie_break_id"])]
            try: value = float(component["measured_value"])
            except (KeyError, TypeError, ValueError) as exc: raise SpecialistError("Grader tie-break fact is not numeric") from exc
            refs = [str(item) for item in component.get("evidence_refs", [])]
            if not math.isfinite(value) or not refs or any(ref not in valid_refs for ref in refs): raise SpecialistError("Grader tie-break fact or evidence is invalid")
            normalized = max(0.0, min(1.0, (value - float(rule["minimum"])) / (float(rule["maximum"]) - float(rule["minimum"]))))
            if rule["direction"] == "lower": normalized = 1.0 - normalized
            tie_break += normalized * float(rule["weight"])
        low, high = (float(item) for item in benchmark["score_range"])
        score = low + (high - low) * (weighted / 100.0)
        if not (low <= score <= high) or not (-1e308 < tie_break < 1e308):
            raise SpecialistError("Grader returned a score outside the locked benchmark")
        evidence = {
            "grader_assessment": str(response.get("reason", "accepted")),
            "activity_summary": activity_summary,
            "rubric_assessment": assessments,
            "anti_fraud_assessment": fraud,
            "failure_assessments": failure_assessments,
            "kernel_verification": kernel_verification,
            "raw_grader_output": response,
            "checkpoint_reviews": reviews,
        }
        return ObjectiveResult(score, evidence, tie_break, "success")


class TweakerAgent:
    def __init__(self, model: SpecialistModel, instruction: str): self.model, self.instruction = model, instruction

    def run(self, epoch: int, ranked: list[dict[str, Any]], elite_count: int, middle_count: int, birth_count: int, analysis: dict[str, Any], checkpoint_dir: Path | None = None) -> dict[str, Any]:
        maximum = max(50_000, min(int(os.getenv("KADATH_TWEAKER_CHARS", "250000")), 1_000_000))

        def validated(summary: dict[str, Any], expected_fragments: set[str], valid_agents: set[str]) -> dict[str, Any]:
            output: dict[str, Any] = {}
            for key in ("agent_findings", "successful_characteristics", "failed_characteristics", "evidence_quality_notes"):
                values = summary.get(key, [])
                if not isinstance(values, list): raise SpecialistError("Tweaker batch analysis is invalid")
                seen_values: set[str] = set(); unique_values = []
                for value in values:
                    signature = json.dumps(value, sort_keys=True, default=str)
                    if signature in seen_values: continue
                    seen_values.add(signature); unique_values.append(value)
                output[key] = unique_values
            covered = summary.get("covered_fragment_ids")
            if not isinstance(covered, list) or len(covered) != len(set(map(str, covered))) or set(map(str, covered)) != expected_fragments:
                raise SpecialistError("Tweaker did not account for every dossier fragment exactly once")
            for finding in output["agent_findings"]:
                if not isinstance(finding, dict) or str(finding.get("agent_id", "")) not in valid_agents:
                    raise SpecialistError("Tweaker produced an unattributed agent finding")
            output["covered_fragment_ids"] = [str(item) for item in covered]
            return output

        entries = []
        fragment_size = max(20_000, maximum // 2)
        overlap = min(2_000, fragment_size // 20)
        for dossier in analysis.get("tweaker_dossiers", []):
            encoded = json.dumps(dossier, sort_keys=True, default=str, separators=(",", ":"))
            step = max(1, fragment_size - overlap)
            for part, offset in enumerate(range(0, max(1, len(encoded)), step)):
                fragment_id = f"{dossier['agent_id']}#{part}@{offset}"
                entries.append({"agent_id": dossier["agent_id"], "fragment_id": fragment_id, "dossier_fragment": encoded[offset:offset + fragment_size], "encoding": "json", "character_offset": offset, "fragment_overlap": overlap})
        batches: list[list[dict[str, Any]]] = []; current = []; used = 0
        for entry in entries:
            size = len(json.dumps(entry))
            if current and used + size > maximum: batches.append(current); current = []; used = 0
            current.append(entry); used += size
        if current: batches.append(current)
        batch_summaries = []
        for batch_number, batch in enumerate(batches, 1):
            ids = {item["agent_id"] for item in batch}
            fragments = {item["fragment_id"] for item in batch}
            identity = f"tweaker-batch/epoch-{epoch}/{batch_number}"
            system = f"You are a KADATH Tweaker analysis pass. {self.instruction} Analyze every supplied dossier fragment and return every fragment_id exactly once in covered_fragment_ids: behavior, evidence, output, framework source, memory and worker use, lineage, failures, and outcome. Do not edit scores and do not give instructions to middle agents. Preserve useful findings even when an agent dossier spans chunks."
            payload = {"epoch": epoch, "ranked_subset": [row for row in ranked if row["agent_id"] in ids], "dossier_fragments": batch, "required_fields": ["covered_fragment_ids", "agent_findings", "successful_characteristics", "failed_characteristics", "evidence_quality_notes"]}
            checkpoint = checkpoint_dir / f"batch-{batch_number:04d}.json" if checkpoint_dir else None
            batch_summaries.append(checkpointed_json(self.model, identity, system, payload, checkpoint, lambda response, fragments=fragments, ids=ids: validated(response, fragments, ids)))

        reduction_round = 0
        while len(json.dumps(batch_summaries, default=str)) > maximum:
            reduction_round += 1
            if reduction_round > 12: raise SpecialistError("Tweaker consolidation did not converge")
            groups: list[list[dict[str, Any]]] = []; current = []; used = 0
            for summary in batch_summaries:
                size = len(json.dumps(summary, default=str))
                if current and used + size > maximum: groups.append(current); current = []; used = 0
                current.append(summary); used += size
            if current: groups.append(current)
            reduced = []
            for group_number, group in enumerate(groups, 1):
                fragments = {str(item) for summary in group for item in summary["covered_fragment_ids"]}
                valid_agents = {str(finding["agent_id"]) for summary in group for finding in summary["agent_findings"]}
                identity = f"tweaker-reduce/epoch-{epoch}/{reduction_round}/{group_number}"
                system = f"You are a KADATH Tweaker consolidation pass. {self.instruction} Merge these analysis summaries and return every supplied covered_fragment_id exactly once. Do not lose successful characteristics, failures, evidence-quality warnings, or agent attribution. Do not introduce strategy for middle agents."
                payload = {"epoch": epoch, "analysis_summaries": group, "required_fields": ["covered_fragment_ids", "agent_findings", "successful_characteristics", "failed_characteristics", "evidence_quality_notes"]}
                checkpoint = checkpoint_dir / f"reduce-{reduction_round:02d}-{group_number:04d}.json" if checkpoint_dir else None
                reduced.append(checkpointed_json(self.model, identity, system, payload, checkpoint, lambda response, fragments=fragments, agents=valid_agents: validated(response, fragments, agents)))
            if len(reduced) >= len(batch_summaries): raise SpecialistError("Tweaker consolidation could not reduce its summaries")
            batch_summaries = reduced
        compact_analysis = {"ranked_outcomes": ranked, "memory_ranking": analysis.get("memory_ranking"), "batch_summaries": batch_summaries}
        identity = f"tweaker/epoch-{epoch}"
        system = f"You are KADATH Tweaker. {self.instruction} You cannot edit scores or guide middle agents. Extract high-level lessons from the elite evidence solely for Birther reproduction. Return the complete union of covered_fragment_ids."
        payload = {"epoch": epoch, "ranked": ranked, "elite_count": elite_count, "middle_count": middle_count, "birth_count": birth_count, "analysis": compact_analysis,
                   "required_fields": ["covered_fragment_ids", "elite_characteristics", "successful_patterns", "failed_patterns", "reproduction_context", "parent_briefs", "reproduction_assignments"]}
        expected_coverage = {str(item) for summary in batch_summaries for item in summary["covered_fragment_ids"]}
        elite_ids = {row["agent_id"] for row in ranked[:elite_count]}
        def validate_final(response: dict[str, Any]):
            covered = response.get("covered_fragment_ids")
            if not isinstance(covered, list) or len(covered) != len(set(map(str, covered))) or set(map(str, covered)) != expected_coverage:
                raise SpecialistError("Tweaker final report omitted dossier coverage")
            raw = response.get("reproduction_assignments")
            if not isinstance(raw, dict): raise SpecialistError("Tweaker omitted reproduction assignments")
            try: assignments = {str(agent_id): int(count) for agent_id, count in raw.items() if int(count) > 0}
            except (TypeError, ValueError) as exc: raise SpecialistError("Tweaker reproduction assignments are invalid") from exc
            if not assignments or any(agent_id not in elite_ids for agent_id in assignments) or sum(assignments.values()) != birth_count:
                raise SpecialistError("Tweaker reproduction assignments are invalid")
            parent_briefs = response.get("parent_briefs")
            if not isinstance(parent_briefs, dict) or any(agent_id not in elite_ids or not str(brief).strip() for agent_id, brief in parent_briefs.items()):
                raise SpecialistError("Tweaker parent reproduction briefs are invalid")
            if any(agent_id not in parent_briefs for agent_id in assignments):
                raise SpecialistError("Tweaker omitted a reproduction brief for an assigned elite")
            return response
        response = checkpointed_json(self.model, identity, system, payload, checkpoint_dir / "final.json" if checkpoint_dir else None, validate_final)
        raw = response.get("reproduction_assignments")
        assignments = {str(agent_id): int(count) for agent_id, count in raw.items() if int(count) > 0}
        parent_briefs = response.get("parent_briefs")
        return {"epoch": epoch, "covered_fragment_ids": response["covered_fragment_ids"], "elite_characteristics": response["elite_characteristics"], "successful_patterns": response["successful_patterns"], "failed_patterns": response["failed_patterns"], "reproduction_context": response["reproduction_context"], "parent_briefs": {agent_id: str(parent_briefs[agent_id]) for agent_id in assignments}, "reproduction_assignments": assignments, "reproduction_parents": list(assignments)}


class BirtherAgent:
    def __init__(self, model: SpecialistModel, instruction: str): self.model, self.instruction = model, instruction

    def first_birth(self, population: int, objective_prompt: str, checkpoint_dir: Path | None = None) -> list[str]:
        batch_size = max(1, min(int(os.getenv("KADATH_BIRTHER_BATCH_SIZE", "20")), 25))
        variations: list[str] = []
        for start in range(0, population, batch_size):
            count = min(batch_size, population - start)
            identity = "birther/generation-1"
            system = f"You are KADATH Birther. {self.instruction} Create exactly this batch of distinct system-prompt variations. Framework code must remain identical. Use the supplied global variation indices so this batch differs from every other batch."
            payload = {"population": count, "population_total": population, "start_index": start + 1, "end_index": start + count, "objective_prompt": objective_prompt, "required_fields": ["prompt_variations"]}
            def validate(response: dict[str, Any]):
                batch = response.get("prompt_variations")
                if not isinstance(batch, list) or len(batch) != count or any(not isinstance(item, str) or not item.strip() for item in batch):
                    raise SpecialistError(f"Birther did not return Generation 1 batch {start // batch_size + 1}")
                return {"prompt_variations": batch}
            response = checkpointed_json(self.model, identity, system, payload, checkpoint_dir / f"batch-{start // batch_size + 1:04d}.json" if checkpoint_dir else None, validate)
            batch = response["prompt_variations"]
            # The kernel-owned index guarantees distinct genome prompts even
            # if a model repeats prose across two independently generated batches.
            variations.extend(f"Generation-one variation {start + offset + 1}:\n{item.strip()}" for offset, item in enumerate(batch))
        if len(variations) != population or len(set(variations)) != population:
            raise SpecialistError("Birther did not return the complete Generation 1 population")
        return variations

    def run(self, epoch: int, child_id: str, parent: dict[str, Any], tweaker_report: dict[str, Any], rejected_attempts: list[str] | None = None, checkpoint: Path | None = None) -> dict[str, Any]:
        identity = f"birther/epoch-{epoch}/{child_id}"
        system = f"You are KADATH Birther. {self.instruction} Produce one bounded, distinct child mutation derived only from this elite parent's tested genome and Tweaker report."
        payload = {"child_id": child_id, "parent": parent, "tweaker_report": tweaker_report, "rejected_duplicate_attempts": rejected_attempts or [],
                   "required_fields": ["prompt_suffix", "mutation_brief", "files", "delete_files"]}
        def validate(response: dict[str, Any]):
            suffix, brief, files, deletes = response.get("prompt_suffix"), response.get("mutation_brief"), response.get("files", {}), response.get("delete_files", [])
            if not isinstance(suffix, str) or not isinstance(brief, str) or not isinstance(files, dict) or not isinstance(deletes, list):
                raise SpecialistError("Birther output does not contain a valid mutation")
            return {"prompt_suffix": suffix, "mutation_brief": brief, "files": files, "delete_files": deletes}
        return checkpointed_json(self.model, identity, system, payload, checkpoint, validate)


def serialize_architect(output: ArchitectOutput) -> dict[str, Any]:
    return asdict(output)
