"""Telegram's one-message cap applies to a prompt's SENT payload, not to its source text.

Telegram rejects any message over 4096 UTF-16 code units outright, and a control prompt cannot
be chunked the way a reply can — the inline keyboard rides a single message, so an oversize
approval card means the user never sees buttons at all.  A raw-character budget cannot express
that cap: HTML-escaping runs *after* it (``&`` costs five units, ``<`` four) and astral chars
cost two units apiece, so a command comfortably under a 3800-char budget still overflowed once
it was escaped into the card's ``<pre>`` block.

The fix cuts the SOURCE and re-renders the frame around it, so these tests pin the contract
from both sides: an oversize prompt is shrunk until the real payload fits (markup still valid,
buttons still attached, the cut announced), and a prompt that already fits is untouched.

The fitter shrinks only the *variable* source, so the frame around it has to be bounded by its
caller: the exec-approval reason is the one other unbounded field, and no amount of shrinking
the command rescues a card whose reason alone overflows.  ``_EA_REASON_BUDGET`` is that bound,
and the last test pins it.
"""

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)

from gateway.config import PlatformConfig
from gateway.platforms.base import utf16_len
from plugins.platforms.telegram.adapter import TelegramAdapter


# ~10k chars carrying every cost multiplier at once: HTML specials that grow under escaping
# (``&`` → ``&amp;``), MarkdownV2 specials that grow under backslash-escaping, and an astral
# emoji that costs two UTF-16 units but one Python char.
_OVERSIZE_BODY = "rm -rf /srv/data && echo '<script>' | tail -f ~/a\U0001F600 " * 200
_SHORT_BODY = "rm -rf /srv/cache && echo 'done' > ~/log\U0001F600"
# A reason long enough to blow the cap on its own, carrying the same escaping multipliers.
_OVERSIZE_REASON = "writes outside the sandbox & reads <secrets> \U0001F600 " * 200


def _make_adapter():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token", extra={}))
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    message = MagicMock()
    message.message_id = 42
    adapter._bot.send_message = AsyncMock(return_value=message)
    return adapter


async def _exec_approval(adapter, body):
    return await adapter.send_exec_approval(
        chat_id="12345", command=body, session_key="agent:main:telegram:dm:12345:1",
        description="dangerous command")


async def _slash_confirm(adapter, body):
    return await adapter.send_slash_confirm("12345", "Confirm", body, "agent:main", "c1")


async def _clarify(adapter, body):
    return await adapter.send_clarify("12345", body, ["keep going", "stop"], "cl1", "agent:main")


async def _update_prompt(adapter, body):
    return await adapter.send_update_prompt("12345", body, default="y")


# (sender, renders_html) — the HTML pair is parsed for well-formedness; the MarkdownV2 pair only
# has to fit, since an unbalanced MarkdownV2 construct degrades to literal text, not a send error.
_SENDERS = [
    pytest.param(_exec_approval, True, id="exec_approval"),
    pytest.param(_clarify, True, id="clarify"),
    pytest.param(_slash_confirm, False, id="slash_confirm"),
    pytest.param(_update_prompt, False, id="update_prompt"),
]


async def _send_and_capture(send, body, *, bypass_fitting=False):
    """Run ``send`` on a fresh adapter and return the kwargs it handed ``bot.send_message``.

    ``bypass_fitting`` neutralises the length fitter so the frame renders the whole source —
    that is the "before" payload, and what the adapter must reproduce byte-for-byte whenever it
    already fits.
    """
    adapter = _make_adapter()
    if bypass_fitting:
        adapter._fit_prompt_text = lambda source, render: render(str(source or ""))
    result = await send(adapter, body)
    assert result.success is True
    adapter._bot.send_message.assert_called_once()
    return adapter._bot.send_message.call_args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("send,renders_html", _SENDERS)
async def test_oversize_prompt_is_shrunk_until_the_sent_payload_fits(send, renders_html):
    unfitted = await _send_and_capture(send, _OVERSIZE_BODY, bypass_fitting=True)
    assert utf16_len(unfitted["text"]) > TelegramAdapter.MAX_MESSAGE_LENGTH, (
        "fixture no longer overflows — it cannot prove the fitter does anything")

    kwargs = await _send_and_capture(send, _OVERSIZE_BODY)
    text = kwargs["text"]

    assert utf16_len(text) <= TelegramAdapter.MAX_MESSAGE_LENGTH
    # The cut is announced, and it reports a real number of dropped source characters.
    dropped = re.search(r"truncated, (\d+) chars", text)
    assert dropped, f"no truncation marker in: {text[-200:]!r}"
    assert 0 < int(dropped.group(1)) < len(_OVERSIZE_BODY)
    # The keyboard is the whole point of a prompt: it must survive the shrink.
    assert kwargs["reply_markup"] is not None
    assert kwargs["reply_markup"].inline_keyboard
    if renders_html:
        # Cutting the source before escaping is what keeps this parseable: a cut made on the
        # rendering could split "&amp;" or orphan the card's <pre>.
        ET.fromstring(f"<root>{text}</root>")


@pytest.mark.asyncio
@pytest.mark.parametrize("send,renders_html", _SENDERS)
async def test_prompt_that_already_fits_is_sent_unchanged(send, renders_html):
    fitted = await _send_and_capture(send, _SHORT_BODY)
    unfitted = await _send_and_capture(send, _SHORT_BODY, bypass_fitting=True)

    assert fitted["text"] == unfitted["text"]
    assert "truncated" not in fitted["text"]


async def _approval_with_reason(reason, *, reason_budget=None):
    """Send an exec approval whose COMMAND is short and whose reason is ``reason``."""
    adapter = _make_adapter()
    if reason_budget is not None:
        adapter._EA_REASON_BUDGET = reason_budget
    result = await adapter.send_exec_approval(
        chat_id="12345", command=_SHORT_BODY, session_key="agent:main:telegram:dm:12345:1",
        description=reason)
    assert result.success is True
    adapter._bot.send_message.assert_called_once()
    return adapter._bot.send_message.call_args[1]


@pytest.mark.asyncio
async def test_oversize_reason_is_bounded_so_the_card_still_fits():
    # Unbounded reason is the pre-fix state: the fitter can only shrink the command, so a card
    # whose reason alone overflows stays oversize no matter how far the command is cut.
    unbounded = await _approval_with_reason(_OVERSIZE_REASON, reason_budget=0)
    assert utf16_len(unbounded["text"]) > TelegramAdapter.MAX_MESSAGE_LENGTH, (
        "fixture no longer overflows — it cannot prove the reason bound does anything")

    kwargs = await _approval_with_reason(_OVERSIZE_REASON)
    assert utf16_len(kwargs["text"]) <= TelegramAdapter.MAX_MESSAGE_LENGTH
    # The reason is cut, not the command: the user still sees in full what they are approving.
    assert _SHORT_BODY.split()[0] in kwargs["text"]
    assert "truncated" not in kwargs["text"]
    # The buttons are the whole point of a prompt, and the markup must still parse.
    assert kwargs["reply_markup"].inline_keyboard
    ET.fromstring(f"<root>{kwargs['text']}</root>")


@pytest.mark.asyncio
async def test_reason_within_budget_is_not_truncated():
    short_reason = "writes outside the sandbox"
    kwargs = await _approval_with_reason(short_reason)
    assert short_reason in kwargs["text"]
    assert "..." not in kwargs["text"]
