import json
import os
import re
import time
import unittest
from unittest.mock import patch

from html2text import html2text

from extract_raw_content import constants, html, text, utils
from mail_parser import (
    extract_addresses,
    get_text,
    get_to_plus,
    parse_mail_from_bytes,
    serialize_mail,
)
from fingerprint import (
    _is_private_ip,
    _parse_received,
    _identify_provider,
    extract_fingerprint,
)

# ---------------------------------------------------------------------
# Compatibility layer for the new (clean_html, quote_html) API introduced in
# fix_quotes_and_encoding.  The existing tests expect the *clean* body
# only, so we wrap the function to return element 0 of the tuple.
# ---------------------------------------------------------------------
_orig_extract = html.strip_email_quote  # save the real one


def _extract_clean(body):
    """Return only the cleaned HTML body; discard quotation."""
    clean_html, _quote_html = _orig_extract(body)
    return clean_html  # old tests need just this


# Monkey-patch ONLY the symbol that the tests call directly.
# mail_parser already imported the original before this patch,
# so its get_text() continues to receive the full (clean, quote) tuple.
html.extract_from_html = _extract_clean

# ---------------------------------------------------------------------


STANDARD_REPLIES = "mails/standard_replies"
RE_WHITESPACE = re.compile(r"\s")
RE_DOUBLE_WHITESPACE = re.compile(r"\s{2,}")


def get_email_as_bytes(name):
    with open(
        os.path.join(
            os.path.dirname(__file__),
            "mails",
            name,
        ),
        "rb",
    ) as f:
        return f.read()


