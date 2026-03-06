import copy
import logging
import os
import random
import signal
import time

import requests
import sentry_sdk

from config import get_config
from connection import IMAPClient
from mail_parser import serialize_mail
from stats import Stats
from version import __version__

logger = logging.getLogger("imap-to-webhook")

# Maximum email size in bytes (25 MB)
MAX_EMAIL_SIZE = 25 * 1024 * 1024

# Graceful shutdown flag
_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    logger.info("Received signal %s, shutting down after current iteration", signum)
    _shutdown = True


def _interruptible_sleep(seconds):
    """Sleep in 1-second increments so _shutdown signal is respected promptly."""
    for _ in range(int(seconds)):
        if _shutdown:
            return
        time.sleep(1)


def _touch_heartbeat(path=None):
    """Create or update the heartbeat file for Docker HEALTHCHECK."""
    hb_path = path or os.environ.get(
        "HEARTBEAT_FILE", "/tmp/imap-to-webhook-heartbeat"
    )
    try:
        with open(hb_path, "w") as f:
            f.write(str(time.time()))
    except OSError as e:
        logger.warning("Failed to update heartbeat file: %s", e)


def main():
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(message)s",
        level=logging.INFO,
    )
    config = get_config(os.environ)
    config_printout = copy.deepcopy(config)
    for acct in config_printout.get("imap_accounts", []):
        if "password" in acct:
            acct["password"] = "********"
    if "password" in config_printout.get("imap", {}):
        config_printout["imap"]["password"] = "********"
    if config_printout.get("webhook_secret"):
        config_printout["webhook_secret"] = "********"

    session = requests.Session()
    if config.get("webhook_secret"):
        session.headers["X-Webhook-Secret"] = config["webhook_secret"]
    logger.info("Starting daemon version %s", __version__)
    logger.info("Configuration: %s", config_printout)
    sentry_sdk.init(dsn=config["sentry_dsn"], traces_sample_rate=1.0)

    stats = Stats(config.get("stats_log_interval", 300))

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        loop(config, session, stats)
    except Exception as e:
        if config["sentry_dsn"]:
            sentry_sdk.capture_exception(e)
        logger.fatal("Fatal error in main loop: %s", e)
        raise
    finally:
        stats.log_summary()


def loop(config, session, stats=None):
    global _shutdown
    accounts = config.get("imap_accounts", [config["imap"]])
    batch_size = config.get("batch_size", 1)
    delivery_interval = config.get("delivery_interval", 0)
    consecutive_errors = 0

    while not _shutdown:
        any_message_global = False

        for acct_idx, acct in enumerate(accounts):
            if _shutdown:
                break
            acct_config = {**config, "imap": acct}
            label = acct.get("username") or f"acct_{acct_idx}"
            client = None

            try:
                client = IMAPClient(acct_config)
                msg_ids = client.get_mail_ids()
                logger.info("[%s] Found %d mails to download", label, len(msg_ids))
                if msg_ids:
                    logger.info(
                        "[%s] First msg id: %s (total: %d)",
                        label,
                        msg_ids[0],
                        len(msg_ids),
                    )
                if msg_ids and config.get("process_order", "fifo") == "lifo":
                    msg_ids = list(reversed(msg_ids))

                if msg_ids:
                    any_message_global = True
                    batch = msg_ids[:batch_size]
                    if len(batch) > 1:
                        logger.info(
                            "[%s] Batch download: %d of %d messages",
                            label,
                            len(batch),
                            len(msg_ids),
                        )

                    # Phase 1: Batch download
                    downloaded = []
                    for msg_id in batch:
                        if _shutdown:
                            break
                        try:
                            logger.info("[%s] Fetch message ID %s", label, msg_id)
                            start = time.time()
                            raw_mail = client.fetch(msg_id)
                            elapsed = time.time() - start
                            logger.info(
                                "[%s] Message downloaded in %.2f seconds (%d bytes)",
                                label,
                                elapsed,
                                len(raw_mail),
                            )
                            downloaded.append((msg_id, raw_mail))
                        except Exception as fetch_err:
                            logger.error(
                                "[%s] Failed to fetch msg %s, skipping: %s",
                                label,
                                msg_id,
                                fetch_err,
                            )

                    # Phase 2: Throttled delivery
                    phase2_failures = 0
                    for i, (msg_id, raw_mail) in enumerate(downloaded):
                        if _shutdown:
                            break
                        if i > 0 and delivery_interval > 0:
                            logger.debug(
                                "[%s] Delivery interval: waiting %.1fs",
                                label,
                                delivery_interval,
                            )
                            _interruptible_sleep(delivery_interval)
                        try:
                            process_msg_from_raw(
                                client,
                                msg_id,
                                raw_mail,
                                acct_config,
                                session,
                                stats,
                            )
                        except Exception as proc_err:
                            phase2_failures += 1
                            logger.error(
                                "[%s] Failed to process msg %s: %s",
                                label,
                                msg_id,
                                proc_err,
                            )
                            sentry_sdk.capture_exception(proc_err)
                    if downloaded and phase2_failures == len(downloaded):
                        consecutive_errors += 1
                    else:
                        consecutive_errors = 0
                else:
                    consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                sentry_sdk.capture_exception(e)
                logger.error(
                    "[%s] Error in loop iteration (attempt %d): %s",
                    label,
                    consecutive_errors,
                    e,
                )
                # Continue to next account instead of backing off immediately
            finally:
                if client is not None:
                    try:
                        client.expunge()
                    except Exception as exp_err:
                        logger.warning("[%s] expunge() failed: %s", label, exp_err)
                    try:
                        client.connection_close()
                    except Exception as close_err:
                        logger.warning(
                            "[%s] connection_close() failed: %s", label, close_err
                        )

        _touch_heartbeat()
        if stats:
            stats.maybe_log_summary()

        if _shutdown:
            break

        if consecutive_errors > 0:
            backoff = min(
                300,
                (2 ** min(consecutive_errors, 6)) + random.uniform(0, 5),
            )
            logger.info("Backing off %d seconds after errors", int(backoff))
            _interruptible_sleep(backoff)
        elif not any_message_global:
            logger.info("Waiting %d seconds", config["delay"])
            _interruptible_sleep(config["delay"])
            logger.debug("Resume after delay")


