import re
from typing import Tuple

from bs4 import BeautifulSoup, Comment, NavigableString, Tag


def strip_email_quote(msg_body) -> Tuple[str, str]:
    """
    Return ``(clean_html, quote_html)`` where *quote_html* contains every
    fragment that looks like an earlier-message quotation and *clean_html*
    is the remainder.

    The function is defensive against quirky, half-broken markup coming from
    real-world mailers (mixed encodings, stray <>, Word HTML, etc.).  It does
    **not** raise – if a node blows up on serialisation we fall back to its
    plain-text representation so your pipeline keeps moving.
    """

    if isinstance(msg_body, bytes):
        msg_body = msg_body.decode("utf-8", "replace")

    soup = BeautifulSoup(msg_body, "html.parser")  # forgiving parser
    extracted_parts: list[str] = []

    # -- 0 · Cut at first <hr> (if present) ---------------------------------
    _preprocess_outlook(soup)  # Outlook-specific normalisations
    _harvest_from_first_hr(soup, extracted_parts)

    # -- 1 · Strip classic “Original message …” comment blocks ---------------
    for c in soup.find_all(
        string=lambda t: isinstance(t, Comment)
        and re.search(r"(original message|forwarded message|reply below)", t, re.I)
    ):
        _safe_append(extracted_parts, f"<!--{c}-->")
        c.extract()

    # -- 2 · Identify every element that *might* be a quotation --------------
    quote_blocks: list[Tag] = []
    for candidate in soup.find_all(looks_like_quote):
        # keep only *outermost* quote blocks
        if not any(parent in quote_blocks for parent in candidate.parents):
            quote_blocks.append(candidate)

    # -- 3 · Lift the blocks out ---------------------------------------------
    for block in quote_blocks:
        _safe_append(extracted_parts, _outer_html(block))
        block.decompose()

    clean_html = soup.decode(formatter="minimal")  # lightweight serialiser
    quote_html = "".join(extracted_parts)

    return clean_html, quote_html


def looks_like_quote(tag: Tag) -> bool:  # now top-level
    if tag.name == "blockquote":
        return True
    if (tag.get("id") or "").lower() in {
        "gmail_quote",
        "yahoo_quoted",
        "divrplyfwdmsg",
        "outlookquotedcontent",
        "olk_src_body_section",
    }:
        return True
    tcls = " ".join(tag.get("class", [])).lower()
    if any(
        k in tcls
        for k in (
            "gmail_quote",
            "yahoo_quoted",
            "js-email-quote",
            "outlookmessageheader",
        )
    ):
        return True
    style = (tag.get("style") or "").replace(" ", "").lower()
    return bool(re.search(r"border-left[^:]*:\s*\d+px", style))


def _preprocess_outlook(soup: BeautifulSoup) -> None:
    """
    Make old and new Outlook HTML easy for `_harvest_from_first_hr`:
      • lower-case every class/id,
      • if we see the grey divider (`border-top:`) *and* it contains
        header keywords → drop a real `<hr>` before it,
      • else: find the first paragraph that *looks* like the Outlook
        header (From/Sent/To/Subject in EN/PL/RU) and drop `<hr>` before
        that.
    """

    hr = soup.find("hr")
    if hr:
        return

    # 1 – lower-case class/id
    for tag in soup.find_all(True):
        if tag.has_attr("class"):
            tag["class"] = [cls.lower() for cls in tag["class"]]
        if tag.has_attr("id"):
            tag["id"] = tag["id"].lower()

    # ------------------------------------------------------------------
    #  localisation: header keywords we accept as “the quoted part starts
    #  here”.  Lower-case because we lowercase the text before matching.
    # ------------------------------------------------------------------
    HDR_WORDS = {
        # English
        "from:",
        "sent:",
        "to:",
        "cc:",
        "subject:",
        # Polish
        "od:",
        "wysłano:",
        "do:",
        "dw:",
        "temat:",
        # Russian
        "от:",
        "отправлено:",
        "кому:",
        "копия:",
        "тема:",
        # Chinese (simplified + traditional)
        "发件人:",
        "發件人:",
        "收件人:",
        "发送时间:",
        "發送時間:",
        "主题:",
        "主題:",
        "抄送:",
        "日期:",
        # Chinese fullwidth colon variants
        "发件人：",
        "發件人：",
        "收件人：",
        "发送时间：",
        "發送時間：",
        "主题：",
        "主題：",
        "抄送：",
        "日期：",
        # Japanese
        "差出人:",
        "宛先:",
        "送信日時:",
        "件名:",
        # Japanese fullwidth colon variants
        "差出人：",
        "宛先：",
        "送信日時：",
        "件名：",
        # Korean
        "보낸 사람:",
        "받는 사람:",
        "보낸 날짜:",
        "제목:",
        "참조:",
        # Korean fullwidth colon variants
        "보낸 사람：",
        "받는 사람：",
        "보낸 날짜：",
        "제목：",
        "참조：",
        # Spanish
        "de:",
        "enviado:",
        "para:",
        "asunto:",
        # Portuguese
        "assunto:",
        # Italian
        "da:",
        "inviato:",
        "oggetto:",
    }

    # ..................................................................
    #  2a · Try modern Outlook: <div style="border-top:…">
    # ..................................................................
    def is_divider(tag: Tag) -> bool:
        if tag.name not in {"div", "p"} or not tag.has_attr("style"):
            return False
        st = tag["style"].lower()
        if "border-top:" not in st:
            return False
        if not re.search(r"border-top:[^;]*\d+(?:px|pt|em)", st):
            return False
        return any(w in tag.get_text(" ", strip=True).lower() for w in HDR_WORDS)

    for divider in soup.find_all(is_divider):
        marker = soup.new_tag("hr")
        marker["data-synthetic"] = "outlook"
        divider.insert_before(marker)
        return

    # ..................................................................
    #  2b · Fallback for *old* Outlook – no grey line, but a header para
    # ..................................................................
    # Short words that can appear in normal body text — require a second
    # header keyword nearby to treat the paragraph as a quote boundary.
    _AMBIGUOUS_STARTERS = {
        "to:", "de:", "da:", "do:", "cc:", "od:", "an:", "от:",
        "to：", "de：", "da：", "do：", "cc：", "od：", "an：", "от：",
    }

    def is_header_para(tag: Tag) -> bool:
        if tag.name not in {"p", "div"}:
            return False
        text = tag.get_text(" ", strip=True).lower()
        starter = None
        for w in HDR_WORDS:
            if text.startswith(w):
                starter = w
                break
        if starter is None:
            return False
        # Unambiguous starters (e.g. "from:", "subject:") are safe alone.
        if starter not in _AMBIGUOUS_STARTERS:
            return True
        # Ambiguous starters need a second header keyword in the same block.
        return sum(1 for w in HDR_WORDS if w in text) >= 2

    header = soup.find(is_header_para)
    if header is not None:
        marker = soup.new_tag("hr")
        marker["data-synthetic"] = "outlook"
        header.insert_before(marker)


