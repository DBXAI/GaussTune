from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
import unittest
from unittest.mock import patch

from jmt.common.runtime_discovery import (
    calibration_binding, cgroup_directories, cgroup_limits, cpu_list, digest,
    mount_rows, validate_binding, discover, memory_setting_bytes)


class RuntimeDiscoveryTests(unittest.TestCase):
    def test_cpu_ranges_sparse_and_bounded(self):
        self.assertEqual(cpu_list("0-2,8,10-11"), {0, 1, 2, 8, 10, 11})
        self.assertEqual(cpu_list(""), set())
        for text in ("3-1", "-1", "0-1000000", "0,,1", "x", "1-2-3"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                cpu_list(text)

    def test_mount_escapes_and_device_identity(self):
        rows = mount_rows("31 1 259:3 / /data\\040space rw - ext4 /dev/nvme0n1p3 rw")
        self.assertEqual(rows[0]["mountpoint"], "/data space")
        self.assertEqual(rows[0]["major_minor"], "259:3")

    def test_v2_ancestor_minimum_not_only_leaf(self):
        mounts = mount_rows("1 0 0:1 / /cg rw - cgroup2 cgroup rw")
        values = {
            "/cg/team/worker/cpu.max": "200000 100000",
            "/cg/team/cpu.max": "150000 100000",
            "/cg/cpu.max": "max 100000",
            "/cg/team/worker/memory.max": "max",
            "/cg/team/memory.max": "1073741824",
            "/cg/team/memory.current": "536870912",
            "/cg/team/cpuset.cpus.effective": "0-3",
            "/cg/team/worker/cpuset.cpus.effective": "2-5",
        }
        result = cgroup_limits("0::/team/worker", mounts, lambda p: values.get(str(p)))
        self.assertEqual(result["cpu_quota_cores"], 1.5)
        self.assertEqual(result["memory_limit_bytes"], 1073741824)
        self.assertEqual(result["memory_headroom_bytes_snapshot"], 536870912)
        self.assertEqual(result["cpuset_cpus"], [2, 3])
        self.assertEqual(result["ancestor_directories_checked"], 3)

    def test_v1_nonroot_mount_mapping_and_unlimited(self):
        mounts = mount_rows(
            "1 0 0:1 /team /cpu rw - cgroup cgroup rw,cpu,cpuacct\n"
            "2 0 0:2 /team /mem rw - cgroup cgroup rw,memory")
        groups = "1:cpu,cpuacct:/team/x\n2:memory:/team/x"
        directories = cgroup_directories(groups, mounts)
        self.assertEqual([str(x[2]) for x in directories], ["/cpu/x", "/mem/x"])
        values = {
            "/cpu/x/cpu.cfs_quota_us": "-1", "/cpu/x/cpu.cfs_period_us": "100000",
            "/cpu/cpu.cfs_quota_us": "250000", "/cpu/cpu.cfs_period_us": "100000",
            "/mem/x/memory.limit_in_bytes": "9223372036854771712",
            "/mem/memory.limit_in_bytes": "2147483648",
        }
        limits = cgroup_limits(groups, mounts, lambda p: values.get(str(p)))
        self.assertEqual(limits["cpu_quota_cores"], 2.5)
        self.assertEqual(limits["memory_limit_bytes"], 2147483648)

    def test_cgroup_namespace_mismatch_reported_not_silently_unlimited(self):
        mounts = mount_rows("1 0 0:1 /other /cg rw - cgroup2 cgroup rw")
        result = cgroup_limits("0::/team", mounts, lambda p: None)
        self.assertTrue(result["unmapped_membership"])

    def profile(self):
        return {
            "local_server_hardware_verified": True, "calibration_machine_key": digest("host"),
            "resources": {"resource_constraints_verified": True},
            "boot_id": "boot", "database": {"postmaster_start_time": "start"},
            "collected_at": datetime(2026, 11, 17, tzinfo=timezone.utc).isoformat()}

    def test_machine_binding_invalidates_on_machine_boot_instance_and_workload(self):
        profile = self.profile()
        binding = calibration_binding(profile, workload_sha256="dataset", kind="AP", lifetime_seconds=60)
        now = datetime.fromisoformat(profile["collected_at"]) + timedelta(seconds=30)
        self.assertTrue(validate_binding(binding, profile, workload_sha256="dataset", kind="AP", now=now))
        for field in ("machine_key", "boot_id", "postmaster_start_time",
                      "database_resource_settings_key", "workload_sha256", "kind"):
            bad = dict(binding, **{field: "other"})
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_binding(bad, profile, workload_sha256="dataset", kind="AP", now=now)

    def test_future_stale_and_naive_timestamp_rejected(self):
        p = self.profile()
        binding = calibration_binding(p, workload_sha256="x", kind="TP", lifetime_seconds=60)
        created = datetime.fromisoformat(p["collected_at"])
        for now in (created - timedelta(seconds=1), created + timedelta(seconds=61)):
            with self.assertRaises(ValueError):
                validate_binding(binding, p, workload_sha256="x", kind="TP", now=now)
        binding["created_at"] = "2026-11-17T00:00:00"
        with self.assertRaises(ValueError):
            validate_binding(binding, p, workload_sha256="x", kind="TP", now=created)

    def test_remote_profile_cannot_bind_client_hardware(self):
        p = self.profile()
        p["local_server_hardware_verified"] = False
        with self.assertRaises(ValueError):
            calibration_binding(p, workload_sha256="x", kind="AP", lifetime_seconds=60)

    def test_missing_or_unverified_resource_domain_cannot_bind_calibration(self):
        for value in (None, False):
            p = self.profile()
            p["resources"] = {"resource_constraints_verified": value}
            with self.assertRaises(ValueError):
                calibration_binding(p, workload_sha256="x", kind="AP", lifetime_seconds=60)

    def test_multi_instance_requires_selection_before_connect(self):
        with patch("jmt.common.runtime_discovery.local_instances", return_value=[
                {"pid": 1}, {"pid": 2}]), patch("jmt.common.runtime_discovery.Gsql") as client:
            with patch.dict("os.environ", {}, clear=True), self.assertRaises(ValueError):
                discover("db")
            client.assert_not_called()

    def test_remote_host_does_not_enumerate_local_server(self):
        with patch("jmt.common.runtime_discovery.local_instances") as instances:
            with patch.dict("os.environ", {}, clear=True), self.assertRaises(ValueError):
                discover("db", host="remote.example", pid=1)
            instances.assert_not_called()

    def test_digest_order_independent(self):
        self.assertEqual(digest({"a": 1, "b": 2}), digest({"b": 2, "a": 1}))

    def test_database_memory_units_not_assumed_to_all_be_kib(self):
        self.assertEqual(memory_setting_bytes({"setting": "1048576", "unit": "8kB"}), 8 * 1024**3)
        self.assertEqual(memory_setting_bytes({"setting": "1024", "unit": "kB"}), 1024**2)
        self.assertEqual(memory_setting_bytes({"setting": "8192", "unit": "B"}), 8192)
        self.assertIsNone(memory_setting_bytes({"setting": "1", "unit": None}))


if __name__ == "__main__":
    unittest.main()