RESULT_SUCCESS = "success"
RESULT_REFUSED = "refused"
RESULT_FAILED = "failed"
RESULT_OVERSIZED = "oversized"


def _safe_json(res):
    try:
        return res.json()
    except Exception:
        return None


def _safe_move(client, msg_id, folder):
    """Move msg to folder. Returns True on success, False on failure."""
    try:
        client.move(msg_id, folder)
        return True
    except Exception as move_err:
        logger.error("Failed to move msg %s to %s: %s", msg_id, folder, move_err)
        return False


def _last_resort_mark(client, msg_id, config):
    """Last resort: make msg invisible to future searches when all folders fail."""
    try:
        if config["imap"]["on_success"] == "noop":
            client.mark_seen(msg_id)
            logger.warning(
                "LAST RESORT: marked msg %s as \\Seen to prevent re-processing",
                msg_id,
            )
        else:
            client.mark_delete(msg_id)
            logger.warning(
                "LAST RESORT: marked msg %s as \\Deleted to prevent re-processing",
                msg_id,
            )
    except Exception as e:
        logger.critical(
            "LAST RESORT FAILED for msg %s: %s. "
            "This message will be re-processed on next cycle.",
            msg_id,
            e,
        )


def _handle_success(client, msg_id, config):
    try:
        if config["imap"]["on_success"] == "delete":
            client.mark_delete(msg_id)
        elif config["imap"]["on_success"] == "move":
            client.move(msg_id, config["imap"]["success"])
        elif config["imap"]["on_success"] == "noop":
            client.mark_seen(msg_id)
        else:
            logger.info("Unknown on_success mode for message id %s", msg_id)
        return True
    except Exception as e:
        logger.error(
            "Failed to mark msg %s as processed (on_success=%s): %s",
            msg_id,
            config["imap"]["on_success"],
            e,
        )
        return False


def process_msg(client, msg_id, config, session, stats=None):
    logger.info("Fetch message ID %s", msg_id)
    start = time.time()
    raw_mail = client.fetch(msg_id)
    elapsed = time.time() - start
    logger.info("Message downloaded in %.2f seconds (%d bytes)", elapsed, len(raw_mail))
    return process_msg_from_raw(client, msg_id, raw_mail, config, session, stats)


