# Huawei7 audited-data reproduction

This is the runbook for reproducing the experiment, not merely running the
unit tests. It supports two dataset modes: audit and reuse databases that are
already present, or create the original fresh-load profile. Every command is
fail-closed: a missing source version, incompatible dataset structure,
reused training/holdout ID, changed evidence file, unsafe fio
target, out-of-surface queue depth, or failed holdout stops the run.

## 1. Required host and immutable inputs

Use a fresh Ubuntu 20.04 host with Linux 5.4, 8 physical cores/16 logical
CPUs, 29--31 GiB RAM, swap disabled, and Alibaba Cloud Elastic Block Storage.
The fresh-load mode additionally needs at least 160 decimal GB free before
loading data; audited reuse does not duplicate the databases and has no such
free-space gate. Keep the whole hardware
device (`/dev/nvme0n1`) separate from the mounted data partition
(`/dev/nvme0n1p3`). The block collectors take the queue device reported by
`block_rq_complete` (the whole NVMe device on this host), not the filesystem
partition path.

The following inputs must match `config/reproduction_contract.json`:

| Input | Pinned identity |
| --- | --- |
| openGauss source | `b5a8d5b056bbe660a6315cb424253717fb32cd04` |
| `gaussdb` | SHA-256 `d317f2f755c9dd06674e9c064dcbced57a5b77301abe4590321fb7f3f1be6b0c` |
| openGauss JDBC 5.1.0 | SHA-256 `8673d004230ab3fd947ff8850147c42a9ecf575fdba0a958b3d4253152499956` |
| BenchBase | commit `33c00473807ebd49304d114a6d769d2d2b2bbb34`; packaged jar SHA-256 `df81d081842ad9c551c0a585cd718147abe22ffd997832fa09df64e75806ee10` |
| TPC-H dbgen | `32f1c1b92d1664dba542e927d23d86ffa57aa253` |
| tools | sysbench 1.0.18, fio 3.16, bpftrace 0.9.4, `python3-bpfcc` 0.12.0, `python3-lz4` 3.0.2 |

Do not substitute a newly built `gaussdb` and call it the same experiment.
Transfer the pinned binary distribution/JDBC/BenchBase package, and separately
clone the pinned source trees used for formula and provenance checks.

Set task-specific paths (do not put passwords in command arguments):

```bash
export H7_ROOT=/root/GaussTune/experiments/opengauss/huawei7
export H7_GAUSS_HOME=/opt/openGauss
export H7_GAUSS_DATA=/opt/openGauss/data
export H7_SOURCE=/root/openGauss-server-5.1.0
export H7_BENCHBASE_ROOT=/opt/benchbase
export H7_BENCHBASE_HOME=/opt/benchbase/target/benchbase-postgres/benchbase-postgres
export H7_DBGEN=/opt/tpch-dbgen
export H7_JDBC=/opt/opengauss-jdbc-5.1.0.jar
export H7_MACHINE_DEVICE=/dev/nvme0n1
export H7_DATA_DEVICE=/dev/nvme0n1p3
export H7_EVIDENCE=/var/lib/huawei7/evidence
export HUAWEI7_AP_PASSWORD='replace-with-a-different-strong-secret'
export HUAWEI7_SYSBENCH_PASSWORD='replace-with-a-strong-secret'
export HUAWEI7_TPCC_PASSWORD='replace-with-another-strong-secret'
cd "$H7_ROOT"
```

Initialize and start a single-node openGauss 5.1 cluster as OS user `omm` at
`$H7_GAUSS_DATA`, listening on port 5432. Keep the server otherwise idle
during calibration. The benchmark roles must be able to connect over
127.0.0.1; use the site's approved `pg_hba.conf` authentication rather than
checking passwords into this directory.

The Ubuntu sysbench 1.0.18 PostgreSQL driver used by the contract does not
support openGauss's SASL-only host challenge. Initialize/configure the local
benchmark rule as `md5` (for example `gs_initdb --auth-host=md5`) and ensure
role passwords contain an MD5-compatible verifier before loading. Do not
silently switch tool versions: verify one one-second sysbench connection first.

Always run the source doctor first:

```bash
PYTHONPATH=. python3 scripts/doctor.py \
  --source-root "$H7_SOURCE" --gaussdb "$H7_GAUSS_HOME/bin/gaussdb" \
  --out "$H7_EVIDENCE/doctor.json"

```

`valid` must be `true`. Run the reproduction doctor after selecting one of
the dataset modes below. In fresh-load mode capacity is a hard gate; in reuse
mode the validated dataset artifact replaces that gate.

## 2. Select and audit the datasets

### 2A. Reuse the databases already on the current host

The checked contract accepts the actual Huawei6-era databases by logical
workload identity rather than by a mislabeled PPT byte range:

| Workload | Database | Audited shape |
| --- | --- | --- |
| AP | `h5_tpch` | standard TPC-H SF85, all eight tables |
| TP/sysbench | `h5_tpcc` | 16 standard `sbtest` tables, about 1M rows each, `id` and `k` indexes |
| TP/BenchBase | `h5_tpcc_bench` | standard TPCC, 100 warehouses and required transaction indexes |

Generate matching SF85 query text, fingerprint the machine, then perform a
read-only audit. The audit also asks openGauss to plan Q2/Q9/Q13/Q18/Q21, so a
same-named but incompatible schema is rejected:

