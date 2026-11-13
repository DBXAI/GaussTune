"""Prospective read-only openGauss validation, frozen predictions before labels."""
import hashlib
import json
from pathlib import Path
import random
import time
import zipfile
from datetime import datetime, timezone

from jmt.common.db import extract_json
from jmt.innovation_2_spill.catalog import collect_catalog
from jmt.innovation_2_spill.forecast import ForecastSession
from jmt.innovation_2_spill.labels import actual_label, score, shape


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def now():
    return datetime.now(timezone.utc).isoformat()


def source_hashes():
    root = Path(__file__).resolve().parent
    return {str(p.relative_to(root)): digest(p.read_bytes()) for p in sorted(root.rglob("*.py"))}


def run_validation(client, suite_path, output, group_synopsis_path=None):
    suite_path, output = Path(suite_path).resolve(), Path(output).resolve()
    suite_bytes = suite_path.read_bytes()
    suite = json.loads(suite_bytes)
    if suite.get("schema") != "jmt.validation-suite/v1":
        raise ValueError("unsupported suite schema")
    if not suite.get("cases") or len(suite["cases"]) > 64:
        raise ValueError("suite must have 1..64 query cases")
    keys = [case["key"] for case in suite["cases"]]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate query keys")
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    code = source_hashes()
    with zipfile.ZipFile(output / "source_snapshot.zip", "x", compression=zipfile.ZIP_DEFLATED) as archive:
        root = Path(__file__).resolve().parent
        for relative, expected in code.items():
            data = (root / relative).read_bytes()
            if digest(data) != expected:
                raise RuntimeError("source changed while taking the pre-execution snapshot")
            archive.writestr(relative, data)
    catalogs = {}
    for case in suite["cases"]:
        db = case["database"]
        if db not in catalogs:
            catalogs[db] = collect_catalog(client, db)
            save(output / ("catalog_%s.json" % digest(db.encode())[:10]), catalogs[db])
    group_provider, group_bundle, group_mark = None, None, None
    if group_synopsis_path is not None:
        from jmt.innovation_2_spill.group_synopsis import (
            GroupFrequencySynopsis, data_watermark_sql, watermark)
        group_bundle = json.loads(Path(group_synopsis_path).read_text())
        group_db = group_bundle["watermark"]["value"]["database"]
        if group_db not in catalogs:
            raise ValueError("offline group synopsis database is not in this suite")
        profiles = [e["profile"] for e in group_bundle["entries"]]
        group_mark = watermark(client.json(group_db, data_watermark_sql(profiles)))
        group_provider = GroupFrequencySynopsis(group_bundle, group_mark)
        save(output / "preexecution_group_synopsis.json", group_bundle)
        save(output / "group_watermark_precheck.json", group_mark)
    points = []
    for index, case in enumerate(suite["cases"]):
        sql = (suite_path.parent / case["sql_file"]).resolve().read_text()
        session = ForecastSession(
            sql, catalogs[case["database"]],
            group_synopsis=group_provider if group_provider and case["database"] == group_db else None)
        wms = case["work_mem_mib"]
        if not wms or len(wms) > 8:
            raise ValueError("each query requires 1..8 WM points")
        for wm in wms:
            directory = output / ("%03d_wm%d" % (index, wm))
            directory.mkdir()
            blind_raw = client.explain(case["database"], sql, wm, analyze=False)
            save(directory / "blind_raw.json", blind_raw)
            point = {"key": case["key"], "database": case["database"],
                     "role": case.get("role", "coverage"), "work_mem_mib": wm,
                     "sql": sql, "sql_sha256": digest(sql.encode()),
                     "directory": directory.name, "blind_exit_code": blind_raw["exit_code"]}
            if not blind_raw["exit_code"]:
                blind = extract_json(blind_raw["stdout"])
                prediction = session.forecast(blind, wm)
                save(directory / "blind.json", blind)
                save(directory / "prediction.json", prediction)
                point.update({"prediction": prediction,
                              "blind_shape": shape(blind[0]["Plan"]),
                              "blind_sha256": digest((directory / "blind.json").read_bytes()),
                              "prediction_sha256": digest((directory / "prediction.json").read_bytes())})
            points.append(point)
    freeze = {
        "schema": "jmt.prospective-freeze/v1", "frozen_at": now(),
        "suite_sha256": digest(suite_bytes), "source_sha256": code,
        "catalogs": {db: c["catalog_snapshot_fingerprint"] for db, c in catalogs.items()},
        "offline_group_synopsis_sha256": (
            digest((output / "preexecution_group_synopsis.json").read_bytes()) if group_bundle else None),
        "points": points,
        "profile": {"enable_vector_engine": "off", "query_dop": 1,
                    "read_only": True, "timeout_seconds": client.timeout_s},
        "qualification": "All blind forecasts frozen before any target ANALYZE. Fresh rerun of known queries is NOT an unseen-query holdout.",
    }
    save(output / "freeze.json", freeze)
    print("Frozen %d points before target execution; %s" % (len(points), output), flush=True)
    order = list(range(len(points)))
    random.Random(20261110).shuffle(order)
    for i in order:
        if source_hashes() != code:
            raise RuntimeError("predictor source changed after freeze")
        point = points[i]
        if point["blind_exit_code"]:
            point["valid_label"] = False
            continue
        directory = output / point["directory"]
        if digest((directory / "prediction.json").read_bytes()) != point["prediction_sha256"]:
            raise RuntimeError("frozen forecast changed")
        began = now()
        result = client.explain(
            point["database"], point["sql"], point["work_mem_mib"], analyze=True,
        )
        save(directory / "actual_raw.json", {"started_at": began, "finished_at": now(), **result})
        point["valid_label"] = False
        point["execution_exit_code"] = result["exit_code"]
        point["execution_wall_s"] = result["wall_s"]
        if not result["exit_code"]:
            try:
                actual = extract_json(result["stdout"])
                save(directory / "actual.json", actual)
                if shape(actual[0]["Plan"]) != point["blind_shape"]:
                    raise ValueError("runtime plan shape differs from frozen blind plan")
                point["label"] = actual_label(actual)
                point["actual_sha256"] = digest((directory / "actual.json").read_bytes())
                point["valid_label"] = True
            except ValueError as error:
                point["label_error"] = str(error)
        save(directory / "result.json", point)
        print("%s WM=%d %s %.2fs" % (
            point["key"], point["work_mem_mib"],
            "labeled" if point["valid_label"] else "incomplete", result["wall_s"],
        ), flush=True)
    after = {db: collect_catalog(client, db) for db in catalogs}
    unchanged = {db: after[db]["catalog_snapshot_fingerprint"]
                 == catalogs[db]["catalog_snapshot_fingerprint"] for db in catalogs}
    save(output / "catalog_postcheck.json", after)
    group_unchanged = None
    if group_bundle:
        group_after = watermark(client.json(group_db, data_watermark_sql(profiles)))
        save(output / "group_watermark_postcheck.json", group_after)
        group_unchanged = group_after["sha256"] == group_mark["sha256"]
    summary = {
        "schema": "jmt.validation-results/v1", "finished_at": now(),
        "source_sha256": code, "freeze_sha256": digest((output / "freeze.json").read_bytes()),
        "all": score(points),
        "by_role": {role: score([p for p in points if p["role"] == role])
                    for role in sorted({p["role"] for p in points})},
        "elapsed_s": time.monotonic() - started, "catalog_fingerprint_unchanged": unchanged,
        "group_data_watermark_unchanged": group_unchanged,
        "points": points, "automatic_recommendation_qualified": False,
        "qualification": freeze["qualification"] + " Logical temporary buffers, not physical IOPS; session row executor only.",
    }
    save(output / "summary.json", summary)
    return summary
