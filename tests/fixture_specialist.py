class FixtureSpecialistModel:
    """Explicit model double; production never falls back to this."""
    def complete_json(self, identity, system, payload):
        if identity == "architect":
            return {
                "objective_prompt": "Pursue the approved objective with verifiable evidence.",
                "measurement_method": payload["requested_criterion"] or "fixture verified score",
                "attribution_method": "attach run/epoch/agent identity",
                "evidence_requirements": ["value", "receipt"],
                "baseline": "zero verified output",
                "anti_fraud_checks": ["require attributed receipt", "reject late evidence"],
                "tie_breaker": "agent identity ascending",
                "benchmark": {"score_range": [0, 100], "scoring_rubric": [{"id": "verified-completion", "criterion": "verified completion", "weight": 100, "measurement": {"type": "levels", "levels": [{"id": "none", "description": "no verified completion", "fraction": 0}, {"id": "fixture", "description": "fixture evidence demonstrates partial completion", "fraction": .42}, {"id": "complete", "description": "complete verified result", "fraction": 1}]}}], "tie_break_rubric": [{"id": "evidence-count", "description": "number of attributable evidence records", "direction": "higher", "minimum": 0, "maximum": 10, "weight": 1}], "required_outputs": [{"description": "candidate output", "evidence_ref": "organism_evidence"}], "failure_conditions": [{"id": "execution-failure", "condition": "the frozen attempt records an execution failure"}], "grader_rules": ["ignore self-reported scores"]},
                "verification_plan": {"kernel_checks": ["identity_attribution", "artifact_integrity", "required_output_presence"], "external_connectors": [], "limitations": ["fixture has no external outcome connector"]},
                "tool_policy": {"enabled_capabilities": payload["environment_inventory"].get("available_optional_capabilities", [])},
                "special_agent_instructions": {"grader": "validate evidence", "tweaker": "analyze rankings", "birther": "make bounded distinct mutations"},
            }
        if identity.startswith("grader-checkpoint/"):
            return {"reviewed_fragment_ids": [entry["fragment_id"] for entry in payload["evidence_entries"]], "evidence_notes": [{"evidence_ref": entry["evidence_ref"], "note": "fixture evidence"} for entry in payload["evidence_entries"]], "contradictions": [], "fraud_signals": []}
        if identity.startswith("grader-reduce/"):
            return {"reviewed_summary_ids": [review["review_id"] for review in payload["review_summaries"]], "evidence_notes": [note for review in payload["review_summaries"] for note in review["evidence_notes"]], "contradictions": [item for review in payload["review_summaries"] for item in review["contradictions"]], "fraud_signals": [item for review in payload["review_summaries"] for item in review["fraud_signals"]]}
        if identity.startswith("grader/"):
            return {"reason": "frozen evidence satisfies fixture verifier", "criterion_facts": [{"criterion_id": "verified-completion", "level_id": "fixture", "evidence_refs": ["organism_evidence"]}], "tie_break_facts": [{"tie_break_id": "evidence-count", "measured_value": 1, "evidence_refs": ["organism_evidence"]}], "failure_assessments": [{"failure_id": "execution-failure", "triggered": False, "evidence_refs": []}], "anti_fraud_assessments": [{"check_id": "anti-fraud-1", "passed": True, "evidence_refs": ["organism_evidence"]}, {"check_id": "anti-fraud-2", "passed": True, "evidence_refs": ["organism_evidence"]}], "activity_summary": {"investigated": ["fixture objective"], "actions": ["produced evidence"], "outcomes": ["fixture result"]}}
        if identity.startswith("tweaker-batch/"):
            return {"covered_fragment_ids": [item["fragment_id"] for item in payload["dossier_fragments"]], "agent_findings": [{"agent_id": row["agent_id"], "finding": "fixture"} for row in payload["ranked_subset"]], "successful_characteristics": ["attributable evidence"], "failed_characteristics": [], "evidence_quality_notes": ["fixture evidence"]}
        if identity.startswith("tweaker-reduce/"):
            findings = [finding for summary in payload["analysis_summaries"] for finding in summary.get("agent_findings", [])]
            return {"covered_fragment_ids": [item for summary in payload["analysis_summaries"] for item in summary["covered_fragment_ids"]], "agent_findings": findings, "successful_characteristics": ["attributable evidence"], "failed_characteristics": [], "evidence_quality_notes": ["fixture evidence"]}
        if identity.startswith("tweaker/"):
            parents = [row["agent_id"] for row in payload["ranked"][:payload["elite_count"]]]
            assignments = {parent: 0 for parent in parents}
            for index in range(payload["birth_count"]): assignments[parents[index % len(parents)]] += 1
            assigned = {key: value for key, value in assignments.items() if value}
            return {"covered_fragment_ids": [item for summary in payload["analysis"]["batch_summaries"] for item in summary["covered_fragment_ids"]], "elite_characteristics": ["attributable evidence"], "successful_patterns": ["complete output"], "failed_patterns": [], "reproduction_context": "preserve verified elite characteristics in descendants", "parent_briefs": {key: "Preserve this parent's attributable evidence behavior." for key in assigned}, "reproduction_assignments": assigned}
        if identity == "birther/generation-1":
            return {"prompt_variations": [f"Variation {number}: pursue the objective with verifiable evidence." for number in range(1, payload["population"] + 1)]}
        if identity.startswith("birther/"):
            return {"prompt_suffix": f"Try a distinct evidence-first variation for {payload['child_id']}.", "mutation_brief": f"fixture bounded mutation {payload['child_id']}", "files": {"descendant_strategy.py": f"DESCENDANT = {payload['child_id']!r}\n"}, "delete_files": []}
        if identity.startswith("agent/"):
            return {"prompt_suffix": "Use the elite evidence pattern.", "files": {"adaptation.py": "ADAPTED = True\n"}}
        raise AssertionError(identity)