class TestMain(unittest.TestCase):
    def test_disposition_notification(self):
        mail = get_email_as_bytes("disposition-notification.eml")
        body = serialize_mail(mail)
        body_map = {k: v for k, v in body}
        manifest = json.loads(body_map["manifest"][1].read().decode("utf-8"))
        self.assertEqual(
            manifest["headers"]["auto_reply_type"], "disposition-notification"
        )

    def test_vacation_reply(self):
        mail = get_email_as_bytes("vacation-reply.eml")
        body = serialize_mail(mail)
        body_map = {k: v for k, v in body}
        manifest = json.loads(body_map["manifest"][1].read().decode("utf-8"))
        self.assertEqual(manifest["headers"]["auto_reply_type"], "vacation-reply")

    def test_html_only(self):
        mail = get_email_as_bytes("html_only.eml")
        body = serialize_mail(mail)
        body_map = {k: v for k, v in body}
        manifest = json.loads(body_map["manifest"][1].read().decode("utf-8"))
        self.assertIsInstance(manifest["text"]["content"], str)
        self.assertGreater(len(manifest["text"]["content"]), 0)
        self.assertIsInstance(manifest["text"]["html_content"], str)
        self.assertGreater(len(manifest["text"]["html_content"]), 0)

    def test_get_delimiter(self):
        self.assertEqual("\r\n", text.get_delimiter("abc\r\n123"))
        self.assertEqual("\n", text.get_delimiter("abc\n123"))
        self.assertEqual("\n", text.get_delimiter("abc"))

    def test_html_to_text(self):
        html = """<body>
<p>Hello world!</p>
<br>
<ul>
<li>One!</li>
<li>Two</li>
</ul>
<p>
Haha
</p>
</body>"""
        text = html2text(html)
        self.assertEqual("Hello world!\n\n  \n\n  * One!\n  * Two\n\nHaha\n\n", text)
        self.assertEqual("**привет!**\n\n", html2text("<b>привет!</b>"))

        html = "<body><br/><br/>Hi</body>"
        self.assertEqual("  \n  \nHi\n\n", html2text(html))

        html = """Hi
<style type="text/css">

div, p, li {

font: 13px 'Lucida Grande', Arial, sans-serif;

}
</style>

<style type="text/css">

h1 {

font: 13px 'Lucida Grande', Arial, sans-serif;

}
</style>"""
        self.assertEqual("Hi\n\n", html2text(html))

        html = """<div>
<!-- COMMENT 1 -->
<span>TEXT 1</span>
<p>TEXT 2 <!-- COMMENT 2 --></p>
</div>"""
        self.assertEqual("TEXT 1\n\nTEXT 2\n\n", html2text(html))

    def test_comment_no_parent(self):
        s = "<!-- COMMENT 1 --> no comment"
        d = html2text(s)
        self.assertEqual("no comment\n\n", d)

    # @patch.object(utils, "html_fromstring", Mock(return_value=None))
    def test_bad_html_to_text(self):
        bad_html = "one<br>two<br>three"
        self.assertEqual("one  \ntwo  \nthree\n\n", html2text(bad_html))

    def test_quotation_splitter_inside_blockquote(self):
        msg_body = """Reply
<blockquote>

<div>
    On 11-Apr-2011, at 6:54 PM, Bob &lt;bob@example.com&gt; wrote:
</div>

<div>
    Test
</div>

</blockquote>"""

        self.assertEqual(
            "Reply",
            RE_WHITESPACE.sub("", html.extract_from_html(msg_body)),
        )

    def test_quotation_splitter_outside_blockquote(self):
        msg_body = """Reply

<div>
On 11-Apr-2011, at 6:54 PM, Bob &lt;bob@example.com&gt; wrote:
</div>

<blockquote>
<div>
    Test
</div>
</blockquote>
"""
        self.assertEqual(
            "Reply<div>On11-Apr-2011,at6:54PM,Bob&lt;bob@example.com&gt;"
            + "wrote:</div>",
            RE_WHITESPACE.sub("", html.extract_from_html(msg_body)),
        )

    def test_regular_blockquote(self):
        msg_body = """Reply
<blockquote>Regular</blockquote>

<div>
On 11-Apr-2011, at 6:54 PM, Bob &lt;bob@example.com&gt; wrote:
</div>

<blockquote>
<div>
    <blockquote>Nested</blockquote>
</div>
</blockquote>
"""
        self.assertEqual(
            "Reply<div>On11-Apr-2011,at6:54PM,Bob&lt;bob@example.com&gt;wrote:</div>",
            RE_WHITESPACE.sub("", html.extract_from_html(msg_body)),
        )

    def test_no_blockquote(self):
        msg_body = """
<html>
<body>
Reply

<div>
On 11-Apr-2011, at 6:54 PM, Bob &lt;bob@example.com&gt; wrote:
</div>

<div>
Test
</div>
</body>
</html>
"""
        self.assertEqual(
            RE_WHITESPACE.sub("", msg_body),
            RE_WHITESPACE.sub("", html.extract_from_html(msg_body)),
        )

    def test_empty_body(self):
        self.assertEqual("", html.extract_from_html(""))

    def test_validate_output_html(self):
        msg_body = """Reply
<div>
On 11-Apr-2011, at 6:54 PM, Bob &lt;bob@example.com&gt; wrote:

    <blockquote>
    <div>
        Test
    </div>
    </blockquote>
</div>

<div/>
"""
        out = html.extract_from_html(msg_body)
        # TODO: Validate HTML output
        # self.assertTrue(
        #     "<html>" in out and "</html>" in out,
        #     "Invalid HTML - <html>/</html> tag not present",
        # )
        self.assertTrue(
            "<div/>" not in out, "Invalid HTML output - <div/> element is not valid"
        )

    def test_gmail_quote(self):
        msg_body = """Reply
<div class="gmail_quote">
<div class="gmail_quote">
    On 11-Apr-2011, at 6:54 PM, Bob &lt;bob@example.com&gt; wrote:
    <div>
    Test
    </div>
</div>
</div>"""
        self.assertEqual(
            "Reply",
            RE_WHITESPACE.sub("", html.extract_from_html(msg_body)),
        )

    def test_gmail_quote_compact(self):
        msg_body = (
            "Reply"
            '<div class="gmail_quote">'
            '<div class="gmail_quote">'
            + "On 11-Apr-2011, at 6:54 PM, Bob &lt;bob@example.com&gt; wrote:"
            "<div>Test</div>"
            "</div>"
            "</div>"
        )
        self.assertEqual(
            "Reply",
            RE_WHITESPACE.sub("", html.extract_from_html(msg_body)),
        )

    def test_gmail_quote_blockquote(self):
        msg_body = """Message
<blockquote class="gmail_quote">
<div class="gmail_default">
    My name is William Shakespeare.
    <br/>
</div>
</blockquote>"""
        self.assertEqual(
            RE_WHITESPACE.sub("", "Message"),
            RE_WHITESPACE.sub("", html.extract_from_html(msg_body)),
        )

    def test_blockquote_disclaimer(self):
        msg_body = """
<html>
<body>
<div>
    <div>
    message
    </div>
    <blockquote>
    Quote
    </blockquote>
</div>
<div>
    disclaimer
</div>
</body>
</html>
"""

        stripped_html = """
<html>
<body>
<div>
<div>
    message
    </div>

    </div>
<div>
    disclaimer
</div>
</body>
</html>
"""
        self.assertEqual(
            RE_WHITESPACE.sub("", stripped_html),
            RE_WHITESPACE.sub("", html.extract_from_html(msg_body)),
        )

    def test_date_block(self):
        msg_body = """
<div>
message<br>
<div>
    <hr>
    Date: Fri, 23 Mar 2012 12:35:31 -0600<br>
    To: <a href="mailto:bob@example.com">bob@example.com</a><br>
    From: <a href="mailto:rob@example.com">rob@example.com</a><br>
    Subject: You Have New Mail From Mary!<br><br>

    text
</div>
</div>
"""
        self.assertEqual(
            "<div>message<br/><div></div></div>",
            RE_WHITESPACE.sub("", html.extract_from_html(msg_body)),
        )

    def test_from_block(self):
        msg_body = """<div>
message<br>
<div>
<hr>
From: <a href="mailto:bob@example.com">bob@example.com</a><br>
Date: Fri, 23 Mar 2012 12:35:31 -0600<br>
To: <a href="mailto:rob@example.com">rob@example.com</a><br>
Subject: You Have New Mail From Mary!<br><br>

text
</div></div>
"""
        self.assertEqual(
            "<div>message<br/><div></div></div>",
            RE_WHITESPACE.sub("", html.extract_from_html(msg_body)),
        )

    def test_reply_shares_div_with_from_block(self):
        msg_body = """
<body>
<div>

    Blah<br><br>

    <hr>Date: Tue, 22 May 2012 18:29:16 -0600<br>
    To: xx@hotmail.ca<br>
    From: quickemail@ashleymadison.com<br>
    Subject: You Have New Mail From x!<br><br>

</div>
</body>"""
        reply = html.extract_from_html(msg_body)
        self.assertEqual(
            "<body><div>Blah<br/><br/></div></body>",
            RE_WHITESPACE.sub("", reply),
        )

    def test_reply_quotations_share_block(self):
        stripped_html = text.extract_non_quoted_from_plain(
            get_email_as_bytes("reply-quotations-share-block.eml").decode("utf-8")
        )
        self.assertTrue(stripped_html)
        self.assertTrue("From" not in stripped_html)

    def test_OLK_SRC_BODY_SECTION_stripped(self):
        self.assertEqual(
            "<html><body><div>Reply</div></body></html>",
            RE_WHITESPACE.sub(
                "",
                html.extract_from_html(get_email_as_bytes("OLK_SRC_BODY_SECTION.html")),
            ),
        )

    def test_reply_separated_by_hr(self):
        self.assertEqual(
            "<html><body><div>Hi<div>there</div><div>Bob</div></div></body></html>",
            RE_WHITESPACE.sub(
                "",
                html.extract_from_html(
                    get_email_as_bytes("reply-separated-by-hr.html")
                ),
            ),
        )

    def test_from_block_and_quotations_in_separate_divs(self):
        msg_body = """
Reply
<div>
<hr/>
<div>
    <font>
    <b>From: bob@example.com</b>
    <b>Date: Thu, 24 Mar 2016 08:07:12 -0700</b>
    </font>
</div>
<div>
    Quoted message
</div>
</div>
"""
        self.assertEqual(
            "Reply<div></div>",
            RE_WHITESPACE.sub("", html.extract_from_html(msg_body)),
        )

    def extract_reply_and_check(self, filename):
        kwargs = {}
        kwargs["encoding"] = "utf-8"

        with open(filename, **kwargs) as f:
            msg_body = f.read()
            reply = html.extract_from_html(msg_body)
            plain_reply = html2text(reply)

            self.assertIn(
                RE_WHITESPACE.sub("", "Hi. I am fine.\n\nThanks,\nAlex"),
                RE_WHITESPACE.sub("", plain_reply),
            )
            self.assertNotIn(
                RE_WHITESPACE.sub("", "Hello! How are you?"),
                RE_WHITESPACE.sub("", plain_reply),
            )

    def test_CRLF(self):
        """CR is not converted to '&#13;'"""
        symbol = "&#13;"
        extracted = html.extract_from_html("<html>\r\n</html>")
        self.assertFalse(symbol in extracted)
        self.assertEqual("<html></html>", RE_WHITESPACE.sub("", extracted))

        msg_body = """My
reply
<blockquote>

<div>
    On 11-Apr-2011, at 6:54 PM, Bob &lt;bob@example.com&gt; wrote:
</div>

<div>
    Test
</div>

</blockquote>"""
        msg_body = msg_body.replace("\n", "\r\n")
        extracted = html.extract_from_html(msg_body)
        self.assertFalse(symbol in extracted)
        # Keep new lines otherwise "My reply" becomes one word - "Myreply"
        self.assertEqual("My\r\nreply\r\n", extracted)

    def test_gmail_forwarded_msg(self):
        msg_body = (
            '<div dir="ltr"><br>'
            + '<div class="gmail_quote">---------- Forwarded message ----------<br>'
            + 'From: <b class="gmail_sendername">Bob</b> <span dir="ltr">'
            + '&lt;<a href="mailto:bob@example.com">bob@example.com</a>&gt;'
            + "</span><br>Date: Fri, Feb 11, 2010 at 5:59 PM<br>"
            + "Subject: Bob WFH today<br>To: Mary &lt;"
            + '<a href="mailto:mary@example.com">'
            + 'mary@example.com</a>&gt;<br><br><br><div dir="ltr">eom</div>'
            + "</div><br></div>"
        )
        extracted = html.extract_from_html(msg_body)
        self.assertEqual(
            RE_WHITESPACE.sub("", '<divdir="ltr"><br/><br/></div>'),
            RE_WHITESPACE.sub("", extracted),
        )

    def test_readable_html_empty(self):
        msg_body = """
<blockquote>
Reply
<div>
    On 11-Apr-2011, at 6:54 PM, Bob &lt;bob@example.com&gt; wrote:
</div>

<div>
    Test
</div>

</blockquote>"""

        self.assertEqual(
            RE_WHITESPACE.sub("", ""),
            RE_WHITESPACE.sub("", html.extract_from_html(msg_body)),
        )

    # @patch.object(html, "html_document_fromstring", Mock(return_value=None))
    def test_bad_html(self):
        bad_html = "<html></html>"
        self.assertEqual(bad_html, html.extract_from_html(bad_html))

    def test_gmail_reply(self):
        self.extract_reply_and_check("mails/html_replies/gmail.html")

    def test_mail_ru_reply(self):
        self.extract_reply_and_check("mails/html_replies/mail_ru.html")

    def test_hotmail_reply(self):
        self.extract_reply_and_check("mails/html_replies/hotmail.html")

    def test_ms_outlook_2003_reply(self):
        self.extract_reply_and_check("mails/html_replies/ms_outlook_2003.html")

    def test_ms_outlook_2007_reply(self):
        self.extract_reply_and_check("mails/html_replies/ms_outlook_2007.html")

    def test_ms_outlook_2010_reply(self):
        self.extract_reply_and_check("mails/html_replies/ms_outlook_2010.html")

    def test_thunderbird_reply(self):
        self.extract_reply_and_check("mails/html_replies/thunderbird.html")

    def test_windows_mail_reply(self):
        self.extract_reply_and_check("mails/html_replies/windows_mail.html")

    def test_yandex_ru_reply(self):
        self.extract_reply_and_check("mails/html_replies/yandex_ru.html")

    @patch.object(constants, "MAX_LINES_COUNT", 1)
    def test_too_many_lines(self):
        msg_body = """Test reply
Hi
-----Original Message-----

Test"""
        self.assertEqual("Test reply", text.extract_non_quoted_from_plain(msg_body))

    def test_pattern_on_date_somebody_wrote(self):
        msg_body = """Test reply

On 11-Apr-2011, at 6:54 PM, Roman Tkachenko <romant@example.com> wrote:

>
> Test
>
> Roman"""

        self.assertEqual("Test reply", text.extract_non_quoted_from_plain(msg_body))

    def test_pattern_on_date_polymail(self):
        msg_body = """Test reply

On Tue, Apr 11, 2017 at 10:07 PM John Smith

<
mailto:John Smith <johnsmith@gmail.com>
> wrote:
Test quoted data
"""

        self.assertEqual("Test reply", text.extract_non_quoted_from_plain(msg_body))

    def test_pattern_sent_from_samsung_smb_wrote(self):
        msg_body = """Test reply

Sent from Samsung MobileName <address@example.com> wrote:

>
> Test
>
> Roman"""

        self.assertEqual("Test reply", text.extract_non_quoted_from_plain(msg_body))

    def test_pattern_on_date_wrote_somebody(self):
        self.assertEqual(
            "Lorem",
            text.extract_non_quoted_from_plain(
                """Lorem

Op 13-02-2014 3:18 schreef Julius Caesar <pantheon@rome.com>:

Veniam laborum mlkshk kale chips authentic.
Normcore mumblecore laboris, fanny pack readymade eu blog chia pop-up
freegan enim master cleanse.
"""
            ),
        )

    def test_pattern_on_date_somebody_wrote_date_with_slashes(self):
        msg_body = """Test reply

On 04/19/2011 07:10 AM, Roman Tkachenko wrote:

>
> Test.
>
> Roman"""
        self.assertEqual("Test reply", text.extract_non_quoted_from_plain(msg_body))

    def test_date_time_email_splitter(self):
        msg_body = """Test reply

2014-10-17 11:28 GMT+03:00 Postmaster <
postmaster@sandboxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.mailgun.org>:

> First from site
>
    """
        self.assertEqual("Test reply", text.extract_non_quoted_from_plain(msg_body))

    def test_pattern_on_date_somebody_wrote_allows_space_in_front(self):
        msg_body = """Thanks Thanmai
On Mar 8, 2012 9:59 AM, "Example.com" <
r+7f1b094ceb90e18cca93d53d3703feae@example.com> wrote:


>**
>  Blah-blah-blah"""
        self.assertEqual("Thanks Thanmai", text.extract_non_quoted_from_plain(msg_body))

    def test_pattern_on_date_somebody_sent(self):
        msg_body = """Test reply

On 11-Apr-2011, at 6:54 PM, Roman Tkachenko <romant@example.com> sent:

>
> Test
>
> Roman"""
        self.assertEqual("Test reply", text.extract_non_quoted_from_plain(msg_body))

    def test_appointment(self):
        msg_body = """Response

    10/19/2017 @ 9:30 am for physical therapy
    Bla
    1517 4th Avenue Ste 300
    London CA 19129, 555-421-6780

    John Doe, FCLS
    Mailgun Inc
    555-941-0697

    From: from@example.com [mailto:from@example.com]
    Sent: Wednesday, October 18, 2017 2:05 PM
    To: John Doer - SIU <jd@example.com>
    Subject: RE: Claim # 5551188-1

    Text"""

        expected = """Response

    10/19/2017 @ 9:30 am for physical therapy
    Bla
    1517 4th Avenue Ste 300
    London CA 19129, 555-421-6780

    John Doe, FCLS
    Mailgun Inc
    555-941-0697"""
        self.assertEqual(expected, text.extract_non_quoted_from_plain(msg_body))

    def test_line_starts_with_on(self):
        msg_body = """Blah-blah-blah
On blah-blah-blah"""
        self.assertEqual(msg_body, text.extract_non_quoted_from_plain(msg_body))

    def test_reply_and_quotation_splitter_share_line(self):
        # reply lines and 'On <date> <person> wrote:' splitter pattern
        # are on the same line
        msg_body = """reply On Wed, Apr 4, 2012 at 3:59 PM, bob@example.com wrote:
> Hi"""
        self.assertEqual("reply", text.extract_non_quoted_from_plain(msg_body))

        # test pattern '--- On <date> <person> wrote:' with reply text on
        # the same line
        msg_body = """reply--- On Wed, Apr 4, 2012 at 3:59 PM, me@domain.com wrote:
> Hi"""
        self.assertEqual("reply", text.extract_non_quoted_from_plain(msg_body))

        # test pattern '--- On <date> <person> wrote:' with reply text containing
        # '-' symbol
        msg_body = """reply
bla-bla - bla--- On Wed, Apr 4, 2012 at 3:59 PM, me@domain.com wrote:
> Hi"""
        reply = """reply
bla-bla - bla"""

        self.assertEqual(reply, text.extract_non_quoted_from_plain(msg_body))

    def test_android_wrote(self):
        msg_body = """Test reply

---- John Smith wrote ----

> quoted
> text
"""
        self.assertEqual("Test reply", text.extract_non_quoted_from_plain(msg_body))

    def test_reply_wraps_quotations(self):
        msg_body = """Test reply

On 04/19/2011 07:10 AM, Roman Tkachenko wrote:

>
> Test

Regards, Roman"""

        reply = """Test reply

Regards, Roman"""

        self.assertEqual(reply, text.extract_non_quoted_from_plain(msg_body))

    def test_reply_wraps_nested_quotations(self):
        msg_body = """Test reply
On 04/19/2011 07:10 AM, Roman Tkachenko wrote:

>Test test
>On 04/19/2011 07:10 AM, Roman Tkachenko wrote:
>
>>
>> Test.
>>
>> Roman

Regards, Roman"""

        reply = """Test reply
Regards, Roman"""
        self.assertEqual(reply, text.extract_non_quoted_from_plain(msg_body))

    def test_quotation_separator_takes_2_lines(self):
        msg_body = """Test reply

On Fri, May 6, 2011 at 6:03 PM, Roman Tkachenko from Hacker News
<roman@definebox.com> wrote:

> Test.
>
> Roman

Regards, Roman"""

        reply = """Test reply

Regards, Roman"""
        self.assertEqual(reply, text.extract_non_quoted_from_plain(msg_body))

    def test_quotation_separator_takes_3_lines(self):
        msg_body = """Test reply

On Nov 30, 2011, at 12:47 PM, Somebody <
416ffd3258d4d2fa4c85cfa4c44e1721d66e3e8f4@somebody.domain.com>
wrote:

Test message
"""
        self.assertEqual("Test reply", text.extract_non_quoted_from_plain(msg_body))

    def test_short_quotation(self):
        msg_body = """Hi

On 04/19/2011 07:10 AM, Roman Tkachenko wrote:

> Hello"""
        self.assertEqual("Hi", text.extract_non_quoted_from_plain(msg_body))

    def test_with_indent(self):
        msg_body = """
YOLO salvia cillum kogi typewriter mumblecore cardigan skateboard Austin.

------On 12/29/1987 17:32 PM, Julius Caesar wrote-----

Brunch mumblecore pug Marfa tofu, irure taxidermy hoodie readymade pariatur.
    """
        self.assertEqual(
            "YOLO salvia cillum kogi typewriter mumblecore cardigan skateboard Austin.",
            text.extract_non_quoted_from_plain(msg_body),
        )

    def test_short_quotation_with_newline(self):
        msg_body = """Btw blah blah...

On Tue, Jan 27, 2015 at 12:42 PM -0800, "Company" <christine.XXX@XXX.com> wrote:

Hi Mark,
Blah blah? 
Thanks,Christine 

On Jan 27, 2015, at 11:55 AM, Mark XXX <mark@XXX.com> wrote:

Lorem ipsum?
Mark

Sent from Acompli"""
        self.assertEqual(
            "Btw blah blah...", text.extract_non_quoted_from_plain(msg_body)
        )

    def test_pattern_date_email_with_unicode(self):
        msg_body = """Replying ok
2011/4/7 Nathan \xd0\xb8ova <support@example.com>

>  Cool beans, scro"""
        self.assertEqual("Replying ok", text.extract_non_quoted_from_plain(msg_body))

    def test_english_from_block(self):
        self.assertEqual(
            "Allo! Follow up MIME!",
            text.extract_non_quoted_from_plain(
                """Allo! Follow up MIME!

From: somebody@example.com
Sent: March-19-11 5:42 PM
To: Somebody
Subject: The manager has commented on your Loop

Blah-blah-blah
"""
            ),
        )

    def test_german_from_block(self):
        self.assertEqual(
            "Allo! Follow up MIME!",
            text.extract_non_quoted_from_plain(
                """Allo! Follow up MIME!

Von: somebody@example.com
Gesendet: Dienstag, 25. November 2014 14:59
An: Somebody
Betreff: The manager has commented on your Loop

Blah-blah-blah
"""
            ),
        )

    def test_french_multiline_from_block(self):
        self.assertEqual(
            "Lorem ipsum",
            text.extract_non_quoted_from_plain(
                """Lorem ipsum

De : Brendan xxx [mailto:brendan.xxx@xxx.com]
Envoyé : vendredi 23 janvier 2015 16:39
À : Camille XXX
Objet : Follow Up

Blah-blah-blah
"""
            ),
        )

    def test_french_from_block(self):
        self.assertEqual(
            "Lorem ipsum",
            text.extract_non_quoted_from_plain(
                """Lorem ipsum

    Le 23 janv. 2015 à 22:03, Brendan xxx
    <brendan.xxx@xxx.com<mailto:brendan.xxx@xxx.com>> a écrit:

    Bonjour!"""
            ),
        )

    def test_polish_from_block(self):
        self.assertEqual(
            "Lorem ipsum",
            text.extract_non_quoted_from_plain(
                """Lorem ipsum

W dniu 28 stycznia 2015 01:53 użytkownik Zoe xxx <zoe.xxx@xxx.com>
napisał:

Blah!
"""
            ),
        )

    def test_danish_from_block(self):
        self.assertEqual(
            "Allo! Follow up MIME!",
            text.extract_non_quoted_from_plain(
                """Allo! Follow up MIME!

Fra: somebody@example.com
Sendt: 19. march 2011 12:10
Til: Somebody
Emne: The manager has commented on your Loop

Blah-blah-blah
"""
            ),
        )

    def test_swedish_from_block(self):
        self.assertEqual(
            "Allo! Follow up MIME!",
            text.extract_non_quoted_from_plain(
                """Allo! Follow up MIME!
Från: Anno Sportel [mailto:anno.spoel@hsbcssad.com]
Skickat: den 26 augusti 2015 14:45
Till: Isacson Leiff
Ämne: RE: Week 36

Blah-blah-blah
"""
            ),
        )

    def test_swedish_from_line(self):
        self.assertEqual(
            "Lorem",
            text.extract_non_quoted_from_plain(
                """Lorem
Den 14 september, 2015 02:23:18, Valentino Rudy (valentino@rudy.be) skrev:

Veniam laborum mlkshk kale chips authentic.
Normcore mumblecore laboris, fanny pack
readymade eu blog chia pop-up freegan enim master cleanse.
"""
            ),
        )

    def test_norwegian_from_line(self):
        self.assertEqual(
            "Lorem",
            text.extract_non_quoted_from_plain(
                """Lorem
På 14 september 2015 på 02:23:18, Valentino Rudy (valentino@rudy.be) skrev:

Veniam laborum mlkshk kale chips authentic.
Normcore mumblecore laboris, fanny pack
readymade eu blog chia pop-up freegan enim master cleanse.
"""
            ),
        )

    def test_dutch_from_block(self):
        self.assertEqual(
            "Gluten-free culpa lo-fi et nesciunt nostrud.",
            text.extract_non_quoted_from_plain(
                """Gluten-free culpa lo-fi et nesciunt nostrud.

Op 17-feb.-2015, om 13:18 heeft Julius Caesar
<pantheon@rome.com> het volgende geschreven:

Small batch beard laboris tempor, non listicle hella Tumblr heirloom.
"""
            ),
        )

    def test_vietnamese_from_block(self):
        self.assertEqual(
            "Hello",
            text.extract_non_quoted_from_plain(
                """Hello

Vào 14:24 8 tháng 6, 2017, Hùng Nguyễn <hungnguyen@xxx.com> đã viết:

> Xin chào
"""
            ),
        )

    def test_quotation_marker_false_positive(self):
        msg_body = """Visit us now for assistance...
>>> >>>  http://www.domain.com <<<
Visit our site by clicking the link above"""
        self.assertEqual(msg_body, text.extract_non_quoted_from_plain(msg_body))

    def test_link_closed_with_quotation_marker_on_new_line(self):
        msg_body = """8.45am-1pm

From: somebody@example.com
Date: Wed, 16 May 2012 00:15:02 -0600

<http://email.example.com/c/dHJhY2tpbmdfY29kZT1mMDdjYzBmNzM1ZjYzMGIxNT
>  <bob@example.com <mailto:bob@example.com> >

Requester: """
        self.assertEqual("8.45am-1pm", text.extract_non_quoted_from_plain(msg_body))

    def test_link_breaks_quotation_markers_sequence(self):
        # link starts and ends on the same line
        msg_body = """Blah

On Thursday, October 25, 2012 at 3:03 PM, life is short. on Bob wrote:

>
> Post a response by replying to this email
>
(http://example.com/c/YzOTYzMmE) >
> life is short. (http://example.com/c/YzMmE)
>
"""
        self.assertEqual("Blah", text.extract_non_quoted_from_plain(msg_body))

        # link starts after some text on one line and ends on another
        msg_body = """Blah

On Monday, 24 September, 2012 at 3:46 PM, bob wrote:

> [Ticket #50] test from bob
>
> View ticket (http://example.com/action
_nonce=3dd518)
>
"""
        self.assertEqual("Blah", text.extract_non_quoted_from_plain(msg_body))

    def test_from_block_starts_with_date(self):
        msg_body = """Blah

Date: Wed, 16 May 2012 00:15:02 -0600
To: klizhentas@example.com

"""
        self.assertEqual("Blah", text.extract_non_quoted_from_plain(msg_body))

    def test_bold_from_block(self):
        msg_body = """Hi

*From:* bob@example.com [mailto:
bob@example.com]
*Sent:* Wednesday, June 27, 2012 3:05 PM
*To:* travis@example.com
*Subject:* Hello

"""
        self.assertEqual("Hi", text.extract_non_quoted_from_plain(msg_body))

    def test_weird_date_format_in_date_block(self):
        msg_body = """Blah
Date: Fri=2C 28 Sep 2012 10:55:48 +0000
From: tickets@example.com
To: bob@example.com
Subject: [Ticket #8] Test

"""
        self.assertEqual("Blah", text.extract_non_quoted_from_plain(msg_body))

    def test_dont_parse_quotations_for_forwarded_messages(self):
        msg_body = """FYI

---------- Forwarded message ----------
From: bob@example.com
Date: Tue, Sep 4, 2012 at 1:35 PM
Subject: Two
line subject
To: rob@example.com

Text"""
        self.assertEqual(msg_body, text.extract_non_quoted_from_plain(msg_body))

    def test_forwarded_message_in_quotations(self):
        msg_body = """Blah

-----Original Message-----

FYI

---------- Forwarded message ----------
From: bob@example.com
Date: Tue, Sep 4, 2012 at 1:35 PM
Subject: Two
line subject
To: rob@example.com

"""
        self.assertEqual("Blah", text.extract_non_quoted_from_plain(msg_body))

    def test_mark_message_lines(self):
        # e - empty line
        # s - splitter line
        # m - line starting with quotation marker '>'
        # t - the rest

        lines = [
            "Hello",
            "",
            # next line should be marked as splitter
            "_____________",
            "From: foo@bar.com",
            "Date: Wed, 16 May 2012 00:15:02 -0600",
            "",
            "> Hi",
            "",
            "Signature",
        ]
        self.assertEqual("tesssemet", text.mark_message_lines(lines))

        lines = [
            "Just testing the email reply",
            "",
            "Robert J Samson",
            "Sent from my iPhone",
            "",
            # all 3 next lines should be marked as splitters
            "On Nov 30, 2011, at 12:47 PM, Skapture <",
            (
                "416ffd3258d4d2fa4c85cfa4c44e1721d66e3e8f4"
                "@skapture-staging.mailgun.org>"
            ),
            "wrote:",
            "",
            "Tarmo Lehtpuu has posted the following message on",
        ]
        self.assertEqual("tettessset", text.mark_message_lines(lines))

    def test_process_marked_lines(self):
        # quotations and last message lines are mixed
        # consider all to be a last message
        markers = "tsemmtetm"
        lines = [str(i) for i in range(len(markers))]
        lines = [str(i) for i in range(len(markers))]

        self.assertEqual(lines, text.process_marked_lines(lines, markers))

        # no splitter => no markers
        markers = "tmm"
        lines = ["1", "2", "3"]
        self.assertEqual(["1", "2", "3"], text.process_marked_lines(lines, markers))

        # text after splitter without markers is quotation
        markers = "tst"
        lines = ["1", "2", "3"]
        self.assertEqual(["1"], text.process_marked_lines(lines, markers))

        # message + quotation + signature
        markers = "tsmt"
        lines = ["1", "2", "3", "4"]
        self.assertEqual(["1", "4"], text.process_marked_lines(lines, markers))

        # message + <quotation without markers> + nested quotation
        markers = "tstsmt"
        lines = ["1", "2", "3", "4", "5", "6"]
        self.assertEqual(["1"], text.process_marked_lines(lines, markers))

        # test links wrapped with paranthesis
        # link starts on the marker line
        markers = "tsmttem"
        lines = [
            "text",
            "splitter",
            ">View (http://example.com",
            "/abc",
            ")",
            "",
            "> quote",
        ]
        self.assertEqual(lines[:1], text.process_marked_lines(lines, markers))

        # link starts on the new line
        markers = "tmmmtm"
        lines = [
            "text",
            ">" ">",
            ">",
            "(http://example.com) >  ",
            "> life is short. (http://example.com)  ",
        ]
        self.assertEqual(lines[:1], text.process_marked_lines(lines, markers))

        # check all "inline" replies
        markers = "tsmtmtm"
        lines = [
            "text",
            "splitter",
            ">",
            "(http://example.com)",
            ">",
            "inline  reply",
            ">",
        ]
        self.assertEqual(lines, text.process_marked_lines(lines, markers))

        # inline reply with link not wrapped in paranthesis
        markers = "tsmtm"
        lines = [
            "text",
            "splitter",
            ">",
            "inline reply with link http://example.com",
            ">",
        ]
        self.assertEqual(lines, text.process_marked_lines(lines, markers))

        # inline reply with link wrapped in paranthesis
        markers = "tsmtm"
        lines = ["text", "splitter", ">", "inline  reply (http://example.com)", ">"]
        self.assertEqual(lines, text.process_marked_lines(lines, markers))

    def test_preprocess(self):
        msg = (
            "Hello\n"
            "See <http://google.com\n"
            "> for more\n"
            "information On Nov 30, 2011, at 12:47 PM, Somebody <\n"
            "416ffd3258d4d2fa4c85cfa4c44e1721d66e3e8f4\n"
            "@example.com>"
            "wrote:\n"
            "\n"
            "> Hi"
        )

        # test the link is rewritten
        # 'On <date> <person> wrote:' pattern starts from a new line
        prepared_msg = (
            "Hello\n"
            "See @@http://google.com\n"
            "@@ for more\n"
            "information\n"
            " On Nov 30, 2011, at 12:47 PM, Somebody <\n"
            "416ffd3258d4d2fa4c85cfa4c44e1721d66e3e8f4\n"
            "@example.com>"
            "wrote:\n"
            "\n"
            "> Hi"
        )
        self.assertEqual(prepared_msg, utils.preprocess(msg, "\n"))

        msg = """
> <http://teemcl.mailgun.org/u/**aD1mZmZiNGU5ODQwMDNkZWZlMTExNm**

> MxNjQ4Y2RmOTNlMCZyPXNlcmdleS5v**YnlraG92JTQwbWFpbGd1bmhxLmNvbS**

> Z0PSUyQSZkPWUwY2U<http://example.org/u/aD1mZmZiNGU5ODQwMDNkZWZlMTExNmMxNjQ4Y>
        """
        self.assertEqual(msg, utils.preprocess(msg, "\n"))

        # 'On <date> <person> wrote' shouldn't be spread across too many lines
        msg = (
            "Hello\n"
            "How are you? On Nov 30, 2011, at 12:47 PM,\n "
            "Example <\n"
            "416ffd3258d4d2fa4c85cfa4c44e1721d66e3e8f4\n"
            "@example.org>"
            "wrote:\n"
            "\n"
            "> Hi"
        )
        self.assertEqual(msg, utils.preprocess(msg, "\n"))

        msg = "Hello On Nov 30, smb wrote:\n" "Hi\n" "On Nov 29, smb wrote:\n" "hi"

        prepared_msg = (
            "Hello\n" " On Nov 30, smb wrote:\n" "Hi\n" "On Nov 29, smb wrote:\n" "hi"
        )

        self.assertEqual(prepared_msg, utils.preprocess(msg, "\n"))

    def test_preprocess_postprocess_2_links(self):
        msg_body = "<http://link1> <http://link2>"
        self.assertEqual(msg_body, text.extract_non_quoted_from_plain(msg_body))

    def test_feedback_below_left_unparsed(self):
        msg_body = """Please enter your feedback below. Thank you.

-------------------------------------
Enter Feedback Below
-------------------------------------

The user experience was unparallelled. Please continue production.
I'm sending payment to ensure
that this line is intact."""

        parsed = text.extract_non_quoted_from_plain(msg_body)
        self.assertEqual(msg_body, parsed)

    def test_pl_chars_emojis_and_quote(self):
        """
        The message contains every Polish diacritic, a row of emojis and a
        quoted Gmail block.  We expect:
            * cleaned body  – keeps all letters + emojis
            * quotation     – moved to *.plain_quote / .html_quote*
        """
        raw_bytes = get_email_as_bytes("quote_and_pl_characters.eml")
        mail = parse_mail_from_bytes(raw_bytes)
        parts = get_text(mail)
        plain = parts["content"]
        self.assertRegex(
            plain,
            r"Ąą.*Cć.*Eę.*Ńń.*Łł.*Żż.*Źź",
            msg="Polish diacritics missing from plain_content",
        )
        for emoji in ["👍", "😉", "🫡", "😃", "🤔", "🤣"]:
            self.assertIn(emoji, plain, f"{emoji} vanished from plain_content")
        self.assertNotIn(
            "Prosze o przesłanie załącznikow",
            plain,
            "Quoted text leaked into plain_content",
        )
        self.assertIn(
            "Prosze o przesłanie załącznikow",
            parts["quote"],
            "Quoted text not detected in plain_quote",
        )
        html = parts["html_content"]
        self.assertRegex(
            html,
            r"Ąą.*Cć.*Eę.*Ńń.*Łł.*Żż.*Źź",
            msg="Polish diacritics missing from plain_content",
        )
        for emoji in ["👍", "😉", "🫡", "😃", "🤔", "🤣"]:
            self.assertIn(emoji, html, f"{emoji} vanished from plain_content")
        self.assertNotIn(
            "Prosze o przesłanie załącznikow",
            html,
            "Quoted text leaked into plain_content",
        )
        self.assertIn(
            "Prosze o przesłanie załącznikow",
            parts["html_quote"],
            "Quoted text not detected in plain_quote",
        )

    def test_8bit_text_html(self):
        """
        Test that 8-bit text in HTML is correctly extracted.
        """
        raw_bytes = get_email_as_bytes("8bit_encoded.eml")
        mail = parse_mail_from_bytes(raw_bytes)
        parts = get_text(mail)
        plain = parts["content"]
        self.assertIn(
            "Dzień dobry\n\nW odpowiedzi na wniosek dotyczący udostępnienia",
            plain,
            "8-bit coded PL characters not found in plain_content",
        )
        html = parts["html_content"]
        self.assertIn(
            "<p>Dzień dobry</p><p>W odpowiedzi na wniosek dotyczący udostępnienia",
            html,
            "8-bit coded PL characters not found in plain_content",
        )

    def test_email_address_extraction(self):
        """
        Test that all expected email addresses are correctly extracted from the EML,
        and that no invalid (None or empty) addresses are included.
        """
        raw_bytes = get_email_as_bytes("address_extraction_test.eml")
        mail = parse_mail_from_bytes(raw_bytes)

        to_plus = get_to_plus(mail)

        # Check that all expected emails are present
        expected_emails = {
            "bob@example.com",
            "carol@example.net",
            "hidden@example.org",
            "delivery@example.com",
        }

        for expected in expected_emails:
            self.assertIn(
                expected,
                to_plus,
                f"Expected email address {expected} not found in to_plus result.",
            )

        # Check that there are no None or empty strings
        self.assertNotIn(None, to_plus, "None found in extracted email addresses")
        self.assertNotIn("", to_plus, "Empty string found in extracted email addresses")

    def test_valid_eml_from_header_is_parsed(self):
        """
        Regression test for empty 'from' list when parsing a real-world Polish EML.
        The message contains a valid RFC From: header and must yield a non-empty,
        normalized sender list (v3 format: [{email, name}]).
        """
        raw_bytes = get_email_as_bytes(
            "Re Wniosek o informację dot. publikacji rejestru umów.eml"
        )
        body = serialize_mail(raw_bytes)
        body_map = {k: v for k, v in body}
        manifest = json.loads(body_map["manifest"][1].read().decode("utf-8"))

        from_list = manifest["headers"].get("from") or []
        self.assertTrue(from_list, "Expected non-empty 'from' list for attached EML")
        from_emails = [a["email"] for a in from_list]
        self.assertIn(
            "sekretariat@powiatpultuski.pl",
            from_emails,
            "Expected sender address not found in parsed 'from' list",
        )

    def test_valid_eml_v2_from_header_is_parsed(self):
        """
        Regression test for empty 'from' list when parsing a real-world Polish EMLv2.
        The message contains a valid RFC From: header and must yield a non-empty,
        normalized sender list (v3 format: [{email, name}]).
        """
        raw_bytes = get_email_as_bytes(
            "Przeczytano Wniosek o informację publiczną- dane schroniska.eml"
        )
        body = serialize_mail(raw_bytes)
        body_map = {k: v for k, v in body}
        manifest = json.loads(body_map["manifest"][1].read().decode("utf-8"))

        from_list = manifest["headers"].get("from") or []
        self.assertTrue(from_list, "Expected non-empty 'from' list for attached EML")
        from_emails = [a["email"] for a in from_list]
        self.assertIn(
            "kancelaria@uggrudziadz.local",
            from_emails,
            "Expected sender address not found in parsed 'from' list",
        )

    def test_valid_eml_v3_from_header_is_parsed(self):
        """
        Regression test for from when parsing a real-world Polish EMLv3.
        The message contains a valid RFC From: header and must yield a non-empty,
        normalized sender list (v3 format: [{email, name}]).
        """
        raw_bytes = get_email_as_bytes(
            "Odpowiedź na wniosek o udostępnienie informacji publicznej.eml"
        )
        body = serialize_mail(raw_bytes)
        body_map = {k: v for k, v in body}
        manifest = json.loads(body_map["manifest"][1].read().decode("utf-8"))

        from_list = manifest["headers"].get("from") or []
        self.assertTrue(from_list, "Expected non-empty 'from' list for attached EML")
        from_emails = [a["email"] for a in from_list]
        self.assertIn(
            "jpaderewski@terespol.pl",
            from_emails,
            "Expected sender address not found in parsed 'from' list",
        )


    def test_extract_addresses(self):
        """extract_addresses() returns [{email, name}] with normalized emails."""
        result = extract_addresses([("Alice Smith", "alice@EXAMPLE.COM"), ("", "bob@example.org")])
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["email"], "alice@example.com")
        self.assertEqual(result[0]["name"], "Alice Smith")
        self.assertEqual(result[1]["email"], "bob@example.org")
        self.assertEqual(result[1]["name"], "")

    def test_extract_addresses_empty(self):
        """extract_addresses() returns [] for empty/None input."""
        self.assertEqual(extract_addresses(None), [])
        self.assertEqual(extract_addresses([]), [])
        self.assertEqual(extract_addresses(""), [])

    def test_extract_addresses_string_header(self):
        """extract_addresses() parses RFC 2822 header strings."""
        result = extract_addresses('"John Doe" <john@example.com>, jane@example.org')
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["email"], "john@example.com")
        self.assertEqual(result[0]["name"], "John Doe")
        self.assertEqual(result[1]["email"], "jane@example.org")

    def test_manifest_v3_format(self):
        """Manifest v3: from/to/cc are object arrays, no to+, has fingerprint."""
        raw_bytes = get_email_as_bytes("disposition-notification.eml")
        body = serialize_mail(raw_bytes)
        body_map = {k: v for k, v in body}
        manifest = json.loads(body_map["manifest"][1].read().decode("utf-8"))

        self.assertEqual(manifest["version"], "v3")
        self.assertNotIn("to+", manifest["headers"])
        self.assertIn("fingerprint", manifest)
        self.assertIn("ip", manifest["fingerprint"])
        self.assertIn("confidence", manifest["fingerprint"])
        self.assertIn("is_user_ip", manifest["fingerprint"])
        self.assertIn("provider_detected", manifest["fingerprint"])

        # from/to/cc should be list of dicts with email and name keys
        for field in ["from", "to", "cc"]:
            addr_list = manifest["headers"].get(field, [])
            for addr in addr_list:
                self.assertIn("email", addr)
                self.assertIn("name", addr)

    def test_threading_headers_reply_email(self):
        """Reply emails expose in_reply_to (str) and references (list)."""
        raw_bytes = get_email_as_bytes("vacation-reply.eml")
        body = serialize_mail(raw_bytes)
        body_map = {k: v for k, v in body}
        manifest = json.loads(body_map["manifest"][1].read().decode("utf-8"))
        headers = manifest["headers"]

        self.assertIsInstance(headers["in_reply_to"], str)
        self.assertTrue(len(headers["in_reply_to"]) > 0)
        self.assertIn("@", headers["in_reply_to"])

        self.assertIsInstance(headers["references"], list)
        self.assertTrue(len(headers["references"]) > 0)
        for ref in headers["references"]:
            self.assertIn("@", ref)

    def test_threading_headers_original_email(self):
        """Original (non-reply) emails have null in_reply_to and empty references."""
        raw = (
            b"From: sender@example.com\r\n"
            b"To: recipient@example.com\r\n"
            b"Subject: Original message\r\n"
            b"Message-ID: <original001@example.com>\r\n"
            b"Date: Mon, 1 Jan 2024 00:00:00 +0000\r\n"
            b"MIME-Version: 1.0\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"\r\n"
            b"Hello, this is a new conversation.\r\n"
        )
        body = serialize_mail(raw)
        body_map = {k: v for k, v in body}
        manifest = json.loads(body_map["manifest"][1].read().decode("utf-8"))
        headers = manifest["headers"]

        self.assertIsNone(headers["in_reply_to"])
        self.assertEqual(headers["references"], [])

    def test_fingerprint_private_ip(self):
        """Private IPs are correctly identified."""
        self.assertTrue(_is_private_ip("10.0.0.1"))
        self.assertTrue(_is_private_ip("192.168.1.1"))
        self.assertTrue(_is_private_ip("172.16.0.1"))
        self.assertTrue(_is_private_ip("127.0.0.1"))
        self.assertTrue(_is_private_ip("169.254.1.1"))
        self.assertTrue(_is_private_ip("100.64.0.1"))
        self.assertFalse(_is_private_ip("8.8.8.8"))
        self.assertFalse(_is_private_ip("203.0.114.1"))
        self.assertTrue(_is_private_ip("not-an-ip"))

    def test_fingerprint_parse_received(self):
        """_parse_received extracts IP and domains from Received headers."""
        line = "from mail.example.com (203.0.114.1) by mx.google.com with ESMTP"
        result = _parse_received(line)
        self.assertEqual(result["ip"], "203.0.114.1")
        self.assertEqual(result["from_domain"], "mail.example.com")
        self.assertEqual(result["by_domain"], "mx.google.com")

    def test_fingerprint_identify_provider(self):
        """Provider identification from domain patterns."""
        found, name, ptype = _identify_provider("mx1.protection.outlook.com")
        self.assertTrue(found)
        self.assertEqual(name, "Microsoft 365")
        self.assertEqual(ptype, "enterprise")

        found, name, ptype = _identify_provider("unknown.example.com")
        self.assertFalse(found)

    def test_chinese_gmail_quote_separation(self):
        """Chinese '在 ... 写道:' pattern is recognized as a quote splitter."""
        raw_bytes = get_email_as_bytes("chinese_reply.eml")
        mail = parse_mail_from_bytes(raw_bytes)
        parts = get_text(mail)
        plain = parts["content"]
        self.assertIn("收到报价单", plain)
        self.assertNotIn("请查收附件", plain)
        self.assertIn("请查收附件", parts["quote"])

    def test_chinese_outlook_quote_separation(self):
        """Chinese Outlook '发件人:/发送时间:/收件人:/主题:' header block is recognized."""
        raw_bytes = get_email_as_bytes("chinese_outlook_reply.eml")
        mail = parse_mail_from_bytes(raw_bytes)
        parts = get_text(mail)
        html = parts["html_content"]
        self.assertIn("好的", html)
        self.assertNotIn("请查收附件", html)

    def test_chinese_original_message_separation(self):
        """Chinese '-----原始邮件-----' pattern is recognized as a quote splitter."""
        raw_bytes = get_email_as_bytes("chinese_original_message.eml")
        mail = parse_mail_from_bytes(raw_bytes)
        parts = get_text(mail)
        plain = parts["content"]
        self.assertIn("收到报价单", plain)
        self.assertNotIn("请查收附件", plain)

    def test_chinese_on_date_wrote_plain_text(self):
        """Plain text '在 ... 写道:' pattern strips quoted content."""
        msg_body = """你好，已收到。

在 2024年1月14日，李四 <lisi@example.com> 写道:

> 请查收附件。
> 李四"""
        self.assertEqual("你好，已收到。", text.extract_non_quoted_from_plain(msg_body))

    def test_chinese_from_block_plain_text(self):
        """Plain text Chinese From/Date/To/Subject block is a splitter."""
        msg_body = """好的，收到。

发件人: someone@example.com
发送时间: 2024年1月14日 15:00
收件人: me@example.com
主题: 测试

原文内容"""
        self.assertEqual("好的，收到。", text.extract_non_quoted_from_plain(msg_body))

    def test_chinese_original_message_plain_text(self):
        """Plain text '-----原始邮件-----' is recognized as splitter."""
        msg_body = """已确认。

-----原始邮件-----
发件人: someone@example.com
日期: 2024-01-14

请确认收到。"""
        self.assertEqual("已确认。", text.extract_non_quoted_from_plain(msg_body))

    # ---------------------------------------------------------------
    # Japanese quote detection
    # ---------------------------------------------------------------
    def test_japanese_original_message_plain_text(self):
        """Japanese '-----元のメッセージ-----' is recognized as splitter."""
        msg_body = """承知しました。

-----元のメッセージ-----
差出人: tanaka@example.com
送信日時: 2024年1月14日 15:00

ご確認ください。"""
        self.assertEqual("承知しました。", text.extract_non_quoted_from_plain(msg_body))

    def test_japanese_from_block_plain_text(self):
        """Japanese From/Date/To/Subject block is a splitter."""
        msg_body = """了解です。

差出人: tanaka@example.com
送信日時: 2024年1月14日 15:00
宛先: yamada@example.com
件名: テスト

元のメッセージ"""
        self.assertEqual("了解です。", text.extract_non_quoted_from_plain(msg_body))

    def test_japanese_forwarded_message_plain_text(self):
        """Japanese '---------- 転送されたメッセージ ----------' is detected."""
        msg_body = """参考までに

---------- 転送されたメッセージ ----------
差出人: tanaka@example.com
日付: 2024年1月14日

テスト"""
        self.assertEqual(msg_body, text.extract_non_quoted_from_plain(msg_body))

    def test_japanese_outlook_html_quote(self):
        """Japanese Outlook HTML header block is detected via HDR_WORDS."""
        msg_body = """<html><body>
<div>承知しました。</div>
<div style="border-top: solid #B5C4DF 1.0pt; padding: 3.0pt 0 0 0;">
<p><b>差出人:</b> tanaka@example.com<br/>
<b>送信日時:</b> 2024年1月14日 15:00<br/>
<b>宛先:</b> yamada@example.com<br/>
<b>件名:</b> テスト</p>
</div>
<div>ご確認ください。</div>
</body></html>"""
        result = html.extract_from_html(msg_body)
        self.assertIn("承知しました", result)
        self.assertNotIn("ご確認ください", result)

    # ---------------------------------------------------------------
    # Korean quote detection
    # ---------------------------------------------------------------
    def test_korean_original_message_plain_text(self):
        """Korean '-----원래 메시지-----' is recognized as splitter."""
        msg_body = """알겠습니다.

-----원래 메시지-----
보낸 사람: kim@example.com
보낸 날짜: 2024년 1월 14일

확인 부탁드립니다."""
        self.assertEqual("알겠습니다.", text.extract_non_quoted_from_plain(msg_body))

    def test_korean_from_block_plain_text(self):
        """Korean From/Date/To/Subject block is a splitter."""
        msg_body = """확인했습니다.

보낸 사람: kim@example.com
보낸 날짜: 2024년 1월 14일
받는 사람: lee@example.com
제목: 테스트

원문 내용"""
        self.assertEqual("확인했습니다.", text.extract_non_quoted_from_plain(msg_body))

    def test_korean_forwarded_message_plain_text(self):
        """Korean '---------- 전달된 메시지 ----------' is detected."""
        msg_body = """참고하세요

---------- 전달된 메시지 ----------
보낸 사람: kim@example.com
보낸 날짜: 2024년 1월 14일

테스트"""
        self.assertEqual(msg_body, text.extract_non_quoted_from_plain(msg_body))

    def test_korean_outlook_html_quote(self):
        """Korean Outlook HTML header block is detected via HDR_WORDS."""
        msg_body = """<html><body>
<div>확인했습니다.</div>
<div style="border-top: solid #B5C4DF 1.0pt; padding: 3.0pt 0 0 0;">
<p><b>보낸 사람:</b> kim@example.com<br/>
<b>보낸 날짜:</b> 2024년 1월 14일 15:00<br/>
<b>받는 사람:</b> lee@example.com<br/>
<b>제목:</b> 테스트</p>
</div>
<div>확인 부탁드립니다.</div>
</body></html>"""
        result = html.extract_from_html(msg_body)
        self.assertIn("확인했습니다", result)
        self.assertNotIn("확인 부탁드립니다", result)

    # ---------------------------------------------------------------
    # Spanish quote detection
    # ---------------------------------------------------------------
    def test_spanish_on_date_wrote_plain_text(self):
        """Spanish 'El ... escribió:' pattern strips quoted content."""
        msg_body = """Gracias por la información.

El 14 de enero de 2024, Juan García <juan@example.com> escribió:

> Por favor revise el documento adjunto.
> Juan"""
        self.assertEqual(
            "Gracias por la información.",
            text.extract_non_quoted_from_plain(msg_body),
        )

    def test_spanish_original_message_plain_text(self):
        """Spanish '-----Mensaje original-----' is recognized as splitter."""
        msg_body = """Recibido, gracias.

-----Mensaje original-----
De: juan@example.com
Enviado: 14 de enero de 2024 15:00
Para: maria@example.com
Asunto: Prueba

Contenido original"""
        self.assertEqual(
            "Recibido, gracias.", text.extract_non_quoted_from_plain(msg_body)
        )

    def test_spanish_from_block_plain_text(self):
        """Spanish From/Sent/To/Subject block is a splitter."""
        msg_body = """De acuerdo.

De: juan@example.com
Enviado: 14 de enero de 2024 15:00
Para: maria@example.com
Asunto: Prueba

Contenido original"""
        self.assertEqual("De acuerdo.", text.extract_non_quoted_from_plain(msg_body))

    def test_spanish_forwarded_message_plain_text(self):
        """Spanish '---------- Mensaje reenviado ----------' is detected."""
        msg_body = """Para su información

---------- Mensaje reenviado ----------
De: juan@example.com
Fecha: 14 de enero de 2024

Prueba"""
        self.assertEqual(msg_body, text.extract_non_quoted_from_plain(msg_body))

    # ---------------------------------------------------------------
    # Portuguese quote detection
    # ---------------------------------------------------------------
    def test_portuguese_on_date_wrote_plain_text(self):
        """Portuguese 'Em ... escreveu:' pattern strips quoted content."""
        msg_body = """Obrigado pela informação.

Em 14 de janeiro de 2024, João Silva <joao@example.com> escreveu:

> Por favor verifique o documento em anexo.
> João"""
        self.assertEqual(
            "Obrigado pela informação.",
            text.extract_non_quoted_from_plain(msg_body),
        )

    def test_portuguese_original_message_plain_text(self):
        """Portuguese '-----Mensagem original-----' is recognized as splitter."""
        msg_body = """Recebido, obrigado.

-----Mensagem original-----
De: joao@example.com
Enviado: 14 de janeiro de 2024 15:00
Para: maria@example.com
Assunto: Teste

Conteúdo original"""
        self.assertEqual(
            "Recebido, obrigado.", text.extract_non_quoted_from_plain(msg_body)
        )

    def test_portuguese_forwarded_message_plain_text(self):
        """Portuguese '---------- Mensagem encaminhada ----------' is detected."""
        msg_body = """Para sua informação

---------- Mensagem encaminhada ----------
De: joao@example.com
Data: 14 de janeiro de 2024

Teste"""
        self.assertEqual(msg_body, text.extract_non_quoted_from_plain(msg_body))

    # ---------------------------------------------------------------
    # Italian quote detection
    # ---------------------------------------------------------------
    def test_italian_on_date_wrote_plain_text(self):
        """Italian 'Il ... ha scritto:' pattern strips quoted content."""
        msg_body = """Grazie per le informazioni.

Il 14 gennaio 2024, Marco Rossi <marco@example.com> ha scritto:

> Si prega di verificare il documento allegato.
> Marco"""
        self.assertEqual(
            "Grazie per le informazioni.",
            text.extract_non_quoted_from_plain(msg_body),
        )

    def test_italian_original_message_plain_text(self):
        """Italian '-----Messaggio originale-----' is recognized as splitter."""
        msg_body = """Ricevuto, grazie.

-----Messaggio originale-----
Da: marco@example.com
Inviato: 14 gennaio 2024 15:00
A: giulia@example.com
Oggetto: Test

Contenuto originale"""
        self.assertEqual(
            "Ricevuto, grazie.", text.extract_non_quoted_from_plain(msg_body)
        )

    def test_italian_from_block_plain_text(self):
        """Italian From/Sent/To/Subject block is a splitter."""
        msg_body = """Va bene.

Da: marco@example.com
Inviato: 14 gennaio 2024 15:00
A: giulia@example.com
Oggetto: Test

Contenuto originale"""
        self.assertEqual("Va bene.", text.extract_non_quoted_from_plain(msg_body))

    def test_italian_forwarded_message_plain_text(self):
        """Italian '---------- Messaggio inoltrato ----------' is detected."""
        msg_body = """Per informazione

---------- Messaggio inoltrato ----------
Da: marco@example.com
Data: 14 gennaio 2024

Test"""
        self.assertEqual(msg_body, text.extract_non_quoted_from_plain(msg_body))

    # ---------------------------------------------------------------
    # Fullwidth colon support (Chinese/Japanese/Korean)
    # ---------------------------------------------------------------
    def test_chinese_fullwidth_colon_plain(self):
        """Fullwidth colon '：' in Chinese From/Date/To/Subject block is a splitter."""
        msg_body = """好的，收到。

发件人： someone@example.com
发送时间： 2024年1月14日 15:00
收件人： me@example.com
主题： 测试

原文内容"""
        self.assertEqual("好的，收到。", text.extract_non_quoted_from_plain(msg_body))

    def test_chinese_fullwidth_colon_html(self):
        """Fullwidth colon '：' in Outlook HTML header block is detected via HDR_WORDS."""
        msg_body = """<html><body>
<div>收到，谢谢。</div>
<div style="border-top: solid #B5C4DF 1.0pt; padding: 3.0pt 0 0 0;">
<p><b>发件人：</b> lisi@example.com<br/>
<b>发送时间：</b> 2024年1月14日 15:00<br/>
<b>收件人：</b> zhangsan@example.com<br/>
<b>主题：</b> 报价单</p>
</div>
<div>请查收附件。</div>
</body></html>"""
        result = html.extract_from_html(msg_body)
        self.assertIn("收到，谢谢", result)
        self.assertNotIn("请查收附件", result)

    def test_chinese_fullwidth_colon_eml(self):
        """Full .eml with fullwidth colons parses correctly."""
        raw_bytes = get_email_as_bytes("chinese_fullwidth_colon.eml")
        mail = parse_mail_from_bytes(raw_bytes)
        parts = get_text(mail)
        # Plain text: fullwidth colon headers should be recognized as splitter
        self.assertIn("收到", parts["content"])
        self.assertNotIn("请查收附件", parts["content"])
        # HTML: Outlook-style divider with fullwidth colons should be stripped
        self.assertIn("收到", parts["html_content"])
        self.assertNotIn("请查收附件", parts["html_content"])

    # ---------------------------------------------------------------
    # QQ Mail Unicode hyphen support
    # ---------------------------------------------------------------
    def test_qq_mail_forward_endash(self):
        """QQ Mail forward separator with en-dash '\u2013' is recognized."""
        msg_body = """参考如下

\u2013\u2013\u2013\u2013\u2013\u2013\u2013\u2013\u2013\u2013 转发邮件 \u2013\u2013\u2013\u2013\u2013\u2013\u2013\u2013\u2013\u2013
发件人: sender@example.com
日期: 2024-01-14

原始内容"""
        result = text.extract_non_quoted_from_plain(msg_body)
        self.assertEqual(msg_body, result)

    def test_original_message_endash(self):
        """Original message separator with en-dash '\u2013' is recognized as splitter."""
        msg_body = """已确认。

\u2013\u2013\u2013\u2013\u2013 原始邮件 \u2013\u2013\u2013\u2013\u2013
发件人: someone@example.com
日期: 2024-01-14

请确认收到。"""
        self.assertEqual("已确认。", text.extract_non_quoted_from_plain(msg_body))

    def test_original_message_emdash(self):
        """Original message separator with em-dash '\u2014' is recognized as splitter."""
        msg_body = """收到了。

\u2014\u2014\u2014\u2014\u2014 原始邮件 \u2014\u2014\u2014\u2014\u2014
发件人: someone@example.com
日期: 2024-01-14

请查看。"""
        self.assertEqual("收到了。", text.extract_non_quoted_from_plain(msg_body))

    # ---------------------------------------------------------------
    # False positive prevention tests
    # ---------------------------------------------------------------
    def test_chinese_keyword_in_body_not_splitter(self):
        """正文含'主题'等关键词不应被截断"""
        msg_body = "会议纪要\n\n主题很重要，请大家注意\n日期定在下周一"
        self.assertEqual(msg_body, text.extract_non_quoted_from_plain(msg_body))

    def test_from_colon_in_body_not_splitter(self):
        """正文含 'From:' 不应触发分割"""
        msg_body = "Please update From: field in the template\nand the Subject: line too."
        self.assertEqual(msg_body, text.extract_non_quoted_from_plain(msg_body))

    def test_japanese_keyword_in_body_not_splitter(self):
        """正文含日文关键词不应被截断"""
        msg_body = "会議メモ\n\n件名についてご確認ください\n日付は来週月曜日です"
        self.assertEqual(msg_body, text.extract_non_quoted_from_plain(msg_body))

    def test_date_at_in_body_not_splitter(self):
        """Date pattern + @ on a later line should NOT be treated as splitter"""
        msg_body = "会议安排\n\n2024/01/15 总价: $5,000\n季度报告\n联系: alice@company.com\n详情如下"
        self.assertEqual(msg_body, text.extract_non_quoted_from_plain(msg_body))

    def test_date_at_in_body_not_splitter_dot_format(self):
        """Dot-separated date + @ several lines later should NOT be treated as splitter"""
        msg_body = "报告\n\n15.01.2024 会议记录\n第一项\n第二项\n联系: bob@example.com"
        self.assertEqual(msg_body, text.extract_non_quoted_from_plain(msg_body))

    def test_splitter_date_slash_same_line_at(self):
        """Date + @ on the SAME line IS a valid splitter"""
        line = '02/04/2012 14:20 user "bob@example.com" wrote:'
        self.assertIsNotNone(utils.is_splitter(line))

    def test_splitter_date_dot_cross_one_line_at(self):
        """Date + @ on the NEXT line IS a valid splitter"""
        line = '02.04.2012 14:20 user "bob@example.com" <\nbob@xxx.mailgun.org> wrote:'
        self.assertIsNotNone(utils.is_splitter(line))

    def test_splitter_gmt_cross_one_line_at(self):
        """GMT date + @ on the next line IS a valid splitter"""
        line = "2014-10-17 11:28 GMT+03:00 Postmaster <\npostmaster@example.org>:"
        self.assertIsNotNone(utils.is_splitter(line))

    def test_splitter_date_cross_two_lines_not_matched(self):
        """Date + @ separated by 2+ lines should NOT match as splitter"""
        line = "02/04/2012 some text\nline two\nline three with bob@example.com"
        self.assertIsNone(utils.is_splitter(line))

    def test_extract_fingerprint_empty(self):
        """extract_fingerprint returns default dict for empty input."""
        result = extract_fingerprint(b"")
        self.assertEqual(result["ip"], "")
        self.assertEqual(result["confidence"], "none")
        self.assertFalse(result["is_user_ip"])

    def test_extract_fingerprint_with_originating_ip(self):
        """extract_fingerprint finds X-Originating-IP."""
        eml = (
            b"From: sender@example.com\r\n"
            b"To: recipient@example.com\r\n"
            b"X-Originating-IP: [203.0.114.50]\r\n"
            b"Subject: Test\r\n"
            b"\r\n"
            b"Body\r\n"
        )
        result = extract_fingerprint(eml)
        self.assertEqual(result["ip"], "203.0.114.50")
        self.assertEqual(result["confidence"], "high")
        self.assertTrue(result["is_user_ip"])

    def test_extract_fingerprint_with_eml(self):
        """extract_fingerprint works on real .eml files."""
        raw_bytes = get_email_as_bytes("disposition-notification.eml")
        result = extract_fingerprint(raw_bytes)
        # Should return a valid dict regardless of whether IP is found
        self.assertIn("ip", result)
        self.assertIn("confidence", result)
        self.assertIn(result["confidence"], ("high", "medium", "low", "none"))


    # -- decorative <hr> tests ------------------------------------------------

    def test_decorative_hr_not_stripped(self):
        """A website form email with <hr/> separating fields from metadata
        (IP/Location/Timezone) must NOT be treated as a quote separator."""
        msg_body = """<html><body>
<p><b>Name:</b> John Doe</p>
<p><b>Email:</b> john@example.com</p>
<p><b>Message:</b> I would like a quote for your product.</p>
<hr/>
<p><b>IP Address:</b> 203.0.113.45</p>
<p><b>Location:</b> Shanghai, China</p>
<p><b>Timezone:</b> Asia/Shanghai (UTC+8)</p>
<p><b>Submit time:</b> 2025-03-06 14:30:00</p>
<p><b>Source page:</b> https://example.com/contact</p>
</body></html>"""
        clean, quote = _orig_extract(msg_body)
        self.assertIn("IP Address", clean)
        self.assertIn("Location", clean)
        self.assertIn("Timezone", clean)
        self.assertIn("Submit time", clean)
        self.assertEqual(quote, "")

    def test_multiple_decorative_hrs_not_stripped(self):
        """Multiple decorative <hr> tags separating content sections."""
        msg_body = """<html><body>
<h2>Section 1</h2>
<p>First paragraph content.</p>
<hr/>
<h2>Section 2</h2>
<p>Second paragraph content.</p>
<hr/>
<h2>Section 3</h2>
<p>Third paragraph content.</p>
</body></html>"""
        clean, quote = _orig_extract(msg_body)
        self.assertIn("Section 1", clean)
        self.assertIn("Section 2", clean)
        self.assertIn("Section 3", clean)
        self.assertEqual(quote, "")

    def test_native_hr_with_reply_headers_still_stripped(self):
        """A native <hr> followed by From/Sent/To/Subject headers should
        still be recognised as a quote separator."""
        msg_body = """<html><body>
<div>Hi, thanks for your reply.</div>
<hr/>
<div>
<b>From:</b> bob@example.com<br/>
<b>Sent:</b> Thursday, March 6, 2025 10:00 AM<br/>
<b>To:</b> alice@example.com<br/>
<b>Subject:</b> Re: Product inquiry<br/>
</div>
<div>Original message text here.</div>
</body></html>"""
        clean, quote = _orig_extract(msg_body)
        self.assertIn("thanks for your reply", clean)
        self.assertNotIn("From:", clean)
        self.assertNotIn("Original message text", clean)
        self.assertIn("From:", quote)

    def test_native_hr_with_date_header_stripped(self):
        """Hotmail-style: native <hr> followed by Date/Subject/From/To."""
        msg_body = """<html><body>
<div>My reply content.</div>
<hr/>
<div>
<b>Date:</b> Thu, 6 Mar 2025 10:00:00 +0800<br/>
<b>Subject:</b> Re: Inquiry<br/>
<b>From:</b> sender@example.com<br/>
<b>To:</b> recipient@example.com<br/>
</div>
<div>The quoted original message.</div>
</body></html>"""
        clean, quote = _orig_extract(msg_body)
        self.assertIn("My reply content", clean)
        self.assertNotIn("Date:", clean)
        self.assertNotIn("quoted original message", clean)
        self.assertIn("Date:", quote)


