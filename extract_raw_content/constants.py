import re

MAX_LINES_COUNT = 1000
SPLITTER_MAX_LINES = 6
_MAX_TAGS_COUNT = 419
_BLOCKTAGS = ["div", "p", "ul", "li", "h1", "h2", "h3"]
_HARDBREAKS = ["br", "hr", "tr"]
_RE_EXCESSIVE_NEWLINES = re.compile("\n{2,10}")

QUOT_PATTERN = re.compile("^>+ ?")
RE_PARENTHESIS_LINK = re.compile(r"\(https?://")
RE_NORMALIZED_LINK = re.compile("@@(http://[^>@]*)@@")
RE_FWD = re.compile(
    "^[-\u2010\u2012\u2013\u2014\u2015\u2212]+[ ]*(?:{})[ ]*[-\u2010\u2012\u2013\u2014\u2015\u2212]+$".format(
        "|".join(
            (
                "Forwarded message",
                # Chinese
                "转发的邮件",
                "轉發的郵件",
                "转发邮件",
                "轉發郵件",
                # Japanese
                "転送されたメッセージ",
                # Korean
                "전달된 메시지",
                # Spanish
                "Mensaje reenviado",
                # Portuguese
                "Mensagem encaminhada",
                # Italian
                "Messaggio inoltrato",
            )
        )
    ),
    re.I | re.M,
)
RE_DELIMITER = re.compile("\r?\n")
RE_LINK = re.compile("<(http://[^>]*)>")
RE_ON_DATE_SMB_WROTE = re.compile(
    "(-*[>]?[ ]?({0})[ ].*({1})(.*\n){{0,2}}.*({2}):?-*)".format(
        # Beginning of the line
        "|".join(
            (
                # English
                "On",
                # French
                "Le",
                # Polish
                "W dniu",
                # Dutch
                "Op",
                # German
                "Am",
                # Norwegian
                "På",
                # Swedish, Danish
                "Den",
                # Vietnamese
                "Vào",
                # Chinese
                "在",
                # Spanish
                "El",
                # Portuguese
                "Em",
                # Italian
                "Il",
            )
        ),
        # Date and sender separator
        "|".join(
            (
                # most languages separate date and sender address by comma
                ",",
                # Chinese comma
                "，",
                # polish date and sender address separator
                "użytkownik",
            )
        ),
        # Ending of the line
        "|".join(
            (
                # English
                "wrote",
                "sent",
                # French
                "a écrit",
                # Polish
                "napisał",
                # Dutch
                "schreef",
                "verzond",
                "geschreven",
                # German
                "schrieb",
                # Norwegian, Swedish
                "skrev",
                # Vietnamese
                "đã viết",
                # Chinese
                "写道",
                "寫道",
                # Spanish
                "escribió",
                # Portuguese
                "escreveu",
                # Italian
                "ha scritto",
            )
        ),
    )
)
RE_ORIGINAL_MESSAGE = re.compile(
    "[\\s]*[-\u2010\u2012\u2013\u2014\u2015\u2212]+[ ]*({})[ ]*[-\u2010\u2012\u2013\u2014\u2015\u2212]+".format(
        "|".join(
            (
                # English
                "Original Message",
                "Reply Message",
                # German
                "Ursprüngliche Nachricht",
                "Antwort Nachricht",
                # Danish
                "Oprindelig meddelelse",
                # Chinese
                "原始邮件",
                "原始郵件",
                "原邮件",
                "原郵件",
                "转发邮件",
                "轉發郵件",
                # Japanese
                "元のメッセージ",
                # Korean
                "원래 메시지",
                # Spanish
                "Mensaje original",
                # Portuguese
                "Mensagem original",
                # Italian
                "Messaggio originale",
            )
        )
    ),
    re.I,
)
RE_ON_DATE_WROTE_SMB = re.compile(
    "(-*[>]?[ ]?({0})[ ].*(.*\n){{0,2}}.*({1})[ ]*.*:)".format(
        # Beginning of the line
        "|".join(
            (
                "Op",
                # German
                "Am",
            )
        ),
        # Ending of the line
        "|".join(
            (
                # Dutch
                "schreef",
                "verzond",
                "geschreven",
                # German
                "schrieb",
            )
        ),
    )
)

