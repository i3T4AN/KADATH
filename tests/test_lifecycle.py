import json
import hashlib
import os
import sqlite3
import subprocess
import tempfile
import unittest
import sys
import urllib.error
import urllib.parse
import urllib.request
import base64
from threading import Event, Thread
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kadath.engine import Kernel, RunError, make_tree_owner_writable
from kadath.cli import _render_approval, _render_dashboard
from kadath.mutations import Mutation
from kadath.browsers import copy_browser_profile
from kadath.contracts import ExecutionRequest
from kadath.contracts import ObjectiveResult
from kadath.executors import CommandExecutor, ExecutionError
from kadath.store import Store
from kadath.workers import WorkerLimitError, WorkerPool
from kadath.worker_broker import ParentWorkerScope, WorkerBroker
from kadath.specialists import ArchitectAgent, BirtherAgent, GraderAgent, TweakerAgent, LiteLLMSpecialistModel, checkpointed_json
from kadath.artifacts import ArtifactStore
from tests.fixture_specialist import FixtureSpecialistModel


def kernel_at(root: Path) -> Kernel:
    return Kernel(root, FixtureSpecialistModel())


class LifecycleTests(unittest.TestCase):
    def test_guided_launcher_collects_setup_and_run_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); binary = root / "bin"; binary.mkdir()
            log = root / "docker.log"; config = root / "config.env"
            docker = binary / "docker"
            docker.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$KADATH_FAKE_DOCKER_LOG\"\n")
            docker.chmod(0o755)
            answers = "\n".join([
                "sk-test-key", "gpt-test", "maximize verified profit", "5m", "8", "2", "",
            ])
            environment = {**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"], "KADATH_ENV_FILE": str(config), "KADATH_FAKE_DOCKER_LOG": str(log)}
            result = subprocess.run([str(Path(__file__).parents[1] / "kadath.sh")], input=answers, text=True, capture_output=True, env=environment, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Made by i3t4an", result.stdout)
            self.assertIn("Credit to Hugging Face for smolagents", result.stdout)
            self.assertIn("OPENAI_API_KEY=sk-test-key", config.read_text())
            self.assertRegex(config.read_text(), r"KADATH_POSTGRES_PASSWORD=[0-9a-f]{48}")
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            calls = log.read_text()
            self.assertIn("build control organism-worker", calls)
            self.assertIn("up -d postgres minio searxng litellm playwright-mcp", calls)
            self.assertIn("start --goal maximize verified profit --epochs 2 --population 8 --epoch-seconds 300 --dashboard", calls)

    def test_litellm_runtime_resolves_the_selected_model(self) -> None:
        template = (Path(__file__).parents[1] / "deploy" / "litellm" / "config.yaml").read_text()
        rendered = template.replace("__KADATH_UPSTREAM_MODEL__", "gpt-selected")
        self.assertIn("model: openai/gpt-selected", rendered)
        self.assertNotIn("${KADATH_UPSTREAM_MODEL}", template)
        compose = (Path(__file__).parents[1] / "docker-compose.yml").read_text()
        self.assertIn('os.environ["KADATH_UPSTREAM_MODEL"]', compose)
        self.assertIn("OPENAI_API_KEY: ${OPENAI_API_KEY:-}", compose)
        self.assertIn("LITELLM_MASTER_KEY: ${LITELLM_MASTER_KEY:?set LITELLM_MASTER_KEY}", compose)
        self.assertIn("api_key: os.environ/OPENAI_API_KEY", template)

    def test_live_dashboard_prints_real_agent_activity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            kernel = kernel_at(Path(directory) / "runs")
            run_id = kernel.init("show real activity", "verified", 1, 4, 10)
            kernel._store(run_id).add_knowledge(
                run_id, 0, "agent-001", "activity",
                {"summary": "Investigated the objective and saved verified evidence."},
                datetime.now(UTC).isoformat(),
            )
            output = _render_dashboard(kernel.status(run_id), styled=True)
            self.assertIn("RECENT AGENT ACTIVITY", output)
            self.assertIn("agent-001", output)
            self.assertIn("Investigated the objective and saved verified evidence.", output)

    def test_confirmation_screen_contains_the_complete_architect_contract(self) -> None:
        architect = FixtureSpecialistModel().complete_json("architect", "", {"requested_criterion": "metric", "environment_inventory": {}})
        screen = _render_approval(architect, {"configured_services": ["searxng"], "agent_environment_keys": []})
        for expected in (
            "Objective:", "Metric:", "Attribution:", "Baseline:", "Score range:", "Rubric:",
            "Required outputs:", "Evidence requirements:", "Automatic failures:", "Tie breaks:",
            "Tie-break policy:", "Anti-fraud:", "Grader rules:", "Enabled tools:", "Kernel checks:",
            "Independent connectors:", "Measurement limitations:", "Specialist instructions:",
        ):
            self.assertIn(expected, screen)

    def test_architect_repairs_unselected_required_external_connector(self) -> None:
        fixture = FixtureSpecialistModel(); calls = {"count": 0}; repair_errors = []
        class Model:
            def complete_json(self, identity, system, payload):
                calls["count"] += 1
                if "contract_repair" in payload: repair_errors.append(payload["contract_repair"]["validation_error"])
                response = fixture.complete_json(identity, system, payload)
                response["benchmark"]["required_outputs"] = [{"description": "ledger receipt", "evidence_ref": "external:ledger"}]
                response["verification_plan"]["external_connectors"] = [] if calls["count"] == 1 else ["ledger"]
                return response
        output = ArchitectAgent(Model()).run(
            "earn verified profit", "net profit", 60, 100,
            environment_inventory={"available_optional_capabilities": [], "grader_connectors": ["ledger"]},
        )
        self.assertEqual(output.verification_plan["external_connectors"], ["ledger"])
        self.assertEqual(calls["count"], 2)
        self.assertIn("not selected", repair_errors[0])

    def test_architect_repairs_invalid_scoring_numbers(self) -> None:
        fixture = FixtureSpecialistModel(); calls = {"count": 0}; repair_errors = []
        class Model:
            def complete_json(self, identity, system, payload):
                calls["count"] += 1
                if "contract_repair" in payload: repair_errors.append(payload["contract_repair"]["validation_error"])
                response = fixture.complete_json(identity, system, payload)
                if calls["count"] == 1:
                    response["benchmark"]["score_range"] = [float("nan"), 100]
                elif calls["count"] == 2:
                    response["benchmark"]["scoring_rubric"] = [
                        {"id": "harmful", "criterion": "harmful result", "weight": -1, "measurement": {"type": "binary"}},
                        {"id": "useful", "criterion": "useful result", "weight": 101, "measurement": {"type": "binary"}},
                    ]
                return response
        output = ArchitectAgent(Model()).run("goal", "metric", 60, 100, environment_inventory={})
        self.assertEqual(output.benchmark["score_range"], [0, 100])
        self.assertEqual(calls["count"], 3)
        self.assertIn("score_range", repair_errors[0])
        self.assertIn("finite and positive", repair_errors[1])

    def test_architect_repairs_missing_anti_fraud_rules(self) -> None:
        fixture = FixtureSpecialistModel(); calls = {"count": 0}
        class Model:
            def complete_json(self, identity, system, payload):
                calls["count"] += 1
                response = fixture.complete_json(identity, system, payload)
                if calls["count"] == 1: response["anti_fraud_checks"] = [None]
                return response
        output = ArchitectAgent(Model()).run("goal", "metric", 60, 100, environment_inventory={})
        self.assertTrue(output.anti_fraud_checks)
        self.assertEqual(calls["count"], 2)

    def test_grader_repairs_unsupported_negative_verdicts(self) -> None:
        fixture = FixtureSpecialistModel(); final_calls = {"count": 0}; repair_errors = []
        class Model:
            def complete_json(self, identity, system, payload):
                response = fixture.complete_json(identity, system, payload)
                if not identity.startswith("grader/"): return response
                final_calls["count"] += 1
                if "contract_repair" in payload: repair_errors.append(payload["contract_repair"]["validation_error"])
                if final_calls["count"] == 1:
                    response["failure_assessments"][0].update(triggered=True, evidence_refs=[])
                elif final_calls["count"] == 2:
                    response["anti_fraud_assessments"][0].update(passed=False, evidence_refs=[])
                return response
        objective = fixture.complete_json("architect", "", {"requested_criterion": "verified score", "environment_inventory": {}})
        request = ExecutionRequest("run", 1, "agent-001", "genome", "goal", "criterion", "prompt", Path("."), Path("."), datetime.now(UTC))
        result = GraderAgent(Model(), "validate evidence").run(
            request,
            {"organism_evidence": {"receipt": "verified"}, "activity": [], "files": [], "tool_trace": []},
            objective,
            {"valid_evidence_refs": ["organism_evidence"]},
        )
        self.assertEqual(result.outcome, "success")
        self.assertEqual(final_calls["count"], 3)
        self.assertIn("without evidence", repair_errors[0])
        self.assertIn("anti-fraud", repair_errors[1])

    def test_blank_grader_connector_is_not_advertised(self) -> None:
        previous = os.environ.get("KADATH_GRADER_CONNECTOR_LEDGER_URL")
        os.environ["KADATH_GRADER_CONNECTOR_LEDGER_URL"] = ""
        try: inventory = Kernel._environment_inventory("simulated", {})
        finally:
            if previous is None: os.environ.pop("KADATH_GRADER_CONNECTOR_LEDGER_URL", None)
            else: os.environ["KADATH_GRADER_CONNECTOR_LEDGER_URL"] = previous
        self.assertNotIn("ledger", inventory["grader_connectors"])

    def test_frozen_attempt_uses_only_its_attempt_scoped_model_traces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); kernel = kernel_at(root / "runs"); run_id = "run"
            repository = root / "repository"; state = root / "state"; repository.mkdir(); state.mkdir()
            trace_root = kernel._run_dir(run_id) / "model-calls" / "epoch-0001"
            old = trace_root / "attempt-old" / "agent-001"; old.mkdir(parents=True); (old / "old.json").write_text("old")
            current = trace_root / "attempt-current" / "agent-001"; current.mkdir(parents=True); (current / "current.json").write_text("current")
            request = ExecutionRequest("run", 1, "agent-001", "genome", "goal", "criterion", "prompt", repository, state, datetime.now(UTC), attempt_id="attempt-current")
            frozen = kernel._freeze_attempt(request, ObjectiveResult(0, {}), None, "objective")
            paths = {record["path"] for record in frozen["files"]}
            self.assertIn("model-calls/current.json", paths)
            self.assertNotIn("model-calls/old.json", paths)
            self.assertEqual(frozen["attempt_id"], "attempt-current")

    def test_binary_image_is_attached_for_multimodal_grading(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); image = root / "workspace" / "proof.png"; image.parent.mkdir()
            image.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="))
            manifest = root / "attempt.json"; manifest.write_text("{}")
            frozen = {"manifest_path": str(manifest), "files": [{"path": "workspace/proof.png", "size": image.stat().st_size, "sha256": hashlib.sha256(image.read_bytes()).hexdigest()}]}
            entries = [entry for chunk in GraderAgent._evidence_chunks(frozen, 10_000) for entry in chunk]
            self.assertEqual(entries[0]["_attachment"]["kind"], "image")
            self.assertEqual(entries[0]["evidence_ref"], "file:workspace/proof.png")

    def test_pdf_pages_are_extracted_and_rendered_for_multimodal_grading(self) -> None:
        import pymupdf
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); pdf = root / "workspace" / "proof.pdf"; pdf.parent.mkdir()
            document = pymupdf.open(); page = document.new_page(); page.insert_text((72, 72), "verified PDF evidence"); document.save(pdf); document.close()
            manifest = root / "attempt.json"; manifest.write_text("{}")
            frozen = {"manifest_path": str(manifest), "files": [{"path": "workspace/proof.pdf", "size": pdf.stat().st_size, "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest()}]}
            entries = [entry for chunk in GraderAgent._evidence_chunks(frozen, 10_000) for entry in chunk]
            self.assertIn("verified PDF evidence", json.dumps(entries))
            attachment = next(entry["_attachment"] for entry in entries if "_attachment" in entry)
            label, content = LiteLLMSpecialistModel._attachment_part(attachment)
            self.assertIn("PDF page 1", label)
            self.assertTrue(content["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    def test_specialist_contract_validation_repairs_before_checkpoint(self) -> None:
        calls = {"count": 0}
        class Model:
            def complete_json(self, identity, system, payload):
                calls["count"] += 1
                if calls["count"] == 1: return {"value": "wrong"}
                self.repair = payload["contract_repair"]
                return {"value": 7}
        model = Model()
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "result.json"
            result = checkpointed_json(model, "specialist", "system", {"input": True}, checkpoint, lambda response: response if response.get("value") == 7 else (_ for _ in ()).throw(ValueError("value must equal 7")))
            self.assertEqual(result["value"], 7)
            self.assertEqual(calls["count"], 2)
            self.assertEqual(model.repair["validation_error"], "value must equal 7")
            self.assertEqual(json.loads(checkpoint.read_text())["response"], {"value": 7})

    def test_artifact_store_streams_large_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "large.bin"
            with source.open("wb") as stream:
                for _ in range(16): stream.write(b"x" * 1024 * 1024)
            result = ArtifactStore(root / "artifacts").put_file("run", 1, "agent", "large.bin", source)
            target = Path(result["uri"])
            self.assertEqual(target.stat().st_size, source.stat().st_size)
            self.assertEqual(result["sha256"], hashlib.sha256(b"x" * (16 * 1024 * 1024)).hexdigest())

    def test_memory_quality_uses_the_producing_epoch_cohort(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite"); timestamp = datetime.now(UTC).isoformat()
            store.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,0,?)", ("run", "goal", "criterion", 1, 1, 100, "paused", timestamp))
            for rank in range(1, 101):
                agent_id = f"agent-{rank:03d}"
                store.execute("INSERT INTO agents VALUES(?,?,?,?,?,?,?,?)", ("run", agent_id, "active", f"g-{rank}", None, agent_id, 0, rank))
                store.execute("INSERT INTO scores VALUES(?,?,?,?,?,?,?,?)", ("run", 1, agent_id, f"g-{rank}", 101-rank, 0, "success", "{}"))
            for number in range(101, 131):
                agent_id = f"agent-{number:03d}"
                store.execute("INSERT INTO agents VALUES(?,?,?,?,?,?,?,NULL)", ("run", agent_id, "active", f"g-{number}", None, agent_id, 1))
            trust = store.epoch_memory_trust("run")
            self.assertGreater(trust[("agent-030", 1)], trust[("agent-031", 1)])
            self.assertGreater(trust[("agent-031", 1)], trust[("agent-071", 1)])
            payload = {"summary": "same useful memory", "visibility": "shared"}
            store.add_knowledge("run", 1, "agent-001", "activity", payload, timestamp)
            store.add_knowledge("run", 1, "agent-100", "activity", payload, timestamp)
            memory = store.ranked_memory("run", "agent-040", "useful", 10)
            self.assertEqual(memory[0]["agent_id"], "agent-001")

    def test_system_prompt_file_mutation_becomes_registered_genome_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            kernel = kernel_at(Path(directory) / "runs"); run_id = kernel.init("goal", "criterion", 1, 4, 10); kernel.approve(run_id)
            store = kernel._store(run_id)
            agent = store.one("SELECT * FROM agents WHERE run_id=? AND agent_id='agent-001'", (run_id,))
            parent = store.one("SELECT * FROM genomes WHERE hash=?", (agent["active_genome"],))
            mutation = Mutation.from_payload({"action": "mutate", "reason": "rewrite", "prompt_suffix": "", "files": {"SYSTEM_PROMPT.md": "A completely rewritten evolved prompt."}, "delete_files": []})
            commit, prompt = kernel._materialize_agent(run_id, agent["agent_id"], agent["branch_name"], kernel._git_commit(parent), parent["prompt"], "prompt rewrite proof", mutation)
            child = kernel._create_genome(store, run_id, prompt, parent["hash"], 1, commit)
            registered = store.one("SELECT * FROM genomes WHERE hash=?", (child,))
            self.assertEqual(registered["prompt"], "A completely rewritten evolved prompt.")
            self.assertEqual((kernel._agent_dir(run_id, agent["agent_id"]) / "repository" / "SYSTEM_PROMPT.md").read_text(), registered["prompt"] + "\n")

    def test_grader_writes_kernel_owned_activity_for_every_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            kernel = kernel_at(Path(directory) / "runs"); run_id = kernel.init("goal", "criterion", 1, 4, 10); kernel.approve(run_id); kernel.run(run_id)
            records = kernel._store(run_id).rows("SELECT agent_id,payload_json FROM knowledge WHERE run_id=? AND kind='kernel_activity' ORDER BY agent_id", (run_id,))
            self.assertEqual(len(records), 4)
            self.assertTrue(all(json.loads(row["payload_json"])["source"] == "kernel" for row in records))

    def test_large_frozen_text_is_streamed_into_grader_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); evidence = root / "workspace" / "large.txt"; evidence.parent.mkdir()
            content = ("begin\n" + "x" * 150_000 + "\nEND-MARKER")
            evidence.write_text(content)
            manifest = root / "attempt.json"; manifest.write_text("{}")
            frozen = {"manifest_path": str(manifest), "files": [{"path": "workspace/large.txt", "size": evidence.stat().st_size, "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}]}
            entries = [entry for chunk in GraderAgent._evidence_chunks(frozen, 10_000) for entry in chunk]
            self.assertGreater(len(entries), 10)
            self.assertTrue(any("END-MARKER" in entry.get("content_fragment", "") for entry in entries))
            self.assertEqual(len(entries), len({entry["fragment_id"] for entry in entries}))

    def test_generation_one_model_results_are_checkpointed(self) -> None:
        calls = {"count": 0}
        class Model:
            def complete_json(self, identity, system, payload):
                calls["count"] += 1
                return {"prompt_variations": [f"variation-{index}" for index in range(payload["population"])]}
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory)
            first = BirtherAgent(Model(), "distinct").first_birth(4, "goal", checkpoint)
            second = BirtherAgent(Model(), "distinct").first_birth(4, "goal", checkpoint)
            self.assertEqual(first, second)
            self.assertEqual(calls["count"], 1)

    def test_generation_one_is_batched_and_kernel_distinct(self) -> None:
        model = FixtureSpecialistModel(); calls = []
        class Recording:
            def complete_json(self, identity, system, payload):
                calls.append((identity, payload["population"]))
                return model.complete_json(identity, system, payload)
        previous = os.environ.get("KADATH_BIRTHER_BATCH_SIZE"); os.environ["KADATH_BIRTHER_BATCH_SIZE"] = "20"
        try: variations = BirtherAgent(Recording(), "bounded").first_birth(53, "objective")
        finally:
            if previous is None: os.environ.pop("KADATH_BIRTHER_BATCH_SIZE", None)
            else: os.environ["KADATH_BIRTHER_BATCH_SIZE"] = previous
        self.assertEqual([count for _identity, count in calls], [20, 20, 13])
        self.assertEqual(len(variations), len(set(variations)))

    def test_every_grader_evidence_type_is_chunked(self) -> None:
        frozen = {"candidate": {"value": "a" * 50_000}, "organism_evidence": {"value": "b" * 50_000}, "activity": [{"value": "c" * 50_000}], "files": [], "tool_trace": [{"value": "d" * 50_000}], "external_measurements": {"ledger": {"value": "e" * 50_000}}}
        chunks = GraderAgent._evidence_chunks(frozen, 10_000)
        entries = [entry for chunk in chunks for entry in chunk]
        self.assertTrue({"candidate", "organism_evidence", "activity:0", "tool_trace:0", "external:ledger"}.issubset({entry["evidence_ref"] for entry in entries}))
        self.assertTrue(all(len(json.dumps(entry)) < 10_000 for entry in entries))

    def test_grader_reviews_are_hierarchically_consolidated(self) -> None:
        class Reducer:
            def complete_json(self, identity, system, payload):
                refs = []
                for review in payload["review_summaries"]:
                    refs.extend(note["evidence_ref"] for note in review.get("evidence_notes", []))
                return {"reviewed_summary_ids": [review["review_id"] for review in payload["review_summaries"]], "evidence_notes": [{"evidence_ref": ref, "note": "merged"} for ref in dict.fromkeys(refs)], "contradictions": [], "fraud_signals": []}
        request = ExecutionRequest("run", 1, "agent-001", "genome", "goal", "criterion", "prompt", Path("."), Path("."), datetime.now(UTC))
        reviews = [{"review_id": f"leaf:{index}", "coverage": GraderAgent._coverage({f"activity:{index}#0"}), "chunk": index, "evidence_notes": [{"evidence_ref": f"activity:{index}", "details": "x" * 800}], "contradictions": [], "fraud_signals": []} for index in range(100)]
        consolidated = GraderAgent(Reducer(), "")._consolidate_reviews(request, {"benchmark": {}}, reviews, 10_000, None)
        self.assertLessEqual(len(json.dumps(consolidated)), 10_000)
        self.assertEqual({note["evidence_ref"] for review in consolidated for note in review["evidence_notes"]}, {f"activity:{index}" for index in range(100)})
        self.assertEqual(sum(review["coverage"]["count"] for review in consolidated), 100)
        self.assertTrue(all("reviewed_fragment_ids" not in review for review in consolidated))

    def test_tweaker_chunks_all_dossiers_before_birther_summary(self) -> None:
        calls = {"batch": 0, "reduce": 0}
        class Model:
            def complete_json(self, identity, system, payload):
                if identity.startswith("tweaker-batch/"):
                    calls["batch"] += 1
                    return {"covered_fragment_ids": [item["fragment_id"] for item in payload["dossier_fragments"]], "agent_findings": [{"agent_id": row["agent_id"], "finding": "x" * 1_000} for row in payload["ranked_subset"]], "successful_characteristics": ["works"], "failed_characteristics": [], "evidence_quality_notes": []}
                if identity.startswith("tweaker-reduce/"):
                    calls["reduce"] += 1
                    findings = [finding for summary in payload["analysis_summaries"] for finding in summary.get("agent_findings", [])]
                    return {"covered_fragment_ids": [item for summary in payload["analysis_summaries"] for item in summary["covered_fragment_ids"]], "agent_findings": findings, "successful_characteristics": ["works"], "failed_characteristics": [], "evidence_quality_notes": []}
                parents = [row["agent_id"] for row in payload["ranked"][:payload["elite_count"]]]
                return {"covered_fragment_ids": [item for summary in payload["analysis"]["batch_summaries"] for item in summary["covered_fragment_ids"]], "elite_characteristics": ["works"], "successful_patterns": ["verified"], "failed_patterns": [], "reproduction_context": "summary", "parent_briefs": {parent: "brief" for parent in parents}, "reproduction_assignments": {parent: 1 for parent in parents}}
        ranked = [{"agent_id": f"agent-{index:03d}", "value": 100 - index} for index in range(1, 21)]
        dossiers = [{"agent_id": row["agent_id"], "payload": "x" * 100_000} for row in ranked]
        previous = os.environ.get("KADATH_TWEAKER_CHARS"); os.environ["KADATH_TWEAKER_CHARS"] = "50000"
        try: report = TweakerAgent(Model(), "analyze").run(1, ranked, 6, 8, 6, {"tweaker_dossiers": dossiers, "memory_ranking": "ranked"})
        finally:
            if previous is None: os.environ.pop("KADATH_TWEAKER_CHARS", None)
            else: os.environ["KADATH_TWEAKER_CHARS"] = previous
        self.assertGreater(calls["batch"], 20)
        self.assertGreater(calls["reduce"], 0)
        self.assertEqual(sum(report["reproduction_assignments"].values()), 6)

    def test_browser_snapshot_uses_consistent_sqlite_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "source"; destination = root / "destination"; source.mkdir()
            connection = sqlite3.connect(source / "Cookies")
            connection.execute("CREATE TABLE cookies(name TEXT)"); connection.execute("INSERT INTO cookies VALUES('session')"); connection.commit()
            (source / "Preferences").write_text('{"theme":"dark"}')
            try: copy_browser_profile(source, destination)
            finally: connection.close()
            copied = sqlite3.connect(destination / "Cookies")
            try: self.assertEqual(copied.execute("SELECT name FROM cookies").fetchone()[0], "session")
            finally: copied.close()
            self.assertEqual((destination / "Preferences").read_text(), '{"theme":"dark"}')

    def test_frozen_attempt_seal_detects_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            kernel = kernel_at(Path(directory) / "runs"); root = kernel._run_dir("run") / "frozen-attempts" / "epoch-0001" / "agent-001"
            root.mkdir(parents=True); (root / "attempt.json").write_text('{"fixed":true}')
            store = kernel._store("run")
            kernel._seal_frozen_attempt(store, "run", 1, "agent-001"); kernel._verify_frozen_attempt_seal(root)
            (root / "attempt.json").chmod(0o600); (root / "attempt.json").write_text('{"fixed":false}')
            with self.assertRaisesRegex(RunError, "seal failed"): kernel._verify_frozen_attempt_seal(root)
            make_tree_owner_writable(root)

    def test_export_requires_terminal_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            kernel = kernel_at(Path(directory) / "runs"); run_id = kernel.init("finish first", "verified", 1, 4, 10)
            with self.assertRaisesRegex(RunError, "terminal state"): kernel.export(run_id)

    def test_external_measurement_connector_is_identity_attributed(self) -> None:
        calls = {"count": 0}
        class Connector(BaseHTTPRequestHandler):
            def log_message(self, *_args): pass
            def do_POST(self):
                calls["count"] += 1
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                payload = json.dumps({"attribution": {key: request[key] for key in ("run_id", "epoch", "agent_id", "genome")}, "facts": {"verified_profit": 12}}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Connector); Thread(target=server.serve_forever, daemon=True).start()
        previous = os.environ.get("KADATH_GRADER_CONNECTOR_LEDGER_URL")
        os.environ["KADATH_GRADER_CONNECTOR_LEDGER_URL"] = f"http://127.0.0.1:{server.server_port}"
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); repository = root / "repo"; state = root / "state"; repository.mkdir(); state.mkdir()
                request = ExecutionRequest("run", 2, "agent-001", "genome", "goal", "criterion", "prompt", repository, state, datetime.now(UTC))
                frozen = root / "frozen"; frozen.mkdir()
                objective = {"objective_hash": "objective", "verification_plan": {"external_connectors": ["ledger"]}, "benchmark": {}}
                kernel = Kernel(root / "runs", FixtureSpecialistModel())
                result = kernel._external_measurements(request, objective, frozen)
                self.assertEqual(result["ledger"]["facts"]["verified_profit"], 12)
                self.assertEqual(kernel._external_measurements(request, objective, frozen), result)
                self.assertEqual(calls["count"], 1)
        finally:
            if previous is None: os.environ.pop("KADATH_GRADER_CONNECTOR_LEDGER_URL", None)
            else: os.environ["KADATH_GRADER_CONNECTOR_LEDGER_URL"] = previous
            server.shutdown(); server.server_close()

    def test_environment_inventory_exposes_names_not_secret_values(self) -> None:
        inventory = Kernel._environment_inventory("docker", {"PAYMENT_API_KEY": "do-not-leak", "ACCOUNT_NAME": "private-account"})
        encoded = json.dumps(inventory)
        self.assertIn("PAYMENT_API_KEY", inventory["agent_environment_keys"])
        self.assertIn("PAYMENT_API_KEY", inventory["credential_like_environment_keys"])
        self.assertNotIn("do-not-leak", encoded)
        self.assertNotIn("private-account", encoded)

    def test_memory_bank_deduplicates_links_and_ranks_usefulness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite")
            timestamp = datetime.now(UTC).isoformat()
            payload = {"summary": "A durable useful method", "visibility": "private"}
            first = store.add_knowledge("run", 1, "parent", "activity", payload, timestamp)
            second = store.add_knowledge("run", 1, "parent", "activity", payload, timestamp)
            self.assertEqual(first, second)
            self.assertEqual(len(store.rows("SELECT * FROM knowledge")), 1)
            store.link_memory("run", "child", first, "parent", 1, timestamp)
            store.rate_knowledge("run", first, "child", 1, timestamp)
            memory = store.ranked_memory("run", "child", "useful", 10)
            self.assertEqual(len(memory), 1)
            self.assertEqual(memory[0]["agent_id"], "child")
            self.assertEqual(memory[0]["memory_scope"], "inherited")
            self.assertEqual(memory[0]["source_agent_id"], "parent")
            self.assertGreater(memory[0]["usefulness"], 0.0)
            self.assertLess(memory[0]["usefulness"], 1.0)
            with self.assertRaisesRegex(ValueError, "own knowledge"):
                store.rate_knowledge("run", first, "parent", 1, timestamp)

    def test_existing_sqlite_memory_schema_migrates_and_deduplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.sqlite"
            con = sqlite3.connect(path)
            con.execute("CREATE TABLE knowledge(id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT,epoch INTEGER,agent_id TEXT,kind TEXT,payload_json TEXT,published_at TEXT)")
            con.execute("CREATE TABLE memory_links(run_id TEXT,agent_id TEXT,knowledge_id INTEGER,inherited_from_agent_id TEXT,inherited_at_epoch INTEGER,created_at TEXT,PRIMARY KEY(run_id,agent_id,knowledge_id))")
            con.execute("CREATE TABLE knowledge_ratings(run_id TEXT,knowledge_id INTEGER,agent_id TEXT,value INTEGER,created_at TEXT,PRIMARY KEY(run_id,knowledge_id,agent_id))")
            payload = json.dumps({"summary": "duplicate", "visibility": "shared"}, sort_keys=True)
            con.execute("INSERT INTO knowledge(run_id,epoch,agent_id,kind,payload_json,published_at) VALUES('run',1,'agent','activity',?,'now')", (payload,))
            con.execute("INSERT INTO knowledge(run_id,epoch,agent_id,kind,payload_json,published_at) VALUES('run',1,'agent','activity',?,'now')", (payload,))
            con.execute("INSERT INTO memory_links VALUES('run','child-only-duplicate',2,'agent',1,'now')")
            con.execute("INSERT INTO memory_links VALUES('run','child-conflict',1,'agent',1,'first')")
            con.execute("INSERT INTO memory_links VALUES('run','child-conflict',2,'agent',2,'duplicate')")
            con.execute("INSERT INTO knowledge_ratings VALUES('run',2,'reviewer-only-duplicate',-1,'now')")
            con.execute("INSERT INTO knowledge_ratings VALUES('run',1,'reviewer-conflict',1,'first')")
            con.execute("INSERT INTO knowledge_ratings VALUES('run',2,'reviewer-conflict',-1,'duplicate')")
            con.commit(); con.close()
            store = Store(path)
            self.assertEqual(len(store.rows("SELECT * FROM knowledge")), 1)
            self.assertTrue(store.one("SELECT content_hash FROM knowledge")["content_hash"])
            links = store.rows("SELECT agent_id,knowledge_id,inherited_at_epoch FROM memory_links ORDER BY agent_id")
            self.assertEqual(links, [
                {"agent_id": "child-conflict", "knowledge_id": 1, "inherited_at_epoch": 1},
                {"agent_id": "child-only-duplicate", "knowledge_id": 1, "inherited_at_epoch": 1},
            ])
            ratings = store.rows("SELECT knowledge_id,agent_id,value FROM knowledge_ratings ORDER BY agent_id")
            self.assertEqual(ratings, [
                {"knowledge_id": 1, "agent_id": "reviewer-conflict", "value": 1},
                {"knowledge_id": 1, "agent_id": "reviewer-only-duplicate", "value": -1},
            ])

    def test_grader_outage_regrades_without_rerunning_organisms(self) -> None:
        class FlakyModel(FixtureSpecialistModel):
            fail_grading = True
            def complete_json(self, identity, system, payload):
                if identity.startswith("grader/") and self.fail_grading: raise RuntimeError("provider unavailable")
                return super().complete_json(identity, system, payload)
        class CountingExecutor:
            def __init__(self): self.calls = 0
            def execute(self, request):
                self.calls += 1
                return ObjectiveResult(999, {"receipt": request.agent_id})
        with tempfile.TemporaryDirectory() as directory:
            model = FlakyModel(); kernel = Kernel(Path(directory) / "runs", model)
            run_id = kernel.init("regrade safely", "verified output", 1, 4, 10)
            kernel.approve(run_id); executor = CountingExecutor()
            kernel._executor = lambda _run_id: executor  # type: ignore[method-assign]
            with self.assertRaisesRegex(Exception, "grading paused"):
                kernel.run(run_id)
            self.assertEqual(executor.calls, 4)
            self.assertEqual(kernel._store(run_id).one("SELECT status FROM epochs WHERE run_id=? AND epoch=1", (run_id,))["status"], "grading_interrupted")
            model.fail_grading = False
            kernel.run(run_id)
            self.assertEqual(executor.calls, 4)
            self.assertEqual(len(kernel._store(run_id).rows("SELECT * FROM scores WHERE run_id=?", (run_id,))), 4)

    def test_selection_rollback_restores_complete_agent_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            kernel = kernel_at(Path(directory) / "runs")
            run_id = kernel.init("rollback state", "verified", 2, 4, 10)
            kernel.approve(run_id); store = kernel._store(run_id)
            state = kernel._agent_dir(run_id, "agent-001") / "state"
            (state / "custom-memory").mkdir(); (state / "custom-memory" / "fact.txt").write_text("before")
            kernel._selection_snapshot(store, run_id, 1)
            (state / "custom-memory" / "fact.txt").write_text("corrupted")
            (state / "new-partial-file").write_text("partial")
            kernel._rollback_selection(store, run_id, 1)
            restored = kernel._agent_dir(run_id, "agent-001") / "state"
            self.assertEqual((restored / "custom-memory" / "fact.txt").read_text(), "before")
            self.assertFalse((restored / "new-partial-file").exists())

    def test_full_cycle_preserves_population_and_exports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            kernel = kernel_at(Path(directory) / "runs")
            run_id = kernel.init("improve benchmark", "verified score", epochs=3, population=10, epoch_seconds=1)
            self.assertEqual(kernel.status(run_id)["run"]["status"], "awaiting_approval")
            kernel.approve(run_id)
            self.assertEqual(kernel.run(run_id), 3)
            state = kernel.status(run_id)
            self.assertEqual(state["run"]["status"], "complete")
            self.assertEqual(next(row["count"] for row in state["population"] if row["status"] == "active"), 10)
            self.assertEqual(next(row["count"] for row in state["population"] if row["status"] == "archived"), 6)
            report = kernel.export(run_id) / "run-report.json"
            payload = json.loads(report.read_text())
            self.assertEqual(len(payload["scores"]), 30)
            self.assertGreaterEqual(len(payload["lineage"]), 16)
            self.assertTrue((report.parent / "git-repository").is_dir())
            self.assertEqual(len(list((report.parent / "final-population").glob("agent-*/SYSTEM_PROMPT.md"))), 10)
            self.assertEqual(len(list((report.parent / "final-population").glob("agent-*/organism.py"))), 10)
            self.assertEqual(len(list((report.parent / "archived-agents").glob("agent-*/organism.py"))), 6)
            self.assertTrue((report.parent / "epoch-champions" / "records.json").is_file())
            self.assertTrue((report.parent / "top-historical-genomes" / "records.json").is_file())
            historical = json.loads((report.parent / "top-historical-genomes" / "records.json").read_text())
            self.assertEqual(len(historical), len({row["genome_hash"] for row in historical}))
            self.assertTrue((report.parent / "architect-output.json").is_file())
            self.assertEqual(len(list((report.parent / "tweaker-reports").glob("epoch-*.json"))), 2)
            self.assertEqual(len(list((report.parent / "birther-reports").glob("epoch-*.json"))), 2)
            state = Path(directory) / "runs" / run_id / "agents" / "agent-001" / "state"
            self.assertTrue(json.loads((state / "shared-knowledge.json").read_text())["records"])
            self.assertTrue(list((Path(directory) / "runs" / run_id / "agents").glob("*/state/adaptation-context.json")))
            inherited = kernel._store(run_id).rows("SELECT * FROM memory_links WHERE run_id=?", (run_id,))
            self.assertTrue(inherited)

    def test_reset_is_scoped_to_one_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            kernel = kernel_at(Path(directory) / "runs")
            first = kernel.init("a", "b", 1, 4, 1)
            second = kernel.init("c", "d", 1, 4, 1)
            kernel.reset(first)
            self.assertFalse((Path(directory) / "runs" / first).exists())
            self.assertTrue((Path(directory) / "runs" / second).exists())
            with self.assertRaisesRegex(Exception, "run not found"):
                kernel.status(first)

    def test_cleanup_removes_only_terminal_runs_and_preserves_exports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            kernel = kernel_at(root / "runs")
            finished = kernel.init("finished", "verified", 1, 4, 1)
            active = kernel.init("active", "verified", 1, 4, 1)
            kernel._store(finished).execute(
                "UPDATE runs SET status='failed' WHERE id=?", (finished,)
            )
            export = root / "exports" / finished
            export.mkdir(parents=True)
            (export / "verified.txt").write_text("preserve")

            result = kernel.cleanup_completed()

            self.assertEqual(result["removed"], [finished])
            self.assertEqual(result["protected"], [active])
            self.assertFalse((root / "runs" / finished).exists())
            self.assertTrue((root / "runs" / active).exists())
            self.assertEqual((export / "verified.txt").read_text(), "preserve")

    def test_command_executor_collects_agent_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            kernel = kernel_at(Path(directory) / "runs")
            fixture = Path(__file__).with_name("fixture_agent.py")
            run_id = kernel.init("verify a result", "verified output", 1, 4, 10, "command", [sys.executable, str(fixture)])
            kernel.approve(run_id)
            kernel.run(run_id)
            report = json.loads((kernel.export(run_id) / "run-report.json").read_text())
            self.assertEqual({score["value"] for score in report["scores"]}, {42.0})
            self.assertTrue(all("grader_assessment" in json.loads(score["evidence_json"]) for score in report["scores"]))
            self.assertFalse((Path(directory) / "runs" / run_id / "agents" / "agent-002" / "repository" / "agent_strategy.py").is_file())
            activities = kernel._store(run_id).rows("SELECT payload_json FROM knowledge WHERE run_id=? AND kind='activity'", (run_id,))
            self.assertEqual(len(activities), 4)

    def test_worker_limit_is_kernel_enforced(self) -> None:
        pool = WorkerPool(max_workers_per_parent=5)
        release = Event()
        try:
            handles = [pool.spawn("agent-001", lambda: (release.wait(), 1)[1]) for _ in range(5)]
            with self.assertRaises(WorkerLimitError):
                pool.spawn("agent-001", lambda: 1)
            release.set()
            self.assertEqual([future.result() for _, future in handles], [1] * 5)
        finally:
            pool.shutdown()

    def test_command_executor_rejects_mid_epoch_genome_edits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); worktree = root / "worktree"; state = root / "state"
            worktree.mkdir(); state.mkdir(); (worktree / "SYSTEM_PROMPT.md").write_text("locked\n")
            request = ExecutionRequest("run", 1, "agent-001", "genome", "goal", "criterion", "prompt", worktree, state, datetime.now(UTC) + timedelta(seconds=10))
            script = Path(__file__).with_name("evil_agent.py")
            with self.assertRaises(ExecutionError):
                CommandExecutor([sys.executable, str(script)]).execute(request)

    def test_epoch_rejects_repository_changed_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            kernel = kernel_at(Path(directory) / "runs")
            run_id = kernel.init("protect scored genome", "verified output", 1, 4, 10)
            kernel.approve(run_id)
            repository = kernel._agent_dir(run_id, "agent-001") / "repository"
            (repository / "host-tamper.py").write_text("TAMPERED = True\n")
            with self.assertRaisesRegex(Exception, "does not match the scored genome"):
                kernel.run(run_id)

    def test_frozen_evidence_rejects_symlinks_without_reading_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); runs = root / "runs"; repository = root / "repo"; state = root / "state"
            repository.mkdir(); (state / "workspace").mkdir(parents=True)
            secret = root / "control-secret"; secret.write_text("must-not-leak")
            (state / "workspace" / "leak").symlink_to(secret)
            request = ExecutionRequest("run", 1, "agent-001", "genome", "goal", "criterion", "prompt", repository, state, datetime.now(UTC))
            frozen = Kernel(runs, FixtureSpecialistModel())._freeze_attempt(request, ObjectiveResult(0, {}), None, "objective")
            self.assertIn("unsafe evidence was rejected", frozen["execution_error"])
            self.assertNotIn("must-not-leak", json.dumps(frozen))

    def test_architect_grader_ignores_agent_reported_score(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            kernel = kernel_at(root / "runs")
            run_id = kernel.init("benchmark", "verified benchmark", 1, 4, 10)
            kernel.approve(run_id)
            kernel.run(run_id)
            scores = kernel._store(run_id).rows("SELECT value FROM scores WHERE run_id=? ORDER BY value", (run_id,))
            self.assertEqual([score["value"] for score in scores], [42.0] * 4)

    def test_interrupted_epoch_can_restart_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            kernel = kernel_at(Path(directory) / "runs")
            run_id = kernel.init("recover", "verified output", 1, 4, 10)
            kernel.approve(run_id)

            class FailingExecutor:
                def execute(self, _request):
                    raise ExecutionError("intentional interruption")

            normal = kernel._executor
            kernel._executor = lambda _run_id: FailingExecutor()  # type: ignore[method-assign]
            self.assertEqual(kernel.run(run_id), 1)
            self.assertEqual(kernel.status(run_id)["run"]["status"], "failed")
            scores = kernel._store(run_id).rows("SELECT * FROM scores WHERE run_id=?", (run_id,))
            self.assertEqual(len(scores), 4)
            self.assertTrue(all(score["outcome"] == "failed" for score in scores))
            kernel._executor = normal  # type: ignore[method-assign]

    def test_one_agent_failure_does_not_abort_population(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            kernel = kernel_at(Path(directory) / "runs")
            run_id = kernel.init("survive one failure", "verified output", 1, 4, 10)
            kernel.approve(run_id)
            class PartialExecutor:
                def execute(self, request):
                    if request.agent_id == "agent-001": raise ExecutionError("one agent died")
                    return ObjectiveResult(999, {"candidate": request.agent_id})
            kernel._executor = lambda _run_id: PartialExecutor()  # type: ignore[method-assign]
            self.assertEqual(kernel.run(run_id), 1)
            scores = kernel._store(run_id).rows("SELECT agent_id,outcome FROM scores WHERE run_id=? ORDER BY agent_id", (run_id,))
            self.assertEqual([row["outcome"] for row in scores], ["failed", "success", "success", "success"])
            self.assertEqual(kernel.status(run_id)["run"]["status"], "complete")

    def test_missing_adaptation_proposal_is_explicitly_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            kernel = kernel_at(Path(directory) / "runs")
            run_id = kernel.init("keep valid genomes", "verified output", 2, 4, 1)
            kernel.approve(run_id)
            for number in range(1, 5):
                state = kernel._agent_dir(run_id, f"agent-{number:03d}") / "state"
                (state / "mutation.json").write_text(json.dumps({"action": "mutate", "reason": "stale", "files": {"stale.py": "BAD = True\n"}}))
            kernel.run(run_id)
            unchanged = kernel._store(run_id).rows("SELECT payload_json FROM knowledge WHERE run_id=? AND kind='adaptation'", (run_id,))
            self.assertTrue(any(json.loads(row["payload_json"]).get("outcome") == "unchanged" for row in unchanged))
            self.assertFalse(any((path / "stale.py").exists() for path in (kernel._run_dir(run_id) / "agents").glob("*/repository")))

    def test_locked_runtime_rejects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            kernel = kernel_at(Path(directory) / "runs")
            run_id = kernel.init("protect experiment", "verified output", 1, 4, 10)
            kernel.approve(run_id)
            runtime = kernel._run_dir(run_id) / "runtime.json"
            runtime.write_text(runtime.read_text() + " ")
            with self.assertRaisesRegex(Exception, "locked runtime"):
                kernel.run(run_id)

    def test_locked_model_rejects_a_different_resume_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original_model = os.environ.get("KADATH_MODEL")
            original_upstream = os.environ.get("KADATH_UPSTREAM_MODEL")
            try:
                os.environ["KADATH_MODEL"] = "kadath-default"
                os.environ["KADATH_UPSTREAM_MODEL"] = "model-a"
                kernel = kernel_at(Path(directory) / "runs")
                run_id = kernel.init("protect model selection", "verified output", 1, 4, 10)
                kernel.approve(run_id)
                os.environ["KADATH_UPSTREAM_MODEL"] = "model-b"
                with self.assertRaisesRegex(Exception, "locked model configuration"):
                    kernel.run(run_id)
            finally:
                if original_model is None: os.environ.pop("KADATH_MODEL", None)
                else: os.environ["KADATH_MODEL"] = original_model
                if original_upstream is None: os.environ.pop("KADATH_UPSTREAM_MODEL", None)
                else: os.environ["KADATH_UPSTREAM_MODEL"] = original_upstream

    def test_epoch_deadline_starts_after_state_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            kernel = kernel_at(Path(directory) / "runs")
            run_id = kernel.init("preserve execution time", "verified output", 1, 4, 10)
            kernel.approve(run_id)
            original_snapshot = kernel._snapshot_epoch_state
            snapshot_finished: list[datetime] = []
            deadlines: list[datetime] = []

            def delayed_snapshot(selected_run: str, epoch: int) -> None:
                original_snapshot(selected_run, epoch)
                Event().wait(.1)
                snapshot_finished.append(datetime.now(UTC))

            class RecordingExecutor:
                def execute(self, request):
                    deadlines.append(request.deadline)
                    return ObjectiveResult(0, {"receipt": request.agent_id})

            kernel._snapshot_epoch_state = delayed_snapshot  # type: ignore[method-assign]
            kernel._executor = lambda _run_id: RecordingExecutor()  # type: ignore[method-assign]
            kernel.run(run_id)
            self.assertEqual(len(deadlines), 4)
            self.assertGreaterEqual(min(deadlines), snapshot_finished[0] + timedelta(seconds=9.95))

    def test_worker_broker_requires_token_and_rejects_late_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); repository = root / "repo"; state = root / "state"
            repository.mkdir(); state.mkdir()
            scope = ParentWorkerScope("agent-001", "secret", repository, state, "genome", "objective", datetime.now(UTC) - timedelta(seconds=1))
            broker = WorkerBroker([scope], "unused", ["unused"])
            broker.start(host="127.0.0.1")
            try:
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    urllib.request.urlopen(f"http://127.0.0.1:{broker.server.server_port}/workers/agent-001%2Fworker-1")
                self.assertEqual(denied.exception.code, 403)
                request = urllib.request.Request(f"http://127.0.0.1:{broker.server.server_port}/workers", data=json.dumps({"agent_id": "agent-001", "token": "secret", "task": {}}).encode(), headers={"Content-Type": "application/json"}, method="POST")
                with self.assertRaises(urllib.error.HTTPError) as late:
                    urllib.request.urlopen(request)
                self.assertEqual(late.exception.code, 400)
            finally:
                broker.stop()

    def test_broker_publishes_scoped_shared_knowledge_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); repository = root / "repo"; state = root / "state"
            repository.mkdir(); state.mkdir()
            store = Store(root / "state.sqlite")
            deadline = datetime.now(UTC) + timedelta(minutes=1)
            scopes = [
                ParentWorkerScope("agent-001", "token-a", repository, state, "genome-a", "objective", deadline),
                ParentWorkerScope("agent-002", "token-b", repository, state, "genome-b", "objective", deadline),
            ]
            broker = WorkerBroker(scopes, "unused", ["unused"], store=store, run_id="run", epoch=1)
            broker.start(host="127.0.0.1")
            url = f"http://127.0.0.1:{broker.server.server_port}"
            try:
                body = json.dumps({"agent_id": "agent-001", "token": "token-a", "summary": "Tested approach A.", "visibility": "shared"}).encode()
                request = urllib.request.Request(url + "/knowledge", data=body, headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(response.status, 201)
                query = urllib.parse.urlencode({"agent_id": "agent-002", "token": "token-b"})
                with urllib.request.urlopen(url + "/knowledge?" + query) as response:
                    records = json.loads(response.read())["records"]
                self.assertEqual(records[0]["agent_id"], "agent-001")
                self.assertEqual(records[0]["payload"]["summary"], "Tested approach A.")
            finally:
                broker.stop()

    def test_worker_identity_logs_activity_and_cannot_request_unselected_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); repository = root / "repo"; state = root / "state"; repository.mkdir(); state.mkdir()
            store = Store(root / "state.sqlite")
            scope = ParentWorkerScope("agent-001", "parent-token", repository, state, "genome", "objective", datetime.now(UTC) + timedelta(minutes=1), ("web_search",))
            broker = WorkerBroker([scope], "unused", ["unused"], store=store, run_id="run", epoch=1)
            broker._model_identities["worker-token"] = {"scope": scope, "worker_id": "agent-001/worker-1"}
            broker.start(host="127.0.0.1"); url = f"http://127.0.0.1:{broker.server.server_port}"
            try:
                body = json.dumps({"agent_id": "agent-001", "token": "worker-token", "summary": "Worker investigated the assigned subproblem.", "visibility": "private"}).encode()
                with urllib.request.urlopen(urllib.request.Request(url + "/knowledge", data=body, headers={"Content-Type": "application/json"}, method="POST")) as response: self.assertEqual(response.status, 201)
                record = store.one("SELECT kind,payload_json FROM knowledge WHERE run_id=?", ("run",))
                self.assertEqual(record["kind"], "worker_activity")
                self.assertEqual(json.loads(record["payload_json"])["worker_id"], "agent-001/worker-1")
                invalid = json.dumps({"agent_id": "agent-001", "token": "parent-token", "task": {}, "tools": ["browser"]}).encode()
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    urllib.request.urlopen(urllib.request.Request(url + "/workers", data=invalid, headers={"Content-Type": "application/json"}, method="POST"))
                self.assertEqual(denied.exception.code, 400)
            finally: broker.stop()

    def test_model_broker_scopes_identity_and_hides_master_key(self) -> None:
        received = {}
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_args): pass
            def do_POST(self):
                received["authorization"] = self.headers.get("Authorization")
                received["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                payload = json.dumps({"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 3}}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream); Thread(target=upstream.serve_forever, daemon=True).start()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); repository = root / "repo"; state = root / "state"; repository.mkdir(); state.mkdir()
            store = Store(root / "state.sqlite")
            scope = ParentWorkerScope("agent-001", "scoped-token", repository, state, "genome-abc", "objective", datetime.now(UTC) + timedelta(minutes=1))
            broker = WorkerBroker([scope], "unused", ["unused"], environment={"LITELLM_API_BASE": f"http://127.0.0.1:{upstream.server_port}", "LITELLM_API_KEY": "master-secret"}, store=store, run_id="run", epoch=2)
            broker.start(host="127.0.0.1")
            try:
                body = json.dumps({"model": "fixture", "messages": [{"role": "user", "content": "hello"}]}).encode()
                request = urllib.request.Request(f"http://127.0.0.1:{broker.server.server_port}/v1/chat/completions", data=body, headers={"Content-Type": "application/json", "Authorization": "Bearer scoped-token"}, method="POST")
                with urllib.request.urlopen(request) as response: self.assertEqual(response.status, 200)
                self.assertEqual(received["authorization"], "Bearer master-secret")
                self.assertEqual(received["body"]["metadata"]["kadath_agent_id"], "agent-001")
                self.assertEqual(received["body"]["metadata"]["kadath_genome"], "genome-abc")
                events = store.rows("SELECT * FROM events WHERE event_type='model_call'")
                self.assertEqual(len(events), 1)
                trace = Path(json.loads(events[0]["payload_json"])["trace"])
                self.assertTrue(trace.is_file())
                self.assertIn("hello", trace.read_text())
            finally:
                broker.stop(); upstream.shutdown(); upstream.server_close()

    def test_continue_from_selected_genome(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            kernel = kernel_at(Path(directory) / "runs")
            source = kernel.init("continue goal", "verified result", 1, 4, 10)
            kernel.approve(source)
            parent = kernel._store(source).one("SELECT agent_id,active_genome FROM agents WHERE run_id=? ORDER BY agent_id LIMIT 1", (source,))
            selected = parent["active_genome"]
            parent_state = kernel._agent_dir(source, parent["agent_id"]) / "state"
            (parent_state / "workspace").mkdir(); (parent_state / "workspace" / "memory.txt").write_text("remember me")
            (parent_state / "artifacts").mkdir(); (parent_state / "artifacts" / "evidence.txt").write_text("copy me")
            (parent_state / "progress.json").write_text(json.dumps({"iteration": 7}))
            (parent_state / "custom-framework-state").mkdir(); (parent_state / "custom-framework-state" / "learned.json").write_text('{"works": true}')
            kernel._store(source).add_knowledge(source, 0, parent["agent_id"], "activity", {"summary": "inherited note", "visibility": "private"}, datetime.now(UTC).isoformat())
            continued = kernel.continue_from(source, selected, epochs=2, population=4)
            self.assertEqual(kernel.status(continued)["run"]["status"], "awaiting_approval")
            kernel.approve(continued)
            agents = kernel._store(continued).rows("SELECT parent_agent_id FROM agents WHERE run_id=?", (continued,))
            self.assertTrue(all(agent["parent_agent_id"] for agent in agents))
            first_state = kernel._agent_dir(continued, "agent-001") / "state"
            self.assertEqual((first_state / "workspace" / "memory.txt").read_text(), "remember me")
            self.assertEqual((first_state / "artifacts" / "evidence.txt").read_text(), "copy me")
            self.assertEqual((first_state / "custom-framework-state" / "learned.json").read_text(), '{"works": true}')
            self.assertTrue(kernel._store(continued).rows("SELECT * FROM memory_links WHERE run_id=?", (continued,)))

    def test_export_survives_reset_and_can_continue_selected_genome(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            kernel = kernel_at(Path(directory) / "runs")
            source = kernel.init("portable lineage", "verified result", 1, 4, 10)
            kernel.approve(source); kernel.run(source)
            selected = kernel._store(source).one("SELECT genome_hash FROM scores WHERE run_id=? ORDER BY value DESC LIMIT 1", (source,))["genome_hash"]
            exported = kernel.export(source)
            self.assertTrue((exported / "genome-registry" / "records.json").is_file())
            kernel.reset(source)
            self.assertTrue(exported.is_dir())
            continued = kernel.continue_from_export(exported, selected, epochs=1, population=4)
            kernel.approve(continued)
            self.assertEqual(kernel.status(continued)["run"]["status"], "ready")
            self.assertTrue(kernel._store(continued).rows("SELECT * FROM memory_links WHERE run_id=?", (continued,)))