_HR_VALIDATION_WORDS = {
    # English
    "from:",
    "sent:",
    "to:",
    "subject:",
    "date:",
    # Polish
    "od:",
    "wysłano:",
    "temat:",
    # Russian
    "от:",
    "отправлено:",
    "кому:",
    "тема:",
    # Chinese
    "发件人:",
    "發件人:",
    "收件人:",
    "主题:",
    "主題:",
    "发件人：",
    "發件人：",
    "收件人：",
    "主题：",
    "主題：",
    # Japanese
    "差出人:",
    "宛先:",
    "件名:",
    "差出人：",
    "宛先：",
    "件名：",
    # Korean
    "보낸 사람:",
    "받는 사람:",
    "제목:",
    "보낸 사람：",
    "받는 사람：",
    "제목：",
    # Spanish / Portuguese / Italian
    "de:",
    "enviado:",
    "para:",
    "asunto:",
    "assunto:",
    "da:",
    "inviato:",
    "oggetto:",
}


def _harvest_from_first_hr(soup: BeautifulSoup, bucket: list[str]) -> None:
    """
    Move the first <hr> *and everything that follows it* into *bucket*,
    then delete those nodes from *soup*.

    Synthetic <hr> tags (injected by ``_preprocess_outlook``) are trusted
    unconditionally.  Native <hr> tags are only treated as quote separators
    when the text immediately following them contains mail-header keywords
    (From:/To:/Subject:/Date: etc.).
    """
    hr = soup.find("hr")
    if hr is None:
        return

    # Native <hr> (no data-synthetic) — validate before cutting
    if not hr.get("data-synthetic"):
        following_text = ""
        for sibling in hr.next_elements:
            if isinstance(sibling, NavigableString):
                following_text += str(sibling)
            elif isinstance(sibling, Tag):
                following_text += sibling.get_text(" ", strip=False)
            if len(following_text) >= 500:
                break
        following_lower = following_text[:500].lower()
        if not any(w in following_lower for w in _HR_VALIDATION_WORDS):
            return  # decorative <hr>, leave it alone

    # Collect the <hr> and everything after it in the document.
    # We walk the ancestor chain, collecting all following siblings at each
    # level.  This handles nested structures (e.g., Outlook 2003 where the
    # <hr> is deep inside wrapper divs) while avoiding the id()-aliasing
    # problem of the old next_elements approach.
    to_remove = []

    # Start at the <hr> itself: collect it and its following siblings.
    node = hr
    while node is not None:
        to_remove.append(node)
        node = node.next_sibling

    # Walk up ancestors and collect their following siblings too.
    ancestor = hr.parent
    while ancestor and ancestor.name not in (None, "[document]", "html", "body"):
        sibling = ancestor.next_sibling
        while sibling is not None:
            to_remove.append(sibling)
            sibling = sibling.next_sibling
        ancestor = ancestor.parent

    for node in to_remove:
        if isinstance(node, Tag):
            _safe_append(bucket, _outer_html(node))
        elif isinstance(node, NavigableString) and str(node).strip():
            _safe_append(bucket, str(node))
        node.extract()


def _outer_html(tag: Tag) -> str:
    """
    Serialise a Tag to HTML as safely as possible.

    BeautifulSoup occasionally hits a corner-case where ``tag.name`` becomes
    *None* (malformed input or after certain tree mutations).  When that
    happens we fall back to its text representation so callers never see the
    ugly ``TypeError: can only concatenate str (not "NoneType") to str``.
    """
    try:
        return tag.decode(formatter="minimal")
    except Exception:  # pragma: no cover  – last-chance net
        return tag.get_text(strip=False)


def _safe_append(lst: list[str], fragment: str) -> None:
    """
    Append ``fragment`` to ``lst``, guaranteeing that every entry is text –
    prevents later ``TypeError`` when ``"".join(...)`` is called.
    """
    lst.append(fragment or "")
