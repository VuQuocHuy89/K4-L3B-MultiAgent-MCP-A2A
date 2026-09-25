from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from .cases import CaseSet, load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


def _archive_artifacts(root: Path) -> Path:
    """Copy the current artifacts before a validated fresh run replaces them."""
    root = root.resolve()
    backup = root / "run-backups" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    targets = list((root / "outputs").glob("*.json"))
    targets.extend(root / relative for relative in (
        Path("traces/trace.jsonl"),
        Path("dist/submission.zip"),
    ))
    for target in targets:
        if target.exists() and (target.is_symlink() or not target.resolve().is_relative_to(root)):
            raise ValueError("artifact path is outside the workspace")
    for target in targets:
        if target.is_file():
            destination = backup / target.relative_to(root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, destination)
    return backup


def _promote_fresh_run(
    root: Path, staging: Path, case_set: CaseSet, contracts: Contracts
) -> None:
    """Publish a complete staged run, restoring the last run if promotion fails."""
    validate_artifacts(staging, case_set, contracts)
    backup = _archive_artifacts(root)
    output_root = root / "outputs"
    trace_root = root / "traces"
    staged_outputs = staging / "outputs"
    staged_trace = staging / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_root.mkdir(parents=True, exist_ok=True)
    expected_names = {path.name for path in staged_outputs.glob("*.json")}
    moved_outputs: list[str] = []
    moved_trace = False
    try:
        for stale in output_root.glob("*.json"):
            if stale.name not in expected_names:
                stale.unlink()
        for source in staged_outputs.glob("*.json"):
            source.replace(output_root / source.name)
            moved_outputs.append(source.name)
        staged_trace.replace(trace_root / "trace.jsonl")
        moved_trace = True
        validate_artifacts(root, case_set, contracts)
        package_submission(root, root / "dist" / "submission.zip")
    except Exception:
        for name in moved_outputs:
            source = output_root / name
            if source.exists():
                source.replace(staged_outputs / name)
        published_trace = trace_root / "trace.jsonl"
        if moved_trace and published_trace.is_file():
            published_trace.replace(staged_trace)
        for source in (backup / "outputs").glob("*.json"):
            shutil.copy2(source, output_root / source.name)
        previous_trace = backup / "traces" / "trace.jsonl"
        if previous_trace.is_file():
            shutil.copy2(previous_trace, trace_root / "trace.jsonl")
        previous_submission = backup / "dist" / "submission.zip"
        if previous_submission.is_file():
            shutil.copy2(previous_submission, root / "dist" / "submission.zip")
        raise
    print(
        f"Fresh run packaged at {root / 'dist' / 'submission.zip'}; "
        f"previous artifacts archived at {backup}",
        flush=True,
    )


async def _run(root: Path, concurrency: int, fresh: bool) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    run_root = root / "run-staging" / "active" if fresh else root
    output_root = run_root / "outputs"
    trace_path = run_root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    finalized: set[str] = set()
    retained_trace: list[str] = []
    if trace_path.exists():
        parsed_events = [
            json.loads(line)
            for line in trace_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        finalized = {
            event["case_id"]
            for event in parsed_events
            if event.get("event_type") == "case_finalized"
            and (output_root / f"{event['case_id']}.json").is_file()
        }
        retained_trace = [
            json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            for event in parsed_events
            if event.get("case_id") in finalized
        ]
        trace_path.write_text(
            "\n".join(retained_trace) + ("\n" if retained_trace else ""), encoding="utf-8"
        )
        for stale in output_root.glob("*.json"):
            if stale.stem not in finalized:
                stale.unlink()
        print(
            f"Resuming {'fresh ' if fresh else ''}run with "
            f"{len(finalized)}/{len(case_set.case_ids)} finalized cases",
            flush=True,
        )
    else:
        if fresh:
            print(f"Fresh run staged at {run_root}; current outputs remain available", flush=True)
        for stale in output_root.glob("*.json"):
            stale.unlink()
        trace_path.unlink(missing_ok=True)
    trace = TraceWriter(trace_path, contracts)

    completed = len(finalized)
    total = len(case_set.case_ids)
    queue: asyncio.Queue[str] = asyncio.Queue()
    for case_id in case_set.case_ids:
        if case_id not in finalized:
            queue.put_nowait(case_id)

    async def worker() -> None:
        nonlocal completed
        async with connect_gateway(
            settings.mcp_endpoint, settings.team_api_key, contracts
        ) as gateway:
            discovered_tools = await gateway.list_tools()
            if not discovered_tools:
                raise RuntimeError("MCP Gateway returned no tools")
            while not queue.empty():
                try:
                    case_id = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                case = case_set.cases[case_id]
                trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
                output = await solve_case(case, gateway, trace)
                contracts.validate_output(output, f"outputs/{case_id}.json")
                if output.get("case_id") != case_id:
                    raise ValueError(f"solver returned a mismatched case_id for {case_id}")
                target = output_root / f"{case_id}.json"
                temporary = target.with_suffix(".json.tmp")
                temporary.write_text(
                    json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                temporary.replace(target)
                trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
                completed += 1
                print(f"[{completed}/{total}] {case_id}", flush=True)

    remaining = total - completed
    if remaining == 0:
        if fresh:
            _promote_fresh_run(root, run_root, case_set, contracts)
        return
    workers = [asyncio.create_task(worker()) for _ in range(min(concurrency, remaining))]
    results = await asyncio.gather(*workers, return_exceptions=True)
    failures = [result for result in results if isinstance(result, BaseException)]
    if failures:
        details = "; ".join(_failure_details(failure) for failure in failures)
        raise RuntimeError(f"one or more case workers failed: {details}")
    if fresh:
        _promote_fresh_run(root, run_root, case_set, contracts)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1 or parsed > 32:
        raise argparse.ArgumentTypeError("must be between 1 and 32")
    return parsed


def _failure_details(error: BaseException) -> str:
    if isinstance(error, BaseExceptionGroup):
        return "; ".join(_failure_details(child) for child in error.exceptions)
    return f"{type(error).__name__}: {error}"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3B student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    run = commands.add_parser("run", help="run the implemented workflow for all cases")
    run.add_argument(
        "--concurrency",
        type=_positive_int,
        default=4,
        help="number of cases processed concurrently (default: 4, maximum: 32)",
    )
    run.add_argument(
        "--fresh",
        action="store_true",
        help="stage a new evidence run and keep current artifacts until it validates",
    )
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / {len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            asyncio.run(_run(root, args.concurrency, args.fresh))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError, ExceptionGroup) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