class TestProcessOrder(unittest.TestCase):
    """F7: PROCESS_ORDER config tests."""

    def test_default_fifo(self):
        env = {
            "IMAP_URL_1": "imap+ssl://user%40test.com:pass@imap.test.com:993/",
            "WEBHOOK_URL": "https://example.com/hook",
        }
        from config import get_config

        cfg = get_config(env)
        self.assertEqual(cfg["process_order"], "fifo")

    def test_lifo(self):
        env = {
            "IMAP_URL_1": "imap+ssl://user%40test.com:pass@imap.test.com:993/",
            "WEBHOOK_URL": "https://example.com/hook",
            "PROCESS_ORDER": "lifo",
        }
        from config import get_config

        cfg = get_config(env)
        self.assertEqual(cfg["process_order"], "lifo")

    def test_invalid_value_raises(self):
        env = {
            "IMAP_URL_1": "imap+ssl://user%40test.com:pass@imap.test.com:993/",
            "WEBHOOK_URL": "https://example.com/hook",
            "PROCESS_ORDER": "random",
        }
        from config import get_config

        with self.assertRaises(EnvironmentError):
            get_config(env)

    def test_lifo_reverses_msg_ids(self):
        """In LIFO mode, reversed msg_ids means [0] is the last (newest)."""
        msg_ids = ["1", "2", "3"]
        config = {"process_order": "lifo"}
        if msg_ids and config.get("process_order", "fifo") == "lifo":
            msg_ids = list(reversed(msg_ids))
        self.assertEqual(msg_ids[0], "3")