```bash
python3 scripts/render_tpch_queries.py --dbgen-root "$H7_DBGEN" \
  --scale 85 --seed 15721 --out-dir "$H7_EVIDENCE/queries"

PYTHONPATH=. python3 scripts/fingerprint_machine.py \
  --device "$H7_MACHINE_DEVICE" --gaussdb "$H7_GAUSS_HOME/bin/gaussdb" \
  --source-root "$H7_SOURCE" --out "$H7_EVIDENCE/machine.json"

PYTHONPATH=. python3 scripts/audit_dataset_contract.py \
  --contract config/current_dataset_contract.json \
  --machine "$H7_EVIDENCE/machine.json" \
  --gsql "$H7_GAUSS_HOME/bin/gsql" \
  --library-dir "$H7_GAUSS_HOME/lib" \
  --query-dir "$H7_EVIDENCE/queries" \
  --out "$H7_EVIDENCE/dataset-audit.json"

PYTHONPATH=. python3 scripts/doctor_reproduction.py \
  --device "$H7_MACHINE_DEVICE" --gauss-home "$H7_GAUSS_HOME" \
  --source-root "$H7_SOURCE" --jdbc-jar "$H7_JDBC" \
  --benchbase-root "$H7_BENCHBASE_ROOT" \
  --benchbase-home "$H7_BENCHBASE_HOME" --dbgen-root "$H7_DBGEN" \
  --data-filesystem-path "$H7_GAUSS_DATA" \
  --dataset-audit "$H7_EVIDENCE/dataset-audit.json" \
  --out "$H7_EVIDENCE/fresh-machine-doctor.json"
```

Both JSON files must have `valid=true`. Use
`config/stage_runtime.current.example.json` as the runtime template, changing
only machine-local absolute paths if `$H7_EVIDENCE` differs. Do not edit its
table counts or warehouse count to bypass an audit; rerun the audit after any
intentional database change. A TPCC workload legitimately updates rows during
measurement, so the artifact denotes the audited starting snapshot and all
models/episodes must retain that same artifact and fingerprint.

### 2B. Optional fresh-load profile

Use this path only when no compatible databases exist or the reuse audit
reports a real schema/index defect. The legacy fresh loaders create one
`h7_tp` role for both TP databases, so give the three runtime names the same
secret for this mode only:

```bash
export HUAWEI7_TP_PASSWORD='replace-with-one-fresh-load-tp-secret'
export HUAWEI7_SYSBENCH_PASSWORD="$HUAWEI7_TP_PASSWORD"
export HUAWEI7_TPCC_PASSWORD="$HUAWEI7_TP_PASSWORD"
```

Then load:

```bash
python3 scripts/create_experiment_roles.py --gauss-home "$H7_GAUSS_HOME"

TPCH_DBGEN_ROOT="$H7_DBGEN" GAUSS_HOME="$H7_GAUSS_HOME" \
  scripts/load_tpch_sf60.sh

GAUSS_HOME="$H7_GAUSS_HOME" scripts/load_sysbench_20gb.sh

python3 scripts/load_benchbase_tpcc_20gb.py \
  --gauss-home "$H7_GAUSS_HOME" --benchbase-home "$H7_BENCHBASE_HOME" \
  --jdbc-jar "$H7_JDBC"

PYTHONPATH=. python3 scripts/fingerprint_machine.py \
  --device "$H7_MACHINE_DEVICE" --gaussdb "$H7_GAUSS_HOME/bin/gaussdb" \
  --source-root "$H7_SOURCE" --out "$H7_EVIDENCE/machine.json"

python3 scripts/audit_dataset_contract.py \
  --contract config/reproduction_contract.json \
  --machine "$H7_EVIDENCE/machine.json" \
  --gsql "$H7_GAUSS_HOME/bin/gsql" \
  --library-dir "$H7_GAUSS_HOME/lib" \
  --out "$H7_EVIDENCE/dataset-audit.json"
```

The loaders refuse to overwrite an existing database. The audit requires:
TPC-H SF60 at 85--100 decimal GB with its largest table at 55--63 GB;
16 sysbench tables with 4,000,000 rows each and an 18--22 GB database; and
125 TPCC warehouses in an 18--22 GB database.

Before loading, run `doctor_reproduction.py` without `--dataset-audit`; its
160 GB free-space check must pass. After loading, rerun the dataset audit and
the doctor with `--dataset-audit` exactly as in mode 2A, using
`config/reproduction_contract.json` and the SF60 query directory.

Render the five deterministic AP queries:

```bash
python3 scripts/render_tpch_queries.py --dbgen-root "$H7_DBGEN" \
  --scale 60 --seed 15721 --out-dir "$H7_EVIDENCE/queries"
```

Use the `machine_fingerprint` field from `machine.json` everywhere below; evidence
from another fingerprint is rejected.

Measure the PPT fixed allocatable pool instead of typing a convenient number.
At three or more distinct idle `shared_buffers` settings (for example 2048,
4096 and 8192 MiB), restart with `restart_with_shared_buffers.py`, wait for
background loading to settle, and collect at least three samples per setting:

```bash
PYTHONPATH=. python3 scripts/collect_memory_snapshot.py \
  --machine "$H7_EVIDENCE/machine.json" --data-dir "$H7_GAUSS_DATA" \
  --gsql "$H7_GAUSS_HOME/bin/gsql" --library-dir "$H7_GAUSS_HOME/lib" \
  --samples 3 --interval-seconds 5 \
  --out "$H7_EVIDENCE/memory/sb-REPLACE.json"
```

Create `huawei7.memory-budget-manifest/v1` with those snapshot paths and an
explicit operational `safety_margin_mb`, then build the bound artifact:

```bash
PYTHONPATH=. python3 -m huawei7.memory_budget \
  "$H7_EVIDENCE/memory/manifest.json" \
  "$H7_EVIDENCE/memory/memory-budget.json"
```

The collector queries `pg_stat_activity` before and after every `/proc`
sample and aborts unless both counts are zero. The builder verifies the
SysV-size/SB slope, derives fixed database memory
from the fitted intercept plus maximum private RSS, and reserves the maximum
observed non-database non-reclaimable memory plus the declared safety margin.
The pipeline rehashes and revalidates every underlying snapshot, not only the
top-level budget JSON.

## 3. AP white-box calibration and blind candidate plans

