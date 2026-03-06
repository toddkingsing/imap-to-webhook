import logging
import time

logger = logging.getLogger("imap-to-webhook")


class Stats:
    def __init__(self, log_interval=300):
        self.log_interval = log_interval
        self.processed = 0
        self.success = 0
        self.failed = 0
        self.refused = 0
        self.retried = 0
        self.oversized = 0
        self._durations = []
        self._last_log_time = time.time()

    def record_success(self, duration):
        self.processed += 1
        self.success += 1
        self._durations.append(duration)

    def record_failure(self):
        self.processed += 1
        self.failed += 1

    def record_refused(self):
        self.processed += 1
        self.refused += 1

    def record_retry(self):
        self.retried += 1

    def record_oversized(self):
        self.processed += 1
        self.oversized += 1

    def maybe_log_summary(self):
        now = time.time()
        if now - self._last_log_time >= self.log_interval:
            self.log_summary()
            self._last_log_time = now

    def log_summary(self):
        avg_dur = (
            sum(self._durations) / len(self._durations) if self._durations else 0
        )
        logger.info(
            "STATS | processed=%d success=%d failed=%d refused=%d "
            "retried=%d oversized=%d avg_duration=%.2fs",
            self.processed,
            self.success,
            self.failed,
            self.refused,
            self.retried,
            self.oversized,
            avg_dur,
        )
