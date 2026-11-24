"""Small pure helpers: parsing, validation and formatting. No Telegram calls here."""

import html
import re
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- X links

_X_HOSTS = r"(?:x|twitter|fxtwitter|vxtwitter|fixupx|fixvx)\.com"
X_STATUS_RE = re.compile(
    r"https?://(?:www\.|mobile\.|m\.)?" + _X_HOSTS +
    r"/(?:i/web|(?P<user>[A-Za-z0-9_]{1,15}))/status(?:es)?/(?P<id>\d{5,25})",
    re.IGNORECASE,
)


def extract_x_links(text: str) -> list[tuple[str, str]]:
    """Return (tweet_id, clean_url) for every X post link in the text, in order."""
    found = []
    seen = set()
    for m in X_STATUS_RE.finditer(text or ""):
        tweet_id = m.group("id")
        if tweet_id in seen:
            continue
        seen.add(tweet_id)
        user = m.group("user")
        if user and user.lower() != "i":
            url = f"https://x.com/{user}/status/{tweet_id}"
        else:
            url = f"https://x.com/i/web/status/{tweet_id}"
        found.append((tweet_id, url))
    return found


def tweet_id_to_timestamp(tweet_id: str) -> float:
    """X post IDs carry their creation time (milliseconds since 2010-11-04)."""
    return ((int(tweet_id) >> 22) + 1288834974657) / 1000.0


# ---------------------------------------------------------------- X handles

_RESERVED_X_PATHS = {
    "home", "i", "intent", "share", "search", "explore", "settings", "messages",
    "notifications", "compose", "login", "signup", "tos", "privacy", "hashtag",
}
_X_PROFILE_RE = re.compile(
    r"^(?:https?://)?(?:www\.|mobile\.|m\.)?(?:x|twitter)\.com/@?([A-Za-z0-9_]{1,15})(?:[/?#].*)?$",
    re.IGNORECASE,
)


def normalize_x_handle(raw: str) -> str | None:
    """Turn '@name', 'name' or an x.com/twitter.com profile link into 'name'."""
    s = (raw or "").strip()
    m = _X_PROFILE_RE.match(s)
    handle = m.group(1) if m else s.lstrip("@").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
        return None
    if handle.lower() in _RESERVED_X_PATHS:
        return None
    return handle


# ---------------------------------------------------------------- Verona (xion) addresses

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_polymod(values: list[int]) -> int:
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            if (top >> i) & 1:
                chk ^= gen[i]
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def bech32_decode(addr: str) -> tuple[str, list[int]] | None:
    """Decode a bech32 string. Returns (prefix, 5-bit data) or None if invalid."""
    if any(ord(c) < 33 or ord(c) > 126 for c in addr):
        return None
    if addr.lower() != addr and addr.upper() != addr:
        return None
    addr = addr.lower()
    pos = addr.rfind("1")
    if pos < 1 or pos + 7 > len(addr) or len(addr) > 90:
        return None
    if any(c not in _BECH32_CHARSET for c in addr[pos + 1:]):
        return None
    hrp = addr[:pos]
    data = [_BECH32_CHARSET.find(c) for c in addr[pos + 1:]]
    if _bech32_polymod(_hrp_expand(hrp) + data) != 1:
        return None
    return hrp, data[:-6]


def bech32_encode(hrp: str, data5: list[int]) -> str:
    values = _hrp_expand(hrp) + data5
    polymod = _bech32_polymod(values + [0] * 6) ^ 1
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_BECH32_CHARSET[d] for d in data5 + checksum)


def convert_bits(data: list[int], from_bits: int, to_bits: int, pad: bool) -> list[int] | None:
    acc = 0
    bits = 0
    out = []
    maxv = (1 << to_bits) - 1
    for value in data:
        if value < 0 or value >> from_bits:
            return None
        acc = (acc << from_bits) | value
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            out.append((acc >> bits) & maxv)
    if pad:
        if bits:
            out.append((acc << (to_bits - bits)) & maxv)
    elif bits >= from_bits or ((acc << (to_bits - bits)) & maxv):
        return None
    return out


def validate_xion_address(raw: str) -> str | None:
    """Return the address in lowercase if it's a valid xion1... address, else None.

    The checksum is verified, so a single mistyped character is rejected.
    Both normal (20 byte) and smart-account (32 byte) addresses are accepted.
    """
    s = (raw or "").strip()
    decoded = bech32_decode(s)
    if not decoded:
        return None
    hrp, data = decoded
    if hrp != "xion":
        return None
    raw_bytes = convert_bits(data, 5, 8, False)
    if raw_bytes is None or len(raw_bytes) not in (20, 32):
        return None
    return s.lower()


# ---------------------------------------------------------------- time

_DURATION_RE = re.compile(
    r"(?:(\d+)\s*h(?:ours?|rs?)?)?\s*(?:(\d+)\s*m(?:in(?:ute)?s?)?)?", re.IGNORECASE
)


def parse_duration(raw: str) -> int | None:
    """'60m', '60', '2h', '1h30m' -> seconds. A bare number means minutes."""
    s = (raw or "").strip().lower()
    if not s:
        return None
    if s.isdigit():
        seconds = int(s) * 60
    else:
        m = _DURATION_RE.fullmatch(s)
        if not m or not (m.group(1) or m.group(2)):
            return None
        seconds = int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60
    if seconds <= 0 or seconds > 24 * 3600:
        return None
    return seconds


def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    hours, rest = divmod(seconds, 3600)
    return f"{hours}h {rest // 60:02d}m"


def utc_str(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def week_start(ts: float) -> datetime:
    """Monday 00:00 UTC of the week containing ts."""
    d = datetime.fromtimestamp(ts, timezone.utc)
    monday = d - timedelta(days=d.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------- formatting

def chunk(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def mention_html(user_id: int | None, username: str | None, name: str | None) -> str:
    """A mention that notifies the person. Works with or without a username."""
    if username:
        return "@" + html.escape(username)
    return f'<a href="tg://user?id={int(user_id)}">{html.escape(name or "member")}</a>'


def plain_name(username: str | None, name: str | None) -> str:
    """A name that does NOT ping anyone, for summaries and leaderboards."""
    return html.escape(username or name or "member")


def csv_safe(value) -> str:
    """Stop spreadsheet apps from treating a cell as a formula."""
    s = "" if value is None else str(value)
    if s and s[0] in "=+-@\t\r":
        return "'" + s
    return s


def render_template(template_html: str, url: str) -> str:
    return template_html.replace("{link}", html.escape(url))
