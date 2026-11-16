import importlib.util
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from jmt.innovation_1_cache.cache_boundary import (
    derive_boundary, derive_miss_plateau_boundary, load_boundary, SCHEMA, LEGACY_SCHEMA,
)
from jmt.innovation_3_search.current_joint import evaluate_current_joint
from jmt.innovation_3_search.search import QueryOption

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("replay_cache_boundary",
                                         str(ROOT/"tools/replay_tp_cache_boundary.py"))
runner=importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
from collect_current_tp_matrix import OnlineSB, CPUWindowBoundaryError, validate_cpu_window


def curve(values):
    return [{"shared_buffers_mib":sb,"p_sb":hit,"p_os":(1-hit)*.8,
             "p_disk":(1-hit)*.2} for sb,hit in values]


class CacheBoundaryTests(unittest.TestCase):
    def test_CPU_window_endpoint_timing_validated_without_rescaling(self):
        row={"measurement_window":{"start_ns":10**9,"end_ns":11*10**9},
             "CPU_snapshot_before":{"started_ns":10**9+200000,"finished_ns":10**9+3000000},
             "CPU_snapshot_after":{"started_ns":11*10**9+200000,"finished_ns":11*10**9+3000000}}
        validate_cpu_window(row)
        row["CPU_snapshot_before"]={"started_ns":10**9+499479825,"finished_ns":10**9+502000000}
        with self.assertRaises(CPUWindowBoundaryError):
            validate_cpu_window(row)

    def test_CPU_window_rejects_slow_snapshot_and_end_offset(self):
        row={"measurement_window":{"start_ns":10**9,"end_ns":11*10**9},
             "CPU_snapshot_before":{"started_ns":10**9,"finished_ns":10**9+100000000},
             "CPU_snapshot_after":{"started_ns":11*10**9,"finished_ns":11*10**9}}
        with self.assertRaises(CPUWindowBoundaryError):
            validate_cpu_window(row)
        row["CPU_snapshot_before"]["finished_ns"]=10**9
        row["CPU_snapshot_after"]["started_ns"]+=100000001
        row["CPU_snapshot_after"]["finished_ns"]=row["CPU_snapshot_after"]["started_ns"]
        with self.assertRaises(CPUWindowBoundaryError):
            validate_cpu_window(row)

    def test_resize_waits_for_session_guc_propagation_after_physical_commit(self):
        sb=OnlineSB.__new__(OnlineSB)
        sb.original_status={"postmaster_start_time":"same","maximum_buffers":1048576,
                            "target_buffers":1048576}
        old={**sb.original_status,"target_buffers":1024*128}
        new={**sb.original_status,"target_buffers":1536*128}
        with patch.object(sb,"status",side_effect=[old,new]),patch("time.sleep"):
            status,observations=sb.wait_status_target(1536)
        self.assertEqual(status,new)
        self.assertEqual(len(observations),2)

    def test_resize_never_accepts_nonconvergent_guc_value(self):
        sb=OnlineSB.__new__(OnlineSB)
        sb.original_status={"postmaster_start_time":"same","maximum_buffers":1048576,
                            "target_buffers":1048576}
        with patch.object(sb,"status",return_value=sb.original_status):
            with self.assertRaises(RuntimeError):
                sb.wait_status_target(1536,seconds=0)

    def test_resize_allows_propagation_after_five_seconds_but_stays_bounded(self):
        sb=OnlineSB.__new__(OnlineSB)
        sb.original_status={"postmaster_start_time":"same","maximum_buffers":1048576,
                            "target_buffers":1048576}
        new={**sb.original_status,"target_buffers":1536*128}
        with patch.object(sb,"status",side_effect=[sb.original_status,new]), \
                patch("time.monotonic",side_effect=[0,6]),patch("time.sleep"):
            status,observations=sb.wait_status_target(1536)
        self.assertEqual(status,new)
        self.assertEqual(len(observations),2)
        with patch.object(sb,"status",return_value=sb.original_status), \
                patch("time.monotonic",side_effect=[0,31]):
            with self.assertRaisesRegex(RuntimeError,"did not converge"):
                sb.wait_status_target(1536)

    def test_resize_rejects_changed_identity_even_when_target_matches(self):
        sb=OnlineSB.__new__(OnlineSB)
        sb.original_status={"postmaster_start_time":"same","maximum_buffers":1048576,
                            "target_buffers":1048576}
        for changes in ({"postmaster_start_time":"restarted"},{"maximum_buffers":524288}):
            status={**sb.original_status,"target_buffers":1536*128,**changes}
            with patch.object(sb,"status",return_value=status):
                with self.assertRaisesRegex(RuntimeError,"identity/maximum"):
                    sb.wait_status_target(1536)

    def test_bound_uses_simulated_SB_hits_not_TPS_or_OS_hits(self):
        rows=curve([(128,.8),(256,.985),(512,.995),(1024,.999)])
        for r in rows:
            r["TPS"]=1e9 if r["shared_buffers_mib"]==1024 else 1
        self.assertEqual(derive_boundary(rows)["B_high_mib"],512)

    def test_noisy_spike_does_not_cut_off_unsaturated_tail(self):
        self.assertEqual(derive_boundary(
            curve([(128,.8),(256,.999),(512,.97),(1024,.999)]))["B_high_mib"],1024)

    def test_left_censoring_is_explicit_not_exact_knee(self):
        d=derive_boundary(curve([(128,.995),(256,.996),(512,.997)]))
        self.assertTrue(d["left_censored"])

    def test_fraction_is_relative_to_curve_max_not_absolute_99_percent(self):
        d=derive_boundary(curve([(128,.8),(256,.89),(512,.895),(1024,.9)]))
        self.assertEqual(d["B_high_mib"],512)
        self.assertAlmostEqual(d["saturation_threshold"],.891)

    def test_boolean_fraction_is_not_a_numeric_policy(self):
        with self.assertRaises(ValueError):
            derive_boundary(curve([(128,.8),(256,.9),(512,.999)]),True)

    def test_invalid_paths_or_state_anomalies_refused(self):
        rows=curve([(128,.8),(256,.9),(512,.999)])
        rows[0]["p_disk"]=1
        with self.assertRaises(ValueError):derive_boundary(rows)
        rows=curve([(128,.8),(256,.9),(512,.999)])
        rows[0]["measured_state_anomalies"]=1
        with self.assertRaises(ValueError):derive_boundary(rows)

    def test_search_really_excludes_SB_above_cap_before_predicting(self):
        called=[]
        def predict(sb,base,state):
            called.append(sb)
            return {"predicted_tps":float(sb)}
        result=evaluate_current_joint(
            options_by_query={1:[QueryOption(1,64,64,0,0,10,"same",1)]},
            baseline_rows=[{"shared_buffers_mib":x} for x in (256,512,1024,2048)],
            total_pool_mib=8192,predict_tp=predict,sb_upper_bound_mib=512)
        self.assertEqual(result["selected"]["shared_buffers_mb"],512)
        self.assertEqual(set(called),{256,512})
        self.assertEqual(result["SB_candidates_excluded_by_simulated_bound"],[1024,2048])

    def test_empty_measured_domain_not_replaced_by_2GB(self):
        result=evaluate_current_joint(
            options_by_query={1:[QueryOption(1,64,64,0,0,10,"same",1)]},
            baseline_rows=[{"shared_buffers_mib":x} for x in (2048,4096,8192)],
            total_pool_mib=16384,predict_tp=lambda*a:{"predicted_tps":7500},
            sb_upper_bound_mib=512)
        self.assertIsNone(result["selected"])
        self.assertEqual(result["reason"],"no_calibrated_SB_within_simulated_cache_bound")


