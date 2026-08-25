import json
import tempfile
import unittest
from pathlib import Path

from huawei7.stage_spec import read_stage_spec
from scripts.run_ppt_pipeline_matrix import (
    _build_config, _load_manifest, _require_strict_artifact,
)


class PptPipelineMatrixTest(unittest.TestCase):
    def test_manifest_builds_the_exact_ppt_pipeline_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = [
                "machine.json", "memory.json", "ap.json", "fio.json",
                "service.json", "os128.json", "sweep128.json",
                "cal128.json", "collection128.json", "overhead128.json",
                "os144.json", "sweep144.json", "cal144.json",
                "collection144.json", "overhead144.json",
            ]
            paths = {}
            for name in names:
                path = root / name
                path.write_text("{}\n", encoding="utf-8")
                paths[name] = str(path)

            def row(prefix):
                return {
                    "os_cache_model": paths["os%s.json" % prefix],
                    "tp_sweep": paths["sweep%s.json" % prefix],
                    "tp_calibration": paths["cal%s.json" % prefix],
                    "tp_collection": paths["collection%s.json" % prefix],
                    "buffer_probe_overhead": paths["overhead%s.json" % prefix],
                }

            manifest = {
                "schema": "huawei7.ppt-pipeline-artifacts/v1",
                "machine_fingerprint": "m" * 64,
                "common": {
                    "machine": paths["machine.json"],
                    "memory_budget": paths["memory.json"],
                    "ap_model_bundle": paths["ap.json"],
                    "openGauss_data_dir": str(root),
                },
                "storage": {
                    "fio_validation": paths["fio.json"],
                    "service_calibration": paths["service.json"],
                },
                "topologies": {
                    "sysbench": {"128": row("128"), "144": row("144")},
                    "benchbase-tpcc": {"128": row("128"), "144": row("144")},
                },
            }
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest), encoding="utf-8",
            )
            loaded = _load_manifest(manifest_path)
            stage = read_stage_spec(
                Path(__file__).resolve().parents[1]
                / "config" / "ppt_five_stages.json"
            )[0]
            config = _build_config(
                manifest=loaded,
                manifest_path=manifest_path,
                common=loaded["common"],
                benchmark="sysbench",
                stage=stage,
            )
            self.assertEqual(config["schema"], "huawei7.pipeline-config/v1")
            self.assertEqual(config["tp_benchmark"], "sysbench")
            self.assertEqual(config["stage"]["tp_terminals"], 128)
            self.assertEqual(config["stage"]["tp_surge_terminals"], 0)
            self.assertIn("os_cache_model", config)
            self.assertIn("tp_sweep", config)
            self.assertIn("tp_calibration", config)
            self.assertIn("storage", config)

    def test_native_empirical_artifact_is_not_accepted_as_ppt_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            native = root / "native.json"
            native.write_text(json.dumps({
                "schema": "huawei7.tp-empirical-model/v1",
                "machine_fingerprint": "m" * 64,
                "benchmark": "sysbench",
                "terminals": 128,
                "valid": True,
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "strict PPT schema"):
                _require_strict_artifact(
                    {"os_cache_model": str(native)},
                    "os_cache_model",
                    machine="m" * 64,
                    benchmark="sysbench",
                    terminals=128,
                )


if __name__ == "__main__":
    unittest.main()
