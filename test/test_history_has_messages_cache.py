"""``ConversationLog.has_messages`` memoizes its answer per file stamp.

The Older Sessions listing asks ``has_messages`` of every untitled transcript on
each history refresh. These tests pin that an unchanged file is answered without
re-reading it, and that every kind of write still changes the answer.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from kiro_crew.history import ConversationLog


def _write_metadata_only(log: ConversationLog, key: str) -> None:
    path = log._dir / f"{key}.jsonl"
    path.write_text(json.dumps({"_type": "metadata", "title": key}) + "\n", encoding="utf-8")


def test_unchanged_file_is_answered_without_a_read(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    _write_metadata_only(log, "k1")
    assert log.has_messages("k1") is False
    with patch.object(log, "_read_has_messages", side_effect=AssertionError("re-read")):
        assert log.has_messages("k1") is False


def test_append_flips_a_cached_false(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    log.set_title("k1", "only metadata")
    assert log.has_messages("k1") is False
    log.append("k1", "user", "hello")
    assert log.has_messages("k1") is True


def test_metadata_write_keeps_the_answer_and_reads_again(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    log.append("k1", "user", "hello")
    assert log.has_messages("k1") is True
    log.set_title("k1", "renamed")
    with patch.object(log, "_read_has_messages", wraps=log._read_has_messages) as read:
        assert log.has_messages("k1") is True
    assert read.call_count == 1


def test_external_rewrite_with_new_size_is_seen(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    _write_metadata_only(log, "k1")
    assert log.has_messages("k1") is False
    path = log._dir / "k1.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"role": "user", "content": "hi"}) + "\n")
    assert log.has_messages("k1") is True


def test_absent_file_is_false_and_not_cached(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    assert log.has_messages("missing") is False
    assert log._has_messages_cache.get("missing") is None
    log.append("missing", "user", "now here")
    assert log.has_messages("missing") is True