For Q2/Q9/Q13/Q18/Q21 collect at least nine training executions and at least
three disjoint holdout executions across representative `work_mem` values and
plan families. Build an AP command with `--explain-analyze`; then
`scripts/collect_isolated_device_delta.py` performs three randomized paired
whole-device idle/query repetitions. The three query arms are themselves
`EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` executions, so cardinality, BUFFERS,
runtime and request labels come from the same executions rather than two
unpaired runs.

Do not hand-write the isolated-I/O argv. Build its secret-free, query-bound
artifact, then pass the same query file to the collector:

```bash
PYTHONPATH=. python3 scripts/build_ap_collection_command.py \
  --runtime-config config/stage_runtime.json \
  --query-file "$H7_EVIDENCE/queries/q18.sql" --query-id 18 \
  --work-mem-mb REPLACE --machine-fingerprint REPLACE_FINGERPRINT \
  --application-name ppt5_ap_q18_io_REPLACE \
  --explain-analyze \
  --out "$H7_EVIDENCE/ap/q18-REPLACE.command.json"

PYTHONPATH=. python3 scripts/collect_isolated_device_delta.py \
  --device "$H7_MACHINE_DEVICE" \
  --command-json "$H7_EVIDENCE/ap/q18-REPLACE.command.json" \
  --query-file "$H7_EVIDENCE/queries/q18.sql" --query-id 18 \
  --plan-family REPLACE_FROM_EXPLAIN --work-mem-mb REPLACE \
  --machine-fingerprint REPLACE_FINGERPRINT \
  --out-dir "$H7_EVIDENCE/ap/q18-REPLACE-device"
```

The physical request probe counts exact target-device `block_rq_complete`
events and bytes. It deliberately does not infer service time by correlating
`(sector,length)`, because Linux 5.4 exposes no unique request pointer and
that key collides under concurrency. Service time comes only from the separate
four-class fio evidence. Probe output and query stdout/stderr stay under
`/dev/shm` until the probe has stopped, so the collector cannot count its own
evidence writes. Promoted files are `fsync`ed before the next randomized arm.
The bundle rehashes the completion-probe program, requires the tmpfs/durable
promotion proof, and requires its machine, query SHA, `work_mem`, row executor
and measured plan family to equal the paired EXPLAIN.

All AP collection commands force `enable_vector_engine=off` and
`query_dop=1`; the source
formulas in this experiment are the openGauss 5.1 row-executor formulas, and a
Vector plan or a DOP other than one is rejected rather than silently modeled
or executed with a different code path.

Keep each generated `collection.json` next to its `explain_analyze.json`.
Every training/holdout manifest row must include `query_id`,
`explain_collection`, and `explain_analyze`; the builder verifies that the
collection binds the same machine, query SHA, `work_mem`, row executor and
EXPLAIN SHA.

openGauss 5.1 documents that only normal EXPLAIN mode takes effect in the
current version; the target build therefore does not provide a usable native
`A-width` table. Do not label plan `E-width` as an observation. For each
distinct plan family, run a query-family projection sample whose fields cover
the tuples consumed by its memory operators:

```bash
PYTHONPATH=. python3 scripts/collect_family_width_anchors.py \
  --explain "$H7_EVIDENCE/ap/blind/q18-wm-REPLACE.json" \
  --sample-sql config/width_samples/q18.sql \
  --query-file "$H7_EVIDENCE/queries/q18.sql" --query-id 18 \
  --gsql "$H7_GAUSS_HOME/bin/gsql" \
  --library-dir "$H7_GAUSS_HOME/lib" --database REPLACE_AP_DATABASE \
  --user REPLACE_AP_USER --password-env HUAWEI7_AP_PASSWORD \
  --machine-fingerprint REPLACE_FINGERPRINT \
  --out "$H7_EVIDENCE/ap/width-q18-REPLACE.json"
```

The collector retains the exact sampling SQL and its SHA, requires at least
30 real rows, computes `max(1, measured_projection/root_plan_width)`, and
applies that factor only to the exact required node signatures in that plan
family. This is a transparent conservative correction, not a claim that the
sample is native per-node executor instrumentation.

Merge all query-family artifacts into one width-evidence file. The old native
width/fallback split does not apply to this openGauss 5.1 target.

```bash
PYTHONPATH=. python3 scripts/merge_width_anchors.py \
  "$H7_EVIDENCE"/ap/width-*.json \
  --out "$H7_EVIDENCE/ap/width-evidence.json"
```

The merged artifact must have the same machine fingerprint as the AP bundle.
Plan width is never accepted as an observation.

Plan-switch evidence must cover the complete declared `work_mem` grid. For
each grid value collect a non-executing plan with `scripts/collect_explain.py`.
It writes both the plan and a `.collection.json` sidecar. Every
`huawei7.plan-switch-manifest/v1` plan row must name both `explain` and
`collection`, then run:

```bash
PYTHONPATH=. python3 -m huawei7.plan_switch \
  "$H7_EVIDENCE/ap/plan-switch-manifest.json" \
  "$H7_EVIDENCE/ap/plan-switch-evidence.json"
```

An operator source-formula boundary may lie above the declared grid maximum.
Such a boundary is retained as right-censored evidence, with its modeled grid
location and `*_in_search_interval=false`; it does not require or authorize a
candidate outside the complete declared grid. Candidate generation continues
to filter every source boundary and plan switch to that declared interval.

The AP calibration manifest schema is
`huawei7.ap-calibration-manifest/v1`. It contains:

- machine fingerprint, source manifest path and its SHA-256;
- `query_files`, mapping every modeled query ID to the exact SQL file;
- one width-evidence artifact;
- `training_runs` (at least 9) and `holdout_runs` (at least 3), each with a
  unique `trace_id`, `query_id`, `explain_analyze`, `explain_collection`,
  `device_delta`, `work_mem_mb`, and DOP;
- runtime/request MAPE limits;
- `work_mem_search` for every query, with minimum/maximum/grid and the
  plan-switch evidence path;
