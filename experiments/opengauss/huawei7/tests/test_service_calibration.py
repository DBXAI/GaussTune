import json
from pathlib import Path
import tempfile
import unittest

from huawei7.service_calibration import build_service_times, CLASSES


class ServiceCalibrationTest(unittest.TestCase):
    def test_every_class_requires_three_real_block_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = {}
            for output, (workload, direction) in CLASSES.items():
                paths = []
                for repeat in range(3):
                    path = root / (output + str(repeat) + ".json")
                    path.write_text(json.dumps({
                        "schema": "huawei7.block-calibration/v1",
                        "machine_fingerprint": "m", "valid": True,
                        "required_class": workload,
                        "summary": {"rows": [{
                            "workload_class": workload, "rw": direction,
                            "requests": 10, "service_time_ms": repeat + 1,
                        }]},
                    }))
                    paths.append(path.name)
                inputs[output] = paths
            result = build_service_times({
                "schema": "huawei7.service-time-manifest/v1",
                "machine_fingerprint": "m", "inputs": inputs,
            }, root)
            self.assertTrue(result["valid"])
            self.assertEqual(result["service_times_ms"]["tp_read_ms"], 2)
            self.assertEqual(result["evidence"]["ap_write_ms"]["repeats"], 3)


if __name__ == "__main__":
    unittest.main()
