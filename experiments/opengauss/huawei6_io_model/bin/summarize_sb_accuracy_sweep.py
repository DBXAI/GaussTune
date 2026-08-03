#!/usr/bin/env python3
"""Summarize a Huawei5 shared_buffers accuracy sweep."""

from __future__ import annotations

import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def sb_from_dir(path: Path) -> int | None:
    m = re.fullmatch(r"sb(\d+)mb", path.name)
    return int(m.group(1)) if m else None


def plot_errors(out_root: Path, rows: list[dict[str, object]]) -> None:
    plot_dir = out_root / "summary_plots"
    plot_dir.mkdir(parents=True, exist_ok=True)
    stages = sorted({str(r["stage"]) for r in rows})

    for metric, title in [
        ("sb_err_pp", "SB prediction error"),
        ("os_err_pp", "OS prediction error"),
        ("combined_err_pp", "Combined prediction error"),
    ]:
        fig, ax = plt.subplots(figsize=(10, 5.5), facecolor="#f4f6f8")
        ax.set_facecolor("#fafbfc")
        for stage in stages:
            sr = sorted([r for r in rows if r["stage"] == stage], key=lambda r: int(r["sb_mb"] or 0))
            xs = [int(r["sb_mb"]) for r in sr]
            ys = [float(r[metric]) for r in sr]
            ax.plot(xs, ys, marker="o", linewidth=1.8, label=stage)
        ax.axhline(0, color="#333", linewidth=0.8)
        ax.set_xscale("log", base=2)
        ax.set_xticks(sorted({int(r["sb_mb"]) for r in rows}))
        ax.get_xaxis().set_major_formatter(lambda x, _pos: f"{int(x)}")
        ax.set_xlabel("shared_buffers (MB)")
        ax.set_ylabel("error (pp)")
        ax.set_title(f"Huawei5 SB sweep: {title}", fontweight="bold")
        ax.grid(True, linestyle=":", alpha=0.4)
        ax.legend(fontsize=8, ncol=2)
        fig.tight_layout()
        for suffix in ("png", "svg"):
            fig.savefig(plot_dir / f"{metric}_by_sb.{suffix}", dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
        plt.close(fig)

    # MAE summary bar chart
    by_sb: dict[int, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        by_sb[int(row["sb_mb"])].append(row)
    summary = []
    for sb_mb in sorted(by_sb):
        rs = by_sb[sb_mb]
        summary.append(
            {
                "sb_mb": sb_mb,
                "sb_mae_pp": sum(abs(float(r["sb_err_pp"])) for r in rs) / len(rs),
                "os_mae_pp": sum(abs(float(r["os_err_pp"])) for r in rs) / len(rs),
                "combined_mae_pp": sum(abs(float(r["combined_err_pp"])) for r in rs) / len(rs),
            }
        )
    write_csv(out_root / "sb_accuracy_summary.csv", summary)

    x = np.arange(len(summary))
    width = 0.25
    fig, ax = plt.subplots(figsize=(10, 5.5), facecolor="#f4f6f8")
    ax.set_facecolor("#fafbfc")
    labels = [str(r["sb_mb"]) for r in summary]
    for offset, key, label, color in [
        (-width, "sb_mae_pp", "SB MAE", "#5b8def"),
        (0, "os_mae_pp", "OS MAE", "#e67e22"),
        (width, "combined_mae_pp", "Combined MAE", "#27ae60"),
    ]:
        vals = [float(r[key]) for r in summary]
        ax.bar(x + offset, vals, width, label=label, color=color, edgecolor="#222", linewidth=0.4)
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_xlabel("shared_buffers (MB)")
    ax.set_ylabel("MAE across 5 stages (pp)")
    ax.set_title("Huawei5 SB sweep: accuracy MAE by shared_buffers", fontweight="bold")
    ax.grid(axis="y", linestyle=":", alpha=0.4)
    ax.legend()
    fig.tight_layout()
    for suffix in ("png", "svg"):
        fig.savefig(plot_dir / f"mae_by_sb.{suffix}", dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("sweep_root")
    args = parser.parse_args()
    out_root = Path(args.sweep_root)

    rows: list[dict[str, object]] = []
    for run_dir in sorted(out_root.glob("sb*mb"), key=lambda p: sb_from_dir(p) or -1):
        sb_mb = sb_from_dir(run_dir)
        if sb_mb is None:
            continue
        best = run_dir / "continuous_best_predictions.csv"
        if not best.exists():
            continue
        for row in read_rows(best):
            rows.append(
                {
                    "sb_mb": sb_mb,
                    "stage": row["mode"],
                    "strategy": row.get("strategy", ""),
                    "model": row.get("model", ""),
                    "readahead_pages": row.get("readahead_pages", ""),
                    "os_scale": row.get("os_scale", ""),
                    "actual_sb": row.get("meas_sb_hr", ""),
                    "pred_sb": row.get("sb_hit_rate", ""),
                    "sb_err_pp": row.get("sb_err_pp", ""),
                    "actual_os": row.get("meas_os_hr", ""),
                    "pred_os": row.get("physical_os_cond_hit_rate", ""),
                    "os_err_pp": row.get("os_err_pp", ""),
                    "actual_combined": row.get("meas_combined", ""),
                    "pred_combined": row.get("physical_combined_hit_rate", ""),
                    "combined_err_pp": row.get("combined_err_pp", ""),
                    "run_dir": str(run_dir),
                }
            )

    if not rows:
        raise SystemExit(f"no completed runs found under {out_root}")
    write_csv(out_root / "sb_accuracy_by_stage.csv", rows)
    plot_errors(out_root, rows)
    print(out_root / "sb_accuracy_by_stage.csv")
    print(out_root / "sb_accuracy_summary.csv")
    print(out_root / "summary_plots")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
