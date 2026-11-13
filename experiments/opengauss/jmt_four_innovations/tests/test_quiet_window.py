import datetime as dt
import tempfile
import unittest
from pathlib import Path

from jmt.common import quiet_window as qw

LISTING = """Wed 2026-10-14 11:43:48 CST 1h 31min left Tue 2026-10-13 20:53:48 CST 13h ago apt-daily.timer apt-daily.service
Thu 2026-10-15 00:00:00 CST 13h left      Wed 2026-10-14 00:00:00 CST 10h ago logrotate.timer logrotate.service
n/a                         n/a           n/a                         n/a     snapd.snap-repair.timer snapd.snap-repair.service
"""


class QuietWindowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cron = Path(self.tmp.name) / "crontab"
        self.cron.write_text(
            "SHELL=/bin/sh\n"
            "5-55/10 * * * * root command -v debian-sa1 > /dev/null && debian-sa1 1 1\n"
            "31 11 * * *   root    /etc/cron.daily/popularity-contest --crond\n"
            "30 3 * * 0 root test -e /run/systemd/system || /usr/bin/e2scrub\n"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def overlaps(self, start, minutes):
        end = start + dt.timedelta(minutes=minutes)
        return qw.maintenance_overlaps(start, end, timer_listing=LISTING, cron_paths=[self.cron])

    def test_timer_parsing(self):
        names = [name for name, _ in qw.timer_events(LISTING)]
        self.assertEqual(names, ["apt-daily.timer", "logrotate.timer"])

    def test_timer_and_cron_overlap(self):
        found = self.overlaps(dt.datetime(2026, 10, 14, 11, 0), 60)
        self.assertEqual({row["name"].split()[0] for row in found},
                         {"apt-daily.timer", "/etc/cron.daily/popularity-contest"})

    def test_allowed_sysstat_and_quiet_afternoon(self):
        self.assertEqual(self.overlaps(dt.datetime(2026, 10, 14, 13, 0), 120), [])

    def test_cron_day_of_week(self):
        sunday = dt.datetime(2026, 10, 18, 3, 0)
        hits = qw.cron_times(("30", "3", "*", "*", "0"), sunday, sunday + dt.timedelta(hours=1))
        self.assertEqual(hits, [dt.datetime(2026, 10, 18, 3, 30)])
        monday = sunday + dt.timedelta(days=1)
        self.assertEqual(qw.cron_times(("30", "3", "*", "*", "0"), monday,
                                       monday + dt.timedelta(hours=1)), [])

    def test_run_parts_ignores_placeholder(self):
        parts = Path(self.tmp.name) / "cron.hourly"
        parts.mkdir()
        (parts / ".placeholder").write_text("")
        command = "cd / && run-parts --report %s" % parts
        self.assertFalse(qw._runs(command))
        (parts / "job").write_text("#!/bin/sh\n")
        self.assertTrue(qw._runs(command))

    def test_step_ranges(self):
        start = dt.datetime(2026, 10, 14, 10, 0)
        hits = qw.cron_times(("5-55/10", "*", "*", "*", "*"), start, start + dt.timedelta(minutes=30))
        self.assertEqual([h.minute for h in hits], [5, 15, 25])


if __name__ == "__main__":
    unittest.main()