- blind `candidate_plans` for exactly the candidate set derived from source
  mode boundaries and plan switches.

Build the bundle:

```bash
PYTHONPATH=. python3 -m huawei7.model_bundle \
  "$H7_EVIDENCE/ap/ap-calibration-manifest.json" \
  "$H7_EVIDENCE/ap/ap-model-bundle.json"
```

Candidate files containing `Actual *` or runtime fields are rejected. Each
candidate EXPLAIN SHA must equal the already-bound complete blind-grid row at
the same `work_mem`. Query SHA values propagate through model results,
recommendations, stage SQL and final raw-log evidence. The runtime and
physical-request holdouts must pass before a bundle is valid.

AP request holdouts retain the three nonnegative paired request observations
as an empirical cold/warm-cache interval. Error is the percentage distance to
that interval, rather than unstable point MAPE against a background-dominated
median. Only directions with a positive modeled logical-page source are
scored; whole-device activity in a zero-logical-page direction remains in the
raw windows but is not attributed to the query. Runtime remains a global
nonnegative fit with training-only, plan-family residual scales.

## 4. Device service times and TP/AP latency surface

First prepare one explicit disposable, preconditioned file. Never point fio
at a raw device or database file:

```bash
PYTHONPATH=. python3 -m huawei7.fio_surface prepare \
  /var/lib/huawei7/huawei7-fio-calibration.img --size-gib 4

PYTHONPATH=. python3 scripts/collect_fio_service_times.py \
  --target /var/lib/huawei7/huawei7-fio-calibration.img \
  --machine-fingerprint REPLACE_FINGERPRINT --repeats 3 \
  --runtime-seconds 10 --ramp-seconds 2 \
  --out-dir "$H7_EVIDENCE/fio/service-times"
```

Derive the AP read fraction from the AP training device deltas. Measure a
rectangular training grid and disjoint interior holdout grid with that exact
mix. Example axes (expand them if modeled queue depths exceed the domain):

```bash
PYTHONPATH=. python3 -m huawei7.fio_surface collect \
  /var/lib/huawei7/huawei7-fio-calibration.img \
  "$H7_EVIDENCE/fio/training.csv" --split train \
  --tp-qd 1,2,4,8 --ap-qd 0,1,2,4,8 --repeats 3 \
  --runtime-seconds 15 --ramp-seconds 3 \
  --ap-read-fraction REPLACE_MEASURED_FRACTION --ap-block-kib 128

PYTHONPATH=. python3 -m huawei7.fio_surface collect \
  /var/lib/huawei7/huawei7-fio-calibration.img \
  "$H7_EVIDENCE/fio/holdout.csv" --split holdout \
  --tp-qd 3,6 --ap-qd 3,6 --repeats 3 \
  --runtime-seconds 15 --ramp-seconds 3 \
  --ap-read-fraction REPLACE_MEASURED_FRACTION --ap-block-kib 128

PYTHONPATH=. python3 -m huawei7.fio_surface validate \
  "$H7_EVIDENCE/fio/training.csv" "$H7_EVIDENCE/fio/holdout.csv" \
  "$H7_EVIDENCE/fio/holdout-report.json" \
  --machine-fingerprint REPLACE_FINGERPRINT --maximum-mape .20
```

The validation report is source-bound v2 evidence: it stores absolute paths
and SHA-256 values for both CSVs. The architecture pipeline rereads those
CSVs and recomputes the grid split, medians, surface and holdout MAPE. The
four-class service artifact likewise retains every raw fio/block-calibration
file (at least three per class), and the pipeline reparses those files before
accepting the reported medians.

If the measured AP stages do not all fall within the original mix tolerance
of one curve, collect and validate another surface at the uncovered exact
read fraction. Freeze all accepted curves into one source-bound set:

```bash
python3 scripts/build_fio_surface_set.py \
  --report "$H7_EVIDENCE/fio/holdout-report-93pct.json" \
  --report "$H7_EVIDENCE/fio/holdout-report-100pct.json" \
  --out "$H7_EVIDENCE/fio/surface-set-v1.json"
```

`--fio-validation` accepts either one validation report or this set. For each
candidate the pipeline selects the closest measured surface, still enforces
the surface's original ±5% AP-read-mix tolerance, and binds every selected
report and its raw CSVs. A surface set is therefore coverage evidence, not a
way to loosen the acceptance threshold.

No queue-depth extrapolation is permitted. If any candidate lies outside the
surface, expand and remeasure the grid instead of raising a software limit.

## 5. Native synchronized TP response and independent holdout

Perform this entire section independently for `sysbench` and
`benchbase-tpcc`. Build a secret-safe, versioned command artifact with
`scripts/build_tp_collection_command.py`; the matrix invokes
`scripts/collect_synchronized_tp_native.py`. The production collector:

- measures a paired whole-device idle baseline;
- starts the real TP driver and uses its warmup marker as the phase boundary;
- snapshots native `pg_stat_database` counters before and after the scored
  window, deriving real buffer accesses, hit ratio and database transactions;
- keeps one local omm `gsql` control session open across the run so those two
  counter queries do not pay per-boundary connection startup; every measured
  server-side snapshot span must remain at or below 5% of the scored interval
  (1.5 seconds for the 30-second matrix runs). The SQL row
  carries `statement_timestamp()` and a trailing `clock_timestamp()` which
  are mapped onto the monotonic capture clock, so delayed client log polling
  cannot shift the scored boundary. The collector and its exact openGauss
  control-backend thread use nice -20; TP drivers and the accepted block
  observer retain nice 0, and the scheduler identities are recorded in every
  collection;
- counts whole-device read/write completions and bytes, subtracting the
  measured idle rate without discarding kernel writeback;
- requires at least 85% overlap between the native counter window and the
  driver's declared measurement phase. TPS and per-transaction response use
  target-database transactions from that exact common window; the complete
  native driver output is independently parsed and hashed to prove workload,
  duration and topology integrity; and
