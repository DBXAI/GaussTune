import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_stage_episode.py"
SPEC = importlib.util.spec_from_file_location("run_stage_episode", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class StageEpisodeTest(unittest.TestCase):
    def test_shared_buffers_check_uses_stdin_password_wrapper(self):
        config = {
            "postgres": {
                "gsql": "/opt/openGauss/bin/gsql",
                "host": "127.0.0.1",
                "port": 5432,
                "ap_user": "ap_user",
                "ap_database": "ap_database",
                "ap_password_env": "AP_PASSWORD",
                "ld_library_path": "/opt/openGauss/lib",
            },
        }
        with mock.patch.object(
            MODULE.subprocess, "check_output", return_value="5120MB\n",
        ) as check:
            self.assertEqual(MODULE._shared_buffers_mb(config), 5120)
        command = check.call_args.args[0]
        self.assertEqual(Path(command[1]).name, "run_gsql_with_password.py")
        self.assertIn("--password-env", command)
        self.assertIn("AP_PASSWORD", command)
        self.assertNotIn("secret", command)
        self.assertEqual(command[-2:], ["-c", "SHOW shared_buffers;"])

    def test_sysbench_password_file_is_private_and_rejects_newlines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sysbench.cfg"
            MODULE._write_sysbench_secret_config(path, "fake=test#password")
            self.assertEqual(
                path.read_text(), "pgsql-password=fake=test#password\n",
            )
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            with mock.patch.object(MODULE.pwd, "getpwnam") as lookup, \
                    mock.patch.object(MODULE.os, "chown") as chown:
                lookup.return_value.pw_uid = 10
                lookup.return_value.pw_gid = 20
                MODULE._write_sysbench_secret_config(path, "dummy", owner="omm")
                chown.assert_called_once_with(path, 10, 20)
        with self.assertRaises(ValueError):
            MODULE._write_sysbench_secret_config(Path("unused"), "bad\nvalue")


if __name__ == "__main__":
    unittest.main()
