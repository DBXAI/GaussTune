import unittest

from scripts.reset_benchbase_tpcc import _identifier, _load_xml


class TpccDatasetResetTest(unittest.TestCase):
    def test_seeded_loader_xml_uses_the_audited_runtime_identity(self):
        runtime = {
            "postgres": {"host": "127.0.0.1", "port": 5432},
            "tp": {"benchbase-tpcc": {
                "database": "tpcc", "user": "tp_user",
                "password_env": "TP_PASSWORD", "warehouses": 100,
                "batch_size": 128,
            }},
        }
        xml = _load_xml(runtime, password="p&x", seed=15721)
        self.assertIn("<randomSeed>15721</randomSeed>", xml)
        self.assertIn("<scalefactor>100</scalefactor>", xml)
        self.assertIn("<username>tp_user</username>", xml)
        self.assertIn("<password>p&amp;x</password>", xml)
        self.assertNotIn("reWriteBatchedInserts", xml)

    def test_database_identifiers_are_fail_closed(self):
        self.assertEqual(_identifier("h5_tpcc_bench", "database"), "h5_tpcc_bench")
        with self.assertRaisesRegex(ValueError, "simple identifier"):
            _identifier("h5_tpcc_bench;drop database postgres", "database")


if __name__ == "__main__":
    unittest.main()