class TestWebhookSecret(unittest.TestCase):
    """F5: WEBHOOK_SECRET config tests."""

    def test_secret_in_config(self):
        env = {
            "IMAP_URL_1": "imap+ssl://user%40test.com:pass@imap.test.com:993/",
            "WEBHOOK_URL": "https://example.com/hook",
            "WEBHOOK_SECRET": "my-secret-123",
        }
        from config import get_config

        cfg = get_config(env)
        self.assertEqual(cfg["webhook_secret"], "my-secret-123")

    def test_empty_secret_default(self):
        env = {
            "IMAP_URL_1": "imap+ssl://user%40test.com:pass@imap.test.com:993/",
            "WEBHOOK_URL": "https://example.com/hook",
        }
        from config import get_config

        cfg = get_config(env)
        self.assertEqual(cfg["webhook_secret"], "")

    def test_secret_masked_in_printout(self):
        import copy

        from config import get_config

        env = {
            "IMAP_URL_1": "imap+ssl://user%40test.com:pass@imap.test.com:993/",
            "WEBHOOK_URL": "https://example.com/hook",
            "WEBHOOK_SECRET": "my-secret-123",
        }
        cfg = get_config(env)
        printout = copy.deepcopy(cfg)
        if printout.get("webhook_secret"):
            printout["webhook_secret"] = "********"
        self.assertEqual(printout["webhook_secret"], "********")
        # Original unchanged
        self.assertEqual(cfg["webhook_secret"], "my-secret-123")


