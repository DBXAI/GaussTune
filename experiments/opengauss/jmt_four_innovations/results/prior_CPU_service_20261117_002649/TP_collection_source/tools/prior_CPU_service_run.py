"""Two sequential one-shot BPF windows: 4s prior probe, then 20s future target.

Never attach two observers concurrently. The short probe's CPU and commits
are prediction inputs, not labels for the later target window.
"""
import json
import time

from CPU_queue_feedback_run import CPUQueueFeedbackRun
from collect_current_tp_matrix import dump,digest
from measure_current_mixed_five_stages import Run
from validate_capacity_TPS_live import DiagnosticRun
from jmt.innovation_4_performance.prior_cpu_service import predict_prior_service


class PriorCPUServiceRun(CPUQueueFeedbackRun):
    in_probe = False
    last_probe_end_ns = None

    def before_scored_window(self,stage,repeat,expected_ap,gate_surge,observer):
        if not expected_ap or self.in_probe:
            return
        if gate_surge or self.last_probe_end_ns is None:
            raise ValueError("loaded TP128 probe required, no S5 extrapolation")
        if time.monotonic_ns()-self.last_probe_end_ns > 12*10**9:
            raise ValueError("prior CPU service probe too old before target arming")

    def window(self,stage,repeat,expected_ap,gate_surge=False):
        if not expected_ap:
            return DiagnosticRun.window(self,stage,repeat,expected_ap,gate_surge)
        policy = self.frozen["prior_CPU_service_policy"]
        original = self.args.window_seconds
        probe_stage = "PRIOR_CPU_PROBE_"+stage
        try:
            self.in_probe = True
            self.args.window_seconds = policy["probe_seconds"]
            Run.window(self,probe_stage,repeat,expected_ap,gate_surge)
        finally:
            self.in_probe = False
            self.args.window_seconds = original
        path = self.out/("%s_r%02d.WINDOW.json"%(probe_stage,repeat))
        probe = json.loads(path.read_text())
        self.last_probe_end_ns = probe["measurement_window"]["end_ns"]
        started = time.perf_counter_ns()
        cpu_started = time.process_time_ns()
        prediction = predict_prior_service(
            reference=self.control_reference,probe=probe,prediction_ns=time.monotonic_ns(),
            AP_task_identities={str(q):self.ap[q].identity for q in expected_ap},
            cpu_capacity=self.frozen["calibration"]["cpu_capacity"],
            probe_max_age_seconds=policy["probe_max_age_at_prediction_seconds"],
            control_max_age_seconds=policy["control_max_age_seconds"])
        prediction.update({
            "probe_relative_path":path.name,"probe_sha256":digest(path),
            "control_reference":self.control_reference,
            "forecast_saved_monotonic_ns":time.monotonic_ns(),
            "prediction_compute_milliseconds":(time.perf_counter_ns()-started)/1e6,
            "prediction_process_CPU_milliseconds":(time.process_time_ns()-cpu_started)/1e6,
            "prior_observer_closed_before_target_observer_attached":True})
        dump(self.out/("%s_r%02d.PRIOR_CPU_FORECAST.json"%(stage,repeat)),prediction)
        return DiagnosticRun.window(self,stage,repeat,expected_ap,gate_surge)