- keeps service time exclusively in the independent four-class fio evidence.

Complete `ReadBuffer`/`PinBuffer` uprobe replay remains implemented as a
diagnostic in `probes/opengauss_buffer_trace_bcc.py`, including binary LZ4
output and loss accounting. It is not the production calibration method: on
this host both the textual and optimized binary complete streams slowed the
real workload by about 86%, far above the unchanged 5% acceptance gate.
Sampling that stream would alter cache semantics. The accepted replacement
uses real database counters at every real SB setting; it does not fabricate a
page trace or claim the rejected replay passed.

During the scored interval, block-probe output and driver logs are written
under `/dev/shm`; BenchBase result directories are
also temporary tmpfs paths. They are copied into the evidence directory only
after both probes have stopped, then `fsync`ed before another trace starts.
The matrix's child-process console logs follow the same tmpfs/promote/fsync
sequence. The collector rejects a host where `/dev/shm` is not tmpfs,
preventing its own logs from becoming database-device write requests.

Run all four benchmark/topology chains with the resumable matrix runner. It
uses three uniformly spaced real SB settings, three Sysbench repeats and four
TPCC repeats per point,
and quantifies observer overhead with at least three randomized runs per arm
under the same command contract. Each chain tests overhead first at the
largest SB point and stops if the 5% gate fails. After every TPCC SB restart,
the runner records three to twelve excluded `pNN` runs. Formal `rNN` runs begin
only when the most recent three runs simultaneously have at most 20% span
relative to their median for TPS, buffer accesses/transaction, physical read
requests/transaction and physical read bytes/transaction, plus at most 0.02
absolute hit-ratio span. Failure to settle after twelve runs rejects the point;
the runner never selects a convenient post-hoc subset. A partial point is
archived and recollected from one common restart rather than mixing states.
Fully hash-valid chains and points resume without another restart.

```bash
python3 scripts/run_tp_calibration_matrix.py \
  --runtime-config config/stage_runtime.json \
  --memory-budget "$H7_EVIDENCE/memory/memory-budget.json" \
  --device "$H7_MACHINE_DEVICE" --data-dir "$H7_GAUSS_DATA" \
  --gauss-home "$H7_GAUSS_HOME" \
  --machine-fingerprint REPLACE_FINGERPRINT \
  --shared-buffers-mb 2048,5120,8192 --repeats 3 --tpcc-repeats 4 \
  --warmup-seconds 10 --measure-seconds 30 \
  --out-dir "$H7_EVIDENCE/tp/matrix"
```

For a manual single-chain overhead check, use:

```bash
PYTHONPATH=. python3 scripts/measure_buffer_probe_overhead.py \
  --command-json "$H7_EVIDENCE/tp/sysbench-command.json" \
  --benchmark sysbench --device "$H7_MACHINE_DEVICE" \
  --target-db-node REPLACE_DATABASE_OID \
  --machine-fingerprint REPLACE_FINGERPRINT \
  --warmup-seconds 30 --measure-seconds 60 --repeats 3 \
  --maximum-slowdown-fraction .05 \
  --out-dir "$H7_EVIDENCE/tp/probe-overhead"
```

Every pipeline config references its accepted overhead artifact. A failed
gate requires a lower-overhead exact observer and a new measurement; it does
not authorize relaxing 5% or sampling cache events. Sysbench's aggregate
bpftrace observer is accepted only for Sysbench. TPCC uses
`block_rq_completion_total_bcc.py`: its hot path updates per-CPU arrays and
prints cumulative one-second snapshots, which are differenced without a
read/clear loss race. The validator reparses native benchmark summaries,
rehashes the exact observer source/raw files and recomputes the randomized
arm medians and slowdown; a self-declared `valid=true` is insufficient.

Example sysbench repeat:

```bash
python3 scripts/build_tp_collection_command.py \
  --runtime-config config/stage_runtime.json --benchmark sysbench \
  --machine-fingerprint REPLACE_FINGERPRINT \
  --terminals 128 --warmup-seconds 30 --measure-seconds 60 \
  --out-command "$H7_EVIDENCE/tp/sysbench-r1-command.json"

PYTHONPATH=. python3 scripts/collect_synchronized_tp_native.py \
  --device "$H7_MACHINE_DEVICE" --target-database REPLACE_SYSBENCH_DATABASE \
  --target-db-node REPLACE_DATABASE_OID \
  --control-dsn 'dbname=postgres host=/tmp port=5432 user=omm application_name=huawei7_attribution' \
  --machine-fingerprint REPLACE_FINGERPRINT --trace-id sysbench-r1 \
  --benchmark sysbench --terminals 128 \
  --tp-command-json "$H7_EVIDENCE/tp/sysbench-r1-command.json" \
  --idle-seconds 30 --warmup-seconds 30 --measure-seconds 60 \
  --actual-shared-buffers-mb REPLACE_ACTUAL_SB \
  --out-dir "$H7_EVIDENCE/tp/sysbench-r1"
```

The command builder emits `huawei7.tp-command/v2`. For S1--S4 the topology is
one 128-terminal baseline driver. For S5, do not build a single 144-thread
driver. Build an explicit measurement-phase surge:

```bash
python3 scripts/build_tp_collection_command.py \
  --runtime-config config/stage_runtime.json --benchmark sysbench \
  --machine-fingerprint REPLACE_FINGERPRINT \
  --terminals 144 --surge-terminals 16 \
  --warmup-seconds 30 --measure-seconds 60 \
  --out-command "$H7_EVIDENCE/tp/sysbench-s5-r1-command.json"
```

This creates a 128-terminal baseline command lasting warmup plus measurement
and a separate 16-terminal command started by the collector exactly at the
measurement boundary. The collector reparses both raw outputs and sums their
transactions over the common window. A collapsed 144-terminal command is a
hard error.

