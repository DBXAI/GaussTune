from pathlib import Path
import tempfile
import unittest

from huawei7.relation_paths import relation_path
from huawei7.schema import PageKey


class RelationPathTest(unittest.TestCase):
    def test_default_global_bucket_and_fork_paths(self):
        root = Path("/data")
        self.assertEqual(
            relation_path(root, PageKey(1663, 9, 42, -1, 0, 0)),
            root / "base/9/42",
        )
        self.assertEqual(
            relation_path(root, PageKey(1663, 9, 42, 7, 2, 0)),
            root / "base/9/42_b7_vm",
        )
        self.assertEqual(
            relation_path(root, PageKey(1664, 0, 42, -1, 1, 0)),
            root / "global/42_fsm",
        )


if __name__ == "__main__":
    unittest.main()