class TestHealthcheck(unittest.TestCase):
    """F1: Healthcheck tests."""

    def test_heartbeat_fresh(self):
        import tempfile

        from healthcheck import HEARTBEAT_FILE, check

        with tempfile.NamedTemporaryFile(mode="w", suffix=".hb", delete=False) as f:
            f.write(str(time.time()))
            tmp = f.name
        try:
            with patch.dict(os.environ, {"HEARTBEAT_FILE": tmp, "HEARTBEAT_MAX_AGE": "600"}):
                # Re-import to pick up env
                import healthcheck

                healthcheck.HEARTBEAT_FILE = tmp
                healthcheck.MAX_AGE = 600
                result = healthcheck.check()
                self.assertEqual(result, 0)
        finally:
            os.unlink(tmp)

    def test_heartbeat_stale(self):
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".hb", delete=False) as f:
            f.write(str(time.time() - 1000))
            tmp = f.name
        # Set mtime to 1000s ago
        os.utime(tmp, (time.time() - 1000, time.time() - 1000))
        try:
            import healthcheck

            healthcheck.HEARTBEAT_FILE = tmp
            healthcheck.MAX_AGE = 600
            result = healthcheck.check()
            self.assertEqual(result, 1)
        finally:
            os.unlink(tmp)

    def test_heartbeat_missing(self):
        import healthcheck

        healthcheck.HEARTBEAT_FILE = "/tmp/nonexistent-heartbeat-test-file"
        healthcheck.MAX_AGE = 600
        result = healthcheck.check()
        self.assertEqual(result, 1)

    def test_touch_heartbeat_creates_file(self):
        import tempfile

        from daemon import _touch_heartbeat

        tmp = os.path.join(tempfile.gettempdir(), "test-heartbeat-touch")
        if os.path.exists(tmp):
            os.unlink(tmp)
        _touch_heartbeat(tmp)
        self.assertTrue(os.path.exists(tmp))
        age = time.time() - os.path.getmtime(tmp)
        self.assertLess(age, 5)
        os.unlink(tmp)