For BenchBase, also pass `--benchbase-xml`, `--benchbase-result-dir` to the
command builder. S5 additionally requires `--surge-benchbase-xml` and
`--surge-benchbase-result-dir`; each result directory must yield exactly one
summary per collection. `--benchbase-summary-glob` remains only as a legacy
single-driver fallback. Its overhead run uses `--benchmark benchbase-tpcc`
and reads the command artifact's per-driver result directories. The pipeline
requires the benchmark-specific overhead artifact and the same command
contract ID; sysbench overhead cannot stand in for TPCC overhead.

Build the four empirical responses directly from the completed matrix:

```bash
python3 scripts/build_tp_models_from_matrix.py \
  --matrix-index "$H7_EVIDENCE/tp/matrix/matrix-index.json" \
  --data-dir "$H7_GAUSS_DATA" \
  --maximum-holdout-mape .20 \
  --out-dir "$H7_EVIDENCE/tp/native-models"
```

At every Sysbench SB point r01/r02 train and r03 is withheld; at every TPCC
point r01--r03 train and r04 is withheld. The model uses medians of training
only and piecewise-linear
interpolation strictly inside the measured 2/5/8 GiB interval. It gates TPS,
buffer accesses/transaction, read requests/transaction and read
bytes/transaction at 20% MAPE, plus shared-hit ratio at 0.02 absolute error.
These are precisely the empirical fields used downstream. TPCC checkpoint
writes remain in the evidence but are asynchronous and are not mapped onto
the fio TP axis, because that axis is explicitly calibrated with `randread`;
their TP-only cost is already present in empirical TPS. If paired idle
subtraction makes only that unused W increment negative, the artifact retains
the signed value and explicitly left-censors the physical count at zero. Read
requests remain fail-closed. Excluded `pNN` preconditioning runs are never
fitted.

Repeat the evidence chain for the topology actually modeled: one 128 baseline
for S1--S4, and a prewarmed 128 baseline plus a separate 16-terminal
measurement-boundary surge for S5, independently for Sysbench and TPCC. The
command contract, empirical response, overhead report and final episode must
share that topology. S5 cannot reuse an N=128 model or a collapsed N=144
driver.

## 6. Evaluate every native V3 candidate and freeze the historical baseline

The command below is intentionally the historical V3/native evaluator.  It
does **not** implement the strict PPT cache-replay/BIO/TPS fixed-point path.
For the version-6 PPT chain, use
`docs/PPT_CLOSED_LOOP_DEPLOYMENT.md` and
`scripts/run_ppt_pipeline_matrix.py` instead.

Evaluate all ten benchmark/stage combinations from the native model matrix:

```bash
python3 scripts/run_pipeline_matrix.py \
  --tp-model-matrix "$H7_EVIDENCE/tp/native-models/tp-model-matrix.json" \
  --tp-calibration-matrix "$H7_EVIDENCE/tp/matrix/matrix-index.json" \
  --ap-model-bundle "$H7_EVIDENCE/ap/ap-model-bundle.json" \
  --machine "$H7_EVIDENCE/machine.json" \
  --memory-budget "$H7_EVIDENCE/memory/memory-budget.json" \
  --fio-validation "$H7_EVIDENCE/fio/surface-set-v1.json" \
  --service-calibration "$H7_EVIDENCE/fio/service-times/service-times.json" \
  --data-dir "$H7_GAUSS_DATA" \
  --out-dir "$H7_EVIDENCE/pipeline-native" \
  --recommendations-out "$H7_EVIDENCE/five-stage-recommendations.json"
```

The native pipeline samples only the measured SB interval, interpolates the
accepted TP-only response, keeps the full AP work-memory Pareto frontier and
adds only the measured fio AP-contention delay. It never extrapolates queue
depth or SB. The measured memory artifact supplies
`tunable_pool = host - database_fixed - system_reserve`; AP runtime/request
holdouts, fio holdout, AP mix and every TP empirical holdout must already be
valid. Each result recursively binds the native collections, benchmark
summaries, database-counter evidence, observer source/raw files, AP
EXPLAIN/SQL and fio raw inputs by absolute path and SHA-256.

Freeze all ten results (two TP benchmarks times five stages) before running a
real stage:

```bash
python3 scripts/compile_stage_recommendations.py \
  --machine-fingerprint REPLACE_FINGERPRINT \
  --sysbench-s1 SYS_S1.json --sysbench-s2 SYS_S2.json \
  --sysbench-s3 SYS_S3.json --sysbench-s4 SYS_S4.json \
  --sysbench-s5 SYS_S5.json \
  --benchbase-tpcc-s1 TPCC_S1.json --benchbase-tpcc-s2 TPCC_S2.json \
  --benchbase-tpcc-s3 TPCC_S3.json --benchbase-tpcc-s4 TPCC_S4.json \
  --benchbase-tpcc-s5 TPCC_S5.json \
  --out "$H7_EVIDENCE/five-stage-recommendations.json"
```

## 7. Blind real five-stage validation

For reuse mode copy `config/stage_runtime.current.example.json`; for a fresh
load copy `config/stage_runtime.example.json`. Fill only paths/users/machine
fingerprint and keep passwords in the three named environment variables.
The AP, Sysbench and TPCC accounts are intentionally separate; sharing one
fallback credential across both TP databases is rejected. Create a JSON argv
array for the restart helper, for
example:

```json
[
  "runuser", "-u", "omm", "--", "python3",
  "/root/GaussTune/experiments/opengauss/huawei7/scripts/restart_with_shared_buffers.py",
  "--data-dir", "/opt/openGauss/data", "--gauss-home", "/opt/openGauss",
  "--shared-buffers-mb", "{shared_buffers_mb}"
]
```

The stage executor feeds the openGauss password to `gsql -2` through stdin.
For Sysbench it writes a mode-0600 config file on tmpfs, passes only that file
path in the process argv, and deletes it before successful or failed logs are
promoted. Do not put a password directly in an argv array or retained log.

