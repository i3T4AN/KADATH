"""Generation-1 smolagents organism entry point.

The container injects the model gateway and MCP endpoint. It returns a
candidate artifact, never writes a score: only KADATH's external Grader can
turn independently verified evidence into fitness.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path


def run() -> str:
    from smolagents import CodeAgent, LiteLLMModel, MCPClient, tool
    from kadath_runtime import (capture_step, delete_workspace_file, fetch_web, flush_activity,
        list_repository, list_workspace, log_activity, propose_mutation, read_repository_file,
        read_shared_knowledge, rate_knowledge, read_workspace_file, run_workspace_command, save_artifact,
        search_web, spawn_worker, worker_result, write_workspace_file)

    model = LiteLLMModel(
        model_id=os.environ["KADATH_MODEL"],
        api_base=os.environ.get("LITELLM_API_BASE"),
        api_key=os.environ.get("LITELLM_API_KEY"),
    )
    if os.environ.get("KADATH_PHASE") == "adaptation":
        context_path = os.environ.get("KADATH_ADAPTATION_CONTEXT_PATH")
        context = json.loads(Path(context_path).read_text()) if context_path else {}
        task = "You are in the post-epoch adaptation window. Reflect on your own scored behavior and the directly supplied elite evidence. Inspect your framework source. Then call propose_mutation exactly once with action='mutate' and a bounded self-change, or action='unchanged' if keeping the tested genome is best. Do not change active repository files directly.\n" + json.dumps(context)
    else:
        progress_path = Path(os.environ.get("KADATH_STATE_DIR", ".")) / "progress.json"
        progress = json.loads(progress_path.read_text()) if progress_path.is_file() else {}
        context = {"shared_knowledge": read_shared_knowledge(limit=30), "previous_progress": progress}
        task = os.environ["KADATH_TASK"]
    prompt_path = Path(__file__).resolve().with_name("SYSTEM_PROMPT.md")
    genome_prompt = prompt_path.read_text() if prompt_path.is_file() else os.environ["KADATH_OBJECTIVE_PROMPT"]
    instructions = genome_prompt + "\nPopulation context:\n" + json.dumps(context)
    instructions += "\nMemory records marked own or inherited are your continuing personal memory. Inherited records came with your working lineage and should be treated as remembered experience; population records are observations published by others. Rate useful or misleading records so retrieval improves."
    instructions += "\nUse log_activity after a substantial line of work to record what you investigated, what you did, and the outcome. Do not log every search or low-level click. Save durable work in the workspace and use save_artifact for evidence the Grader should inspect."
    local_tools = [tool(list_repository), tool(read_repository_file), tool(list_workspace), tool(read_workspace_file), tool(write_workspace_file), tool(delete_workspace_file), tool(run_workspace_command), tool(save_artifact), tool(log_activity), tool(read_shared_knowledge), tool(rate_knowledge)]
    if os.environ.get("KADATH_PHASE") == "adaptation":
        local_tools.append(tool(propose_mutation))
    enabled = set(filter(None, os.environ.get("KADATH_ENABLED_OPTIONAL_TOOLS", "").split(",")))
    if os.environ.get("KADATH_PHASE") == "adaptation": enabled = set()
    if os.environ.get("KADATH_SEARXNG_URL") and ({"web_search", "web_fetch"} & enabled):
        if "web_search" in enabled: local_tools.append(tool(search_web))
        if "web_fetch" in enabled: local_tools.append(tool(fetch_web))
        instructions += "\nUse search_web for discovery and fetch_web for bounded public-page retrieval."
        if "browser" in enabled: instructions += " Use the browser tools for interactive sites."
    if os.environ.get("KADATH_WORKER_TASK"):
        instructions += "\nYou are a temporary worker. Put any return files under /worker. Return structured findings and evidence to your parent; do not attempt framework mutation or scoring."
    if "workers" in enabled and os.environ.get("KADATH_WORKER_BROKER_URL") and os.environ.get("KADATH_WORKERS_ENABLED") == "1":
        local_tools.extend([tool(spawn_worker), tool(worker_result)])
        instructions += (
            "\nYou may delegate bounded independent subproblems through "
            "kadath_runtime.spawn_worker(task, tools). The task must be a small JSON object and tools must explicitly select only the parent's optional capabilities needed by that worker. "
            "Use kadath_runtime.worker_result(worker_id) to collect the result, then "
            "use that result in your own work. Worker work is read-only and temporary; "
            "you remain responsible for the final answer."
        )
    endpoint = os.environ.get("KADATH_PLAYWRIGHT_MCP_URL")
    if endpoint and "browser" in enabled:
        with MCPClient({"url": endpoint, "transport": "streamable-http"}, structured_output=False) as tools:
            agent = CodeAgent(tools=[*local_tools, *tools], model=model, instructions=instructions, additional_authorized_imports=[], step_callbacks=[capture_step], max_steps=int(os.getenv("KADATH_MAX_STEPS", "12")))
            answer = str(agent.run(task)); flush_activity(answer); return answer
    agent = CodeAgent(tools=local_tools, model=model, instructions=instructions, additional_authorized_imports=[], step_callbacks=[capture_step], max_steps=int(os.getenv("KADATH_MAX_STEPS", "12")))
    answer = str(agent.run(task)); flush_activity(answer); return answer


if __name__ == "__main__":
    state = Path(os.environ.get("KADATH_STATE_DIR", "."))
    state.mkdir(parents=True, exist_ok=True)
    answers: list[str] = []
    limit = int(os.getenv("KADATH_MAX_ITERATIONS", "0"))
    deadline_text = os.environ.get("KADATH_DEADLINE")
    while True:
        answer = run(); answers.append(answer)
        (state / "progress.json").write_text(json.dumps({"iterations": len(answers), "last_summary": answer[-4000:]}, sort_keys=True))
        (state / "candidate-output.json").write_text(json.dumps({"answer": answer, "agent": os.getenv("KADATH_AGENT_ID"), "epoch": os.getenv("KADATH_EPOCH"), "iterations": len(answers)}))
        if os.environ.get("KADATH_RESULT_PATH"):
            Path(os.environ["KADATH_RESULT_PATH"]).write_text(json.dumps({"value": 0, "evidence": {"candidate_output": str(state / "candidate-output.json"), "iterations": len(answers), "self_reported_score_ignored": True}}))
        if os.environ.get("KADATH_PHASE") == "adaptation" or os.environ.get("KADATH_WORKER_TASK"):
            break
        if limit and len(answers) >= limit:
            break
        if (state / "finalize.requested").exists() or not deadline_text or (datetime.fromisoformat(deadline_text) - datetime.now(UTC)).total_seconds() < 30:
            break
    answer = answers[-1]
    (state / "candidate-output.json").write_text(json.dumps({"answer": answer, "agent": os.getenv("KADATH_AGENT_ID"), "epoch": os.getenv("KADATH_EPOCH")}))
    if os.environ.get("KADATH_WORKER_TASK"):
        Path("/worker/result.json").write_text(json.dumps({"answer": answer, "worker": os.getenv("KADATH_WORKER_ID")}))
    elif os.environ.get("KADATH_RESULT_PATH"):
        Path(os.environ["KADATH_RESULT_PATH"]).write_text(json.dumps({"value": 0, "evidence": {"candidate_output": str(state / "candidate-output.json"), "iterations": len(answers), "self_reported_score_ignored": True}}))
    print(answer)
