# imap-to-webhook

Poll one or more IMAP mailboxes, parse each email into structured data, and deliver it to an HTTP endpoint as `multipart/form-data`.

Forked from [watchdogpolska/imap-to-webhook](https://github.com/watchdogpolska/imap-to-webhook). This fork adds multi-account support, batch processing, webhook retries, IP fingerprinting, Chinese email quote detection, a v3 manifest format, and Python 3.14 compatibility.

```
┌───────────┐      ┌──────────────┐      ┌──────────────┐
│   IMAP    │ poll │  daemon.py   │ POST │  Your HTTP   │
│ mailboxes ├─────►│ parse + send ├─────►│   endpoint   │
└───────────┘      └──────┬───────┘      └──────────────┘
                          │
              on success: move / delete / mark read
              on failure: move to ERROR folder
```

## Quick start

```bash
git clone https://github.com/toddkingsing/imap-to-webhook.git
cd imap-to-webhook

# Configure
mkdir -p env
cp .env.sample env/aggregator.env
# Edit env/aggregator.env — fill in IMAP_URL and WEBHOOK_URL

# Run
cp docker-compose.example.yml docker-compose.yml
docker compose up -d --build

# Verify
docker logs -f imap-to-webhook
```

## Configuration

All settings are environment variables. See [`.env.sample`](.env.sample) for the full annotated template.

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
| `IMAP_URL` | IMAP connection URL (see format above) |
| `WEBHOOK_URL` | HTTP(S) endpoint that receives parsed emails |

**Processing:**

| Variable | Default | Description |
|----------|---------|-------------|
| `ON_SUCCESS` | `move` | After successful delivery: `move`, `delete`, or `noop` (mark as read, leave in inbox) |
| `NOOP_FLAG` | `\Seen` | IMAP flag for noop mode tracking (`$WebhookProcessed` if other clients read mail) |
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
│   ├── UID SEARCH (ALL, UNSEEN, or UNKEYWORD in noop mode)
│   ├── Batch download up to BATCH_SIZE messages
│   │
│   ├── For each downloaded message:
│   │   ├── Reject if > 25 MB → move to ERROR
│   │   ├── serialize_mail() → multipart/form-data body
│   │   ├── POST to WEBHOOK_URL
│   │   │   ├── 2xx           → move to SUCCESS / delete / mark processed
│   │   │   ├── 4xx + REFUSED → move to REFUSED
│   │   │   ├── 5xx           → retry with exponential backoff
│   │   │   └── final failure → move to ERROR
│   │   └── Wait DELIVERY_INTERVAL (if batching)
│   │
│   └── EXPUNGE → CLOSE → LOGOUT
│
├── Update heartbeat file
├── Log stats (if interval elapsed)
└── Sleep DELAY seconds (if all inboxes were empty)
```

When messages are present, the daemon processes them immediately without sleeping — it only pauses when all inboxes are empty.

## Testing

```bash
# Build the testing image stage — runs all tests at build time (recommended)
docker build --target testing -t imap-to-webhook-test .

# Or run inside a running container
docker exec imap-to-webhook python test.py

# Or run locally (Python 3.12+)
pip install -r requirements.txt
python test.py
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
├── version.py                 # Version string (1.3.0)
│
├── extract_raw_content/       # Email body / quote separation
│   ├── constants.py           #   Quote detection regex (EN/DE/FR/NL/ZH/JA/KO/...)
│   ├── html.py                #   HTML quote stripping (BeautifulSoup)
│   ├── text.py                #   Plain text quote extraction
│   └── utils.py               #   Preprocessing (link normalization, splitter detection)
│
├── test.py                    # Unit tests
├── mails/                     # Test .eml and HTML samples
├── mock/                      # Flask mock webhook server for development
│
├── Dockerfile                 # Dev image (CMD: sleep infinity for debugging)
├── Dockerfile.production      # Prod image (CMD: python daemon.py)
├── docker-compose.example.yml # Compose template
├── .env.sample                # Environment variable template
└── env/
    └── aggregator.env         # Your actual env file (gitignored)
```

## Production deployment

Use `Dockerfile.production` — code is copied into the image, no volume mount required:

```bash
docker build -f Dockerfile.production -t imap-to-webhook:prod .
docker run -d --name imap-to-webhook \
  --env-file ./env/aggregator.env \
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

## Upstream sync

```bash
git remote add upstream https://github.com/watchdogpolska/imap-to-webhook.git
git fetch upstream
git merge upstream/master
```

## License

[MIT](LICENSE) — Original copyright (c) 2018 Sieć Obywatelska - Watchdog Polska
