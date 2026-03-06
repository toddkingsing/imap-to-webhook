"""
IP fingerprint extraction from email headers.

Translated 1:1 from PHP extractFingerprint() and related functions
in imap_webhook.php. Uses Python's ipaddress stdlib for private IP
detection instead of PHP's regex approach.
"""

import ipaddress
import re
from email import policy
from email.parser import BytesParser

# ---------------------------------------------------------------------------
# Provider identification
# ---------------------------------------------------------------------------

PROVIDER_MAP = {
    "protection.outlook.com": ("Microsoft 365", "enterprise"),
    ".prod.outlook.com": ("Microsoft 365", "enterprise"),
    "outlook.com": ("Outlook.com", "public"),
    "hotmail.com": ("Hotmail", "public"),
    "google.com": ("Google", "enterprise"),
    "googlemail.com": ("Gmail", "public"),
    "gmail-smtp": ("Gmail", "public"),
    "qiye.163.com": ("NetEase Enterprise", "enterprise"),
    "ntesmail.com": ("NetEase", "enterprise"),
    "163.com": ("NetEase 163", "public"),
    "126.com": ("NetEase 126", "public"),
    "exmail.qq.com": ("Tencent Exmail", "enterprise"),
    "qq.com": ("Tencent QQ", "public"),
    "mxhichina.com": ("Aliyun Mail", "enterprise"),
    "alibaba.com": ("Alibaba", "enterprise"),
    "yahoo.com": ("Yahoo", "public"),
    "yahoodns.net": ("Yahoo", "public"),
    "pphosted.com": ("Proofpoint", "relay"),
    "mimecast.com": ("Mimecast", "relay"),
    "barracudanetworks.com": ("Barracuda", "relay"),
    "sendgrid.net": ("SendGrid", "relay"),
    "amazonses.com": ("Amazon SES", "relay"),
    "mailgun.net": ("Mailgun", "relay"),
    "secureserver.net": ("GoDaddy", "enterprise"),
    "oxcs.net": ("GoDaddy", "relay"),
    "protonmail.com": ("ProtonMail", "public"),
    "proton.me": ("ProtonMail", "public"),
    "zoho.com": ("Zoho", "enterprise"),
    "icloud.com": ("Apple iCloud", "public"),
}

# Sorted by pattern length descending to avoid substring conflicts
_SORTED_PROVIDER_PATTERNS = sorted(
    PROVIDER_MAP.items(), key=lambda x: len(x[0]), reverse=True
)

# Direct IP headers: header_name -> (confidence, is_user_ip)
DIRECT_HEADERS = {
    "x-originating-ip": ("high", True),
    "x-sender-ip": ("high", True),
    "x-source-ip": ("medium", True),
    "x-real-ip": ("medium", True),
    "x-client-ip": ("medium", True),
    "x-qq-originating-ip": ("high", True),
    "x-mailer-ip": ("medium", True),
}

# Provider detection from address domains
ADDRESS_PROVIDER_MAP = {
    "@outlook.com": "Outlook.com",
    "@hotmail.com": "Hotmail",
    "@live.com": "Live",
    "@gmail.com": "Gmail",
    "@qq.com": "QQ Mail",
    "@foxmail.com": "Foxmail",
    "@163.com": "NetEase 163",
    "@126.com": "NetEase 126",
    "@yahoo.com": "Yahoo",
    "@icloud.com": "iCloud",
    "@protonmail.com": "ProtonMail",
    "@proton.me": "ProtonMail",
}

_IP_RE = re.compile(r"\[?(\d{1,3}(?:\.\d{1,3}){3})\]?")

# Received header IP extraction patterns (ordered by specificity)
_RECEIVED_IP_PATTERNS = [
    re.compile(r"from\s+\S+\s+\(\[?(\d{1,3}(?:\.\d{1,3}){3})\]?\)", re.I),
    re.compile(r"from\s+\S+\s+\([^\[]*\[(\d{1,3}(?:\.\d{1,3}){3})\]\)", re.I),
    re.compile(r"\((\d{1,3}(?:\.\d{1,3}){3})\)"),
    re.compile(r"\[(\d{1,3}(?:\.\d{1,3}){3})\]"),
]

_FROM_DOMAIN_RE = re.compile(r"from\s+([^\s(\[]+)", re.I)
_BY_DOMAIN_RE = re.compile(r"by\s+([^\s(\[]+)", re.I)


_CGN_NETWORK = ipaddress.ip_network("100.64.0.0/10")