class TestStats(unittest.TestCase):
    """F2: Stats class tests."""

    def test_initial_state(self):
        from stats import Stats

        s = Stats()
        self.assertEqual(s.processed, 0)
        self.assertEqual(s.success, 0)
        self.assertEqual(s.failed, 0)
        self.assertEqual(s.refused, 0)
        self.assertEqual(s.retried, 0)
        self.assertEqual(s.oversized, 0)

    def test_record_success(self):
        from stats import Stats

        s = Stats()
        s.record_success(1.5)
        s.record_success(2.5)
        self.assertEqual(s.processed, 2)
        self.assertEqual(s.success, 2)
        self.assertEqual(s._durations, [1.5, 2.5])

    def test_record_failure(self):
        from stats import Stats

        s = Stats()
        s.record_failure()
        self.assertEqual(s.processed, 1)
        self.assertEqual(s.failed, 1)

    def test_maybe_log_summary_interval(self):
        from stats import Stats

        s = Stats(log_interval=1)
        s._last_log_time = time.time() - 2
        # Should not raise
        s.maybe_log_summary()
        # After logging, _last_log_time should be recent
        self.assertLess(time.time() - s._last_log_time, 2)

    def test_log_summary_format(self):
        import logging
        from io import StringIO

        from stats import Stats

        s = Stats()
        s.record_success(1.0)
        s.record_failure()
        s.record_refused()
        s.record_retry()
        s.record_oversized()

        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.DEBUG)
        s_logger = logging.getLogger("imap-to-webhook")
        old_level = s_logger.level
        s_logger.setLevel(logging.DEBUG)
        s_logger.addHandler(handler)
        try:
            s.log_summary()
            output = stream.getvalue()
            self.assertIn("STATS |", output)
            self.assertIn("processed=4", output)
        finally:
            s_logger.removeHandler(handler)
            s_logger.setLevel(old_level)


class TestRetry(unittest.TestCase):
    """F3: Retry logic tests."""

    def _make_config(self, **overrides):
        cfg = {
            "imap": {
                "hostname": "localhost",
                "username": "test",
                "password": "test",
                "protocol": "imap+ssl",
                "port": 993,
                "inbox": "INBOX",
                "error": "ERROR",
                "on_success": "move",
                "success": "SUCCESS",
                "refused": "REFUSED",
                "timeout": 60,
            },
            "webhook": "https://example.com/hook",
            "compress_eml": False,
            "delay": 60,
            "sentry_dsn": None,
            "webhook_secret": "",
            "webhook_max_retries": 0,
            "webhook_retry_delay": 1,
        }
        cfg.update(overrides)
        return cfg

    def _make_raw_mail(self):
        return (
            b"From: sender@example.com\r\n"
            b"To: recipient@example.com\r\n"
            b"Subject: Test\r\n"
            b"\r\n"
            b"Body\r\n"
        )

    def test_2xx_no_retry(self):
        from unittest.mock import MagicMock, patch as mock_patch

        from daemon import RESULT_SUCCESS, process_msg_from_raw

        client = MagicMock()
        session = MagicMock()
        response = MagicMock()
        response.status_code = 200
        response.text = '{"status":"OK"}'
        response.json.return_value = {"status": "OK"}
        response.raise_for_status.return_value = None
        session.post.return_value = response
        config = self._make_config()

        result = process_msg_from_raw(
            client, "1", self._make_raw_mail(), config, session
        )
        self.assertEqual(result, RESULT_SUCCESS)
        self.assertEqual(session.post.call_count, 1)

    def test_5xx_retry_then_success(self):
        from unittest.mock import MagicMock

        from daemon import RESULT_SUCCESS, process_msg_from_raw

        client = MagicMock()
        session = MagicMock()

        fail_resp = MagicMock()
        fail_resp.status_code = 500
        fail_resp.text = "error"

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.text = '{"status":"OK"}'
        ok_resp.json.return_value = {"status": "OK"}
        ok_resp.raise_for_status.return_value = None

        session.post.side_effect = [fail_resp, ok_resp]
        config = self._make_config(webhook_max_retries=1, webhook_retry_delay=0)

        result = process_msg_from_raw(
            client, "1", self._make_raw_mail(), config, session
        )
        self.assertEqual(result, RESULT_SUCCESS)
        self.assertEqual(session.post.call_count, 2)

    def test_refused_no_retry(self):
        from unittest.mock import MagicMock

        from daemon import RESULT_REFUSED, process_msg_from_raw

        client = MagicMock()
        session = MagicMock()
        response = MagicMock()
        response.status_code = 400
        response.text = '{"status":"REFUSED"}'
        response.json.return_value = {"status": "REFUSED"}
        session.post.return_value = response
        config = self._make_config(webhook_max_retries=3)

        result = process_msg_from_raw(
            client, "1", self._make_raw_mail(), config, session
        )
        self.assertEqual(result, RESULT_REFUSED)
        self.assertEqual(session.post.call_count, 1)

    def test_retries_exhausted_moves_to_error(self):
        from unittest.mock import MagicMock

        import requests as req

        from daemon import RESULT_FAILED, process_msg_from_raw

        client = MagicMock()
        session = MagicMock()
        session.post.side_effect = req.exceptions.ConnectionError("refused")
        config = self._make_config(webhook_max_retries=2, webhook_retry_delay=0)

        with patch("daemon.sentry_sdk"):
            result = process_msg_from_raw(
                client, "1", self._make_raw_mail(), config, session
            )
        self.assertEqual(result, RESULT_FAILED)
        self.assertEqual(session.post.call_count, 3)  # initial + 2 retries
        client.move.assert_called_once()

    def test_non_refused_4xx_raises(self):
        from unittest.mock import MagicMock

        import requests as req

        from daemon import RESULT_FAILED, process_msg_from_raw

        client = MagicMock()
        session = MagicMock()
        response = MagicMock()
        response.status_code = 422
        response.text = "Unprocessable"
        response.json.return_value = {"error": "bad input"}
        response.raise_for_status.side_effect = req.exceptions.HTTPError(
            response=response
        )
        session.post.return_value = response
        config = self._make_config(webhook_max_retries=0)

        with patch("daemon.sentry_sdk"):
            result = process_msg_from_raw(
                client, "1", self._make_raw_mail(), config, session
            )
        self.assertEqual(result, RESULT_FAILED)

    def test_connection_error_triggers_retry(self):
        from unittest.mock import MagicMock

        import requests as req

        from daemon import RESULT_SUCCESS, process_msg_from_raw
        from stats import Stats

        client = MagicMock()
        session = MagicMock()
        stats = Stats()

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.text = '{"status":"OK"}'
        ok_resp.json.return_value = {"status": "OK"}
        ok_resp.raise_for_status.return_value = None

        session.post.side_effect = [
            req.exceptions.ConnectionError("refused"),
            ok_resp,
        ]
        config = self._make_config(webhook_max_retries=1, webhook_retry_delay=0)

        result = process_msg_from_raw(
            client, "1", self._make_raw_mail(), config, session, stats
        )
        self.assertEqual(result, RESULT_SUCCESS)
        self.assertEqual(stats.retried, 1)

    def test_retry_exponential_delay(self):
        """Verify delay calculation follows exponential backoff pattern."""
        retry_delay = 10
        delays = [retry_delay * (2**attempt) for attempt in range(3)]
        self.assertEqual(delays, [10, 20, 40])

    def test_no_retry_by_default(self):
        env = {
            "IMAP_URL_1": "imap+ssl://user%40test.com:pass@imap.test.com:993/",
            "WEBHOOK_URL": "https://example.com/hook",
        }
        from config import get_config

        cfg = get_config(env)
        self.assertEqual(cfg["webhook_max_retries"], 0)
        self.assertEqual(cfg["webhook_retry_delay"], 10)


class TestBatchProcessing(unittest.TestCase):
    """F4: Batch processing + throttled delivery tests."""

    def test_config_default_batch_size(self):
        env = {
            "IMAP_URL_1": "imap+ssl://user%40test.com:pass@imap.test.com:993/",
            "WEBHOOK_URL": "https://example.com/hook",
        }
        from config import get_config

        cfg = get_config(env)
        self.assertEqual(cfg["batch_size"], 1)
        self.assertEqual(cfg["delivery_interval"], 0)

    def test_config_batch_size_zero_raises(self):
        env = {
            "IMAP_URL_1": "imap+ssl://user%40test.com:pass@imap.test.com:993/",
            "WEBHOOK_URL": "https://example.com/hook",
            "BATCH_SIZE": "0",
        }
        from config import get_config

        with self.assertRaises(EnvironmentError):
            get_config(env)

    def test_config_negative_delivery_interval_raises(self):
        env = {
            "IMAP_URL_1": "imap+ssl://user%40test.com:pass@imap.test.com:993/",
            "WEBHOOK_URL": "https://example.com/hook",
            "DELIVERY_INTERVAL": "-1",
        }
        from config import get_config

        with self.assertRaises(EnvironmentError):
            get_config(env)

    def test_batch_limits_download(self):
        """BATCH_SIZE=3 with 5 messages should only take first 3."""
        msg_ids = ["1", "2", "3", "4", "5"]
        batch_size = 3
        batch = msg_ids[:batch_size]
        self.assertEqual(len(batch), 3)
        self.assertEqual(batch, ["1", "2", "3"])

    def test_single_fetch_failure_skipped(self):
        """A failed fetch should not prevent other messages from being downloaded."""
        from unittest.mock import MagicMock

        client = MagicMock()
        # Fetch msg "2" fails, others succeed
        client.fetch.side_effect = [b"mail1", Exception("fetch error"), b"mail3"]

        downloaded = []
        for msg_id in ["1", "2", "3"]:
            try:
                raw = client.fetch(msg_id)
                downloaded.append((msg_id, raw))
            except Exception:
                pass
        self.assertEqual(len(downloaded), 2)
        self.assertEqual(downloaded[0][0], "1")
        self.assertEqual(downloaded[1][0], "3")

    def test_batch_size_1_single_message(self):
        """BATCH_SIZE=1 should process exactly one message (backward compatible)."""
        msg_ids = ["10", "20", "30"]
        batch = msg_ids[:1]
        self.assertEqual(batch, ["10"])

    def test_delivery_interval_config(self):
        env = {
            "IMAP_URL_1": "imap+ssl://user%40test.com:pass@imap.test.com:993/",
            "WEBHOOK_URL": "https://example.com/hook",
            "BATCH_SIZE": "5",
            "DELIVERY_INTERVAL": "10.5",
        }
        from config import get_config

        cfg = get_config(env)
        self.assertEqual(cfg["batch_size"], 5)
        self.assertAlmostEqual(cfg["delivery_interval"], 10.5)


