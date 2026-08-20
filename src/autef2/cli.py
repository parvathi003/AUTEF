"""Command line entry points.

    autef2 check   <project>    ingest + run the suite, no model calls
    autef2 run     <project>    generate, repair, improve coverage and mutation
                                score (--generate / --coverage / --mutation,
                                or --all-phases for v1's full flow)
    autef2 compare <project>    v1 versus v2 on one project, test by test
    autef2 bench   <manifest>   both arms over a sample, with metrics
    autef2 inject  <project>    seed known faults into a copy

``check`` exists because the first question about any new project is whether it
is in scope at all, and that question costs nothing to answer.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from .config import AutefConfig, configure_logging
from .ingest import IngestError, ingest

logger = logging.getLogger("autef2")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level)

    if not getattr(args, "command", None):
        parser.print_help()
        return 2

    config = AutefConfig.from_env(
        **{
            k: v
            for k, v in {
                "workspace": Path(args.workspace) if args.workspace else None,
                "model": args.model,
                "max_attempts": args.max_attempts,
                "use_venv": args.venv,
                "use_signature_cache": not args.no_cache,
            }.items()
            if v is not None
        }
    )

    handlers = {
        "check": _cmd_check,
        "run": _cmd_run,
        "compare": _cmd_compare,
        "bench": _cmd_bench,
        "inject": _cmd_inject,
        "web": _cmd_web,
    }
    try:
        return handlers[args.command](args, config)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def _cmd_check(args, config: AutefConfig) -> int:
    from .pipeline import run_project, summarise

    report = run_project(
        args.project, config, name_hint=args.name, dry_run=True
    )
    print(summarise(report))

    if report.layout:
        print("\nDetected layout")
        print(f"  root         : {report.layout.root}")
        print(f"  style        : {report.layout.layout_style}")
        print(f"  import roots : {report.layout.import_roots}")
        print(f"  test roots   : {report.layout.test_roots}")
        print(f"  dependencies : {len(report.layout.declared_dependencies)} declared")
        for note in report.layout.notes:
            print(f"  note         : {note}")

    if report.before:
        # Collection errors are repairable failures too -- a test file that will
        # not import is exactly what the import strategies exist for -- so they
        # belong in this list rather than only in the counts above.
        failures = list(report.before.failures) + list(report.before.collection_errors)
        if failures:
            print(f"\nFailing tests ({len(failures)}):")
            for failure in failures[:20]:
                label = " [collection]" if failure.phase == "collect" else ""
                print(f"  {failure.nodeid}{label}")
                print(
                    f"      {failure.exception_type}: "
                    f"{failure.exception_message[:120]}"
                )
                print(f"      test file : {failure.test_file}")
                print(f"      source    : {failure.source_files or '(none resolved)'}")
            if len(failures) > 20:
                print(f"  ... and {len(failures) - 20} more")

    _maybe_write_json(args, report.to_dict())
    return 0 if report.error is None else 1


def _cmd_run(args, config: AutefConfig) -> int:
    from .enhance import EnhanceOptions
    from .pipeline import run_project, summarise

    report = run_project(
        args.project,
        config,
        name_hint=args.name,
        max_tests=args.max_tests,
        enhance=EnhanceOptions(
            generate=args.generate or args.all_phases,
            coverage=args.coverage or args.all_phases,
            mutation=args.mutation or args.all_phases,
            max_modules=args.max_modules,
            max_coverage_files=args.max_coverage_files,
            max_mutants=args.max_mutants,
            max_survivors=args.max_survivors,
        ),
    )
    print(summarise(report))

    if report.generated or report.coverage_generated or report.mutation_generated:
        print("\nTests written:")
        for label, records in (
            ("generation", report.generated),
            ("coverage", report.coverage_generated),
            ("mutation", report.mutation_generated),
        ):
            for record in records:
                status = "kept" if record.accepted else "rejected"
                name = Path(record.test_file).name if record.test_file else "-"
                print(f"  [{status:8}] {label:10} {name}  ({record.module_import})")
                if record.error:
                    print(f"              {record.error}")

    if report.mutation_before and report.mutation_before.measured:
        survivors = (report.mutation_after or report.mutation_before).survivors()
        if survivors:
            print(f"\nMutants still surviving ({len(survivors)}):")
            for mutant in survivors[:15]:
                print(
                    f"  {Path(mutant.file).name}:{mutant.lineno} {mutant.operator}"
                )
                print(f"      - {mutant.original}")
                print(f"      + {mutant.mutated}")

    if report.records:
        print("\nPer test:")
        for record in report.records:
            status = (
                "FIXED" if record.fixed
                else "SKIPPED" if record.skipped_reason
                else "NOT FIXED"
            )
            cause = record.diagnosis.root_cause.value if record.diagnosis else "?"
            print(f"  [{status:9}] {record.nodeid}")
            print(
                f"              cause={cause} attempts={record.attempts_used} "
                f"strategies={[a.strategy_id for a in record.attempts]}"
            )
            if record.skipped_reason:
                print(f"              {record.skipped_reason}")

    _maybe_write_json(args, report.to_dict())
    return 0 if report.error is None else 1


def _cmd_compare(args, config: AutefConfig) -> int:
    from .eval.compare import compare_project, render_comparison

    result = compare_project(
        args.project,
        config,
        inject=args.inject,
        max_tests=args.max_tests,
        seed=args.seed,
        output_dir=Path(args.output) if args.output else None,
        fault_kinds=tuple(args.fault_kinds or ()),
    )
    print(render_comparison(result))
    if result.output_dir:
        print(f"Artefacts written to {result.output_dir}")

    _maybe_write_json(args, result.to_dict())
    return 0 if result.error is None else 1


def _cmd_bench(args, config: AutefConfig) -> int:
    from .eval.benchmark import load_specs, run_benchmark, stratified_sample

    specs = load_specs(args.manifest)
    specs = stratified_sample(specs, args.per_stratum, seed=args.seed)
    print(f"Running {len(specs)} project(s) x {len(args.arms)} arm(s)\n")

    result = run_benchmark(
        specs,
        config,
        arms=args.arms,
        seed=args.seed,
        output_dir=Path(args.output) if args.output else None,
        use_cache=args.bench_cache,
    )
    print(result.markdown())
    print(f"\nArtefacts written to {result.output_dir}")
    return 0


def _cmd_inject(args, config: AutefConfig) -> int:
    from .eval.faults import FaultInjector

    try:
        layout = ingest(args.project, config, name_hint=args.name)
    except IngestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    from .eval.faults import ALL_KINDS

    injector = FaultInjector(
        layout, seed=args.seed, kinds=tuple(args.fault_kinds or ALL_KINDS)
    )
    records = injector.inject(args.count)
    print(f"Seeded {len(records)} fault(s) into {layout.root}\n")
    if injector.shortfall:
        print(f"  note: {injector.shortfall}\n")
    for record in records:
        print(f"  {record.kind:20} {Path(record.file).name}:{record.lineno}")
        print(f"    - {record.original_line}")
        print(f"    + {record.mutated_line}")

    _maybe_write_json(args, {"root": layout.root, "faults": [r.to_dict() for r in records]})
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def _cmd_web(args, config: AutefConfig) -> int:
    from .web import serve

    serve(port=args.port, host=args.host)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autef2",
        description="Agentic unit-test repair with diagnosed, verified fixes.",
    )
    parser.add_argument("--workspace", help="Where projects are unpacked and worked on")
    parser.add_argument("--model", help="OpenAI model id (default gpt-4o-mini)")
    parser.add_argument(
        "--max-attempts", type=int, help="Escalation rungs per failing test (default 3)"
    )
    parser.add_argument(
        "--venv",
        action="store_true",
        default=None,
        help="Build an isolated virtualenv per project and install its dependencies",
    )
    parser.add_argument(
        "--no-cache", action="store_true", help="Disable the failure-signature cache"
    )
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--json", dest="json_out", help="Also write the report as JSON")

    subparsers = parser.add_subparsers(dest="command")

    check = subparsers.add_parser(
        "check", help="Ingest and run the suite without calling the model"
    )
    _add_project_args(check)

    run = subparsers.add_parser(
        "run", help="Generate, repair, and improve a project's tests"
    )
    _add_project_args(run)
    run.add_argument(
        "--max-tests", type=int, help="Only attempt the first N failing tests"
    )
    run.add_argument(
        "--generate", action="store_true",
        help="Write tests for modules that have none. Happens automatically when "
             "the project ships no tests at all.",
    )
    run.add_argument(
        "--coverage", action="store_true",
        help="Measure line and branch coverage, then write tests for the gaps",
    )
    run.add_argument(
        "--mutation", action="store_true",
        help="Score the suite against mutated source, then write tests for the "
             "mutants that survived (each verified to actually kill its mutant)",
    )
    run.add_argument(
        "--all-phases", action="store_true",
        help="Shorthand for --generate --coverage --mutation",
    )
    run.add_argument("--max-modules", type=int, default=5,
                     help="Cap modules generated for (default 5)")
    run.add_argument("--max-coverage-files", type=int, default=3,
                     help="Cap files given coverage tests (default 3)")
    run.add_argument("--max-mutants", type=int, default=20,
                     help="Cap mutants scored (default 20)")
    run.add_argument("--max-survivors", type=int, default=5,
                     help="Cap surviving mutants given tests (default 5)")

    compare = subparsers.add_parser(
        "compare",
        help="Run v1 and v2 over one project and report the difference test by test",
    )
    _add_project_args(compare)
    compare.add_argument(
        "-n", "--inject", type=int, default=0,
        help="Seed this many faults into currently-passing tests first. Real "
             "repositories mostly pass, so without this there is usually "
             "nothing to compare.",
    )
    compare.add_argument("--max-tests", type=int, help="Cap failing tests attempted")
    compare.add_argument("--seed", type=int, default=1337)
    compare.add_argument("--output", help="Directory for comparison artefacts")
    _add_fault_kinds(compare)

    bench = subparsers.add_parser(
        "bench", help="Compare the baseline and autef2 arms over a project sample"
    )
    bench.add_argument("manifest", help="JSON or YAML manifest of projects")
    bench.add_argument(
        "--arms", nargs="+", default=["baseline", "autef2"], choices=["baseline", "autef2"]
    )
    bench.add_argument("--per-stratum", type=int, help="Sample N projects per stratum")
    bench.add_argument("--seed", type=int, default=1337)
    bench.add_argument("--output", help="Directory for benchmark artefacts")
    bench.add_argument(
        "--bench-cache",
        action="store_true",
        help="Leave the signature cache on during the benchmark (off by default: "
        "it makes a project's result depend on which projects ran before it)",
    )

    web = subparsers.add_parser(
        "web", help="Serve the web front end for the nine-stage pipeline"
    )
    web.add_argument("--port", type=int, default=8000)
    web.add_argument(
        "--host", default="127.0.0.1",
        help="Bind address. Localhost by default: the sign-in is a "
             "demonstration gate, not a security boundary.",
    )

    inject = subparsers.add_parser(
        "inject", help="Seed known faults into a project's tests"
    )
    _add_project_args(inject)
    inject.add_argument("-n", "--count", type=int, default=10)
    inject.add_argument("--seed", type=int, default=1337)
    _add_fault_kinds(inject)

    return parser


def _add_fault_kinds(parser: argparse.ArgumentParser) -> None:
    """``--fault-kinds``: which failures to seed.

    The default mix is whatever the project offers sites for, and that is not a
    protocol. It matters most for ``compare``: the baseline cannot attempt a
    file-scoped failure at all, so a run heavy in ``broken_import`` measures
    reach rather than repair quality.
    """
    from .eval.faults import ALL_KINDS

    parser.add_argument(
        "--fault-kinds", nargs="+", choices=list(ALL_KINDS), metavar="KIND",
        help="Restrict seeding to these kinds (default: all). Choices: "
             + ", ".join(ALL_KINDS),
    )


def _add_project_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("project", help="Path to a project directory or .zip archive")
    parser.add_argument("--name", help="Override the detected project name")


def _maybe_write_json(args, payload: dict) -> None:
    target = getattr(args, "json_out", None)
    if not target:
        return
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nJSON written to {path}")


if __name__ == "__main__":
    raise SystemExit(main())
