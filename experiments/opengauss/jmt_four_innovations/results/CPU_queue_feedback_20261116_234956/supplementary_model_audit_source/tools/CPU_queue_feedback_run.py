"""Live baseline refresh before AP; no target label is used for prediction."""
import json
import time

from collect_current_tp_matrix import dump, digest
from validate_capacity_TPS_live import DiagnosticRun
from jmt.innovation_4_performance.live_tp_reference import reference_from_window
from jmt.innovation_4_performance.cpu_queue_feedback import configuration_lanes


class CPUQueueFeedbackRun(DiagnosticRun):
    def freeze_TP_control(self, stage, repeat):
        path = self.out/("%s_r%02d.WINDOW.json" % (stage, repeat))
        self.control_reference = reference_from_window(
            json.loads(path.read_text()), boot_id=self.frozen["boot_id"],
            postmaster_start_time=self.frozen["postmaster_start_time"],
            machine_fingerprint=self.frozen["machine_fingerprint"],
            source_path=str(path.relative_to(self.args.out)), source_sha256=digest(path),
            frozen_ns=time.monotonic_ns())
        dump(self.out/"TP_REFERENCE_FOR_NEXT_AP.json", self.control_reference)

    def augment_prediction(self, prediction):
        started = time.perf_counter_ns()
        prediction["CPU_queue_feedback"] = configuration_lanes(
            self.frozen, prediction, prediction_ns=time.monotonic_ns(),
            control_reference=self.control_reference)
        prediction["CPU_queue_feedback"]["control_reference"] = self.control_reference
        prediction["CPU_queue_feedback"]["compute_milliseconds"] = (
            time.perf_counter_ns()-started)/1e6
