# imap-to-webhook

Poll one or more IMAP mailboxes, parse each email into structured data, and deliver it to an HTTP endpoint as `multipart/form-data`.

Originally forked from [watchdogpolska/imap-to-webhook](https://github.com/watchdogpolska/imap-to-webhook), now substantially rewritten with multi-account support, configurable noop tracking, batch processing, webhook retries, IP fingerprinting, multilingual quote detection (Chinese/Japanese/Korean/Spanish/Portuguese/Italian/...), a v3 manifest format, and Python 3.14 compatibility.

```
┌───────────┐      ┌──────────────┐      ┌──────────────┐
│   IMAP    │ poll │  daemon.py   │ POST │  Your HTTP   │
│ mailboxes ├─────►│ parse + send ├─────►│   endpoint   │
└───────────┘      └──────┬───────┘      └──────────────┘
                          │
              on success: move / delete / mark processed
              on failure: move to ERROR (3-tier fallback)
```

## Quick start

```bash
git clone https://github.com/toddkingsing/imap-to-webhook.git
cd imap-to-webhook

# Configure
cp .env.example .env
cp docker-compose.example.yml docker-compose.yml
# Edit .env — fill in IMAP_URL and WEBHOOK_URL
# Edit docker-compose.yml — adjust network/volumes if needed

# Run
docker compose up -d --build

# Verify
docker logs -f imap-to-webhook
```

## Configuration

All settings are environment variables. See [`.env.example`](.env.example) for the full annotated template.

### IMAP URL

```
imap+ssl://user@domain.com:password@imap.example.com:993/?inbox=INBOX&error=ERROR&success=SUCCESS&refused=REFUSED
```

The URL is parsed by Python's `urlparse`, which splits at the **last** `@` to separate credentials from host. This means `@` in the username works as-is — no encoding needed.

| Query parameter | Default | Purpose |
|-----------------|---------|---------|
| `inbox` | `INBOX` | Folder to poll for new messages |
| `error` | `ERROR` | Destination for messages that fail processing |
| `success` | `SUCCESS` | Destination for successfully delivered messages |
| `refused` | `REFUSED` | Destination when webhook returns `{"status": "REFUSED"}` |

To monitor multiple mailboxes from a single container, use numbered variables `IMAP_URL_1`, `IMAP_URL_2`, ... instead of `IMAP_URL`.

### Environment variables

**Required:**

| Variable | Description |
|----------|-------------|
| `IMAP_URL` | IMAP connection URL (see format above). Ignored when `IMAP_URL_1` exists |
| `WEBHOOK_URL` | HTTP(S) endpoint that receives parsed emails |

**Processing:**

| Variable | Default | Description |
|----------|---------|-------------|
| `ON_SUCCESS` | `move` | After successful delivery: `move`, `delete`, or `noop` (mark processed, leave in inbox) |
| `NOOP_FLAG` | `\Seen` | IMAP flag used in noop mode. Set to a custom keyword (e.g. `$WebhookProcessed`) if other mail clients also access the mailbox. Requires server support for custom keywords (`\*` in PERMANENTFLAGS) |
| `COMPRESS_EML` | `false` | Gzip-compress the raw `.eml` attachment |
| `DELAY` | `60` | Seconds to wait between poll cycles when the inbox is empty |
| `IMAP_TIMEOUT` | `60` | IMAP connection and socket timeout (seconds) |
| `BATCH_SIZE` | `1` | Number of emails to download per IMAP session |
| `DELIVERY_INTERVAL` | `0` | Seconds to pause between deliveries within a batch |
| `PROCESS_ORDER` | `fifo` | `fifo` (oldest first) or `lifo` (newest first) |

**Webhook:**

| Variable | Default | Description |
|----------|---------|-------------|
| `WEBHOOK_SECRET` | _(empty)_ | Sent as `X-Webhook-Secret` request header |
| `WEBHOOK_MAX_RETRIES` | `0` | Retry count on 5xx or connection error (0 = no retry) |
| `WEBHOOK_RETRY_DELAY` | `10` | Base delay in seconds; doubles each retry (exponential backoff) |

**Monitoring:**

| Variable | Default | Description |
|----------|---------|-------------|
| `SENTRY_DSN` | _(disabled)_ | Sentry error tracking DSN |
| `HEARTBEAT_FILE` | `/tmp/imap-to-webhook-heartbeat` | File path for Docker HEALTHCHECK |
| `HEARTBEAT_MAX_AGE` | `600` | Maximum heartbeat file age (seconds) before reporting unhealthy |
| `STATS_LOG_INTERVAL` | `300` | Seconds between stats summary log lines |

## Webhook output

Each email is sent as a `multipart/form-data` POST containing three types of parts.

### `manifest` — application/json

```json
{
  "version": "v3",
  "headers": {
    "subject": "Meeting notes",
    "from": [{"email": "alice@example.com", "name": "Alice"}],
    "to": [{"email": "bob@example.com", "name": "Bob"}],
    "cc": [],
    "date": "2024-01-15T10:30:00+08:00",
    "message_id": "<abc123@mail.example.com>",
    "in_reply_to": "<parent-id@mail.example.com>",
    "references": ["<root-id@mail.example.com>", "<parent-id@mail.example.com>"],
    "auto_reply_type": null
  },
  "text": {
    "content": "Plain text body (quotes stripped)",
    "html_content": "<p>HTML body (quotes stripped)</p>",
    "quote": "> Quoted plain text",
    "html_quote": "<blockquote>Quoted HTML</blockquote>"
  },
  "files_count": 1,
  "eml": {
    "compressed": false
  },
  "fingerprint": {
    "ip": "203.0.113.1",
    "confidence": "high",
    "is_user_ip": true,
    "provider_detected": "Google Workspace"
  }
}
```

**Field reference:**

| Path | Type | Notes |
|------|------|-------|
| `headers.from` / `to` / `cc` | `[{email, name}]` | Email addresses normalized to lowercase |
| `headers.in_reply_to` | `string \| null` | Message-ID of the parent message |
| `headers.references` | `string[]` | All ancestor Message-IDs in the conversation |
| `headers.auto_reply_type` | `string \| null` | `"disposition-notification"`, `"vacation-reply"`, or `null` |
| `text.content` / `html_content` | `string` | New content only — quoted replies are separated |
| `text.quote` / `html_quote` | `string` | The quoted/reply portion |
| `fingerprint.ip` | `string` | Best-guess sender IP (empty if undetectable) |
| `fingerprint.confidence` | `string` | `"high"`, `"medium"`, `"low"`, or `"none"` |
| `fingerprint.is_user_ip` | `boolean` | Whether the IP likely belongs to the actual sender |
| `fingerprint.provider_detected` | `string` | Identified email provider (e.g. `"Google Workspace"`, `"Microsoft 365"`) |

### `eml` — message/rfc822 (or application/gzip)

The original `.eml` file with a random UUID filename. Gzip-compressed when `COMPRESS_EML=true`.

### `attachment` — application/octet-stream

One part per attachment, using the original filename.

### Receiving example (PHP)

All data arrives in `$_FILES`, not `$_POST`:

```php
$manifest = json_decode(file_get_contents($_FILES['manifest']['tmp_name']), true);
$eml_path = $_FILES['eml']['tmp_name'];
// Attachments: $_FILES['attachment']['tmp_name'][0], [1], ...
```

## Processing flow

```
loop() — repeats until shutdown signal (SIGTERM / SIGINT):
│
├── For each IMAP account:
│   ├── Connect → LOGIN → SELECT inbox
│   ├── UID SEARCH (ALL, UNSEEN, or UNKEYWORD depending on mode and NOOP_FLAG)
│   ├── Batch download up to BATCH_SIZE messages
│   │
│   ├── For each downloaded message:
│   │   ├── Reject if > 25 MB → move to ERROR
│   │   ├── serialize_mail() → multipart/form-data body
│   │   ├── POST to WEBHOOK_URL
│   │   │   ├── 2xx           → move to SUCCESS / delete / mark processed
│   │   │   ├── 4xx + REFUSED → move to REFUSED → fallback ERROR → last resort
│   │   │   ├── 5xx           → retry with exponential backoff
│   │   │   └── final failure → move to ERROR → fallback last resort
│   │   └── Wait DELIVERY_INTERVAL (if batching)
│   │
│   └── EXPUNGE → CLOSE → LOGOUT
│
├── Update heartbeat file
├── Log stats (if interval elapsed)
└── Sleep DELAY seconds (if all inboxes were empty)
```

When messages are present, the daemon processes them immediately without sleeping — it only pauses when all inboxes are empty.

All IMAP folder operations have a **3-tier fallback**: target folder → ERROR folder → last-resort flag marking (`\Deleted` or the configured `NOOP_FLAG`).

## Testing

```bash
# Recommended: build the testing stage (runs all 216 tests at build time)
docker build --target testing -t imap-to-webhook-test .

# Or use the Makefile shortcut
make test

# Or run inside a running container (requires correct volume mount)
docker exec imap-to-webhook python test.py
```

## Project structure

```
├── daemon.py                  # Main loop: multi-account polling, delivery, retry
├── config.py                  # Environment variable parsing and validation
├── connection.py              # IMAP client wrapper (UID-based operations)
├── mail_parser.py             # Email parsing, manifest building, serialization
├── fingerprint.py             # Sender IP extraction from email headers
├── healthcheck.py             # Docker HEALTHCHECK script
├── stats.py                   # Runtime statistics counter
├── version.py                 # Version string
│
├── extract_raw_content/       # Email body / quote separation
│   ├── constants.py           #   Quote detection regex (EN/DE/FR/NL/ZH/JA/KO/ES/PT/IT/...)
│   ├── html.py                #   HTML quote stripping (BeautifulSoup)
│   ├── text.py                #   Plain text quote extraction
│   └── utils.py               #   Preprocessing (link normalization, splitter detection)
│
├── test.py                    # 216 unit tests (10 test classes)
├── mails/                     # Test .eml and HTML samples
├── Dockerfile                 # Dev image (CMD: sleep infinity for debugging)
├── Dockerfile.production      # Prod image (CMD: python daemon.py)
├── docker-compose.example.yml # Compose template (cp to docker-compose.yml)
├── Makefile                   # Shortcuts: make start/stop/build/test/lint
└── .env.example               # Environment variable template (cp to .env)
```

## Production deployment

Use `Dockerfile.production` — code is copied into the image, no volume mount required:

```bash
docker build -f Dockerfile.production -t imap-to-webhook:prod .
docker run -d --name imap-to-webhook \
  --env-file .env \
  --restart unless-stopped \
  imap-to-webhook:prod
```

Or switch the Dockerfile in `docker-compose.yml`:

```yaml
services:
  imap_to_webhook:
    build:
      context: .
      dockerfile: Dockerfile.production
    # Remove the volumes section — production image has code baked in
```

## License

[MIT](LICENSE) — Original copyright (c) 2018 Sieć Obywatelska - Watchdog Polska
