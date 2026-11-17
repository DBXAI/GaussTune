"""Refuse to start long experiments that would overlap host maintenance.

Approved 2026-10-14: a run must not start when its load phase would cross
local midnight or coincide with a systemd timer or cron job (apt, logrotate,
man-db, fstrim, ...).  ``debian-sa1`` (sysstat, one sample every 10 minutes)
is allowed because it never stops and is negligible.
"""

from __future__ import annotations

import datetime as dt
import re
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

DEFAULT_ALLOWED = ("debian-sa1",)
TIMER_LINE = re.compile(
    r"^\w{3} (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \S+\s.*?(\S+\.timer)\s+\S+\s*$"
)


def _field_values(field: str, low: int, high: int) -> Set[int]:
    values: Set[int] = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            step = int(step_text)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            start, end = (int(x) for x in part.split("-", 1))
        else:
            start = end = int(part)
        values.update(range(start, end + 1, step))
    return values


def cron_times(schedule: Sequence[str], start: dt.datetime, end: dt.datetime) -> List[dt.datetime]:
    """Minutes in [start, end] matched by a 5-field cron schedule."""

    minute, hour, dom, month, dow = schedule
    minutes = _field_values(minute, 0, 59)
    hours = _field_values(hour, 0, 23)
    doms = _field_values(dom, 1, 31)
    months = _field_values(month, 1, 12)
    dows = {d % 7 for d in _field_values(dow, 0, 7)}
    current = start.replace(second=0, microsecond=0)
    if current < start:
        current += dt.timedelta(minutes=1)
    hits = []
    while current <= end:
        day_ok = current.day in doms if dow == "*" else (
            current.isoweekday() % 7 in dows if dom == "*" else
            (current.day in doms or current.isoweekday() % 7 in dows))
        if (current.minute in minutes and current.hour in hours
                and current.month in months and day_ok):
            hits.append(current)
        current += dt.timedelta(minutes=1)
    return hits


def _runs(command: str) -> bool:
    """Skip cron lines that are no-ops on this host."""

    if command.startswith("test -e /run/systemd/system ||") and Path("/run/systemd/system").exists():
        return False
    match = re.search(r"run-parts\s+(?:--\S+\s+)*(/\S+)", command)
    if match:
        directory = Path(match.group(1))
        # run-parts only executes names made of [A-Za-z0-9_-]; e.g. the
        # packaged ``.placeholder`` never runs.
        valid = re.compile(r"^[A-Za-z0-9_-]+$")
        return directory.is_dir() and any(
            valid.match(entry.name) and entry.is_file() for entry in directory.iterdir()
        )
    return True


def cron_entries(paths: Iterable[Path]) -> List[Tuple[Tuple[str, ...], str, str]]:
    rows = []
    for path in paths:
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            text = line.strip()
            if not text or text.startswith("#") or re.match(r"^[A-Za-z_]+=", text):
                continue
            parts = text.split(None, 6)
            if len(parts) < 7 or text.startswith("@"):
                continue
            rows.append((tuple(parts[:5]), parts[6], str(path)))
    return rows


def timer_events(listing: str) -> List[Tuple[str, dt.datetime]]:
    events = []
    for line in listing.splitlines():
        match = TIMER_LINE.match(line.strip())
        if match:
            events.append((match.group(2), dt.datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")))
    return events


def maintenance_overlaps(
    start: dt.datetime, end: dt.datetime, *, timer_listing: str,
    cron_paths: Iterable[Path], allowed: Sequence[str] = DEFAULT_ALLOWED,
) -> List[Dict[str, str]]:
    found = []
    for name, when in timer_events(timer_listing):
        if start <= when <= end:
            found.append({"source": "systemd", "name": name, "at": when.isoformat(sep=" ")})
    for schedule, command, path in cron_entries(cron_paths):
        if any(token in command for token in allowed) or not _runs(command):
            continue
        hits = cron_times(schedule, start, end)
        if hits:
            found.append({"source": path, "name": command[:120], "at": hits[0].isoformat(sep=" ")})
    return found


def check_quiet_window(
    seconds: float, *, margin_seconds: float = 300, allow_midnight: bool = False,
    now: Optional[dt.datetime] = None,
) -> Dict[str, object]:
    """Raise RuntimeError if [now, now + seconds + margin] is not quiet."""

    start = now or dt.datetime.now()
    end = start + dt.timedelta(seconds=float(seconds) + float(margin_seconds))
    listing = subprocess.run(
        ["systemctl", "list-timers", "--all", "--no-pager", "--no-legend"],
        text=True, capture_output=True, check=True,
    ).stdout
    paths = [Path("/etc/crontab"), *sorted(Path("/etc/cron.d").glob("*"))]
    overlaps = maintenance_overlaps(start, end, timer_listing=listing, cron_paths=paths)
    crosses_midnight = end.date() != start.date()
    report = {
        "start": start.isoformat(sep=" ", timespec="seconds"),
        "end_with_margin": end.isoformat(sep=" ", timespec="seconds"),
        "crosses_midnight": crosses_midnight, "maintenance": overlaps,
    }
    problems = list(overlaps)
    if crosses_midnight and not allow_midnight:
        problems.append({"source": "clock", "name": "local midnight", "at": ""})
    if problems:
        raise RuntimeError("run window %s .. %s is not quiet: %s" % (
            report["start"], report["end_with_margin"],
            "; ".join("%s %s %s" % (p["source"], p["name"], p["at"]) for p in problems)))
    return report