RE_FROM_COLON_OR_DATE_COLON = re.compile(
    "(_+\r?\n)?[\\s]*(:?[*]?{})[\\s]?[:：][*]?.*".format(
        "|".join(
            (
                # "From" in different languages.
                "From",
                "Van",
                "De",
                "Von",
                "Fra",
                "Från",
                # "Date" in different languages.
                "Date",
                "Datum",
                "Envoyé",
                "Skickat",
                "Sendt",
                # Chinese
                "发件人",
                "發件人",
                "收件人",
                "发送时间",
                "發送時間",
                "日期",
                "主题",
                "主題",
                "抄送",
                # Japanese
                "差出人",
                "宛先",
                "送信日時",
                "件名",
                # Korean
                "보낸 사람",
                "받는 사람",
                "보낸 날짜",
                "제목",
                "참조",
                # Spanish
                "Enviado",
                "Para",
                "Asunto",
                # Portuguese
                "Assunto",
                # Italian
                "Da",
                "Inviato",
                "Oggetto",
            )
        )
    ),
    re.I,
)
RE_ANDROID_WROTE = re.compile(
    r"[\s]*[-]+.*({})[ ]*[-]+".format(
        "|".join(
            (
                # English
                "wrote",
                # Spanish
                "escribió",
                # Portuguese
                "escreveu",
                # Italian
                "ha scritto",
                # French
                "a écrit",
                # German
                "schrieb",
                # Chinese
                "写道",
            )
        )
    ),
    re.I,
)
RE_POLYMAIL = re.compile(r"On.*\s{2}<\smailto:.*\s> wrote:", re.I)
RE_QUOTATION = re.compile(
    r"""
    (
        # quotation border: splitter line or a number of quotation marker lines
        (?:
            s
            |
            (?:me*){2,}
        )

        # quotation lines could be marked as splitter or text, etc.
        .*

        # but we expect it to end with a quotation marker line
        me*
    )

    # after quotations should be text only or nothing at all
    [te]*$
    """,
    re.VERBOSE,
)
RE_EMPTY_QUOTATION = re.compile(
    r"""
    (
        # quotation border: splitter line or a number of quotation marker lines
        (?:
            (?:se*)+
            |
            (?:me*){2,}
        )
    )
    e*
    """,
    re.VERBOSE,
)

SPLITTER_PATTERNS = [
    RE_ORIGINAL_MESSAGE,
    RE_ON_DATE_SMB_WROTE,
    RE_ON_DATE_WROTE_SMB,
    RE_FROM_COLON_OR_DATE_COLON,
    # 02.04.2012 14:20 пользователь "bob@example.com" <
    # bob@xxx.mailgun.org> написал:
    # Allow at most 1 newline between date and @, to avoid false positives
    # when body text contains a date pattern and a later line has an @ sign.
    re.compile(r"(\d+/\d+/\d+|\d+\.\d+\.\d+)[^\n]*\n?[^\n]*@"),
    # 2014-10-17 11:28 GMT+03:00 Bob <
    # bob@example.com>:
    re.compile(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s+GMT[^\n]*\n?[^\n]*@"),
    # Thu, 26 Jun 2014 14:00:51 +0400 Bob <bob@example.com>:
    re.compile(
        r"\S{3,10}, \d\d? \S{3,10} 20\d\d,? \d\d?:\d\d(:\d\d)?" r"( \S+){3,6}@\S+:"
    ),
    # Sent from Samsung MobileName <address@example.com> wrote:
    re.compile("Sent from Samsung .*@.*> wrote"),
    RE_ANDROID_WROTE,
    RE_POLYMAIL,
]
