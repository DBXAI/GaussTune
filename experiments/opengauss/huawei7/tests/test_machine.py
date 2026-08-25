import unittest

from huawei7.machine import validate_ppt_hardware


class MachineTest(unittest.TestCase):
    def test_ppt_hardware_contract_is_fail_closed(self):
        valid = {
            "kernel_release": "5.4.0-216-generic",
            "logical_cpus": 16, "physical_cores": 8,
            "memory_bytes": 30 * 1024 ** 3, "swap_bytes": 0,
            "device_model": "Cloud Elastic Block Storage",
        }
        validate_ppt_hardware(valid)
        invalid = dict(valid, swap_bytes=1024)
        with self.assertRaisesRegex(RuntimeError, "swap"):
            validate_ppt_hardware(invalid)


if __name__ == "__main__":
    unittest.main()