Run both TP drivers, all exact PPT stages, and at least three repeats. The
runner freezes and records a seeded randomized 30-episode schedule and resumes
only hash-consistent completed episodes:

```bash
python3 scripts/run_five_stage_validation.py \
  --recommendations "$H7_EVIDENCE/five-stage-recommendations.json" \
  --runtime-config config/stage_runtime.json \
  --restart-command-json config/restart-command.json \
  --repeats 3 --warmup-seconds 30 --measure-seconds 120 \
  --maximum-stage-mape .20 \
  --out-root "$H7_EVIDENCE/five-stage-real"
```

After the A/A stability gate has passed, the normalized-state holdout must use
a new seed and output root. In addition to stable warmup, every BenchBase TPCC
episode reloads the same seeded 100-warehouse state, verifies its exact row
counts and database OID, cold-normalizes every audited workload database file
by exact OID during a clean stop, runs the adaptive TP-only tail gate, and
records a final CHECKPOINT/storage-quiescence barrier:

```bash
python3 scripts/run_five_stage_validation.py \
  --recommendations "$H7_EVIDENCE/five-stage-recommendations-native.json" \
  --runtime-config "$H7_EVIDENCE/stage_runtime.holdout_roles.json" \
  --restart-command-json "$H7_EVIDENCE/restart-command-normalized.json" \
  --repeats 3 --seed 817031 \
  --warmup-seconds 210 --measure-seconds 120 \
  --require-stable-warmup --warmup-sample-seconds 30 \
  --warmup-stability-windows 3 \
  --warmup-comparison-blocks 2 \
  --maximum-warmup-relative-span .20 \
  --maximum-warmup-relative-drift .10 \
  --tp-precondition-run-seconds 30 \
  --tp-precondition-minimum-runs 3 \
  --tp-precondition-maximum-runs 20 \
  --tp-precondition-tail-runs 3 \
  --maximum-tp-precondition-relative-range .10 \
  --checkpoint-command-json "$H7_EVIDENCE/checkpoint-command.json" \
  --dataset-reset-command-json "$H7_EVIDENCE/tpcc-reset-command.json" \
  --maximum-stage-mape .20 \
  --out-root "$H7_EVIDENCE/stable-holdout-seed-817031"
```

This mode produces schedule schema
`huawei7.five-stage-randomized-schedule/v2` and final schema
`huawei7.real-five-stage-validation/v4`. Resume accepts an episode only if its
summary, restart/cache record, and (for TPCC) complete
reset/precondition/quiescence chain rehash and recompute.

After the holdout completes, independently rehash and rescore it. This audit
separates reproducibility of the normalized state from model accuracy:

```bash
python3 scripts/audit_stable_holdout.py \
  --holdout "$H7_EVIDENCE/stable-holdout-seed-817031" \
  --stage-spec config/ppt_five_stages.json \
  --out "$H7_EVIDENCE/stable-holdout-seed-817031/normalized_state_holdout_audit.json"
```

`stability_valid=true` means all 30 episodes and all ten repeat groups rehash
and recompute under the stability gates. It does not imply model accuracy;
`accuracy_valid` records the declared median-error gate independently.

The runner verifies the restarted SB value, waits for the baseline driver's
warmup-complete marker, starts the separate S5 surge driver at that exact
measurement boundary, maintains one continuously occupied AP slot per stage
query, hashes every SQL/model/raw driver result, and reports predicted versus
median real TPS. Each episode writes TP/AP logs and BenchBase results to tmpfs
during the scored interval and promotes them only after all workload processes
stop. `valid=true` requires all 30 episodes, the exact frozen randomized
schedule/input hashes, and no stage median prediction error above the declared
threshold.

On the current host the frozen device-only recommendations were tested with
seed 90217 (30 episodes). A diagnostic joint-contention correction was then
built from that entire first matrix, frozen as v4 recommendations, and tested
independently with seed 63017 (another 30 episodes):

```bash
python3 scripts/build_joint_contention_calibration.py \
  --validation "$H7_EVIDENCE/five-stage-real/five_stage_validation.json" \
  --out "$H7_EVIDENCE/joint-contention-calibration-v1.json"

python3 scripts/apply_joint_contention_calibration.py \
  --recommendations "$H7_EVIDENCE/five-stage-recommendations.json" \
  --calibration "$H7_EVIDENCE/joint-contention-calibration-v1.json" \
  --out "$H7_EVIDENCE/five-stage-recommendations-v4.json"
```

These scripts are diagnostic, not evidence that the architecture passes.
The first matrix passed 2/10 stage medians and the independent second matrix
passed 4/10, so both final reports have `accuracy_valid=false`. See
`docs/CURRENT_RESULTS.md` for the exact errors and artifact hashes. The second
matrix is the final holdout and must not be fitted back into another correction.

## 8. What constitutes completion

Only after a five-stage runner succeeds should the complete-reproduction
audit recursively rehash and cross-check the entire retained evidence tree:

```bash
python3 scripts/audit_complete_reproduction.py \
  --doctor "$H7_EVIDENCE/doctor.json" \
  --fresh-doctor "$H7_EVIDENCE/fresh-machine-doctor.json" \
  --dataset-audit "$H7_EVIDENCE/dataset-audit.json" \
  --machine "$H7_EVIDENCE/machine.json" \
  --recommendations "$H7_EVIDENCE/five-stage-recommendations.json" \
  --final-validation "$H7_EVIDENCE/five-stage-real/five_stage_validation.json" \
  --out "$H7_EVIDENCE/complete-reproduction-audit.json"
```

The audit follows all ten model results back through their exact pipeline
configs, collections, traces, transaction evidence, memory, OS-cache, TP,
AP and fio artifacts; it then reparses all 30 stage summaries and rehashes
every raw TP/AP log. A missing, edited or cross-machine file makes the audit
fail. Only `complete-reproduction-audit.json` with `valid=true` constitutes a
complete reproduction.

