import contextlib
import io
import json
import unittest

from jmt import __version__
from jmt.cli import main


class ReleaseVersionTest(unittest.TestCase):
    def test_doctor_and_version_are_not_stale_migration_literals(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            main(["doctor"])
        self.assertEqual(json.loads(output.getvalue())["release"], __version__)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
            main(["--version"])
        self.assertIn(__version__, output.getvalue())


if __name__ == "__main__":
    unittest.main()
