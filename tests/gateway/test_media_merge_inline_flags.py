"""Merging inbound media must keep ``media_text_inlined`` aligned with ``media_urls``.

``gateway/session_envelope.py::_media_clause`` refuses an event whose inline-flag list is
neither empty nor one entry per attachment. Every batcher that appends attachments to a
pending event therefore has to pad the flags it does not know (``None`` = legacy "no
per-attachment answer"), the way ``merge_pending_message_event`` already does — otherwise a
Telegram album of text documents reaches native admission with three URLs and one flag and is
refused with ``media_text_inlined_length``.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from gateway.session_envelope import _media_clause
from plugins.platforms.telegram.adapter import TelegramAdapter

# Smallest valid PNG: the photo path sniffs magic bytes before caching.
_PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _file_obj(data: bytes, file_path: str):
    f = AsyncMock()
    f.download_as_bytearray = AsyncMock(return_value=bytearray(data))
    f.file_path = file_path
    return f


def _document(file_name: str, data: bytes = b"content"):
    doc = MagicMock()
    doc.file_name = file_name
    doc.mime_type = "text/plain"
    doc.file_size = len(data)
    doc.get_file = AsyncMock(return_value=_file_obj(data, f"documents/{file_name}"))
    return doc


def _photo():
    photo = MagicMock()
    photo.get_file = AsyncMock(return_value=_file_obj(_PNG_1X1, "photos/pic.png"))
    photo.file_size = len(_PNG_1X1)
    return photo


def _update(media_group_id: str, message_id: int, *, document=None, photo=None):
    msg = MagicMock()
    msg.message_id = message_id
    msg.text = ""
    msg.caption = None
    msg.date = None
    msg.photo = [photo] if photo is not None else None
    msg.video = None
    msg.audio = None
    msg.voice = None
    msg.sticker = None
    msg.document = document
    msg.media_group_id = media_group_id
    msg.chat = MagicMock()
    msg.chat.id = 100
    msg.chat.type = "private"
    msg.chat.title = None
    msg.chat.full_name = "Test User"
    msg.from_user = MagicMock()
    msg.from_user.id = 1
    msg.from_user.full_name = "Test User"
    msg.message_thread_id = None
    msg.reply_text = AsyncMock()
    update = MagicMock()
    update.message = msg
    update.update_id = message_id
    return update


@pytest.fixture()
def adapter():
    a = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token"))
    a.handle_message = AsyncMock()
    a._is_callback_user_authorized = lambda user_id, **_kw: True
    yield a
    for task in list(a._media_group_tasks.values()) + list(a._pending_text_batch_tasks.values()):
        task.cancel()


@pytest.fixture(autouse=True)
def _redirect_cache(tmp_path, monkeypatch):
    """Keep every cache write inside tmp_path."""
    monkeypatch.setattr("gateway.platforms.base.DOCUMENT_CACHE_DIR", tmp_path / "doc_cache")
    monkeypatch.setattr("gateway.platforms.base.VIDEO_CACHE_DIR", tmp_path / "video_cache")
    monkeypatch.setattr("gateway.platforms.base.AUDIO_CACHE_DIR", tmp_path / "audio_cache")


@pytest.mark.asyncio
@pytest.mark.parametrize("attachments, expected_flags", [
    # Three inlined text documents: every slot has its own answer.
    ([("doc", "one.txt"), ("doc", "two.txt"), ("doc", "three.txt")], [True, True, True]),
    # A photo answers for no slot; the document's True must not shift onto it.
    ([("photo", ""), ("doc", "notes.txt")], [None, True]),
], ids=["text-documents", "photo-then-document"])
async def test_telegram_album_merge_keeps_one_inline_flag_per_attachment(
        adapter, attachments, expected_flags):
    """A Telegram album merges into one event; it must reach native admission with one inline
    flag per attachment, or ``_media_clause`` refuses the whole album on length."""
    for index, (kind, name) in enumerate(attachments, start=1):
        await adapter._handle_media_message(
            _update("album", index, document=_document(name) if kind == "doc" else None,
                    photo=_photo() if kind == "photo" else None),
            MagicMock())

    merged = adapter._media_group_events["album"]
    assert len(merged.media_urls) == len(attachments)
    assert merged.media_text_inlined == expected_flags
    assert _media_clause(merged) is None


@pytest.mark.asyncio
async def test_text_batch_merge_pads_inline_flags_for_appended_media(adapter):
    """The shared text batcher appends attachments too, so it owes the same alignment."""
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="text-batch", chat_type="dm",
                           user_id="42", user_name="Tester")
    first = MessageEvent(text="here", message_type=MessageType.TEXT, source=source,
                         media_urls=["/cache/pic.png"], media_types=["image/png"])
    second = MessageEvent(text="and this", message_type=MessageType.TEXT, source=source,
                          media_urls=["/cache/notes.txt"], media_types=["text/plain"],
                          media_text_inlined=[True])

    adapter._enqueue_text_event(first)
    adapter._enqueue_text_event(second)

    merged = adapter._pending_text_batches[adapter._text_batch_key(first)]
    assert merged.media_urls == ["/cache/pic.png", "/cache/notes.txt"]
    assert merged.media_text_inlined == [None, True]
    assert _media_clause(merged) is None