Unit tests are necessary but never performance evidence. A complete
reproduction has all of the following:

- both doctors and the dataset audit valid;
- AP runtime/request and OS-cache disjoint holdouts valid;
- fio surface disjoint holdout valid and all candidates in-domain;
- four class-specific service times, each with at least three raw samples;
- both benchmark-specific TP sweeps/calibrations valid;
- ten model results frozen before any final-stage episode;
- 30 valid real episodes and accepted prediction accuracy;
- all raw artifacts and SHA-256 links retained under the evidence root.

If any item is absent, report it as pending. Do not reuse Huawei6 or the
component smoke evidence as a substitute.

The current host has complete raw execution evidence but does not meet the
accepted-prediction-accuracy item, so a successful complete-reproduction audit
must not be claimed for these results.

## 9. Normalize initial state before another final holdout

Database restart alone does not reset Linux file cache. For a stable protocol,
add every audited workload database OID to the restart command while retaining
the exact shared-buffer placeholder:

```json
[
  "runuser", "-u", "omm", "--", "env",
  "GAUSSHOME=/opt/openGauss", "LD_LIBRARY_PATH=/opt/openGauss/lib",
  "PATH=/opt/openGauss/bin:/usr/bin:/bin", "/usr/bin/python3",
  "scripts/restart_with_shared_buffers.py",
  "--data-dir", "/opt/openGauss/data", "--gauss-home", "/opt/openGauss",
  "--shared-buffers-mb", "{shared_buffers_mb}",
  "--evict-database-oid", "REPLACE_AP_OID",
  "--evict-database-oid", "REPLACE_SYSBENCH_OID",
  "--evict-database-oid", "REPLACE_TPCC_OID"
]
```

The helper performs a clean stop, advises only regular files below those exact
`pg_default` OID directories with `POSIX_FADV_DONTNEED`, starts openGauss, and
emits a machine-readable record. Symlinks, missing OIDs, partial OID sets and a
cache operation while the server is running are rejected.

TPCC is not read-only: NewOrder and Delivery advance `district.d_next_o_id`,
append orders/history and change the physical table state.  A cache reset cannot
undo that drift.  Before **every** TPCC repeat, replace only the nine dedicated
TPCC tables with the same warehouse count and random seed.  Put a command
template like the following in a private evidence directory; it contains an
environment-variable name, never a password value:

```json
[
  "/usr/bin/python3", "scripts/reset_benchbase_tpcc.py",
  "--runtime-config", "config/stage_runtime.json",
  "--gauss-home", "/opt/openGauss",
  "--owner-role", "REPLACE_TPCC_OWNER_ROLE",
  "--random-seed", "15721",
  "--minimum-free-bytes", "21474836480",
  "--confirm-replace-tables",
  "--out", "{reset_report}"
]
```

The reset keeps the audited database/OID, loads through the dedicated
password-authenticated role, restores table ownership, revokes temporary
schema CREATE, runs ANALYZE, verifies exact cardinalities and
`d_next_o_id=3001`, and enforces the free-space gate.  Its XML is a 0600 tmpfs
file and is removed on both success and failure.

TPCC additionally needs an adaptive TP-only precondition.  Each 30-second run
is followed by an explicit CHECKPOINT and storage-quiescence wait.  Continue
until the last three complete runs have relative range at most 10%; do not use
a fixed number of warmup runs.  The final stage warmup uses 30-second native
transaction windows.  Three tail windows therefore cover 90 seconds and do
not alias the short TPCC throughput cycle seen with 15-second samples.

First run A/A for the most unstable stages.  The formal runner defaults are
180 seconds of warmup, 30-second stability samples, and a 120-second scored
window; the values are shown explicitly here to freeze the protocol:

```bash
python3 scripts/run_stage_stability_aa.py \
  --recommendations "$H7_EVIDENCE/five-stage-recommendations.json" \
  --runtime-config config/stage_runtime.json \
  --restart-command-json config/restart-command-normalized.json \
  --benchmark benchbase-tpcc --stage S5 --repeats 3 \
  --warmup-seconds 180 --measure-seconds 120 \
  --warmup-sample-seconds 30 --warmup-stability-windows 3 \
  --tp-precondition-run-seconds 30 \
  --tp-precondition-minimum-runs 3 \
  --tp-precondition-maximum-runs 16 \
  --tp-precondition-tail-runs 3 \
  --maximum-tp-precondition-relative-range .10 \
  --checkpoint-command-json config/checkpoint-storage-command.json \
  --dataset-reset-command-json config/tpcc-reset-command.json \
  --out-root "$H7_EVIDENCE/stability/tpcc-s5"

python3 scripts/validate_stage_stability_aa.py \
  --report "$H7_EVIDENCE/stability/tpcc-s5/stability_report.json"
```

The baseline runs alone during warmup. A persistent native counter session is
closed before measurement. The last three transaction-rate windows must have
relative span ≤20% and first-to-last drift ≤10%; the three measured repeats
must have relative range ≤20% and CV ≤10%.  The v3 report binds and revalidates
every seeded reset, adaptive precondition run, checkpoint/quiescence record,
cache-normalization record and raw stage summary.  Only after password-auth
Sysbench and TPCC S1/S5 A/A pass should all ten stages be rerun.  The complete
audit rejects diagnostic local-peer transport.

On the current host, password-authenticated TPCC S1/S5 and Sysbench S1/S5
have all passed this three-repeat protocol.  Their reports are under
`validation/stability_20260817/` and are summarized with SHA-256 values in
`docs/STABILITY_RESULTS.md`.  This authorizes the next all-ten-stage holdout;
it does not retroactively make the frozen v1/v2 accuracy reports valid.  After
the boundary experiments, TPCC was reset once more to the same canonical
baseline and checkpointed to storage quiescence before the temporary login
roles and password environment variables were removed.
