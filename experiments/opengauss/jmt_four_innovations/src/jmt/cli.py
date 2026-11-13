"""Small unified entry point. No live apply/restart/reset command is shipped."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys
from jmt import __version__


def write_json(path, value):
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            stream.write(text)
    else:
        print(text, end="")


def db_arguments(parser):
    parser.add_argument("--gsql", default="gsql")
    parser.add_argument("--host", default="/tmp")
    parser.add_argument("--port", default=5432, type=int)
    parser.add_argument("--user")
    parser.add_argument("--run-as-user", help="Optional local peer-auth OS user, no default/root assumption")
    parser.add_argument("--timeout", default=45, type=float)


def client_from(args):
    from jmt.common.db import Gsql
    return Gsql(args.gsql, args.host, args.port, args.user, args.run_as_user, args.timeout)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version="jmt-opengauss " + __version__)
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="Dependencies/build/profile; read-only")
    db_arguments(doctor)
    doctor.add_argument("--database", help="If absent, offline dependency check only")
    doctor.add_argument("--out")
    forecast = commands.add_parser("forecast", help="Offline SQL+blind plan+catalog; no target execution")
    forecast.add_argument("--sql", type=Path, required=True)
    forecast.add_argument("--plan", type=Path, required=True)
    forecast.add_argument("--catalog", type=Path, required=True)
    forecast.add_argument("--wm", type=float, required=True)
    forecast.add_argument("--out")
    validate = commands.add_parser("validate", help="Explicit read-only prospective ANALYZE suite")
    db_arguments(validate)
    validate.add_argument("--suite", type=Path, required=True)
    validate.add_argument("--out", type=Path, required=True, help="New directory, never reused")
    validate.add_argument("--group-synopsis", type=Path,
                          help="Optional explicit OFFLINE base-group statistics; never collected implicitly")
    demo = commands.add_parser("demo", help="Offline four-innovation executable examples")
    demo.add_argument("--fixtures", type=Path, required=True)
    demo.add_argument("--out")
    evidence = commands.add_parser("verify-evidence", help="Verify recursive evidence SHA; no DB")
    evidence.add_argument("artifact", type=Path)
    recommend = commands.add_parser("rank", help="Diagnostic candidate ranking, never applies GUCs")
    recommend.add_argument("--candidates", type=Path, required=True)
    recommend.add_argument("--target-tps", type=float, required=True)
    recommend.add_argument("--out")
    args = parser.parse_args(argv)
    if args.command == "doctor":
        report = {"python": sys.version.split()[0], "release": __version__}
        for module in ("numpy", "sqlglot", "lz4"):
            report[module] = bool(importlib.util.find_spec(module))
        if args.database:
            from jmt.innovation_2_spill.catalog import collect_catalog
            catalog = collect_catalog(client_from(args), args.database)
            report.update({
                "database": catalog["database"], "database_build": catalog["version"],
                "sql_compatibility": catalog["sql_compatibility"],
                "session_profile": catalog["settings"],
                "public_relations": len(catalog["relations"]),
                "profile_qualification": "Session-only row executor, DOP=1; not a global configuration change.",
            })
        write_json(args.out, report)
    elif args.command == "forecast":
        from jmt.innovation_2_spill.forecast import ForecastSession
        catalog = json.loads(args.catalog.read_text())
        document = json.loads(args.plan.read_text())
        result = ForecastSession(args.sql.read_text(), catalog).forecast(document, args.wm)
        write_json(args.out, result)
    elif args.command == "validate":
        from jmt.validation import run_validation
        report = run_validation(client_from(args), args.suite, args.out, args.group_synopsis)
        print(json.dumps({"all": report["all"], "by_role": report["by_role"]},
                         ensure_ascii=False, indent=2))
    elif args.command == "verify-evidence":
        from jmt.common.provenance import validate_json_evidence_tree
        count = validate_json_evidence_tree(args.artifact.resolve())
        write_json(None, {"evidence_checked": count,
                          "qualification": "Provenance only, not workload accuracy."})
    elif args.command == "rank":
        from jmt.innovation_3_search.objective import select_for_target_tps
        candidates = json.loads(args.candidates.read_text())
        selected, diagnostic = select_for_target_tps(candidates, args.target_tps)
        write_json(args.out, {
            "selected": selected, "selection": diagnostic,
            "automatic_recommendation_qualified": False,
            "qualification": "Diagnostic ranking only; current AP/runtime/machine calibration must be independently qualified.",
        })
    elif args.command == "demo":
        from jmt.demo import run
        write_json(args.out, run(args.fixtures))


if __name__ == "__main__":
    main()