class MissPlateauBoundaryTests(unittest.TestCase):
    def test_first_small_gain_chooses_right_endpoint_four_not_three(self):
        rows=curve([(1024,.985888941),(1536,.994755182),(2048,.998644412),
                    (3072,.999938314),(4096,.999945837),(5120,.999963891)])
        result=derive_miss_plateau_boundary(rows)
        self.assertEqual(result["B_high_mib"],4096)
        self.assertEqual(result["plateau_interval_mib"],[3072,4096])
        self.assertAlmostEqual(result["selected_interval"]["miss_reduction_per_10000_per_gib"],
                               .07523,places=5)
        self.assertTrue(result["hard_pruning_supported_by_curve"])

    def test_S5_relative_miss_drop_can_be_large_but_absolute_gain_small(self):
        result=derive_miss_plateau_boundary(curve([
            (1024,.986120275),(2048,.998799575),(3072,.999974037),
            (4096,.999983200),(5120,.999987782)]))
        self.assertEqual(result["B_high_mib"],4096)

    def test_rule_not_hardcoded_to_four_GiB(self):
        result=derive_miss_plateau_boundary(curve([
            (1024,.8),(2048,.9),(8192,.999),(10240,.99901),(12288,.99902)]))
        self.assertEqual(result["B_high_mib"],10240)
        self.assertEqual(result["plateau_interval_mib"],[8192,10240])

    def test_dense_grid_normalized_by_actual_GiB_increment(self):
        result=derive_miss_plateau_boundary(curve([
            (1024,.9),(1124,.90005),(1224,.90010),(2248,.90011)]))
        self.assertEqual(result["B_high_mib"],2248)
        self.assertGreater(result["intervals"][0]["miss_reduction_per_10000_per_gib"],1.)

    def test_OS_path_and_TPS_do_not_enter_miss_plateau(self):
        rows=curve([(1024,.9),(2048,.99),(3072,.999),(4096,.99901)])
        expected=derive_miss_plateau_boundary(rows)
        for row in rows:
            row.update(p_os=0.,p_disk=1-row["p_sb"],TPS=1e9)
        self.assertEqual(derive_miss_plateau_boundary(rows),expected)

    def test_no_plateau_rejected_instead_of_inventing_endpoint(self):
        with self.assertRaisesRegex(ValueError,"not bracketed"):
            derive_miss_plateau_boundary(curve([(1024,.7),(2048,.8),(3072,.9)]))

    def test_first_interval_plateau_is_left_censored(self):
        result=derive_miss_plateau_boundary(curve([(1024,.99),(2048,.99001),(3072,.99002)]))
        self.assertTrue(result["left_censored"])
        self.assertFalse(result["hard_pruning_supported_by_curve"])

    def test_later_large_gain_disqualifies_first_plateau_for_hard_pruning(self):
        result=derive_miss_plateau_boundary(curve([
            (1024,.9),(2048,.99),(3072,.99001),(4096,.999)]))
        self.assertEqual(result["B_high_mib"],3072)
        self.assertFalse(result["hard_pruning_supported_by_curve"])
        self.assertEqual(len(result["later_significant_miss_reductions"]),1)

    def test_threshold_is_explicit_parameter_and_invalid_values_rejected(self):
        rows=curve([(1024,.9),(2048,.99),(3072,.999),(4096,.99901)])
        self.assertEqual(derive_miss_plateau_boundary(rows,.1)["B_high_mib"],4096)
        for value in (True,False,0,-1,float("nan"),float("inf"),"1"):
            with self.assertRaises(ValueError):
                derive_miss_plateau_boundary(rows,value)

    def fixture(self,directory,legacy=False):
        path=Path(directory)/"boundary.json"
        rows=curve([(1024,.9),(2048,.99),(3072,.999),(4096,.99901)])
        bound=derive_boundary(rows) if legacy else derive_miss_plateau_boundary(rows)
        document={"schema":LEGACY_SCHEMA if legacy else SCHEMA,
                  "machine_fingerprint":"unit","TP_profile":"tp",
                  "trace_integrity_verified":True,
                  "reference_accelerator_equivalence_verified":True,
                  "topologies":{name:{"curve":rows,"boundary":bound,
                                     "actual_capacity_validation":{"valid":True}}
                                for name in ("TP128","S5_128_plus_16")},
                  "source_artifacts":[{"path":"source.txt","sha256":
                      "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"}]}
        (Path(directory)/"source.txt").write_text("abc")
        path.write_text(json.dumps(document))
        return path

    def test_loader_requires_new_rule_for_current_recommendations(self):
        with tempfile.TemporaryDirectory() as directory:
            path=self.fixture(directory,True)
            load_boundary(path,machine_fingerprint="unit",profile="tp")
            with self.assertRaisesRegex(ValueError,"requires SB-miss"):
                load_boundary(path,machine_fingerprint="unit",profile="tp",require_miss_plateau=True)
            path=self.fixture(directory)
            checked=load_boundary(path,machine_fingerprint="unit",profile="tp",require_miss_plateau=True)
            self.assertEqual(checked["topologies"]["TP128"]["boundary"]["B_high_mib"],4096)

    def test_loader_recomputes_bound_and_rejects_missing_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            path=self.fixture(directory)
            document=json.loads(path.read_text())
            document["topologies"]["TP128"]["boundary"]["B_high_mib"]=3072
            path.write_text(json.dumps(document))
            with self.assertRaises(ValueError):
                load_boundary(path,machine_fingerprint="unit",profile="tp",require_miss_plateau=True)
            path=self.fixture(directory)
            document=json.loads(path.read_text());document["source_artifacts"]=[]
            path.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError,"source evidence"):
                load_boundary(path,machine_fingerprint="unit",profile="tp",require_miss_plateau=True)


class AcceleratorDifferentialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(prefix="jmt-replay-unit-")
        cls.directory=Path(cls.tmp.name)
        cls.binary=cls.directory/"replay"
        runner.compile_accelerator(cls.binary)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_seeded_random_normal_read_trace_matches_existing_python(self):
        import numpy as np
        rng=random.Random(7211)
        rows=[]
        # Include supplemental pins, victim pins, a skewed working set and
        # arbitrary physical buffer IDs. Observed hit is a diagnostic only.
        for n in range(3000):
            p=rng.randrange(150 if rng.random()<.8 else 800)
            tid=101+rng.randrange(4);t=n*10
            rows.extend([(t,tid,1,p,n%81,int(rng.random()<.9),0),
                         (t+1,tid,2,p,n%81,-1,0),
                         (t+2,tid,8,p,n%81,rows[-1][5] if rows else 0,0),
                         (t+3,tid,5,p,n%81,-1,0)])
            # Same observed flag in access/return, while neither controls hit.
            rows[-2]=tuple(list(rows[-2][:5])+[rows[-4][5],0])
        array=np.array(rows,dtype=runner.COMPACT)
        runner.verify_reference(self.binary,self.directory/"not-used",array,
                                10000,8192,2048,4,[128,512,2048],self.directory)

    def test_target_dirty_or_ring_input_is_rejected(self):
        import numpy as np
        data=np.zeros(1,dtype=runner.RECORD_DTYPE)
        data["db_node"]=23;data["kind"]=1;data["strategy_type"]=1
        with self.assertRaises(ValueError):
            runner.compact_events(data,23,self.directory/"unsupported.events")
        data["kind"]=6
        with self.assertRaises(ValueError):
            runner.compact_events(data,23,self.directory/"unsupported.events")

    def test_observed_hit_labels_cannot_change_counterfactual_hits(self):
        import numpy as np
        rows=[]
        for n in range(1200):
            p=n%50
            rows.extend([(n*4,101,1,p,p,0,0),(n*4+1,101,8,p,p,0,0),
                         (n*4+2,101,5,p,p,-1,0)])
        array=np.array(rows,dtype=runner.COMPACT)
        p=self.directory/"label-invariance.events"
        array.tofile(str(p))
        a=runner.accelerate(self.binary,p,10000,8192,2048,4,[(100,4000),(4000,4800)],[128,512])
        array["observed"][array["kind"]==1]=1
        array["observed"][array["kind"]==8]=1
        array.tofile(str(p))
        b=runner.accelerate(self.binary,p,10000,8192,2048,4,[(100,4000),(4000,4800)],[128,512])
        for x,y in zip(a["rows"],b["rows"]):
            for u,v in zip(x["windows"],y["windows"]):
                self.assertEqual({k:u[k] for k in ("accesses","sb_hits","os_hits","disk")},
                                 {k:v[k] for k in ("accesses","sb_hits","os_hits","disk")})


if __name__=="__main__":
    unittest.main()
