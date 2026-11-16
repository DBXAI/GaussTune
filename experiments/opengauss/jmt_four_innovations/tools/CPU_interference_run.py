"""Opt-in future-window CPU scenario. No target TP counters used to forecast."""
import time

from collect_current_tp_matrix import dump
from collect_current_tp_surge import tids_for
from validate_capacity_TPS_live import DiagnosticRun
from jmt.innovation_4_performance.phase_bridge import task_phase_snapshot, cpu_rate_from_probe
from jmt.innovation_4_performance.cpu_interference import configuration_cpu_lanes


class CPUInterferenceRun(DiagnosticRun):
    def before_scored_window(self, stage, repeat, expected_ap, gate_surge, observer):
        if not expected_ap:
            return
        if gate_surge:
            raise ValueError("TP128 CPU calibration does not support S5 surge")
        # Same probes attached in prewindow and target. Commit window disarmed.
        observer.enroll(tids_for(self.client, self.tag+"_base"))
        begun = time.monotonic_ns()
        samples = {str(q): {"before": task_phase_snapshot(
            self.sb.pid, self.ap[q].identity["linux_tid"])} for q in expected_ap}
        self.wait(time.monotonic_ns()+4*10**9)
        for q in expected_ap:
            samples[str(q)]["after"] = task_phase_snapshot(
                self.sb.pid, self.ap[q].identity["linux_tid"])
        probe = {"started_ns": begun, "finished_ns": time.monotonic_ns(), "queries": samples}
        prediction_ns = time.monotonic_ns()
        phase = cpu_rate_from_probe(probe, prediction_ns=prediction_ns)
        started = time.perf_counter_ns()
        lanes = configuration_cpu_lanes(self.frozen, self.cpu_context,
                                       AP_cores=phase["AP_CPU_cores"])
        dump(self.out/("%s_r%02d.CPU_PREWINDOW.json" % (stage, repeat)), {
            "stage": stage, "repeat": repeat, "AP_probe": probe, "phase": phase,
            "lanes": lanes, "prediction_monotonic_ns": prediction_ns,
            "forecast_saved_monotonic_ns": time.monotonic_ns(),
            "prediction_compute_milliseconds": (time.perf_counter_ns()-started)/1e6,
            "observer_attached_during_probe": observer.bpf is not None,
            "commit_count_window_disarmed_during_probe": observer.interval is None,
            "target_window_TPS_or_CPU_used": False,
            "cold_start_configuration_forecast": False,
            "automatic_recommendation_qualified": False})
