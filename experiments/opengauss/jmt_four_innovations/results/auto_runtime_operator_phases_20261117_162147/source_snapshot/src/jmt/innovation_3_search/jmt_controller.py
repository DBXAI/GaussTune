"""JMT online controller: recognise stages, plan with the model, actuate.

Control loop (one ``tick`` per poll interval):

1. ingest AP requests from the gateway spool; identify each SQL by hash;
2. apply client cancellations and reap finished AP sessions;
3. observe workload features (TP terminals, demanded AP set);
4. when the debounced feature signature changes, ask the planner for
   ``(SB, per-query work_mem, admitted, queued)`` and apply SB first
   (shrink completes before new AP memory is granted);
5. launch waiting requests that the current plan admits, with the planned
   work_mem; everything else stays queued.

The controller never changes a running query's work_mem, never restarts the
database, and only uses the evidence-bound model to choose configurations.
The database is reached exclusively through a ``Backend`` object.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence

from jmt.innovation_3_search.jmt_planner import JmtPlanner
from jmt.innovation_3_search.workload_monitor import ActivityRow, StageDetector, StageSignature, count_tp_terminals, query_id_for_sql

WAITING = "waiting"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
REJECTED = "rejected"
ACTIVE_STATES = (WAITING, RUNNING)


@dataclass
class ApRequest:
    request_id: str
    sql_file: str
    submitted_at: float
    query_id: Optional[int]
    state: str = WAITING
    work_mem_mb: Optional[int] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    returncode: Optional[int] = None
    reason: str = ""
    handle: object = field(default=None, repr=False, compare=False)

    def as_status(self) -> Dict[str, object]:
        return {
            "schema": "huawei7.jmt-ap-request-status/v1",
            "request_id": self.request_id,
            "sql_file": self.sql_file,
            "query_id": self.query_id,
            "state": self.state,
            "work_mem_mb": self.work_mem_mb,
            "submitted_at": self.submitted_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "returncode": self.returncode,
            "reason": self.reason,
        }


class Backend:
    """Database/OS boundary.  Real and fake implementations subclass this."""

    def activity(self) -> Sequence[ActivityRow]:
        raise NotImplementedError

    def shared_buffers_target_mb(self) -> float:
        raise NotImplementedError

    def set_shared_buffers_target(self, target_mb: int) -> Mapping[str, object]:
        raise NotImplementedError

    def postmaster_pid(self) -> Optional[int]:
        raise NotImplementedError

    def launch_ap(self, request: ApRequest, work_mem_mb: int) -> object:
        raise NotImplementedError

    def poll_ap(self, handle: object) -> Optional[int]:
        raise NotImplementedError

    def stop_ap(self, handle: object) -> Optional[int]:
        raise NotImplementedError


def _write_json_atomic(path: Path, document: Mapping[str, object]) -> None:
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


class SpoolGateway:
    """File-based AP gateway: clients submit SQL, the controller decides.

    ``requests/<id>.json`` = ``{"request_id", "sql_file", "submitted_at"}``;
    ``cancel/<id>`` = cancel marker; ``status/<id>.json`` = controller state.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        for name in ("requests", "accepted", "cancel", "status", "logs"):
            (root / name).mkdir(parents=True, exist_ok=True)

    # Client side -------------------------------------------------------
    @staticmethod
    def submit(root: Path, request_id: str, sql_file: Path, submitted_at: float) -> None:
        if not request_id or "/" in request_id or request_id.startswith("."):
            raise ValueError("invalid request id")
        _write_json_atomic(root / "requests" / (request_id + ".json"), {
            "request_id": request_id,
            "sql_file": str(Path(sql_file).resolve()),
            "submitted_at": float(submitted_at),
        })

    @staticmethod
    def cancel(root: Path, request_id: str) -> None:
        (root / "cancel" / request_id).write_text("", encoding="utf-8")

    @staticmethod
    def read_status(root: Path, request_id: str) -> Optional[Mapping[str, object]]:
        path = root / "status" / (request_id + ".json")
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    # Controller side ---------------------------------------------------
    def take_requests(self) -> List[Mapping[str, object]]:
        rows = []
        for path in sorted((self.root / "requests").glob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            path.replace(self.root / "accepted" / path.name)
            rows.append(document)
        return sorted(rows, key=lambda row: (
            float(row["submitted_at"]), str(row["request_id"]),
        ))

    def take_cancels(self) -> List[str]:
        result = []
        for path in sorted((self.root / "cancel").iterdir()):
            if path.name.startswith("."):
                continue
            result.append(path.name)
            path.unlink()
        return result

    def write_status(self, request: ApRequest) -> None:
        _write_json_atomic(
            self.root / "status" / (request.request_id + ".json"),
            request.as_status(),
        )

    def heartbeat(self, document: Mapping[str, object]) -> None:
        _write_json_atomic(self.root / "controller-heartbeat.json", document)


class JmtController:
    def __init__(
        self, *, planner: JmtPlanner, backend: Backend, gateway: SpoolGateway,
        tp_database: str, stable_seconds: float, decision_log: Path,
        unmodeled_policy: str = "reject", default_work_mem_mb: int = 32,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if unmodeled_policy not in ("reject", "default_work_mem"):
            raise ValueError("unmodeled_policy must be reject or default_work_mem")
        self.planner = planner
        self.backend = backend
        self.gateway = gateway
        self.tp_database = tp_database
        self.detector = StageDetector(stable_seconds)
        self.decision_log = decision_log
        self.unmodeled_policy = unmodeled_policy
        self.default_work_mem_mb = int(default_work_mem_mb)
        self.clock = clock
        self.wall_clock = wall_clock
        self.requests: Dict[str, ApRequest] = {}
        # Queries the current plan permits to start, with their work_mem.
        self.allowed: Dict[int, int] = {}
        self.current_plan: Optional[Mapping[str, object]] = None
        self.initial_pid = backend.postmaster_pid()
        if self.initial_pid is None:
            raise RuntimeError("database postmaster is not running")
        self.initial_target_mb = backend.shared_buffers_target_mb()
        self.last_signature: Optional[StageSignature] = None

    # ------------------------------------------------------------------
    def _log(self, event: str, document: Mapping[str, object]) -> None:
        row = {"event": event, "wall_time": self.wall_clock(),
               "monotonic": self.clock()}
        row.update(document)
        with self.decision_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")

    def _set_state(self, request: ApRequest, state: str, reason: str = "") -> None:
        request.state = state
        if reason:
            request.reason = reason
        self.gateway.write_status(request)

    def _check_pid(self) -> None:
        pid = self.backend.postmaster_pid()
        if pid != self.initial_pid:
            raise RuntimeError(
                "postmaster PID changed: %s -> %s" % (self.initial_pid, pid)
            )

    # ------------------------------------------------------------------
    def _ingest(self) -> None:
        for row in self.gateway.take_requests():
            request_id = str(row["request_id"])
            if request_id in self.requests:
                continue
            sql_file = Path(str(row["sql_file"]))
            try:
                query_id = query_id_for_sql(
                    sql_file.read_bytes(), self.planner.query_hashes,
                )
            except OSError as error:
                query_id = None
                reason = "SQL file unreadable: %s" % error
            else:
                reason = "" if query_id is not None else "SQL hash not in AP model"
            request = ApRequest(
                request_id, str(sql_file), float(row["submitted_at"]), query_id,
            )
            self.requests[request_id] = request
            if query_id is None:
                if self.unmodeled_policy == "default_work_mem" and sql_file.is_file():
                    self._launch(request, self.default_work_mem_mb, modeled=False)
                    request.reason = reason + "; launched with default work_mem"
                    self.gateway.write_status(request)
                else:
                    self._set_state(request, REJECTED, reason)
                self._log("ap_unmodeled", {"request": request.as_status()})
                continue
            self.gateway.write_status(request)
            self._log("ap_request", {"request": request.as_status()})
        for request_id in self.gateway.take_cancels():
            request = self.requests.get(request_id)
            if request is None or request.state not in ACTIVE_STATES:
                continue
            if request.state == RUNNING:
                request.returncode = self.backend.stop_ap(request.handle)
            request.finished_at = self.wall_clock()
            self._set_state(request, CANCELLED, "client cancel")
            self._log("ap_cancel", {"request": request.as_status()})

    def _reap(self) -> None:
        for request in self.requests.values():
            if request.state != RUNNING:
                continue
            code = self.backend.poll_ap(request.handle)
            if code is None:
                continue
            request.returncode = int(code)
            request.finished_at = self.wall_clock()
            self._set_state(request, DONE if code == 0 else FAILED)
            self._log("ap_finish", {"request": request.as_status()})

    def _signature(self) -> StageSignature:
        terminals = count_tp_terminals(
            self.backend.activity(), tp_database=self.tp_database,
        )
        demanded = sorted({
            int(request.query_id) for request in self.requests.values()
            if request.state in ACTIVE_STATES and request.query_id is not None
        })
        return StageSignature(terminals, tuple(demanded))

    def _running_work_mem(self) -> Dict[int, int]:
        result: Dict[int, int] = {}
        for request in self.requests.values():
            if (
                request.state == RUNNING and request.query_id is not None
                and request.work_mem_mb is not None
            ):
                result[int(request.query_id)] = int(request.work_mem_mb)
        return result

    def _waiting_order(self, running: Mapping[int, int]) -> List[int]:
        order: List[int] = []
        for request in sorted(
            self.requests.values(),
            key=lambda row: (row.submitted_at, row.request_id),
        ):
            if (
                request.state == WAITING and request.query_id is not None
                and request.query_id not in running
                and request.query_id not in order
            ):
                order.append(int(request.query_id))
        return order

    def _replan(self, signature: StageSignature) -> None:
        running = self._running_work_mem()
        waiting = self._waiting_order(running)
        try:
            plan = self.planner.plan(
                observed_terminals=signature.tp_terminals,
                running=running, waiting=waiting,
            )
        except (OSError, RuntimeError, ValueError) as error:
            self.allowed = dict(running)
            self._log("plan_error", {
                "signature": signature.as_dict(), "error": str(error),
            })
            return
        record: Dict[str, object] = {
            "stage_index": self.detector.stage_index,
            "signature": signature.as_dict(),
            "plan": plan,
        }
        if plan.get("status") != "planned":
            # Out of the calibrated domain or no model candidate: keep the
            # current SB and admit nothing new (fail closed).
            self.allowed = dict(running)
            record["action"] = "hold"
            self._log("plan", record)
            self.current_plan = plan
            return
        target_mb = int(plan["shared_buffers_mb"])
        current_mb = self.backend.shared_buffers_target_mb()
        if abs(current_mb - target_mb) > 1e-6:
            started = self.clock()
            try:
                record["resize"] = dict(self.backend.set_shared_buffers_target(target_mb))
            except (OSError, RuntimeError, TimeoutError) as error:
                # Without a confirmed shrink, granting new AP memory could
                # exceed the pool.  Only already-running queries may restart.
                self.allowed = dict(running)
                record.update({"action": "hold", "resize_error": str(error)})
                self._log("plan", record)
                self.current_plan = plan
                return
            record["resize_seconds"] = self.clock() - started
            self._check_pid()
        work_mem = {int(query): int(memory) for query, memory in plan["work_mem"].items()}  # type: ignore[union-attr]
        permitted = set(int(query) for query in plan["admitted"]) | set(running)  # type: ignore[union-attr]
        self.allowed = {query: work_mem[query] for query in permitted if query in work_mem}
        record["action"] = "apply"
        record["allowed_work_mem"] = {str(key): value for key, value in sorted(self.allowed.items())}
        self._log("plan", record)
        self.current_plan = plan

    def _launch(self, request: ApRequest, work_mem_mb: int, *, modeled: bool = True) -> None:
        request.handle = self.backend.launch_ap(request, work_mem_mb)
        request.work_mem_mb = int(work_mem_mb)
        request.started_at = self.wall_clock()
        request.state = RUNNING
        if not modeled:
            request.reason = request.reason or "unmodeled"
        self.gateway.write_status(request)
        self._log("ap_start", {"request": request.as_status()})

    def _launch_ready(self) -> None:
        running_ids = {
            request.query_id for request in self.requests.values()
            if request.state == RUNNING
        }
        for request in sorted(
            self.requests.values(),
            key=lambda row: (row.submitted_at, row.request_id),
        ):
            if request.state != WAITING or request.query_id is None:
                continue
            if request.query_id in running_ids or request.query_id not in self.allowed:
                continue
            self._launch(request, self.allowed[request.query_id])
            running_ids.add(request.query_id)

    # ------------------------------------------------------------------
    def tick(self) -> Optional[StageSignature]:
        self._ingest()
        self._reap()
        signature = self._signature()
        self.last_signature = signature
        changed = self.detector.observe(signature, self.clock())
        if changed is not None:
            self._log("stage_change", {
                "stage_index": self.detector.stage_index,
                "signature": changed.as_dict(),
            })
            self._replan(changed)
        self._launch_ready()
        self.gateway.heartbeat({
            "schema": "huawei7.jmt-controller-heartbeat/v1",
            "wall_time": self.wall_clock(),
            "pid": os.getpid(),
            "stage_index": self.detector.stage_index,
            "observed_signature": signature.as_dict(),
            "allowed_work_mem": {str(key): value for key, value in sorted(self.allowed.items())},
        })
        return changed

    def shutdown(self, *, restore_target: bool = True) -> Dict[str, object]:
        """Stop controller-owned AP sessions and restore the initial SB target."""

        stopped = []
        for request in self.requests.values():
            if request.state == RUNNING:
                request.returncode = self.backend.stop_ap(request.handle)
                request.finished_at = self.wall_clock()
                self._set_state(request, CANCELLED, "controller shutdown")
                stopped.append(request.request_id)
        restored = None
        if restore_target:
            current = self.backend.shared_buffers_target_mb()
            if abs(current - self.initial_target_mb) > 1e-6:
                restored = dict(self.backend.set_shared_buffers_target(
                    int(round(self.initial_target_mb)),
                ))
        document = {
            "stopped_ap_requests": stopped,
            "initial_target_mb": self.initial_target_mb,
            "restored": restored,
            "postmaster_pid": self.backend.postmaster_pid(),
            "initial_postmaster_pid": self.initial_pid,
        }
        self._log("shutdown", document)
        return document
