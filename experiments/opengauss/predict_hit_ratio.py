#!/usr/bin/env python3
import argparse
import json
import math
from pathlib import Path
from typing import Optional


def _clip01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def _parse_size_mb(s: str) -> Optional[float]:
    if not s:
        return None
    v = str(s).strip().upper()
    try:
        if v.endswith("GB"):
            return float(v[:-2].strip()) * 1024.0
        if v.endswith("MB"):
            return float(v[:-2].strip())
        if v.endswith("KB"):
            return float(v[:-2].strip()) / 1024.0
        return float(v) / (1024.0 * 1024.0)
    except Exception:
        return None


def model_hit_ratio(cache_mb: float, w_mb: float, p: float) -> float:
    if cache_mb <= 0 or w_mb <= 0:
        return 0.0
    return _clip01(1.0 - math.exp(-((cache_mb / w_mb) ** p)))

def calibrate_w_from_one_point(x_mb: float, y_hit: float, p: float) -> float:
    y = _clip01(float(y_hit))
    x = float(x_mb)
    if x <= 0:
        raise ValueError("x_mb must be > 0")
    if y <= 0:
        return float("inf")
    if y >= 1.0:
        return 0.0
    a = -math.log(1.0 - y)
    return x / (a ** (1.0 / float(p)))


def fit_exp_saturation(xs_mb, ys_hit):
    xs = [float(x) for x in xs_mb]
    ys = [_clip01(float(y)) for y in ys_hit]
    if len(xs) < 2:
        raise ValueError("need at least 2 points to fit")

    best = None
    for p in [round(0.2 + 0.05 * i, 2) for i in range(1, 61)]:
        for w_mb in [128 * (2**k) for k in range(0, 13)]:
            sse = 0.0
            for x, y in zip(xs, ys):
                yhat = model_hit_ratio(x, w_mb, p)
                sse += (yhat - y) ** 2
            if best is None or sse < best["sse"]:
                best = {"p": float(p), "w_mb": float(w_mb), "sse": float(sse)}

    p0 = best["p"]
    w0 = best["w_mb"]
    p_min = max(0.1, p0 - 0.2)
    p_max = min(3.0, p0 + 0.2)
    w_min = max(32.0, w0 * 0.5)
    w_max = w0 * 1.5

    for p in [p_min + (p_max - p_min) * i / 200.0 for i in range(201)]:
        for i in range(401):
            w_mb = w_min + (w_max - w_min) * i / 400.0
            sse = 0.0
            for x, y in zip(xs, ys):
                yhat = model_hit_ratio(x, w_mb, p)
                sse += (yhat - y) ** 2
            if sse < best["sse"]:
                best = {"p": float(p), "w_mb": float(w_mb), "sse": float(sse)}

    return best


def load_runs(results_dir: Path):
    runs = []
    for p in sorted(results_dir.glob("results_*/run.json")):
        try:
            runs.append(json.loads(p.read_text()))
        except Exception:
            continue
    for p in sorted(results_dir.glob("*sweep_*/summary.json")):
        try:
            data = json.loads(p.read_text())
            if isinstance(data, list):
                runs.extend([x for x in data if isinstance(x, dict)])
        except Exception:
            continue
    return runs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-dir", default="/root/Huawei2")
    ap.add_argument("--fit", choices=["os_cache", "shared_buffers"], default="os_cache")
    ap.add_argument("--predict-x-mb", type=float, default=None)
    ap.add_argument("--predict-list-mb", default=None)
    ap.add_argument("--assume-p", type=float, default=0.7)
    args = ap.parse_args()

    runs = load_runs(Path(args.results_dir))
    if not runs:
        raise SystemExit("no runs found")

    xs = []
    ys = []

    if args.fit == "os_cache":
        for r in runs:
            lrb = r.get("logical_read_bytes", 0) or 0
            if float(lrb) <= 0:
                continue
            label = str(r.get("label", "") or "")
            if label and "phase=" in label and "phase=measure" not in label:
                continue
            x_mb = float(r.get("mem_available_kb_start", 0) or 0) / 1024.0
            y = float(r.get("os_cache_hit_ratio", 0) or 0)
            if x_mb > 0 and 0.0 <= y <= 1.0:
                xs.append(x_mb)
                ys.append(y)

    if args.fit == "shared_buffers":
        for r in runs:
            sb = _parse_size_mb(r.get("shared_buffers", ""))
            if not sb:
                continue
            label = str(r.get("label", "") or "")
            if label and "phase=" in label and "phase=measure" not in label:
                continue
            y = float(r.get("sb_hit_ratio", 0) or 0)
            if sb > 0 and 0.0 <= y <= 1.0:
                xs.append(sb)
                ys.append(y)

    unique_xs = sorted({round(float(x), 6) for x in xs})
    if len(unique_xs) < 1:
        raise SystemExit("no usable runs after filtering")

    mode = "fit"
    fit = None
    if len(unique_xs) >= 2:
        fit = fit_exp_saturation(xs, ys)
    else:
        if args.fit != "shared_buffers":
            raise SystemExit("not enough usable runs to fit (need >=2 distinct X values)")
        mode = "one_point_calibrated"
        x0 = float(unique_xs[0])
        y0 = sum(float(y) for y in ys) / float(len(ys))
        p = float(args.assume_p)
        w_mb = calibrate_w_from_one_point(x0, y0, p)
        fit = {"w_mb": w_mb, "p": p, "sse": None}

    out = {
        "fit": args.fit,
        "mode": mode,
        "model": "hit=1-exp(-(X/W)^p)",
        "W_mb": round(float(fit["w_mb"]), 2),
        "p": round(float(fit["p"]), 4),
        "points": len(xs),
        "distinct_x_values": len(unique_xs),
        "x_mb_min": round(min(xs), 2),
        "x_mb_max": round(max(xs), 2),
    }
    if fit.get("sse") is not None:
        out["sse"] = round(float(fit["sse"]), 8)

    if args.predict_x_mb is not None:
        out["predict_x_mb"] = args.predict_x_mb
        out["predict_hit_ratio"] = round(model_hit_ratio(args.predict_x_mb, float(fit["w_mb"]), float(fit["p"])), 6)

    if args.predict_list_mb:
        xs_list = []
        for part in str(args.predict_list_mb).split(","):
            part = part.strip()
            if not part:
                continue
            xs_list.append(float(part))
        out["predict_list_mb"] = xs_list
        out["predict_list_hit_ratio"] = [
            round(model_hit_ratio(x, float(fit["w_mb"]), float(fit["p"])), 6) for x in xs_list
        ]

    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
