#!/usr/bin/env python3
"""Compare native and Python alert poll latency on a private synthetic cache."""
import argparse
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "helper" / "omarcal-helper"
LAUNCHER = ROOT / "helper" / "omarcal"
NATIVE = ROOT / "helper" / "omarcal-native"


def load_helper():
    loader = importlib.machinery.SourceFileLoader("omarcal_alert_benchmark", str(HELPER))
    spec = importlib.util.spec_from_loader("omarcal_alert_benchmark", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def synthetic_event(index, start):
    end = start + timedelta(hours=1)
    uid = f"benchmark-{index}"
    return "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "BEGIN:VEVENT", f"UID:{uid}",
        f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}",
        f"DTEND:{end.strftime('%Y%m%dT%H%M%SZ')}", f"SUMMARY:Synthetic event {index}",
        "BEGIN:VALARM", "ACTION:DISPLAY", "TRIGGER:-PT5M", "END:VALARM",
        "END:VEVENT", "END:VCALENDAR", "",
    ])


def command_result(command, environment):
    started = time.perf_counter()
    completed = subprocess.run(command, capture_output=True, text=True,
                               env=environment, check=False)
    elapsed = time.perf_counter() - started
    if completed.returncode:
        raise RuntimeError(completed.stderr or completed.stdout)
    return elapsed, json.loads(completed.stdout)["alerts"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=12,
                        help="timed polls per backend (default: 12)")
    parser.add_argument("--objects", type=int, default=500,
                        help="synthetic event resources (default: 500)")
    args = parser.parse_args()
    if args.iterations < 1 or args.objects < 1:
        parser.error("--iterations and --objects must be positive")
    if not NATIVE.is_file() or not os.access(NATIVE, os.X_OK):
        parser.error("native helper is missing; build it first with ./helper/build-native")

    helper = load_helper()
    with tempfile.TemporaryDirectory(prefix="omarcal-alert-bench-") as temporary:
        state_home = Path(temporary) / "state"
        cache_dir = state_home / "omarcal"
        cache_dir.mkdir(parents=True)
        conn = helper.sqlite3.connect(str(cache_dir / "cache.db"))
        conn.executescript(helper.SCHEMA)
        calendar = "https://benchmark.invalid/calendar/"
        conn.execute("INSERT INTO calendars(url,name,enabled) VALUES(?,?,1)",
                     (calendar, "Synthetic benchmark"))

        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        range_start = now
        range_end = now + timedelta(days=31)
        rows = []
        for index in range(args.objects):
            start = now + timedelta(minutes=15 + (index % 30) * 24 * 60 + (index * 17) % 1380)
            ics = synthetic_event(index, start)
            first, last, open_ended, summary, uid = helper.index_of(ics)
            rows.append((f"{calendar}event-{index}.ics", calendar, ics, first, last,
                         open_ended, summary, uid))
        conn.executemany(
            "INSERT INTO objects(url,calendar,ics,first_start,last_end,open_ended,summary,uid) "
            "VALUES(?,?,?,?,?,?,?,?)", rows,
        )
        conn.commit()

        # Build the projection once before timing, so this measures repeated
        # notification polls rather than first-use indexing.
        warmed = helper.alerts(conn, range_start, range_end)["alerts"]
        if len(warmed) != args.objects:
            raise RuntimeError(f"expected {args.objects} alerts, indexed {len(warmed)}")
        conn.close()

        environment = os.environ.copy()
        environment["XDG_STATE_HOME"] = str(state_home)
        command_args = ["alerts", "--from", range_start.isoformat(timespec="seconds"),
                        "--to", range_end.isoformat(timespec="seconds")]
        backends = {
            "native": [str(LAUNCHER), *command_args],
            "python": [sys.executable, str(HELPER), *command_args],
        }
        samples = {name: [] for name in backends}
        expected = None
        for iteration in range(args.iterations):
            order = ("native", "python") if iteration % 2 == 0 else ("python", "native")
            for name in order:
                elapsed, alerts = command_result(backends[name], environment)
                if expected is None:
                    expected = alerts
                elif alerts != expected:
                    raise RuntimeError(f"{name} alerts differ from the first result")
                samples[name].append(elapsed)

    medians = {name: statistics.median(values) for name, values in samples.items()}
    print(f"Synthetic cache: {args.objects} single-event resources, {len(expected)} alerts")
    print(f"Median per-poll latency across {args.iterations} runs:")
    for name, median in medians.items():
        print(f"  {name:6s} {median * 1000:.2f} ms")
    print(f"Native speedup: {medians['python'] / medians['native']:.2f}x")


if __name__ == "__main__":
    main()