def _is_private_ip(ip_str: str) -> bool:
    """Return True if ip_str is private/reserved or not a valid IPv4 address."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # non-IPv4 or invalid → treat as private
    if addr in _CGN_NETWORK:
        return True  # Carrier-grade NAT (RFC 6598)
    return addr.is_private or addr.is_reserved or addr.is_multicast


def _identify_provider(domain: str) -> tuple[bool, str, str]:
    """Identify email provider from domain string.

    Returns (found, provider_name, provider_type).
    """
    domain = domain.lower()
    for pattern, (name, ptype) in _SORTED_PROVIDER_PATTERNS:
        if pattern in domain:
            return (True, name, ptype)
    return (False, "", "")


def _is_known_provider(domain: str) -> bool:
    return _identify_provider(domain)[0]


def _detect_provider_from_headers(headers: dict) -> str:
    """Detect provider from provider-specific email headers."""
    if any(
        headers.get(h)
        for h in (
            "x-ms-exchange-organization-authas",
            "x-ms-exchange-crosstenant-id",
            "x-originatororg",
        )
    ):
        return "Microsoft 365"
    if any(
        headers.get(h) for h in ("x-gm-message-state", "x-google-dkim-signature")
    ):
        return "Google Workspace"
    if any(headers.get(h) for h in ("x-qq-mid", "x-qq-ssf")):
        return "Tencent QQ Mail"
    if any(headers.get(h) for h in ("x-cm-transid", "x-coremail-antispam")):
        return "NetEase"
    if headers.get("x-pm-message-id"):
        return "ProtonMail"
    return ""


def _detect_provider_from_address(headers: dict) -> str:
    """Detect provider from sender email address or return-path."""
    from_val = (headers.get("from") or "").lower()
    rp_val = (headers.get("return-path") or "").lower()
    for pattern, name in ADDRESS_PROVIDER_MAP.items():
        if pattern in from_val or pattern in rp_val:
            return name
    return ""


def _parse_received(line: str) -> dict:
    """Parse a single Received header line.

    Returns {ip, from_domain, by_domain}.
    """
    result = {"ip": "", "from_domain": "", "by_domain": ""}

    m = _FROM_DOMAIN_RE.search(line)
    if m:
        result["from_domain"] = m.group(1).lower().strip()

    m = _BY_DOMAIN_RE.search(line)
    if m:
        result["by_domain"] = m.group(1).lower().strip()

    for pat in _RECEIVED_IP_PATTERNS:
        m = pat.search(line)
        if m:
            ip_str = m.group(1)
            try:
                ipaddress.ip_address(ip_str)  # validate
                result["ip"] = ip_str
                break
            except ValueError:
                continue

    return result


def _flatten_headers(raw_bytes: bytes) -> dict:
    """Parse raw email bytes and return all headers as {lowercase_key: value_str}."""
    msg = BytesParser(policy=policy.default).parsebytes(raw_bytes)
    flat = {}
    seen = set()
    for key in msg.keys():
        lk = key.lower()
        if lk in seen:
            continue
        seen.add(lk)
        vals = msg.get_all(key, []) or []
        flat[lk] = "\n".join(str(v) for v in vals)
    return flat


def extract_fingerprint(raw_bytes: bytes) -> dict:
    """Main entry point: extract sender IP fingerprint from raw email bytes.

    Returns {ip, confidence, is_user_ip, provider_detected}.
    """
    result = {
        "ip": "",
        "confidence": "none",
        "is_user_ip": False,
        "provider_detected": "",
    }

    if not raw_bytes:
        return result

    headers = _flatten_headers(raw_bytes)
    candidates = []

    # Detect provider
    result["provider_detected"] = _detect_provider_from_headers(
        headers
    ) or _detect_provider_from_address(headers)

    # Phase 1: Direct headers
    for header_name, (conf, is_user) in DIRECT_HEADERS.items():
        val = headers.get(header_name, "")
        if not val:
            continue
        m = _IP_RE.search(val)
        if m and not _is_private_ip(m.group(1)):
            candidates.append(
                {"ip": m.group(1), "confidence": conf, "is_user_ip": is_user}
            )
            if conf == "high" and is_user:
                result["ip"] = m.group(1)
                result["confidence"] = "high"
                result["is_user_ip"] = True
                return result

    # Phase 2: Received headers
    received_raw = headers.get("received", "")
    lines = received_raw.split("\n") if received_raw else []

    # Merge continuation lines (lines starting with whitespace)
    merged = []
    cur = ""
    for line in lines:
        if line and line[0] in (" ", "\t"):
            cur += " " + line.strip()
        else:
            if cur:
                merged.append(cur)
            cur = line.strip()
    if cur:
        merged.append(cur)

    for rcv in reversed(merged):
        p = _parse_received(rcv)
        if not p["ip"] or _is_private_ip(p["ip"]):
            continue

        from_prov = bool(p["from_domain"]) and _is_known_provider(p["from_domain"])
        by_prov = bool(p["by_domain"]) and _is_known_provider(p["by_domain"])
        likely_user = not from_prov and by_prov

        conf = "medium" if likely_user else "low"

        candidates.append(
            {"ip": p["ip"], "confidence": conf, "is_user_ip": likely_user}
        )

        if result["ip"] == "" and likely_user:
            result["ip"] = p["ip"]
            result["confidence"] = conf
            result["is_user_ip"] = True

    # Phase 3: Select best candidate
    if result["ip"] == "" and candidates:
        for c in candidates:
            if c.get("is_user_ip"):
                result["ip"] = c["ip"]
                result["confidence"] = c["confidence"]
                result["is_user_ip"] = True
                break
        if result["ip"] == "":
            first = candidates[0]
            result["ip"] = first["ip"]
            result["confidence"] = first.get("confidence", "low")
            result["is_user_ip"] = first.get("is_user_ip", False)

    return result
