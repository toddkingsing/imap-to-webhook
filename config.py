import imaplib
from urllib.parse import parse_qs, unquote, urlparse

transports = {"imap": [imaplib.IMAP4, 143], "imap+ssl": [imaplib.IMAP4_SSL, 993]}


def _safe_int(env, key, default):
    """Parse an env var as int with a descriptive error on failure."""
    raw = env.get(key, default)
    try:
        return int(raw)
    except ValueError:
        raise EnvironmentError(f"{key} must be an integer, got: '{raw}'")


def default_qs(qs, key, default):
    parsed = parse_qs(qs)
    return parsed[key][0] if key in parsed else default


def _parse_imap_config(url_str, on_success, imap_timeout, noop_flag):
    """Parse a single IMAP URL into an account config dict."""
    imap_parse = urlparse(url_str)
    if imap_parse.scheme not in transports:
        raise EnvironmentError(
            f"Unsupported IMAP scheme '{imap_parse.scheme}'. "
            f"Supported: {', '.join(transports.keys())}"
        )
    username = unquote(imap_parse.username) if imap_parse.username else None
    password = unquote(imap_parse.password) if imap_parse.password else None
    return {
        "hostname": imap_parse.hostname,
        "username": username,
        "password": password,
        "protocol": imap_parse.scheme,
        "transport": transports[imap_parse.scheme][0],
        "port": imap_parse.port or transports[imap_parse.scheme][1],
        "inbox": default_qs(imap_parse.query, "inbox", "INBOX"),
        "error": default_qs(imap_parse.query, "error", "ERROR"),
        "on_success": on_success,
        "success": default_qs(imap_parse.query, "success", "SUCCESS"),
        "refused": default_qs(imap_parse.query, "refused", "REFUSED"),
        "timeout": imap_timeout,
        "noop_flag": noop_flag,
    }


def get_config(env):
    if "WEBHOOK_URL" not in env:
        raise EnvironmentError("WEBHOOK_URL environment variable is required")
    webhook = env["WEBHOOK_URL"]
    if not webhook.startswith(("http://", "https://")):
        raise EnvironmentError(
            f"WEBHOOK_URL must start with http:// or https://, got: '{webhook}'"
        )

    on_success = env.get("ON_SUCCESS", "move")
    if on_success not in ("move", "delete", "noop"):
        raise EnvironmentError(
            f"ON_SUCCESS must be 'move', 'delete', or 'noop', got: '{on_success}'"
        )

    try:
        delay = int(env["DELAY"]) if "DELAY" in env else 60
    except ValueError:
        raise EnvironmentError(f"DELAY must be an integer, got: '{env['DELAY']}'")
    if delay < 1:
        raise EnvironmentError(f"DELAY must be >= 1, got: {delay}")

    try:
        imap_timeout = int(env["IMAP_TIMEOUT"]) if "IMAP_TIMEOUT" in env else 60
    except ValueError:
        raise EnvironmentError(f"IMAP_TIMEOUT must be an integer, got: '{env['IMAP_TIMEOUT']}'")
    if imap_timeout < 1:
        raise EnvironmentError(f"IMAP_TIMEOUT must be >= 1, got: {imap_timeout}")

    try:
        batch_size = int(env.get("BATCH_SIZE", "1"))
    except ValueError:
        raise EnvironmentError(f"BATCH_SIZE must be an integer, got: '{env.get('BATCH_SIZE')}'")
    if batch_size < 1:
        raise EnvironmentError(f"BATCH_SIZE must be >= 1, got: {batch_size}")

    try:
        delivery_interval = float(env.get("DELIVERY_INTERVAL", "0"))
    except ValueError:
        raise EnvironmentError(f"DELIVERY_INTERVAL must be a number, got: '{env.get('DELIVERY_INTERVAL')}'")
    if delivery_interval < 0:
        raise EnvironmentError(
            f"DELIVERY_INTERVAL must be >= 0, got: {delivery_interval}"
        )

    process_order = env.get("PROCESS_ORDER", "fifo").lower()
    if process_order not in ("fifo", "lifo"):
        raise EnvironmentError(
            f"PROCESS_ORDER must be 'fifo' or 'lifo', got: '{process_order}'"
        )

    noop_flag = env.get("NOOP_FLAG", r"\Seen")

    # Collect IMAP accounts: IMAP_URL_1, IMAP_URL_2, ...
    accounts = []
    i = 1
    while f"IMAP_URL_{i}" in env:
        accounts.append(
            _parse_imap_config(env[f"IMAP_URL_{i}"], on_success, imap_timeout, noop_flag)
        )
        i += 1

    if not accounts:
        if "IMAP_URL" in env:
            raise EnvironmentError(
                "IMAP_URL is no longer supported. Rename it to IMAP_URL_1"
            )
        raise EnvironmentError("IMAP_URL_1 is required")

    try:
        webhook_timeout = int(env.get("WEBHOOK_TIMEOUT", "90"))
    except ValueError:
        raise EnvironmentError(f"WEBHOOK_TIMEOUT must be an integer, got: '{env.get('WEBHOOK_TIMEOUT')}'")
    if webhook_timeout < 1:
        raise EnvironmentError(f"WEBHOOK_TIMEOUT must be >= 1, got: {webhook_timeout}")

    return {
        "process_order": process_order,
        "batch_size": batch_size,
        "delivery_interval": delivery_interval,
        "imap": accounts[0],
        "imap_accounts": accounts,
        "webhook": webhook,
        "compress_eml": env.get("COMPRESS_EML", "false").lower() in ("true", "1", "yes"),
        "delay": delay,
        "sentry_dsn": env.get("SENTRY_DSN", None),
        "webhook_secret": env.get("WEBHOOK_SECRET", ""),
        "stats_log_interval": _safe_int(env, "STATS_LOG_INTERVAL", "300"),
        "webhook_max_retries": max(0, _safe_int(env, "WEBHOOK_MAX_RETRIES", "0")),
        "webhook_retry_delay": max(1, _safe_int(env, "WEBHOOK_RETRY_DELAY", "10")),
        "webhook_timeout": webhook_timeout,
    }
