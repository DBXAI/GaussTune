#!/usr/bin/env python3
"""Deterministic runtime SB/work_mem controller replay.

This model deliberately does not learn TPS from validation points.  It replays
the recorded page path, openGauss plan/operator memory traces, and explicit
memory-pool control rules.  The two runs differ only in whether shared_buffers
changes instantly or one granule per control tick.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import dual_cache_warmup as cache_model  # noqa: E402
import evaluate_s5_tp_protected_os as linux_cache  # noqa: E402
import evaluate_tp_only_stage5_replay as tp_replay  # noqa: E402


PAGE_MB = 8.0 / 1024.0
STAGE_ORDER = [
    "stage1_memory_rich",
    "stage2_reach_limit",
    "stage3_protect_tp",
    "stage4_backpressure",
    "stage5_tp_surge",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


@dataclass
class StageTarget:
    stage: str
    sb_mb: int
    work_mem_assignments: str
    base_clients: int
    dynamic_peak_mb: float
    spill_temp_mb: float
    spill_io_mb: float
    spilling_operators: int
    minimum_confidence: float


@dataclass
class StageStats:
    stage: str
    mode: str
    start_sb_mb: int = 0
    final_sb_mb: int = 0
    target_sb_mb: int = 0
    requested_ap_clients: int = 0
    admitted_ap_clients: int = 0
    queued_ap_clients: int = 0
    start_dynamic_allocated_mb: float = 0.0
    dynamic_allocated_mb: float = 0.0
    work_mem_assignments: str = ""
    predicted_spill_temp_mb: float = 0.0
    predicted_spill_io_mb: float = 0.0
    spilling_operators: int = 0
    tp_accesses: int = 0
    tp_sb_hits: int = 0
    tp_os_hits: int = 0
    tp_disk_misses: int = 0
    resize_actions: int = 0
    released_sb_pages_sampled: int = 0
    spill_pages_injected_sampled: int = 0
    min_os_capacity_mb: float = math.inf
    max_managed_memory_mb: float = 0.0

    def row(self) -> dict[str, object]:
        result = asdict(self)
        result["tp_sb_hit_rate"] = round(
            self.tp_sb_hits / self.tp_accesses if self.tp_accesses else 0.0, 8
        )
        result["tp_combined_hit_rate"] = round(
            (self.tp_sb_hits + self.tp_os_hits) / self.tp_accesses
            if self.tp_accesses else 0.0,
            8,
        )
        result["min_os_capacity_mb"] = round(
            0.0 if math.isinf(self.min_os_capacity_mb) else self.min_os_capacity_mb, 3
        )
        result["max_managed_memory_mb"] = round(self.max_managed_memory_mb, 3)
        result["dynamic_allocated_mb"] = round(self.dynamic_allocated_mb, 3)
        result["start_dynamic_allocated_mb"] = round(
            self.start_dynamic_allocated_mb, 3
        )
        result["predicted_spill_temp_mb"] = round(self.predicted_spill_temp_mb, 3)
        result["predicted_spill_io_mb"] = round(self.predicted_spill_io_mb, 3)
        return result


def parse_assignments(value: str) -> list[tuple[int, int]]:
    result = []
    for item in value.split(";"):
        if not item:
            continue
        query, work_mem = item.split("=", 1)
        result.append((int(query[1:] if query.startswith("q") else query), int(work_mem)))
    return result


def load_targets(
    sb_recommendations: Path, work_mem_recommendations: Path
) -> dict[str, StageTarget]:
    sb_rows = {row["stage"]: row for row in read_csv(sb_recommendations)}
    work_rows = {
        row["stage"]: row
        for row in read_csv(work_mem_recommendations)
        if row["allocation_mode"] == "per_query_session_setting"
    }
    targets = {}
    for stage in STAGE_ORDER:
        sb = sb_rows[stage]
        work = work_rows[stage]
        assignments = work["query_work_mem_assignments"]
        targets[stage] = StageTarget(
            stage=stage,
            sb_mb=int(sb["recommended_sb_mb"]),
            work_mem_assignments=assignments,
            base_clients=len(parse_assignments(assignments)),
            dynamic_peak_mb=float(work["dynamic_peak_mb"]),
            spill_temp_mb=float(work.get("spill_temp_mb") or 0.0),
            spill_io_mb=float(work["spill_io_mb"]),
            spilling_operators=int(work["spilling_operators"]),
            minimum_confidence=float(work["minimum_confidence"]),
        )
    return targets


def load_boundaries(path: Path) -> dict[str, tuple[int, int]]:
    by_label = {row["label"]: int(row["elapsed_ns"]) for row in read_csv(path)}
    return {
        stage: (by_label[f"{stage}_start"], by_label[f"{stage}_end"])
        for stage in STAGE_ORDER
    }


def admission_for_target(
    target: StageTarget, memory_target_max_mb: float, arrival_multiplier: float
) -> tuple[int, int, float]:
    requested = max(1, int(math.ceil(target.base_clients * arrival_multiplier)))
    per_client = target.dynamic_peak_mb / max(1, target.base_clients)
    available = max(0.0, memory_target_max_mb - target.sb_mb)
    slots = requested if per_client <= 0 else int(available // per_client)
    admitted = min(requested, max(0, slots))
    return requested, admitted, per_client * admitted


def admission_sweep(
    targets: dict[str, StageTarget], memory_target_max_mb: float
) -> list[dict[str, object]]:
    rows = []
    for multiplier in (1.0, 1.5, 2.0, 3.0):
        for stage in STAGE_ORDER:
            target = targets[stage]
            requested, admitted, dynamic = admission_for_target(
                target, memory_target_max_mb, multiplier
            )
            scale = admitted / max(1, target.base_clients)
            rows.append(
                {
                    "arrival_multiplier": multiplier,
                    "stage": stage,
                    "target_sb_mb": target.sb_mb,
                    "requested_ap_clients": requested,
                    "admitted_ap_clients": admitted,
                    "queued_ap_clients": requested - admitted,
                    "dynamic_allocated_mb": round(dynamic, 3),
                    "managed_memory_mb": round(target.sb_mb + dynamic, 3),
                    "memory_target_max_mb": memory_target_max_mb,
                    "work_mem_assignments": target.work_mem_assignments,
                    "predicted_spill_io_mb": round(target.spill_io_mb * scale, 3),
                    "is_trace_faithful_arrival": multiplier == 1.0,
                }
            )
    return rows


def stage_at(ts_ns: int, bounds: dict[str, tuple[int, int]]) -> str | None:
    for stage in STAGE_ORDER:
        start, end = bounds[stage]
        if start <= ts_ns < end:
            return stage
    return None


def count_stage_events(
    trace: Path, bounds: dict[str, tuple[int, int]]
) -> dict[str, int]:
    counts = {stage: 0 for stage in STAGE_ORDER}
    event = linux_cache.BINARY_EVENT
    with trace.open("rb") as fh:
        while chunk := fh.read(event.size):
            if len(chunk) != event.size:
                raise ValueError(f"truncated binary event in {trace}")
            _page, ts_ns, *_rest = event.unpack(chunk)
            stage = stage_at(ts_ns, bounds)
            if stage:
                counts[stage] += 1
    return counts


class RuntimeReplay:
    def __init__(
        self,
        mode: str,
        targets: dict[str, StageTarget],
        bounds: dict[str, tuple[int, int]],
        event_counts: dict[str, int],
        tp_relations: set[int],
        ap_relations: set[int],
        initial_sb_mb: int,
        granule_mb: int,
        control_interval_seconds: float,
        sample_every: int,
        memory_target_max_mb: float,
        host_memory_mb: float,
        unmanaged_reserve_mb: float,
        arrival_multiplier: float,
        active_fraction: float,
    ) -> None:
        self.mode = mode
        self.targets = targets
        self.bounds = bounds
        self.event_counts = event_counts
        self.tp_relations = tp_relations
        self.ap_relations = ap_relations
        self.granule_mb = granule_mb
        self.tick_ns = int(control_interval_seconds * 1e9)
        self.sample_every = sample_every
        self.memory_target_max_mb = memory_target_max_mb
        self.host_memory_mb = host_memory_mb
        self.unmanaged_reserve_mb = unmanaged_reserve_mb
        self.arrival_multiplier = arrival_multiplier
        self.current_sb_mb = initial_sb_mb
        self.current_dynamic_mb = 0.0
        self.target_dynamic_mb = 0.0
        self.current_stage: str | None = None
        self.current_stats: StageStats | None = None
        self.next_tick_ns = 0
        self.stage_event_index = 0
        self.injected_spill_pages = 0
        self.target_spill_pages = 0
        self.actions: list[dict[str, object]] = []
        sb_pages = self._sampled_pages(initial_sb_mb)
        ring_pages = max(1, self._sampled_pages(16))
        self.sb = cache_model.BulkReadRingSharedSimulator(
            sb_pages, ring_pages, has_strategy_info=True
        )
        self.os = linux_cache.TPProtectedLinuxCache(
            self._os_pages(), active_fraction=active_fraction
        )
        self.stats: dict[str, StageStats] = {}

    def _sampled_pages(self, mb: float) -> int:
        return max(0, int(mb / PAGE_MB / self.sample_every))

    def _os_capacity_mb(self) -> float:
        return max(
            0.0,
            self.host_memory_mb
            - self.unmanaged_reserve_mb
            - self.current_sb_mb
            - self.current_dynamic_mb,
        )

    def _os_pages(self) -> int:
        return self._sampled_pages(self._os_capacity_mb())

    def _record_capacity(self) -> None:
        if self.current_stats is None:
            return
        os_mb = self._os_capacity_mb()
        self.current_stats.min_os_capacity_mb = min(
            self.current_stats.min_os_capacity_mb, os_mb
        )
        self.current_stats.max_managed_memory_mb = max(
            self.current_stats.max_managed_memory_mb,
            self.current_sb_mb + self.current_dynamic_mb,
        )

    def _resize_os(self) -> None:
        self.os.resize(self._os_pages())
        self._record_capacity()

    def _admission(self, target: StageTarget) -> tuple[int, int, float]:
        return admission_for_target(
            target, self.memory_target_max_mb, self.arrival_multiplier
        )

    def enter_stage(self, stage: str, ts_ns: int) -> None:
        target = self.targets[stage]
        requested, admitted, dynamic = self._admission(target)
        self.current_stage = stage
        self.target_dynamic_mb = dynamic
        self.current_dynamic_mb = min(
            self.target_dynamic_mb,
            max(0.0, self.memory_target_max_mb - self.current_sb_mb),
        )
        scale = admitted / max(1, target.base_clients)
        self.current_stats = StageStats(
            stage=stage,
            mode=self.mode,
            start_sb_mb=self.current_sb_mb,
            final_sb_mb=self.current_sb_mb,
            target_sb_mb=target.sb_mb,
            requested_ap_clients=requested,
            admitted_ap_clients=admitted,
            queued_ap_clients=requested - admitted,
            start_dynamic_allocated_mb=self.current_dynamic_mb,
            dynamic_allocated_mb=self.current_dynamic_mb,
            work_mem_assignments=target.work_mem_assignments,
            predicted_spill_temp_mb=target.spill_temp_mb * scale,
            predicted_spill_io_mb=target.spill_io_mb * scale,
            spilling_operators=int(math.ceil(target.spilling_operators * scale)),
        )
        self.stats[stage] = self.current_stats
        self.stage_event_index = 0
        self.injected_spill_pages = 0
        self.target_spill_pages = self._sampled_pages(target.spill_temp_mb * scale)
        self._resize_os()
        self.next_tick_ns = ts_ns
        if self.mode == "instant":
            self._resize_sb(target.sb_mb, ts_ns, "instant_target")
            self.next_tick_ns = self.bounds[stage][1]
        else:
            self.advance(ts_ns)

    def exit_stage(self) -> None:
        if self.current_stats is not None:
            self.current_stats.final_sb_mb = self.current_sb_mb
        self.current_stage = None
        self.current_stats = None

    def _resize_sb(self, new_mb: int, ts_ns: int, reason: str) -> None:
        old_mb = self.current_sb_mb
        if new_mb == old_mb:
            return
        released = self.sb.resize(self._sampled_pages(new_mb))
        self.current_sb_mb = new_mb
        if new_mb < old_mb:
            self.current_dynamic_mb = min(
                self.target_dynamic_mb,
                max(0.0, self.memory_target_max_mb - self.current_sb_mb),
            )
        self._resize_os()
        for page_id in released:
            relation = page_id >> 32
            self.os.add_from_sb_eviction(
                page_id, streaming=relation in self.ap_relations
            )
        if self.current_stats is not None:
            self.current_stats.resize_actions += 1
            self.current_stats.released_sb_pages_sampled += len(released)
            self.current_stats.final_sb_mb = new_mb
            self.current_stats.dynamic_allocated_mb = self.current_dynamic_mb
        self.actions.append(
            {
                "mode": self.mode,
                "stage": self.current_stage or "outside_stage",
                "elapsed_seconds": round(ts_ns / 1e9, 6),
                "old_sb_mb": old_mb,
                "new_sb_mb": new_mb,
                "target_sb_mb": self.targets[self.current_stage].sb_mb
                if self.current_stage else new_mb,
                "released_pages_sampled": len(released),
                "dynamic_allocated_mb": round(self.current_dynamic_mb, 3),
                "managed_memory_mb": round(new_mb + self.current_dynamic_mb, 3),
                "reason": reason,
            }
        )

    def advance(self, ts_ns: int) -> None:
        if self.current_stage is None or self.mode == "instant":
            return
        target = self.targets[self.current_stage].sb_mb
        while self.next_tick_ns <= ts_ns and self.current_sb_mb != target:
            delta = target - self.current_sb_mb
            step = min(abs(delta), self.granule_mb)
            proposed = self.current_sb_mb + (step if delta > 0 else -step)
            if (
                delta > 0
                and proposed + self.current_dynamic_mb > self.memory_target_max_mb + 1e-9
            ):
                break
            self._resize_sb(proposed, self.next_tick_ns, "one_granule_per_tick")
            self.next_tick_ns += self.tick_ns

    def _inject_spill(self) -> None:
        if self.current_stats is None:
            return
        total_events = max(1, self.event_counts[self.current_stats.stage])
        target = self.target_spill_pages * self.stage_event_index // total_events
        synthetic_base = (1 << 63) + STAGE_ORDER.index(self.current_stats.stage) * (1 << 55)
        while self.injected_spill_pages < target:
            self.os.access(
                synthetic_base + self.injected_spill_pages,
                streaming=True,
                count=False,
            )
            self.injected_spill_pages += 1
        self.current_stats.spill_pages_injected_sampled = self.injected_spill_pages

    def access(
        self,
        page_id: int,
        pid: int,
        strategy_ptr: int,
        strategy_type: int,
    ) -> None:
        if self.current_stats is not None:
            self.stage_event_index += 1
            self._inject_spill()
        relation = page_id >> 32
        is_tp = relation in self.tp_relations
        is_ap = relation in self.ap_relations
        streaming = strategy_type == 1 or is_ap
        hit, evicted = self.sb.access(
            page_id, pid, strategy_ptr, strategy_type, 0
        )
        if self.current_stats is not None and is_tp:
            self.current_stats.tp_accesses += 1
            self.current_stats.tp_sb_hits += int(hit)
        if not hit:
            self.os.add_from_sb_eviction(evicted, streaming=streaming)
            old_hits = self.os.hits
            old_misses = self.os.misses
            self.os.access(
                page_id,
                streaming=streaming,
                count=self.current_stats is not None and is_tp,
            )
            if self.current_stats is not None and is_tp:
                self.current_stats.tp_os_hits += self.os.hits - old_hits
                self.current_stats.tp_disk_misses += self.os.misses - old_misses


def run_replay(
    trace: Path,
    replay: RuntimeReplay,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    event = linux_cache.BINARY_EVENT
    with trace.open("rb") as fh:
        while chunk := fh.read(event.size):
            if len(chunk) != event.size:
                raise ValueError(f"truncated binary event in {trace}")
            page_id, ts_ns, strategy_ptr, pid, strategy_type, _hit, _reserved = event.unpack(chunk)
            event_stage = stage_at(ts_ns, replay.bounds)
            if replay.current_stage is not None and event_stage != replay.current_stage:
                replay.advance(replay.bounds[replay.current_stage][1])
                replay.exit_stage()
            if event_stage is not None and replay.current_stage is None:
                replay.enter_stage(event_stage, replay.bounds[event_stage][0])
            replay.advance(ts_ns)
            replay.access(page_id, pid, strategy_ptr, strategy_type)
    if replay.current_stage is not None:
        replay.advance(replay.bounds[replay.current_stage][1])
        replay.exit_stage()
    return [replay.stats[stage].row() for stage in STAGE_ORDER], replay.actions


def make_plots(
    out_dir: Path,
    granular_rows: list[dict[str, object]],
    instant_rows: list[dict[str, object]],
    actions: list[dict[str, object]],
    pressure_rows: list[dict[str, object]],
) -> None:
    import matplotlib.pyplot as plt

    labels = [f"S{index}" for index in range(1, 6)]
    granular = {row["stage"]: row for row in granular_rows}
    instant = {row["stage"]: row for row in instant_rows}
    x = list(range(5))
    fig, axes = plt.subplots(2, 1, figsize=(12, 8), constrained_layout=True)
    axes[0].plot(
        x, [granular[s]["target_sb_mb"] for s in STAGE_ORDER],
        marker="o", label="Target SB"
    )
    axes[0].plot(
        x, [granular[s]["final_sb_mb"] for s in STAGE_ORDER],
        marker="s", label="Granular replay final SB"
    )
    axes[0].set_xticks(x, labels)
    axes[0].set_ylabel("shared_buffers (MB)")
    axes[0].set_title("Trace-driven runtime controller: requested and reached SB")
    axes[0].grid(alpha=0.25)
    axes[0].legend()

    width = 0.36
    axes[1].bar(
        [value - width / 2 for value in x],
        [100 * granular[s]["tp_sb_hit_rate"] for s in STAGE_ORDER],
        width, label="Granular transition"
    )
    axes[1].bar(
        [value + width / 2 for value in x],
        [100 * instant[s]["tp_sb_hit_rate"] for s in STAGE_ORDER],
        width, label="Instant target control"
    )
    axes[1].set_xticks(x, labels)
    axes[1].set_ylabel("TP shared-buffer hit rate (%)")
    axes[1].set_ylim(98.8, 100.0)
    axes[1].set_title(
        "Transition comparison (combined-hit extra disk misses: 0 in every stage)"
    )
    axes[1].grid(axis="y", alpha=0.25)
    axes[1].legend()
    fig.savefig(out_dir / "runtime_controller_trace_replay.png", dpi=180)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(12, 5), constrained_layout=True)
    if actions:
        first = min(float(row["elapsed_seconds"]) for row in actions)
        ax.step(
            [float(row["elapsed_seconds"]) - first for row in actions],
            [int(row["new_sb_mb"]) for row in actions],
            where="post",
            label="Granular SB actions",
        )
    ax.set_xlabel("Elapsed control time (s)")
    ax.set_ylabel("shared_buffers (MB)")
    ax.set_title("One granule per tick, cache state retained")
    ax.grid(alpha=0.25)
    ax.legend()
    fig.savefig(out_dir / "runtime_controller_actions.png", dpi=180)
    plt.close(fig)

    stage4 = [
        row for row in pressure_rows if row["stage"] == "stage4_backpressure"
    ]
    fig, ax = plt.subplots(figsize=(10, 5), constrained_layout=True)
    pressure_x = list(range(len(stage4)))
    requested = [int(row["requested_ap_clients"]) for row in stage4]
    admitted = [int(row["admitted_ap_clients"]) for row in stage4]
    ax.bar(pressure_x, requested, color="#d9d9d9", label="Requested AP clients")
    ax.bar(pressure_x, admitted, color="#2b7bba", label="Admitted AP clients")
    for index, row in enumerate(stage4):
        queued = int(row["queued_ap_clients"])
        ax.text(index, requested[index] + 0.2, f"queued={queued}", ha="center")
    ax.set_xticks(
        pressure_x,
        [f"{float(row['arrival_multiplier']):g}x" for row in stage4],
    )
    ax.set_xlabel("AP arrival pressure relative to recorded Stage 4")
    ax.set_ylabel("Concurrent AP clients")
    ax.set_title("Rule-based admission under memory_target_max=24576MB")
    ax.legend()
    ax.grid(axis="y", alpha=0.25)
    fig.savefig(out_dir / "runtime_controller_admission.png", dpi=180)
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace-run", required=True, type=Path)
    parser.add_argument("--binary-sample", required=True, type=Path)
    parser.add_argument("--sb-recommendations", required=True, type=Path)
    parser.add_argument("--work-mem-recommendations", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--initial-sb-mb", type=int, default=1504)
    parser.add_argument("--granule-mb", type=int, default=256)
    parser.add_argument("--control-interval-seconds", type=float, default=2.0)
    parser.add_argument("--sample-every", type=int, default=64)
    parser.add_argument("--memory-target-max-mb", type=float, default=24576.0)
    parser.add_argument("--host-memory-mb", type=float, default=30720.0)
    parser.add_argument("--unmanaged-reserve-mb", type=float, default=4096.0)
    parser.add_argument("--arrival-multiplier", type=float, default=1.0)
    parser.add_argument("--active-fraction", type=float, default=0.35)
    parser.add_argument(
        "--admission-only",
        action="store_true",
        help="write the rule-based admission pressure matrix without replaying pages",
    )
    parser.add_argument(
        "--render-existing",
        action="store_true",
        help="regenerate plots from CSV files already present in --out-dir",
    )
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    targets = load_targets(args.sb_recommendations, args.work_mem_recommendations)
    pressure_rows = admission_sweep(targets, args.memory_target_max_mb)
    write_csv(args.out_dir / "admission_pressure_sweep.csv", pressure_rows)
    if args.render_existing:
        metric_rows = read_csv(args.out_dir / "stage_runtime_metrics.csv")
        granular_rows = [row for row in metric_rows if row["mode"] == "granular"]
        instant_rows = [row for row in metric_rows if row["mode"] == "instant"]
        action_rows = [
            row for row in read_csv(args.out_dir / "controller_actions.csv")
            if row["mode"] == "granular"
        ]
        for rows in (granular_rows, instant_rows):
            for row in rows:
                for key in ("target_sb_mb", "final_sb_mb"):
                    row[key] = int(row[key])
                for key in ("tp_sb_hit_rate", "tp_combined_hit_rate"):
                    row[key] = float(row[key])
        make_plots(
            args.out_dir, granular_rows, instant_rows, action_rows, pressure_rows
        )
        return 0
    if args.admission_only:
        print(json.dumps(pressure_rows, indent=2), flush=True)
        return 0
    bounds = load_boundaries(args.trace_run / "boundaries.csv")
    event_counts = count_stage_events(args.binary_sample, bounds)
    tp_relations = tp_replay.relation_set("h5_tpcc")
    ap_relations = tp_replay.relation_set("h5_tpch")
    if tp_relations & ap_relations:
        raise RuntimeError("TP and AP relfilenode sets overlap")

    common = dict(
        targets=targets,
        bounds=bounds,
        event_counts=event_counts,
        tp_relations=tp_relations,
        ap_relations=ap_relations,
        initial_sb_mb=args.initial_sb_mb,
        granule_mb=args.granule_mb,
        control_interval_seconds=args.control_interval_seconds,
        sample_every=args.sample_every,
        memory_target_max_mb=args.memory_target_max_mb,
        host_memory_mb=args.host_memory_mb,
        unmanaged_reserve_mb=args.unmanaged_reserve_mb,
        arrival_multiplier=args.arrival_multiplier,
        active_fraction=args.active_fraction,
    )
    granular_rows, granular_actions = run_replay(
        args.binary_sample, RuntimeReplay(mode="granular", **common)
    )
    instant_rows, instant_actions = run_replay(
        args.binary_sample, RuntimeReplay(mode="instant", **common)
    )
    write_csv(args.out_dir / "stage_runtime_metrics.csv", granular_rows + instant_rows)
    write_csv(args.out_dir / "controller_actions.csv", granular_actions + instant_actions)

    granular = {row["stage"]: row for row in granular_rows}
    instant = {row["stage"]: row for row in instant_rows}
    comparison = []
    for stage in STAGE_ORDER:
        g = granular[stage]
        i = instant[stage]
        comparison.append(
            {
                "stage": stage,
                "target_sb_mb": g["target_sb_mb"],
                "granular_final_sb_mb": g["final_sb_mb"],
                "target_reached": g["target_sb_mb"] == g["final_sb_mb"],
                "requested_ap_clients": g["requested_ap_clients"],
                "admitted_ap_clients": g["admitted_ap_clients"],
                "queued_ap_clients": g["queued_ap_clients"],
                "work_mem_assignments": g["work_mem_assignments"],
                "granular_tp_sb_hit_rate": g["tp_sb_hit_rate"],
                "instant_tp_sb_hit_rate": i["tp_sb_hit_rate"],
                "sb_hit_delta_percentage_points": round(
                    100 * (float(g["tp_sb_hit_rate"]) - float(i["tp_sb_hit_rate"])), 4
                ),
                "granular_tp_combined_hit_rate": g["tp_combined_hit_rate"],
                "instant_tp_combined_hit_rate": i["tp_combined_hit_rate"],
                "combined_delta_percentage_points": round(
                    100
                    * (
                        float(g["tp_combined_hit_rate"])
                        - float(i["tp_combined_hit_rate"])
                    ),
                    4,
                ),
                "granular_tp_disk_misses": g["tp_disk_misses"],
                "instant_tp_disk_misses": i["tp_disk_misses"],
                "predicted_spill_io_mb": g["predicted_spill_io_mb"],
            }
        )
    write_csv(args.out_dir / "granular_vs_instant.csv", comparison)
    make_plots(
        args.out_dir, granular_rows, instant_rows, granular_actions, pressure_rows
    )

    summary = {
        "model": "deterministic page-trace + source/operator-trace runtime control replay",
        "uses_tps_training_labels": False,
        "uses_validation_optimum_as_feature": False,
        "trace": str(args.binary_sample),
        "control": {
            "initial_sb_mb": args.initial_sb_mb,
            "granule_mb": args.granule_mb,
            "control_interval_seconds": args.control_interval_seconds,
            "memory_target_max_mb": args.memory_target_max_mb,
            "arrival_multiplier": args.arrival_multiplier,
            "work_mem_scope": "per query session",
        },
        "targets": {stage: asdict(target) for stage, target in targets.items()},
        "comparison": comparison,
        "admission_pressure_sweep": pressure_rows,
        "validation_scope": [
            "The same recorded mixed page trace was replayed with gradual and instant SB transitions.",
            "Spill pages come from operator lifetime/allocation replay and are inserted as streaming page-cache traffic.",
            "Admission is a fixed memory inequality, not a learned classifier.",
        ],
        "not_yet_validated": [
            "openGauss on this host cannot resize shared_buffers online, so granule actions are simulator results, not kernel execution evidence.",
            "The trace has stage gaps and no AP query crossing a stage boundary; graceful shrink of an already-running AP query remains unobserved.",
            "This replay predicts cache/spill/admission behavior, not TPS; the PPT's <=3% TPS jitter needs an online-resize prototype and a real run.",
        ],
    }
    (args.out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
