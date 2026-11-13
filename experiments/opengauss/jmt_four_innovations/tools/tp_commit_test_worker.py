#!/usr/bin/env python3
"""Private peer-auth worker for known-count read-only commit/rollback tests."""
import json
import sys
import time
import psycopg2


def send(value):
    print(json.dumps(value), flush=True)


def main():
    init = json.loads(sys.stdin.readline())
    connection = psycopg2.connect(
        dbname=init["database"], host="/tmp", user="omm", connect_timeout=10,
        application_name=init["application_name"],
        options="-c default_transaction_read_only=on -c query_dop=1",
    )
    connection.autocommit = True
    cursor = connection.cursor()
    cursor.execute("SELECT lwtid FROM pg_thread_wait_status WHERE tid=pg_backend_pid()")
    tid = int(cursor.fetchone()[0])
    connection.autocommit = False
    send({"status": "ready", "linux_tid": tid})
    for line in sys.stdin:
        request = json.loads(line)
        if request.get("command") == "close":
            break
        count = request["transactions"]
        if not isinstance(count, int) or not 1 <= count <= 10000:
            raise ValueError("known-count test budget exceeded")
        while time.monotonic_ns() < request["start_ns"]:
            time.sleep(.001)
        started = time.monotonic_ns()
        for _ in range(count):
            cursor.execute("SELECT 1")
            assert cursor.fetchone()[0] == 1
            if request["rollback"]:
                connection.rollback()
            else:
                connection.commit()
        send({"status": "complete", "transactions": count,
              "rollback": request["rollback"], "started_ns": started,
              "ended_ns": time.monotonic_ns()})
    cursor.close()
    connection.close()


if __name__ == "__main__":
    main()