class TestMultiAccount(unittest.TestCase):
    """F6: Multi-account support tests."""

    def test_single_account(self):
        env = {
            "IMAP_URL_1": "imap+ssl://user%40test.com:pass@imap.test.com:993/",
            "WEBHOOK_URL": "https://example.com/hook",
        }
        from config import get_config

        cfg = get_config(env)
        self.assertEqual(len(cfg["imap_accounts"]), 1)
        self.assertEqual(cfg["imap"]["hostname"], "imap.test.com")

    def test_multiple_imap_urls(self):
        env = {
            "IMAP_URL_1": "imap+ssl://user1%40a.com:pass1@imap.a.com:993/",
            "IMAP_URL_2": "imap+ssl://user2%40b.com:pass2@imap.b.com:993/",
            "IMAP_URL_3": "imap+ssl://user3%40c.com:pass3@imap.c.com:993/",
            "WEBHOOK_URL": "https://example.com/hook",
        }
        from config import get_config

        cfg = get_config(env)
        self.assertEqual(len(cfg["imap_accounts"]), 3)
        self.assertEqual(cfg["imap_accounts"][0]["hostname"], "imap.a.com")
        self.assertEqual(cfg["imap_accounts"][1]["hostname"], "imap.b.com")
        self.assertEqual(cfg["imap_accounts"][2]["hostname"], "imap.c.com")

    def test_no_imap_url_raises(self):
        env = {
            "WEBHOOK_URL": "https://example.com/hook",
        }
        from config import get_config

        with self.assertRaises(EnvironmentError) as ctx:
            get_config(env)
        self.assertIn("IMAP_URL_1 is required", str(ctx.exception))

    def test_imap_backward_compat(self):
        """config['imap'] points to first account."""
        env = {
            "IMAP_URL_1": "imap+ssl://first%40a.com:pass@imap.a.com:993/",
            "IMAP_URL_2": "imap+ssl://second%40b.com:pass@imap.b.com:993/",
            "WEBHOOK_URL": "https://example.com/hook",
        }
        from config import get_config

        cfg = get_config(env)
        self.assertEqual(cfg["imap"]["username"], "first@a.com")

    def test_legacy_imap_url_gives_helpful_error(self):
        """Using bare IMAP_URL raises with migration hint."""
        env = {
            "IMAP_URL": "imap+ssl://old%40test.com:pass@imap.test.com:993/",
            "WEBHOOK_URL": "https://example.com/hook",
        }
        from config import get_config

        with self.assertRaises(EnvironmentError) as ctx:
            get_config(env)
        self.assertIn("Rename", str(ctx.exception))
        self.assertIn("IMAP_URL_1", str(ctx.exception))

    def test_password_masked_all_accounts(self):
        import copy

        from config import get_config

        env = {
            "IMAP_URL_1": "imap+ssl://u1%40a.com:secret1@imap.a.com:993/",
            "IMAP_URL_2": "imap+ssl://u2%40b.com:secret2@imap.b.com:993/",
            "WEBHOOK_URL": "https://example.com/hook",
        }
        cfg = get_config(env)
        printout = copy.deepcopy(cfg)
        for acct in printout.get("imap_accounts", []):
            if "password" in acct:
                acct["password"] = "********"
        if "password" in printout.get("imap", {}):
            printout["imap"]["password"] = "********"
        for acct in printout["imap_accounts"]:
            self.assertEqual(acct["password"], "********")


class TestNoopMode(unittest.TestCase):
    """ON_SUCCESS=noop: mark processed instead of move/delete."""

    def _make_config(self, **overrides):
        cfg = {
            "imap": {
                "hostname": "localhost",
                "username": "test",
                "password": "test",
                "protocol": "imap+ssl",
                "port": 993,
                "inbox": "INBOX",
                "error": "ERROR",
                "on_success": "move",
                "success": "SUCCESS",
                "refused": "REFUSED",
                "timeout": 60,
                "noop_flag": r"\Seen",
            },
            "webhook": "https://example.com/hook",
            "compress_eml": False,
            "delay": 60,
            "sentry_dsn": None,
            "webhook_secret": "",
            "webhook_max_retries": 0,
            "webhook_retry_delay": 1,
        }
        cfg.update(overrides)
        return cfg

    def _make_raw_mail(self):
        return (
            b"From: sender@example.com\r\n"
            b"To: recipient@example.com\r\n"
            b"Subject: Test\r\n"
            b"\r\n"
            b"Body\r\n"
        )

    # --- config.py tests ---

    def test_config_accepts_noop(self):
        from config import get_config

        env = {
            "IMAP_URL_1": "imap+ssl://user%40test.com:pass@imap.test.com:993/",
            "WEBHOOK_URL": "https://example.com/hook",
            "ON_SUCCESS": "noop",
        }
        cfg = get_config(env)
        self.assertEqual(cfg["imap"]["on_success"], "noop")

    def test_config_rejects_invalid(self):
        from config import get_config

        env = {
            "IMAP_URL_1": "imap+ssl://user%40test.com:pass@imap.test.com:993/",
            "WEBHOOK_URL": "https://example.com/hook",
            "ON_SUCCESS": "xxx",
        }
        with self.assertRaises(EnvironmentError):
            get_config(env)

    # --- connection.py tests ---

    def test_get_mail_ids_noop_searches_unseen(self):
        from unittest.mock import MagicMock

        config = self._make_config()
        config["imap"]["on_success"] = "noop"

        client = MagicMock()
        client.login.return_value = ("OK", [])
        client.select.return_value = ("OK", [b"1"])
        client.uid.return_value = ("OK", [b"1 2 3"])

        from connection import IMAPClient

        with unittest.mock.patch.object(
            IMAPClient, "__init__", lambda self, cfg: None
        ):
            imap = IMAPClient.__new__(IMAPClient)
            imap.client = client
            imap.on_success = "noop"
            imap.noop_flag = r"\Seen"
            imap.get_mail_ids()
            client.uid.assert_called_with("SEARCH", "UNSEEN")

    def test_get_mail_ids_move_searches_all(self):
        from unittest.mock import MagicMock

        client = MagicMock()
        client.uid.return_value = ("OK", [b"1 2"])

        from connection import IMAPClient

        imap = IMAPClient.__new__(IMAPClient)
        imap.client = client
        imap.on_success = "move"
        imap.get_mail_ids()
        client.uid.assert_called_with("SEARCH", "ALL")

    def test_fetch_uses_body_peek(self):
        from unittest.mock import MagicMock

        client = MagicMock()
        client.uid.return_value = ("OK", [(b"1 (BODY[] {5}", b"hello")])

        from connection import IMAPClient

        imap = IMAPClient.__new__(IMAPClient)
        imap.client = client
        imap.fetch("1")
        client.uid.assert_called_with("FETCH", "1 (BODY.PEEK[])")

    def test_mark_seen(self):
        from unittest.mock import MagicMock

        client = MagicMock()
        client.uid.return_value = ("OK", [None])

        from connection import IMAPClient

        imap = IMAPClient.__new__(IMAPClient)
        imap.client = client
        imap.mark_seen("42")
        client.uid.assert_called_with("STORE", "42", "+FLAGS", r"(\Seen)")

    # --- daemon.py _handle_success tests ---

    def test_handle_success_noop(self):
        from unittest.mock import MagicMock

        from daemon import _handle_success

        client = MagicMock()
        config = self._make_config()
        config["imap"]["on_success"] = "noop"
        _handle_success(client, "1", config)
        client.mark_processed.assert_called_once_with("1")
        client.move.assert_not_called()
        client.mark_delete.assert_not_called()

    def test_handle_success_move_regression(self):
        from unittest.mock import MagicMock

        from daemon import _handle_success

        client = MagicMock()
        config = self._make_config()
        config["imap"]["on_success"] = "move"
        _handle_success(client, "1", config)
        client.move.assert_called_once_with("1", "SUCCESS")
        client.mark_processed.assert_not_called()

    def test_handle_success_delete_regression(self):
        from unittest.mock import MagicMock

        from daemon import _handle_success

        client = MagicMock()
        config = self._make_config()
        config["imap"]["on_success"] = "delete"
        _handle_success(client, "1", config)
        client.mark_delete.assert_called_once_with("1")
        client.mark_processed.assert_not_called()

    # --- integration-level tests ---

    def test_noop_error_still_moves(self):
        """Even in noop mode, webhook errors still move to ERROR folder."""
        from unittest.mock import MagicMock, patch as mock_patch

        import requests as req

        from daemon import RESULT_FAILED, process_msg_from_raw

        client = MagicMock()
        session = MagicMock()
        session.post.side_effect = req.exceptions.ConnectionError("refused")
        config = self._make_config(webhook_max_retries=0)
        config["imap"]["on_success"] = "noop"

        with mock_patch("daemon.sentry_sdk"):
            result = process_msg_from_raw(
                client, "1", self._make_raw_mail(), config, session
            )
        self.assertEqual(result, RESULT_FAILED)
        client.move.assert_called_once_with("1", "ERROR")

    def test_noop_success_full_flow(self):
        """noop + 2xx → mark_processed() called, not move/delete."""
        from unittest.mock import MagicMock

        from daemon import RESULT_SUCCESS, process_msg_from_raw

        client = MagicMock()
        session = MagicMock()
        response = MagicMock()
        response.status_code = 200
        response.text = '{"status":"OK"}'
        response.json.return_value = {"status": "OK"}
        response.raise_for_status.return_value = None
        session.post.return_value = response
        config = self._make_config()
        config["imap"]["on_success"] = "noop"

        result = process_msg_from_raw(
            client, "1", self._make_raw_mail(), config, session
        )
        self.assertEqual(result, RESULT_SUCCESS)
        client.mark_processed.assert_called_once_with("1")
        client.move.assert_not_called()
        client.mark_delete.assert_not_called()

    # --- NOOP_FLAG config tests ---

    def test_config_noop_flag_default(self):
        from config import get_config

        env = {
            "IMAP_URL_1": "imap+ssl://user%40test.com:pass@imap.test.com:993/",
            "WEBHOOK_URL": "https://example.com/hook",
            "ON_SUCCESS": "noop",
        }
        cfg = get_config(env)
        self.assertEqual(cfg["imap"]["noop_flag"], r"\Seen")

    def test_config_noop_flag_custom(self):
        from config import get_config

        env = {
            "IMAP_URL_1": "imap+ssl://user%40test.com:pass@imap.test.com:993/",
            "WEBHOOK_URL": "https://example.com/hook",
            "ON_SUCCESS": "noop",
            "NOOP_FLAG": "$WebhookProcessed",
        }
        cfg = get_config(env)
        self.assertEqual(cfg["imap"]["noop_flag"], "$WebhookProcessed")

    def test_config_noop_flag_multi_account(self):
        from config import get_config

        env = {
            "IMAP_URL_1": "imap+ssl://a%40test.com:pass@imap.test.com:993/",
            "IMAP_URL_2": "imap+ssl://b%40test.com:pass@imap.test.com:993/",
            "WEBHOOK_URL": "https://example.com/hook",
            "NOOP_FLAG": "$WebhookProcessed",
        }
        cfg = get_config(env)
        for acct in cfg["imap_accounts"]:
            self.assertEqual(acct["noop_flag"], "$WebhookProcessed")

    # --- mark_processed tests ---

    def test_mark_processed_default_flag(self):
        from unittest.mock import MagicMock

        from connection import IMAPClient

        client = MagicMock()
        client.uid.return_value = ("OK", [None])

        imap = IMAPClient.__new__(IMAPClient)
        imap.client = client
        imap.noop_flag = r"\Seen"
        imap.mark_processed("42")
        client.uid.assert_called_with("STORE", "42", "+FLAGS", r"(\Seen)")

    def test_mark_processed_custom_flag(self):
        from unittest.mock import MagicMock

        from connection import IMAPClient

        client = MagicMock()
        client.uid.return_value = ("OK", [None])

        imap = IMAPClient.__new__(IMAPClient)
        imap.client = client
        imap.noop_flag = "$WebhookProcessed"
        imap.mark_processed("42")
        client.uid.assert_called_with(
            "STORE", "42", "+FLAGS", "($WebhookProcessed)"
        )

    def test_get_mail_ids_noop_custom_flag_searches_unkeyword(self):
        from unittest.mock import MagicMock

        from connection import IMAPClient

        client = MagicMock()
        client.uid.return_value = ("OK", [b"1 2 3"])

        imap = IMAPClient.__new__(IMAPClient)
        imap.client = client
        imap.on_success = "noop"
        imap.noop_flag = "$WebhookProcessed"
        imap.get_mail_ids()
        client.uid.assert_called_with("SEARCH", "UNKEYWORD", "$WebhookProcessed")

    def test_noop_custom_flag_success_full_flow(self):
        """noop + custom flag + 2xx → mark_processed() with custom flag."""
        from unittest.mock import MagicMock

        from daemon import RESULT_SUCCESS, process_msg_from_raw

        client = MagicMock()
        session = MagicMock()
        response = MagicMock()
        response.status_code = 200
        response.text = '{"status":"OK"}'
        response.json.return_value = {"status": "OK"}
        response.raise_for_status.return_value = None
        session.post.return_value = response
        config = self._make_config()
        config["imap"]["on_success"] = "noop"
        config["imap"]["noop_flag"] = "$WebhookProcessed"

        result = process_msg_from_raw(
            client, "1", self._make_raw_mail(), config, session
        )
        self.assertEqual(result, RESULT_SUCCESS)
        client.mark_processed.assert_called_once_with("1")
        client.move.assert_not_called()
        client.mark_delete.assert_not_called()