def process_msg_from_raw(client, msg_id, raw_mail, config, session, stats=None):
    overall_start = time.time()

    # OPT-4: reject oversized emails
    if len(raw_mail) > MAX_EMAIL_SIZE:
        logger.warning(
            "Message %s exceeds size limit (%d > %d bytes), moving to ERROR",
            msg_id,
            len(raw_mail),
            MAX_EMAIL_SIZE,
        )
        if not _safe_move(client, msg_id, config["imap"]["error"]):
            _last_resort_mark(client, msg_id, config)
        if stats:
            stats.record_oversized()
        return RESULT_OVERSIZED

    max_retries = config.get("webhook_max_retries", 0)
    retry_delay = config.get("webhook_retry_delay", 10)

    for attempt in range(max_retries + 1):
        # Re-serialize each attempt (BytesIO streams are consumed by requests)
        try:
            body = serialize_mail(raw_mail, config["compress_eml"])
        except Exception as e:
            sentry_sdk.capture_exception(e)
            logger.error("Failed to serialize msg %s: %s", msg_id, e)
            if not _safe_move(client, msg_id, config["imap"]["error"]):
                _last_resort_mark(client, msg_id, config)
            if stats:
                stats.record_failure()
            return RESULT_FAILED

        try:
            res = session.post(config["webhook"], files=body, timeout=30)
            logger.info("Received response: %s", res.text[:500])

            # REFUSED: do not retry
            if res.status_code >= 400:
                payload = _safe_json(res)
                if isinstance(payload, dict) and payload.get("status") == "REFUSED":
                    logger.info(
                        "Message refused by webhook (reason=%s); "
                        "moving msg id %s to REFUSED",
                        payload.get("reason"),
                        msg_id,
                    )
                    moved = _safe_move(
                        client,
                        msg_id,
                        config["imap"].get("refused", "REFUSED"),
                    )
                    if not moved:
                        logger.warning(
                            "Cannot move REFUSED msg %s to REFUSED folder, "
                            "trying ERROR",
                            msg_id,
                        )
                        if not _safe_move(client, msg_id, config["imap"]["error"]):
                            _last_resort_mark(client, msg_id, config)
                    if stats:
                        stats.record_refused()
                    return RESULT_REFUSED

            # 5xx with retries remaining: retry
            if res.status_code >= 500 and attempt < max_retries:
                if stats:
                    stats.record_retry()
                delay = retry_delay * (2**attempt)
                logger.warning(
                    "Webhook %d for msg %s, retry %d/%d in %ds",
                    res.status_code,
                    msg_id,
                    attempt + 1,
                    max_retries,
                    delay,
                )
                _interruptible_sleep(delay)
                continue

            res.raise_for_status()

            # 2xx success
            try:
                response = res.json()
                logger.info("Delivered message id %s: %s", msg_id, response)
            except Exception:
                logger.info("Delivered message id %s (non-JSON response)", msg_id)

            success = _handle_success(client, msg_id, config)
            if not success:
                logger.warning(
                    "Webhook delivered but post-delivery action failed for msg %s; "
                    "moving to ERROR to prevent re-delivery",
                    msg_id,
                )
                if not _safe_move(client, msg_id, config["imap"]["error"]):
                    _last_resort_mark(client, msg_id, config)
            if stats:
                stats.record_success(time.time() - overall_start)
            return RESULT_SUCCESS

        except requests.exceptions.RequestException as e:
            if attempt < max_retries:
                if stats:
                    stats.record_retry()
                delay = retry_delay * (2**attempt)
                logger.warning(
                    "Webhook error for msg %s: %s, retry %d/%d in %ds",
                    msg_id,
                    e,
                    attempt + 1,
                    max_retries,
                    delay,
                )
                _interruptible_sleep(delay)
                continue
            # Final failure
            sentry_sdk.capture_exception(e)
            logger.error(
                "Unable to deliver msg %s after %d attempts: %s",
                msg_id,
                attempt + 1,
                e,
            )
            if not _safe_move(client, msg_id, config["imap"]["error"]):
                _last_resort_mark(client, msg_id, config)
            if stats:
                stats.record_failure()
            return RESULT_FAILED

    # Should not reach here, but guard against it
    if not _safe_move(client, msg_id, config["imap"]["error"]):
        _last_resort_mark(client, msg_id, config)
    if stats:
        stats.record_failure()
    return RESULT_FAILED


if __name__ == "__main__":
    main()
