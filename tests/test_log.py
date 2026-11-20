from __future__ import annotations

import io
import json
import logging

from wispr_archiver.log import event, setup_logging


def lines(buf: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(x) for x in buf.getvalue().splitlines() if x.strip()]


def test_event_is_json_with_whitelisted_fields_only() -> None:
    buf = io.StringIO()
    setup_logging("INFO", stream=buf)
    event("meeting_archived", meeting_id="m1", transcript_chars=10, title="Secret title", text="x")
    (rec,) = lines(buf)
    assert rec["event"] == "meeting_archived"
    assert rec["meeting_id"] == "m1"
    assert rec["transcript_chars"] == 10
    assert "title" not in rec
    assert "text" not in rec
    assert rec["dropped_fields"] == ["text", "title"]
    assert "Secret title" not in buf.getvalue()


def test_third_party_logs_are_scrubbed_and_limited() -> None:
    buf = io.StringIO()
    setup_logging("INFO", stream=buf)
    lib = logging.getLogger("mcp.client.auth")
    lib.info("info is below WARNING and must not appear")
    lib.warning("Token refresh failed: token=eyJhbGciOiJIUzI1NiJ9.payloadpayloadpayload.sig")
    out = buf.getvalue()
    assert "below WARNING" not in out
    assert "eyJhbGci" not in out
    (rec,) = lines(buf)
    assert rec["event"] == "lib_log"
    assert rec["logger"] == "mcp.client.auth"
    assert "[redacted]" in str(rec["msg"])
