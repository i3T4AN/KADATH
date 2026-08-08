from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

from .engine import Kernel, RunError


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kadath", description="KADATH evolutionary control kernel")
    p.add_argument("--root", type=Path, default=Path(".kadath/runs"), help="run storage directory")
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="propose an objective and create an unapproved run")
    start = sub.add_parser("start", help="propose, confirm, approve, and launch a run")
    for create in (init, start):
        create.add_argument("--goal", required=True); create.add_argument("--criterion", default="", help="optional requested criterion; Architect proposes the final measurement")
        create.add_argument("--epochs", type=int, default=3); create.add_argument("--population", type=int, default=100); create.add_argument("--epoch-seconds", type=int, default=1800)
        create.add_argument("--executor", choices=["simulated", "command", "docker"], default="docker", help="docker runs real model-driven organisms; simulated is test-only")
        create.add_argument("--command", dest="agent_command", nargs="+", default=["python", "/organism/organism.py"], help="organism command")
        create.add_argument("--image", default="kadath-organism:latest", help="organism image for --executor docker")
        create.add_argument("--network", default="kadath-agent", help="isolated Docker network providing broker/browser/search access")
        create.add_argument("--agent-env", action="append", default=[], metavar="KEY=VALUE", help="repeatable environment variable passed only to the organism runtime")
    start.add_argument("--dashboard", action="store_true", help="render live terminal progress after approval")
    continued_export = sub.add_parser("continue-export", help="create a new run from a verified KADATH export")
    continued_export.add_argument("export_dir", type=Path); continued_export.add_argument("--genome", required=True); continued_export.add_argument("--epochs", type=int, required=True)
    continued_export.add_argument("--population", type=int); continued_export.add_argument("--epoch-seconds", type=int)
    cleanup = sub.add_parser("cleanup", help="remove completed run data while preserving active runs and exports")
    cleanup_group = cleanup.add_mutually_exclusive_group(required=True)
    cleanup_group.add_argument("--older-than-days", type=int)
    cleanup_group.add_argument("--all", action="store_true")
    for name in ("approve", "run", "pause", "resume", "continue", "status", "export", "reset", "dashboard"):
        c = sub.add_parser(name); c.add_argument("run_id")
        if name == "run":
            c.add_argument("--epochs", type=int)
            c.add_argument("--dashboard", action="store_true", help="render live terminal progress while this command runs")
        if name == "dashboard":
            c.add_argument("--interval", type=float, default=1.0, help="refresh interval in seconds")
            c.add_argument("--watch", action="store_true", help="keep refreshing until the run is complete or paused")
        if name == "resume": c.add_argument("--epochs", type=int)
        if name == "continue":
            c.add_argument("--genome", required=True); c.add_argument("--epochs", type=int, required=True)
            c.add_argument("--population", type=int); c.add_argument("--epoch-seconds", type=int)
        if name == "reset": c.add_argument("--yes", action="store_true")
    return p


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv); kernel = Kernel(args.root)
    try:
        if args.command in {"init", "start"}:
            if args.executor in {"command", "docker"} and not args.agent_command:
                raise RunError("--executor command/docker requires --command")
            if args.executor == "docker" and not args.image:
                raise RunError("--executor docker requires --image")
            agent_env = {}
            reserved = {"KADATH_DATABASE_URL", "LITELLM_API_KEY", "LITELLM_MASTER_KEY", "LITELLM_API_BASE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "DOCKER_HOST", "KADATH_WORKER_TOKEN", "KADATH_WORKER_BROKER_URL"}
            for item in args.agent_env:
                if "=" not in item or not item.split("=", 1)[0]:
                    raise RunError("--agent-env must use KEY=VALUE")
                key, value = item.split("=", 1); agent_env[key] = value
                if key in reserved: raise RunError(f"--agent-env cannot override reserved control variable {key}")
            run_id = kernel.init(args.goal, args.criterion, args.epochs, args.population, args.epoch_seconds, args.executor, args.agent_command, agent_env=agent_env)
            if args.executor == "docker":
                runtime = kernel._run_dir(run_id) / "runtime.json"
                config = json.loads(runtime.read_text()); config["image"] = args.image; config["network"] = args.network; runtime.write_text(json.dumps(config, indent=2))
            architect = json.loads((kernel._run_dir(run_id) / "architect-output.json").read_text())
            inventory = json.loads((kernel._run_dir(run_id) / "environment-inventory.json").read_text())
            proposal = {
                "run_id": run_id, "status": "awaiting_approval",
                "proposed_grading_criterion": architect["measurement_method"],
                "measurement_method": architect["measurement_method"],
                "attribution_method": architect["attribution_method"],
                "score_range": architect["benchmark"]["score_range"],
                "scoring_rubric": architect["benchmark"]["scoring_rubric"],
                "required_outputs": architect["benchmark"]["required_outputs"],
                "failure_conditions": architect["benchmark"]["failure_conditions"],
                "anti_fraud_checks": architect["anti_fraud_checks"],
                "verification_plan": architect["verification_plan"],
                "tool_policy": architect["tool_policy"],
                "environment_inventory": inventory,
                "next": f"kadath approve {run_id}",
            }
            if args.command == "init":
                print(json.dumps(proposal, indent=2))
            else:
                interactive = sys.stdout.isatty()
                print(_render_approval(architect, inventory, styled=interactive))
                prompt = "\033[38;5;51mProceed? [Y/n]\033[0m " if interactive else "Proceed? [Y/n] "
                try: confirmed = input(prompt).strip().lower() in {"", "y", "yes"}
                except EOFError: confirmed = False
                if not confirmed:
                    print(f"Not approved. Run remains awaiting approval: {run_id}")
                    return
                kernel.approve(run_id)
                count = _run_with_dashboard(kernel, run_id, None) if args.dashboard else kernel.run(run_id)
                print(f"Completed {count} epoch(s): {run_id}")
        elif args.command == "approve":
            kernel.approve(args.run_id); print(f"Approved objective; generation one is ready: {args.run_id}")
        elif args.command == "run":
            if args.dashboard:
                count = _run_with_dashboard(kernel, args.run_id, args.epochs)
            else:
                count = kernel.run(args.run_id, args.epochs)
            print(f"Completed {count} epoch(s).")
        elif args.command == "pause": kernel.pause(args.run_id); print(f"Pause requested for {args.run_id}; it will stop after the current epoch.")
        elif args.command == "resume": print(f"Completed {kernel.run(args.run_id, args.epochs)} epoch(s).")
        elif args.command == "continue":
            new_id = kernel.continue_from(args.run_id, args.genome, args.epochs, args.population, args.epoch_seconds)
            print(json.dumps({"run_id": new_id, "status": "awaiting_approval", "next": f"kadath approve {new_id}"}, indent=2))
        elif args.command == "continue-export":
            new_id = kernel.continue_from_export(args.export_dir, args.genome, args.epochs, args.population, args.epoch_seconds)
            print(json.dumps({"run_id": new_id, "status": "awaiting_approval", "next": f"kadath approve {new_id}"}, indent=2))
        elif args.command == "status": print(json.dumps(kernel.status(args.run_id), indent=2, default=str))
        elif args.command == "dashboard": _dashboard(kernel, args.run_id, args.interval, args.watch)
        elif args.command == "export": print(kernel.export(args.run_id))
        elif args.command == "cleanup":
            result = kernel.cleanup_completed(None if args.all else args.older_than_days)
            print(json.dumps(result, indent=2))
        elif args.command == "reset":
            if not args.yes: raise RunError("reset requires --yes")
            kernel.reset(args.run_id); print(f"Removed run {args.run_id}")
    except RunError as exc:
        print(f"kadath: {exc}", file=sys.stderr); raise SystemExit(2)
    except KeyboardInterrupt:
        print("kadath: interrupted; the run can be resumed from its last durable boundary", file=sys.stderr); raise SystemExit(130)
    except Exception as exc:
        print(f"kadath: {type(exc).__name__}: {exc}", file=sys.stderr); raise SystemExit(1)


def _ansi(text: object, *codes: str) -> str:
    return "".join(f"\033[{code}m" for code in codes) + str(text) + "\033[0m"


def _progress_bar(completed: int, total: int, width: int = 32) -> str:
    filled = width if total <= 0 else max(0, min(width, round(width * completed / total)))
    return _ansi("█" * filled, "38;5;84") + _ansi("░" * (width - filled), "38;5;245")


def _activity_summary(row: dict) -> str:
    try:
        payload = json.loads(row.get("payload_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    summary = payload.get("summary") or payload.get("outcome") or row.get("kind", "activity")
    return " ".join(str(summary).split())[:140]


def _render_dashboard(status: dict, styled: bool = False) -> str:
    run = status["run"]
    population = ", ".join(f"{row['status']}: {row['count']}" for row in status["population"])
    if styled:
        attempts = {row["status"]: int(row["count"]) for row in status.get("attempts", [])}
        total = int(run["population_size"])
        done = attempts.get("completed", 0)
        failed = attempts.get("failed", 0)
        resolved = min(total, done + failed)
        active = max(0, total - resolved)
        operations = status.get("operations", {})
        epoch_label = f"EPOCH {run['current_epoch']} / {run['total_epochs']}"
        lines = [
            _ansi("KADATH  EVOLUTION DASHBOARD", "1", "38;5;51"),
            f"{_ansi('RUN', '38;5;245')}  {run['id']}    {_ansi('STATUS', '38;5;245')}  {_ansi(str(run['status']).upper(), '1', '38;5;84')}",
            f"{_ansi('GOAL', '38;5;245')} {run['goal']}",
            "",
            _ansi(epoch_label, "1", "38;5;213"),
            f"{_progress_bar(resolved, total)}  {resolved}/{total} agents resolved",
            f"{_ansi(done, '38;5;84')} complete   {_ansi(active, '38;5;51')} active   {_ansi(failed, '38;5;203')} failed",
            "",
            _ansi("LEADERBOARD", "1", "38;5;213"),
            f"{_ansi('#', '38;5;245'):>16}   {'AGENT':<16} {'SCORE':>9}  STATE",
        ]
        leaders = status.get("leaders", [])
        if leaders:
            for index, leader in enumerate(leaders[:10], 1):
                rank = leader.get("last_rank") or index
                rank_text = _ansi(rank, "38;5;220") if index == 1 else str(rank)
                state_color = "38;5;84" if leader["outcome"] == "success" else "38;5;203"
                lines.append(f"{rank_text:>16}   {leader['agent_id']:<16} {float(leader['value']):>9.3f}  {_ansi(leader['outcome'], state_color)}")
        else:
            lines.append(_ansi("Waiting for the first verified scores…", "38;5;245"))
        lines.extend([
            "",
            _ansi("OPERATIONS", "1", "38;5;213"),
            f"model calls  {operations.get('model_calls', 0):<8} workers  {operations.get('worker_records', 0):<8} crashes  {operations.get('runtime_crashes', 0)}",
        ])
        activity = status.get("recent_activity", [])
        lines.extend(["", _ansi("RECENT AGENT ACTIVITY", "1", "38;5;213")])
        if activity:
            for row in activity[:6]:
                identity = row.get("agent_id") or "kernel"
                lines.append(f"{_ansi(identity, '38;5;51')}  {_activity_summary(row)}")
        else:
            lines.append(_ansi("Waiting for agents to publish their first activity…", "38;5;245"))
        lines.extend(["", _ansi("Ctrl-C stops at a durable boundary; the run can be resumed.", "38;5;245")])
        return "\n".join(lines)
    lines = [
        f"KADATH  {run['id']}",
        f"state: {run['status']}    epoch: {run['current_epoch']}/{run['total_epochs']}    population: {population}",
    ]
    epochs = status.get("epochs", [])
    if epochs:
        latest = epochs[0]
        lines.append(f"latest epoch: {latest['epoch']} ({latest['status']})")
    attempts = status.get("attempts", [])
    if attempts:
        done = sum(row["count"] for row in attempts if row["status"] == "completed")
        failed = sum(row["count"] for row in attempts if row["status"] == "failed")
        total = sum(row["count"] for row in attempts)
        lines.append(f"agents: {done}/{total} complete" + (f", {failed} failed" if failed else ""))
    operations = status.get("operations", {})
    if operations:
        lines.append(f"model calls: {operations.get('model_calls', 0)}    workers: {operations.get('worker_records', 0)}    crashes: {operations.get('runtime_crashes', 0)}")
    leaders = status.get("leaders", [])
    if leaders:
        lines.append("leaders: " + "  ".join(f"{leader['agent_id']} {leader['value']:.3f}" for leader in leaders[:5]))
    activity = status.get("recent_activity", [])
    if activity:
        lines.append("recent activity:")
        lines.extend(f"  {row.get('agent_id') or 'kernel'}: {_activity_summary(row)}" for row in activity[:6])
    return "\n".join(lines)


def _render_approval(architect: dict, inventory: dict | None = None, styled: bool = False) -> str:
    benchmark = architect["benchmark"]
    title = _ansi("KADATH  ARCHITECT APPROVAL", "1", "38;5;51") if styled else "KADATH benchmark approval"
    section = (lambda value: _ansi(value, "1", "38;5;213")) if styled else (lambda value: value)
    label = (lambda value: _ansi(value, "1", "38;5;51")) if styled else (lambda value: value)
    lines = [
        title,
        f"{label('Objective:')} {architect['objective_prompt']}",
        f"{label('Metric:')} {architect['measurement_method']}",
        f"{label('Attribution:')} {architect['attribution_method']}",
        f"{label('Baseline:')} {architect['baseline']}",
        f"{label('Score range:')} {benchmark['score_range'][0]} to {benchmark['score_range'][1]}",
        section("Rubric:"),
    ]
    for item in benchmark["scoring_rubric"]:
        lines.append(f"  - {item['criterion']} ({item['weight']}%): " + json.dumps(item["measurement"], sort_keys=True))
    lines.append(f"{label('Required outputs:')} " + "; ".join(f"{item['description']} [{item['evidence_ref']}]" for item in benchmark["required_outputs"]))
    lines.append(f"{label('Evidence requirements:')} " + "; ".join(architect["evidence_requirements"]))
    lines.append(f"{label('Automatic failures:')} " + "; ".join(f"{item['id']}: {item['condition']}" for item in benchmark["failure_conditions"]))
    lines.append(f"{label('Tie breaks:')} " + "; ".join(json.dumps(item, sort_keys=True) for item in benchmark["tie_break_rubric"]))
    lines.append(f"{label('Tie-break policy:')} " + architect["tie_breaker"])
    lines.append(f"{label('Anti-fraud:')} " + "; ".join(architect["anti_fraud_checks"]))
    lines.append(f"{label('Grader rules:')} " + "; ".join(str(item) for item in benchmark["grader_rules"]))
    lines.append(f"{label('Enabled tools:')} " + (", ".join(architect["tool_policy"]["enabled_capabilities"]) or "none"))
    if inventory:
        lines.append(f"{label('Configured services:')} " + (", ".join(inventory.get("configured_services", [])) or "none"))
        lines.append(f"{label('Configured agent environment keys:')} " + (", ".join(inventory.get("agent_environment_keys", [])) or "none"))
    connectors = architect["verification_plan"].get("external_connectors", [])
    checks = architect["verification_plan"].get("kernel_checks", [])
    lines.append(f"{label('Kernel checks:')} " + (", ".join(checks) or "none"))
    lines.append(f"{label('Independent connectors:')} " + (", ".join(connectors) or "none configured"))
    limitations = architect["verification_plan"].get("limitations", [])
    if limitations: lines.append(f"{label('Measurement limitations:')} " + "; ".join(str(item) for item in limitations))
    lines.append(section("Specialist instructions:"))
    for specialist in ("grader", "tweaker", "birther"):
        lines.append(f"  - {specialist}: {architect['special_agent_instructions'][specialist]}")
    lines.append("Final scores are calculated by the kernel from verified criterion fractions; agent self-scores are ignored.")
    return "\n".join(lines)


def _dashboard(kernel: Kernel, run_id: str, interval: float, watch: bool) -> None:
    interval = max(.1, interval)
    interactive = sys.stdout.isatty()
    while True:
        output = _render_dashboard(kernel.status(run_id), styled=interactive)
        if interactive:
            print("\033[H\033[2J" + output, flush=True)
        else:
            print(output, flush=True)
        state = kernel.status(run_id)["run"]["status"]
        if not watch or state in {"complete", "failed", "paused", "awaiting_approval"}:
            return
        time.sleep(interval)


def _run_with_dashboard(kernel: Kernel, run_id: str, epochs: int | None) -> int:
    result: dict[str, object] = {}
    def run() -> None:
        try: result["count"] = kernel.run(run_id, epochs)
        except BaseException as exc: result["error"] = exc
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    interactive = sys.stdout.isatty()
    while thread.is_alive():
        output = _render_dashboard(kernel.status(run_id), styled=interactive)
        print(("\033[H\033[2J" if interactive else "") + output, flush=True)
        thread.join(.75)
    if "error" in result:
        raise result["error"]
    return int(result["count"])


if __name__ == "__main__":
    main()