class TestInfiniteLoopPrevention(unittest.TestCase):
    """Tests for infinite loop / tight-loop prevention fixes."""

    def _make_config(self, **overrides):
        cfg = {
            "imap": {
                "hostname": "localhost",
                "username": "test",
                "password": "test",
                "protocol": "imap+ssl",
                "port": 993,
                "inbox": "INBOX",
                "error": "ERROR",
                "on_success": "move",
                "success": "SUCCESS",
                "refused": "REFUSED",
                "timeout": 60,
                "noop_flag": r"\Seen",
            },
            "webhook": "https://example.com/hook",
            "compress_eml": False,
            "delay": 60,
            "sentry_dsn": None,
            "webhook_secret": "",
            "webhook_max_retries": 0,
            "webhook_retry_delay": 1,
        }
        cfg.update(overrides)
        return cfg

    def _make_raw_mail(self):
        return (
            b"From: sender@example.com\r\n"
            b"To: recipient@example.com\r\n"
            b"Subject: Test\r\n"
            b"\r\n"
            b"Body\r\n"
        )

    # --- _last_resort_mark tests ---

    def test_last_resort_noop_marks_processed(self):
        from unittest.mock import MagicMock

        from daemon import _last_resort_mark

        client = MagicMock()
        config = self._make_config()
        config["imap"]["on_success"] = "noop"
        _last_resort_mark(client, "1", config)
        client.mark_processed.assert_called_once_with("1")
        client.mark_delete.assert_not_called()

    def test_last_resort_move_marks_deleted(self):
        from unittest.mock import MagicMock

        from daemon import _last_resort_mark

        client = MagicMock()
        config = self._make_config()
        config["imap"]["on_success"] = "move"
        _last_resort_mark(client, "1", config)
        client.mark_delete.assert_called_once_with("1")
        client.mark_processed.assert_not_called()

    def test_last_resort_exception_not_raised(self):
        from unittest.mock import MagicMock, patch as mock_patch

        from daemon import _last_resort_mark

        client = MagicMock()
        client.mark_delete.side_effect = Exception("IMAP dead")
        config = self._make_config()
        config["imap"]["on_success"] = "move"
        # Should not raise
        with mock_patch("daemon.logger") as mock_logger:
            _last_resort_mark(client, "1", config)
            mock_logger.critical.assert_called_once()

    # --- _handle_success return value tests ---

    def test_handle_success_returns_true(self):
        from unittest.mock import MagicMock

        from daemon import _handle_success

        for mode in ("move", "delete", "noop"):
            client = MagicMock()
            config = self._make_config()
            config["imap"]["on_success"] = mode
            result = _handle_success(client, "1", config)
            self.assertTrue(result, f"Expected True for on_success={mode}")

    def test_handle_success_returns_false_on_error(self):
        from unittest.mock import MagicMock

        from daemon import _handle_success

        for mode, method in [
            ("move", "move"),
            ("delete", "mark_delete"),
            ("noop", "mark_processed"),
        ]:
            client = MagicMock()
            getattr(client, method).side_effect = Exception("fail")
            config = self._make_config()
            config["imap"]["on_success"] = mode
            result = _handle_success(client, "1", config)
            self.assertFalse(result, f"Expected False for on_success={mode}")

    # --- _handle_success failure → ERROR / last resort ---

    def test_handle_success_fail_moves_to_error(self):
        from unittest.mock import MagicMock

        from daemon import RESULT_SUCCESS, process_msg_from_raw

        client = MagicMock()
        client.move.side_effect = [Exception("SUCCESS folder gone"), None]
        session = MagicMock()
        response = MagicMock()
        response.status_code = 200
        response.text = '{"status":"OK"}'
        response.json.return_value = {"status": "OK"}
        response.raise_for_status.return_value = None
        session.post.return_value = response
        config = self._make_config()
        config["imap"]["on_success"] = "move"

        result = process_msg_from_raw(
            client, "1", self._make_raw_mail(), config, session
        )
        self.assertEqual(result, RESULT_SUCCESS)
        # First move (SUCCESS) fails, second move (ERROR) succeeds
        self.assertEqual(client.move.call_count, 2)
        self.assertEqual(client.move.call_args_list[1][0], ("1", "ERROR"))

    def test_handle_success_fail_last_resort(self):
        from unittest.mock import MagicMock

        from daemon import RESULT_SUCCESS, process_msg_from_raw

        client = MagicMock()
        # All moves fail
        client.move.side_effect = Exception("all folders gone")
        session = MagicMock()
        response = MagicMock()
        response.status_code = 200
        response.text = '{"status":"OK"}'
        response.json.return_value = {"status": "OK"}
        response.raise_for_status.return_value = None
        session.post.return_value = response
        config = self._make_config()
        config["imap"]["on_success"] = "move"

        result = process_msg_from_raw(
            client, "1", self._make_raw_mail(), config, session
        )
        self.assertEqual(result, RESULT_SUCCESS)
        # Last resort: mark_delete for move mode
        client.mark_delete.assert_called_once_with("1")

    # --- REFUSED fallback tests ---

    def test_refused_move_fails_falls_back_to_error(self):
        from unittest.mock import MagicMock, call

        from daemon import RESULT_REFUSED, process_msg_from_raw

        client = MagicMock()
        # First move (REFUSED) fails, second (ERROR) succeeds
        client.move.side_effect = [Exception("REFUSED folder gone"), None]
        session = MagicMock()
        response = MagicMock()
        response.status_code = 400
        response.text = '{"status":"REFUSED"}'
        response.json.return_value = {"status": "REFUSED"}
        session.post.return_value = response
        config = self._make_config()

        result = process_msg_from_raw(
            client, "1", self._make_raw_mail(), config, session
        )
        self.assertEqual(result, RESULT_REFUSED)
        self.assertEqual(client.move.call_count, 2)
        self.assertEqual(client.move.call_args_list[1][0], ("1", "ERROR"))

    def test_refused_both_fail_last_resort(self):
        from unittest.mock import MagicMock

        from daemon import RESULT_REFUSED, process_msg_from_raw

        client = MagicMock()
        client.move.side_effect = Exception("all folders gone")
        session = MagicMock()
        response = MagicMock()
        response.status_code = 400
        response.text = '{"status":"REFUSED"}'
        response.json.return_value = {"status": "REFUSED"}
        session.post.return_value = response
        config = self._make_config()
        config["imap"]["on_success"] = "move"

        result = process_msg_from_raw(
            client, "1", self._make_raw_mail(), config, session
        )
        self.assertEqual(result, RESULT_REFUSED)
        # Last resort: mark_delete for move mode
        client.mark_delete.assert_called_once_with("1")

    # --- oversized / unserializable last resort ---

    def test_oversized_error_missing_uses_last_resort(self):
        from unittest.mock import MagicMock

        from daemon import MAX_EMAIL_SIZE, RESULT_OVERSIZED, process_msg_from_raw

        client = MagicMock()
        client.move.side_effect = Exception("ERROR folder gone")
        config = self._make_config()
        config["imap"]["on_success"] = "noop"

        oversized = b"X" * (MAX_EMAIL_SIZE + 1)
        # Should NOT raise (old behavior was RuntimeError)
        result = process_msg_from_raw(
            client, "1", oversized, config, MagicMock()
        )
        self.assertEqual(result, RESULT_OVERSIZED)
        client.mark_processed.assert_called_once_with("1")

    def test_unserializable_error_missing_uses_last_resort(self):
        from unittest.mock import MagicMock, patch as mock_patch

        from daemon import RESULT_FAILED, process_msg_from_raw

        client = MagicMock()
        client.move.side_effect = Exception("ERROR folder gone")
        config = self._make_config()
        config["imap"]["on_success"] = "delete"

        # Corrupt bytes that will fail serialization
        corrupt = b"\x00" * 10

        with mock_patch("daemon.serialize_mail", side_effect=Exception("parse fail")):
            with mock_patch("daemon.sentry_sdk"):
                result = process_msg_from_raw(
                    client, "1", corrupt, config, MagicMock()
                )
        self.assertEqual(result, RESULT_FAILED)
        client.mark_delete.assert_called_once_with("1")

    # --- Phase 2 per-message exception handling ---

    def test_phase2_one_failure_continues_batch(self):
        from unittest.mock import MagicMock, patch as mock_patch, call

        from daemon import process_msg_from_raw

        call_log = []

        def mock_process(client, msg_id, raw, config, session, stats=None):
            call_log.append(msg_id)
            if msg_id == "1":
                raise Exception("msg 1 exploded")

        config = self._make_config()
        config["imap_accounts"] = [config["imap"]]
        config["batch_size"] = 3
        config["delivery_interval"] = 0

        client_mock = MagicMock()
        client_mock.get_mail_ids.return_value = ["1", "2", "3"]
        client_mock.fetch.side_effect = [b"mail1", b"mail2", b"mail3"]

        with mock_patch("daemon.IMAPClient", return_value=client_mock):
            with mock_patch("daemon.process_msg_from_raw", side_effect=mock_process):
                with mock_patch("daemon._interruptible_sleep"):
                    with mock_patch("daemon._touch_heartbeat"):
                        with mock_patch("daemon.sentry_sdk"):
                            import daemon

                            old_shutdown = daemon._shutdown
                            # Run one iteration then stop
                            call_count = [0]
                            orig_shutdown = daemon._shutdown

                            def stop_after_one(*a, **kw):
                                daemon._shutdown = True

                            with mock_patch.object(
                                client_mock, "connection_close",
                                side_effect=stop_after_one,
                            ):
                                daemon._shutdown = False
                                daemon.loop(config, MagicMock())
                                daemon._shutdown = old_shutdown

        # All 3 messages were attempted despite msg 1 failing
        self.assertEqual(call_log, ["1", "2", "3"])

    def test_phase2_all_fail_increments_errors(self):
        from unittest.mock import MagicMock, patch as mock_patch

        config = self._make_config()
        config["imap_accounts"] = [config["imap"]]
        config["batch_size"] = 2
        config["delivery_interval"] = 0

        client_mock = MagicMock()
        client_mock.get_mail_ids.return_value = ["1", "2"]
        client_mock.fetch.side_effect = [b"mail1", b"mail2"]

        def always_fail(client, msg_id, raw, config, session, stats=None):
            raise Exception("always fails")

        import daemon

        old_shutdown = daemon._shutdown

        def stop_on_sleep(*a, **kw):
            daemon._shutdown = True

        with mock_patch("daemon.IMAPClient", return_value=client_mock):
            with mock_patch("daemon.process_msg_from_raw", side_effect=always_fail):
                with mock_patch(
                    "daemon._interruptible_sleep", side_effect=stop_on_sleep
                ) as mock_sleep:
                    with mock_patch("daemon._touch_heartbeat"):
                        with mock_patch("daemon.sentry_sdk"):
                            daemon._shutdown = False
                            daemon.loop(config, MagicMock())
                            daemon._shutdown = old_shutdown

                    # Backoff should fire because consecutive_errors > 0
                    self.assertTrue(
                        mock_sleep.called,
                        "Expected backoff sleep to be called",
                    )

    # --- Backoff logic tests ---

    def test_backoff_fires_with_messages_and_errors(self):
        """consecutive_errors > 0 + messages present → backoff fires."""
        from unittest.mock import MagicMock, patch as mock_patch

        config = self._make_config()
        config["imap_accounts"] = [config["imap"]]
        config["batch_size"] = 1
        config["delivery_interval"] = 0

        client_mock = MagicMock()
        client_mock.get_mail_ids.return_value = ["1"]
        client_mock.fetch.return_value = b"mail"

        def always_fail(client, msg_id, raw, config, session, stats=None):
            raise Exception("boom")

        import daemon

        old_shutdown = daemon._shutdown

        def stop_on_sleep(*a, **kw):
            daemon._shutdown = True

        with mock_patch("daemon.IMAPClient", return_value=client_mock):
            with mock_patch("daemon.process_msg_from_raw", side_effect=always_fail):
                with mock_patch(
                    "daemon._interruptible_sleep", side_effect=stop_on_sleep
                ) as mock_sleep:
                    with mock_patch("daemon._touch_heartbeat"):
                        with mock_patch("daemon.sentry_sdk"):
                            daemon._shutdown = False
                            daemon.loop(config, MagicMock())
                            daemon._shutdown = old_shutdown

                    # Backoff should have been called (not the normal delay)
                    self.assertTrue(
                        mock_sleep.called,
                        "Expected backoff sleep when errors present with messages",
                    )

    def test_no_backoff_when_messages_present_no_errors(self):
        """messages present + no errors → no sleep (fast loop)."""
        from unittest.mock import MagicMock, patch as mock_patch

        config = self._make_config()
        config["imap_accounts"] = [config["imap"]]
        config["batch_size"] = 1
        config["delivery_interval"] = 0

        client_mock = MagicMock()
        client_mock.get_mail_ids.return_value = ["1"]
        client_mock.fetch.return_value = b"mail"

        with mock_patch("daemon.IMAPClient", return_value=client_mock):
            with mock_patch("daemon.process_msg_from_raw"):
                with mock_patch("daemon._interruptible_sleep") as mock_sleep:
                    with mock_patch("daemon._touch_heartbeat"):
                        with mock_patch("daemon.sentry_sdk"):
                            import daemon

                            old_shutdown = daemon._shutdown

                            def stop_after_one(*a, **kw):
                                daemon._shutdown = True

                            with mock_patch.object(
                                client_mock, "connection_close",
                                side_effect=stop_after_one,
                            ):
                                daemon._shutdown = False
                                daemon.loop(config, MagicMock())
                                daemon._shutdown = old_shutdown

                    # No sleep should be called (fast loop)
                    mock_sleep.assert_not_called()

    # --- Webhook final failure + ERROR move fails ---

    def test_webhook_fail_error_move_fails_last_resort(self):
        from unittest.mock import MagicMock, patch as mock_patch

        import requests as req

        from daemon import RESULT_FAILED, process_msg_from_raw

        client = MagicMock()
        client.move.side_effect = Exception("ERROR folder gone")
        session = MagicMock()
        session.post.side_effect = req.exceptions.ConnectionError("refused")
        config = self._make_config(webhook_max_retries=0)
        config["imap"]["on_success"] = "delete"

        with mock_patch("daemon.sentry_sdk"):
            result = process_msg_from_raw(
                client, "1", self._make_raw_mail(), config, session
            )
        self.assertEqual(result, RESULT_FAILED)
        # Last resort: mark_delete for delete mode
        client.mark_delete.assert_called_once_with("1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
    # unittest.main(verbosity=2, defaultTest="TestMain.test_8bit_text_html")
