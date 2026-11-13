import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "package_runner", str(ROOT / "tools/package_current_modified_opengauss.py"))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
smoke_spec = importlib.util.spec_from_file_location(
    "release_smoke", str(ROOT / "tools/smoke_test_packaged_opengauss.py"))
smoke = importlib.util.module_from_spec(smoke_spec)
smoke_spec.loader.exec_module(smoke)


class ModifiedOpenGaussPackageTests(unittest.TestCase):
    def test_gsql_probe_does_not_use_postgres_only_w_option(self):
        argv = smoke.tcp_rejection_probe_argv(Path("/relocated"), 15439)
        self.assertNotIn("-w", argv)
        self.assertIn("127.0.0.1", argv)

    def test_resize_interval_uses_valid_milliseconds(self):
        self.assertIn("shared_buffers_resize_interval = 1000\n",
                      smoke.smoke_configuration(15439, "/private/socket"))

    def test_opengauss_listen_addresses_must_not_be_empty(self):
        self.assertIn("listen_addresses = '127.0.0.1'\n",
                      smoke.smoke_configuration(15439, "/private/socket"))

    def test_smoke_tcp_is_rejected_not_trusted(self):
        self.assertIn("host all all 127.0.0.1/32 reject\n", smoke.SMOKE_HBA)
        self.assertNotIn("host all all 127.0.0.1/32 trust", smoke.SMOKE_HBA)

    def test_non_utf8_patch_bytes_are_not_decoded_or_replaced(self):
        value = runner.command_bytes(
            [sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'\\x81\\xff\\n')"])
        self.assertEqual(value, b"\x81\xff\n")

    def home(self, path):
        for name in ("bin", "lib", "share", "include", "jre"):
            (path / name).mkdir()
        (path / "version.cfg").write_text("5.1.0")
        return path

    def test_historical_binaries_and_data_are_not_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            home = self.home(Path(directory))
            (home / "bin/gaussdb").write_text("current")
            (home / "bin/gaussdb.before-hot-sb-20260823").write_text("old")
            (home / "bin/runtime_sb_prototype_20260731").mkdir()
            (home / "data").mkdir()
            (home / "data/password.private").write_text("not-for-release")
            names = [str(p.relative_to(home)) for p in runner.runtime_files(home)]
            self.assertEqual(names, ["bin/gaussdb", "version.cfg"])

    def test_potential_runtime_private_key_requires_review(self):
        with tempfile.TemporaryDirectory() as directory:
            home = self.home(Path(directory))
            (home / "lib/server.key").write_text("not-for-release")
            with self.assertRaises(ValueError):
                list(runner.runtime_files(home))

    def test_external_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "escape").symlink_to("/etc/passwd")
            with self.assertRaises(ValueError):
                runner.copy_entry(root / "escape", root / "out/escape", [], root)

    def test_content_and_relative_library_links_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source").mkdir()
            (root / "source/libx.so.1").write_text("library")
            (root / "source/libx.so").symlink_to("libx.so.1")
            records = []
            for name in ("libx.so.1", "libx.so"):
                runner.copy_entry(root / "source" / name, root / "out" / name,
                                  records, root / "source")
            self.assertEqual((root / "out/libx.so").read_text(), "library")
            self.assertEqual(records[0]["sha256"], runner.sha(root / "source/libx.so.1"))

    def test_manifest_excludes_itself_and_detects_content_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "code.py").write_text("print(1)\n")
            runner.regenerate_manifest(root)
            manifest = (root / "FILE_SHA256SUMS").read_text()
            self.assertIn(runner.sha(root / "code.py") + "  code.py\n", manifest)
            self.assertNotIn("FILE_SHA256SUMS", manifest)


if __name__ == "__main__":
    unittest.main()
