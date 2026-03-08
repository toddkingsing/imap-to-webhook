import re

# Maximum non-empty lines (from the bottom) to consider as signature candidate.
SIGNATURE_MAX_LINES = 11

# Lines longer than this are unlikely to be part of a signature.
TOO_LONG_SIGNATURE_LINE = 60

# ---------------------------------------------------------------------------
# Phone / device signatures
# ---------------------------------------------------------------------------
_PHONE_PATTERNS = [
    # English
    r"sent\s+from\s+(?:my\s+)?\w[\w ]{0,80}",
    r"sent\s+from\s+(?:outlook|mail|mailbox)\s+for\s+\w[\w ]{0,80}",
    # French
    r"envoy[ée]\s+depuis\s+mon\s+\w[\w ]{0,80}",
    # Polish
    r"wys[lł]ano\s+z\s+(?:mojego\s+)?\w[\w ]{0,80}",
    # Dutch
    r"verzonden\s+vanaf\s+mijn\s+\w[\w ]{0,80}",
    # German
    r"gesendet\s+von\s+meinem?\s+\w[\w ]{0,80}",
    # Norwegian / Danish
    r"sendt\s+fra\s+min\s+\w[\w ]{0,80}",
    # Swedish
    r"skickat\s+fr[aå]n\s+min\s+\w[\w ]{0,80}",
    # Vietnamese
    r"gửi\s+từ\s+\w[\w ]{0,80}",
    # Chinese simplified / traditional
    r"发自我的\s*\S+",
    r"從我的\s*\S+",
    # Japanese
    r"iPhoneから送信",
    # Korean
    r"[내나]의?\s*(?:iPhone|iPad|Galaxy)에서\s*보냄",
    # Spanish
    r"enviado\s+desde\s+mi\s+\w[\w ]{0,80}",
    # Portuguese
    r"enviado\s+(?:do|pelo)\s+meu\s+\w[\w ]{0,80}",
    # Italian
    r"inviato\s+da[l]?\s+(?:mio\s+)?\w[\w ]{0,80}",
    # Russian
    r"отправлено\s+с\s+\w[\w ]{0,80}",
]

RE_PHONE_SIGNATURE = re.compile(
    r"^\s*(?:{})".format("|".join(_PHONE_PATTERNS)),
    re.I | re.M,
)

# ---------------------------------------------------------------------------
# Dash delimiter  (RFC 3676: "-- " or common variants "--", "---", …)
# ---------------------------------------------------------------------------
RE_DASH_DELIMITER = re.compile(r"^[\s]*-{2,}[\s]*$")

# ---------------------------------------------------------------------------
# Closing phrases — multi-language
# ---------------------------------------------------------------------------
# Trailing punctuation allowed after the phrase.
_TRAIL = r"[\s,!.。！，、;；：:\?？…~～\-]*"

_CLOSING_PHRASES = [
    # English
    r"thanks",
    r"thank\s+you",
    r"regards",
    r"best\s+regards",
    r"kind\s+regards",
    r"warm\s+regards",
    r"cheers",
    r"best\s+wishes",
    r"best",
    r"sincerely",
    r"yours\s+(?:truly|sincerely|faithfully)",
    # French
    r"cordialement",
    r"bien\s+cordialement",
    r"merci",
    r"salutations\s+distingu[ée]es",
    # Polish
    r"pozdrawiam",
    r"z\s+poważaniem",
    r"dziękuję",
    # Dutch
    r"met\s+vriendelijke\s+groet(?:en)?",
    r"groet(?:en)?",
    r"bedankt",
    # German
    r"mit\s+freundlichen\s+gr[üu][ßs]en",
    r"viele\s+gr[üu][ßs]e",
    r"freundliche\s+gr[üu][ßs]e",
    r"danke",
    r"mfg",
    # Norwegian
    r"med\s+vennlig\s+hilsen",
    r"hilsen",
    r"takk",
    # Swedish
    r"med\s+v[äa]nliga\s+h[äa]lsningar",
    r"h[äa]lsningar",
    r"tack",
    # Danish
    r"med\s+venlig\s+hilsen",
    r"mvh",
    # Vietnamese
    r"trân\s+trọng",
    r"cảm\s+ơn",
    # Chinese (simplified + traditional)
    r"此致敬礼",
    r"此致",
    r"敬上",
    r"谢谢",
    r"謝謝",
    r"感谢",
    r"顺颂商祺",
    r"敬祝",
    # Japanese
    r"よろしくお願い(?:します|いたします|致します)",
    r"敬具",
    r"草々",
    # Korean
    r"감사합니다",
    r"감사드립니다",
    # Spanish
    r"saludos?\s*(?:cordiales)?",
    r"gracias",
    r"atentamente",
    r"cordialmente",
    r"un\s+saludo",
    # Portuguese
    r"obrigad[oa]",
    r"atenciosamente",
    r"cumprimentos",
    # Italian
    r"cordiali\s+saluti",
    r"distinti\s+saluti",
    r"grazie",
    r"saluti",
    # Russian
    r"с\s+уважением",
    r"спасибо",
    r"с\s+наилучшими\s+пожеланиями",
]

RE_CLOSING = re.compile(
    r"^\s*(?:{}){}\s*$".format("|".join(_CLOSING_PHRASES), _TRAIL),
    re.I,
)


def extract_signature(text):
    """
    Detect and return an email signature from *text* (plain text, quotes
    already removed).

    Returns the signature as a string, or ``""`` if none is detected.
    The input text is **never modified**.
    """
    if not text or not text.strip():
        return ""

    lines = text.splitlines()

    # Need at least 2 non-empty lines (1 body + 1 signature).
    non_empty = [i for i, line in enumerate(lines) if line.strip()]
    if len(non_empty) <= 1:
        return ""

    # Scan range: last SIGNATURE_MAX_LINES non-empty lines, excluding the
    # very first non-empty line (it cannot be the start of a signature).
    scan_pool = non_empty[1:][-SIGNATURE_MAX_LINES:]
    scan_start = scan_pool[0]

    # 1. Dash delimiter (highest confidence) — "-- " / "--" / "---" …
    for i in range(scan_start, len(lines)):
        if RE_DASH_DELIMITER.match(lines[i]):
            return "\n".join(lines[i:]).strip()

    # 2. Closing phrase
    for i in range(scan_start, len(lines)):
        if not lines[i].strip():
            continue
        if RE_CLOSING.match(lines[i]):
            remaining = lines[i:]
            # All remaining non-empty lines must be short.
            if all(
                len(l.strip()) <= TOO_LONG_SIGNATURE_LINE
                for l in remaining
                if l.strip()
            ):
                return "\n".join(remaining).strip()

    # 3. Phone / device signature (fallback — usually last line)
    # Restrict search to the same tail region as steps 1 & 2.
    tail_text = "\n".join(lines[scan_start:])
    last_phone = None
    for m in RE_PHONE_SIGNATURE.finditer(tail_text):
        last_phone = m
    if last_phone:
        return tail_text[last_phone.start():].strip()

    return ""
