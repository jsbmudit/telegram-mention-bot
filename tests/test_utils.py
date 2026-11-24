import logging

from raidbot.logsafe import RedactingFormatter
from raidbot.utils import (
    bech32_decode, bech32_encode, convert_bits, csv_safe, extract_x_links, format_duration,
    mention_html, normalize_x_handle, parse_duration, tweet_id_to_timestamp, validate_xion_address,
)


def make_xion(n_bytes: int) -> str:
    return bech32_encode("xion", convert_bits(list(range(1, n_bytes + 1)), 8, 5, True))


def test_bech32_known_vectors():
    # Valid examples from the bech32 specification (BIP-173).
    assert bech32_decode("A12UEL5L") is not None
    assert bech32_decode("abcdef1qpzry9x8gf2tvdw0s3jn54khce6mua7lmqqqxw") is not None
    assert bech32_decode("a12uel5m") is None  # checksum broken


def test_xion_address_valid_and_typos():
    for size in (20, 32):
        addr = make_xion(size)
        assert addr.startswith("xion1")
        assert validate_xion_address(addr) == addr
        assert validate_xion_address("  " + addr.upper() + " ") == addr
        # Change one character: checksum must fail.
        last = addr[-1]
        typo = addr[:-1] + ("q" if last != "q" else "p")
        assert validate_xion_address(typo) is None
    assert validate_xion_address(make_xion(32)[:-5]) is None
    assert validate_xion_address(bech32_encode("cosmos", convert_bits([1] * 20, 8, 5, True))) is None
    assert validate_xion_address(make_xion(16)) is None  # wrong length
    assert validate_xion_address("xion1notanaddress") is None
    assert validate_xion_address("") is None


def test_normalize_x_handle():
    for raw in ("@Alice_01", "Alice_01", " https://x.com/Alice_01 ", "x.com/Alice_01?s=21",
                "https://twitter.com/Alice_01/status/123", "https://mobile.twitter.com/@Alice_01"):
        assert normalize_x_handle(raw) == "Alice_01", raw
    for bad in ("", "@", "has space", "waytoolonghandle123", "https://x.com/home", "a-b"):
        assert normalize_x_handle(bad) is None, bad


def test_extract_x_links():
    text = ("look https://x.com/Burnt/status/1861234567890123456?s=20 and "
            "https://twitter.com/foo/status/1861234567890123457 plus "
            "https://x.com/i/web/status/1861234567890123458 and again "
            "https://fxtwitter.com/Burnt/status/1861234567890123456")
    assert extract_x_links(text) == [
        ("1861234567890123456", "https://x.com/Burnt/status/1861234567890123456"),
        ("1861234567890123457", "https://x.com/foo/status/1861234567890123457"),
        ("1861234567890123458", "https://x.com/i/web/status/1861234567890123458"),
    ]
    assert extract_x_links("https://x.com/Burnt") == []


def test_tweet_id_time():
    # 1212092628029698048 was posted 2019-12-31 19:26:16 UTC
    assert abs(tweet_id_to_timestamp("1212092628029698048") - 1577820376.0) < 2


def test_durations():
    assert parse_duration("60m") == 3600
    assert parse_duration("60") == 3600
    assert parse_duration("2h") == 7200
    assert parse_duration("1h30m") == 5400
    assert parse_duration("90min") == 5400
    for bad in ("", "abc", "0m", "25h", "m"):
        assert parse_duration(bad) is None, bad
    assert format_duration(45) == "45s"
    assert format_duration(134) == "2m 14s"
    assert format_duration(3900) == "1h 05m"


def test_mentions_and_csv():
    assert mention_html(1, "alice", "Alice") == "@alice"
    assert mention_html(42, None, "<Bob>") == '<a href="tg://user?id=42">&lt;Bob&gt;</a>'
    assert csv_safe("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert csv_safe("alice") == "alice"


def test_log_redaction():
    secret = "123456789:" + "fakeA" * 8  # not a real token
    fmt = RedactingFormatter([secret, "bearer-secret-xyz"], "%(message)s")
    rec = logging.LogRecord("t", logging.INFO, "", 0,
                            "url https://api.telegram.org/bot%s/getMe auth Bearer bearer-secret-xyz",
                            (secret,), None)
    out = fmt.format(rec)
    assert secret not in out and "bearer-secret-xyz" not in out
    # Even a token we weren't told about is caught by its shape.
    rec2 = logging.LogRecord("t", logging.INFO, "", 0, "987654321:" + "ZZZ-another" + "x" * 30, (), None)
    assert "ZZZ-another" not in fmt.format(rec2)


def test_reply_search_asks_for_no_author_profiles():
    """X may bill each returned profile as a user read, so the reply search must not ask for them."""
    import asyncio
    import httpx
    from raidbot.xapi import XClient

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={
            "data": [{"id": "9", "author_id": "111", "created_at": "2026-11-24T10:00:00.000Z"}],
            "meta": {"newest_id": "9", "result_count": 1},
        })

    async def run():
        client = XClient("unused")
        await client._client.aclose()
        client._client = httpx.AsyncClient(base_url="https://api.x.com/2", transport=httpx.MockTransport(handler))
        replies, newest = await client.search_replies("500", since_id="8")
        await client.close()
        return replies, newest

    replies, newest = asyncio.run(run())
    assert newest == "9" and replies[0].author_id == "111"
    assert "expansions" not in seen[0] and "user.fields" not in seen[0]
    assert seen[0]["since_id"] == "8"


def test_default_check_intervals(monkeypatch):
    from raidbot.config import load_config
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x" * 10)  # placeholder, not a token
    for name in ("TRACK_INTERVAL_MINUTES", "LOCK_CHECK_INTERVAL_MINUTES", "FEED_INTERVAL_MINUTES"):
        monkeypatch.delenv(name, raising=False)
    cfg = load_config()
    assert cfg.track_interval == 120 and cfg.lock_check_interval == 120 and cfg.feed_interval == 300
