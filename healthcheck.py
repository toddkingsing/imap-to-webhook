#!/usr/bin/env python3
import os
import sys
import time

HEARTBEAT_FILE = os.environ.get("HEARTBEAT_FILE", "/tmp/imap-to-webhook-heartbeat")
MAX_AGE = int(os.environ.get("HEARTBEAT_MAX_AGE", "600"))


def check():
    try:
        age = time.time() - os.path.getmtime(HEARTBEAT_FILE)
    except FileNotFoundError:
        print(f"UNHEALTHY: heartbeat file not found")
        return 1
    if age > MAX_AGE:
        print(f"UNHEALTHY: heartbeat age {age:.0f}s > {MAX_AGE}s")
        return 1
    print(f"HEALTHY: heartbeat age {age:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(check())
