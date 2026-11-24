"""Minimal X API v2 client: look up accounts and read replies to a post.

The bearer token is sent only in a request header and is never logged.
Error messages carry the endpoint and status code, nothing else.
"""

import time
from dataclasses import dataclass
from datetime import datetime

import httpx

from .utils import tweet_id_to_timestamp


class XApiError(Exception):
    pass


class XNotFound(XApiError):
    pass


class XRateLimited(XApiError):
    def __init__(self, retry_at: float):
        super().__init__("X API rate limit reached")
        self.retry_at = retry_at


@dataclass
class Reply:
    tweet_id: str
    author_id: str
    author_username: str | None
    created_at: float

    @property
    def url(self) -> str:
        who = self.author_username or "i/web"
        return f"https://x.com/{who}/status/{self.tweet_id}"


def _parse_time(value: str | None, tweet_id: str) -> float:
    if value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return tweet_id_to_timestamp(tweet_id)


@dataclass
class PostAuthor:
    name: str | None
    username: str | None


async def oembed_author(tweet_id: str, handle: str | None = None) -> PostAuthor | None:
    """The post author's display name from X's public embed service. No API key needed."""
    post_url = f"https://twitter.com/{handle or 'i'}/status/{tweet_id}"
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            r = await client.get("https://publish.twitter.com/oembed",
                                 params={"url": post_url, "omit_script": "1"})
        if r.status_code != 200:
            return None
        data = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    author_url = data.get("author_url") or ""
    username = author_url.rstrip("/").rsplit("/", 1)[-1] or None
    return PostAuthor(name=data.get("author_name") or None, username=username)


class XClient:
    def __init__(self, bearer_token: str, base_url: str = "https://api.x.com/2"):
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {bearer_token}"},
            timeout=20.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict) -> dict:
        try:
            r = await self._client.get(path, params=params)
        except httpx.HTTPError as e:
            raise XApiError(f"network error calling X ({type(e).__name__})") from None
        if r.status_code == 429:
            reset = r.headers.get("x-rate-limit-reset")
            retry_at = float(reset) if reset and reset.isdigit() else time.time() + 15 * 60
            raise XRateLimited(retry_at)
        if r.status_code == 404:
            raise XNotFound(path.split("/")[1] if "/" in path else path)
        if r.status_code in (401, 403):
            raise XApiError(f"X refused the request (status {r.status_code}). Check X_BEARER_TOKEN and your plan.")
        if r.status_code >= 400:
            raise XApiError(f"X API returned status {r.status_code}")
        try:
            return r.json()
        except ValueError:
            raise XApiError("X API returned something that isn't JSON") from None

    async def get_user(self, username: str) -> tuple[str, str] | None:
        """Look up an account. Returns (x_user_id, exact_username) or None if it doesn't exist."""
        try:
            data = await self._get(f"/users/by/username/{username}", {})
        except XNotFound:
            return None
        user = data.get("data")
        if not user:
            return None
        return str(user["id"]), user["username"]

    async def get_post_info(self, tweet_id: str) -> tuple[str | None, PostAuthor | None]:
        """(conversation_id, author) for a post, in one request."""
        try:
            data = await self._get(f"/tweets/{tweet_id}", {
                "tweet.fields": "conversation_id,author_id",
                "expansions": "author_id",
                "user.fields": "name,username",
            })
        except XNotFound:
            return None, None
        tweet = data.get("data") or {}
        users = (data.get("includes") or {}).get("users") or []
        author = None
        for u in users:
            if u.get("id") == tweet.get("author_id"):
                author = PostAuthor(name=u.get("name"), username=u.get("username"))
        return tweet.get("conversation_id"), author

    async def get_conversation_id(self, tweet_id: str) -> str | None:
        try:
            data = await self._get(f"/tweets/{tweet_id}", {"tweet.fields": "conversation_id"})
        except XNotFound:
            return None
        tweet = data.get("data") or {}
        return tweet.get("conversation_id")

    async def search_replies(self, conversation_id: str, since_id: str | None = None,
                             max_pages: int = 10) -> tuple[list[Reply], str | None]:
        """All replies in the post's thread newer than since_id.

        Returns (replies, newest_id). Uses recent search, which covers 7 days,
        plenty for a 12 hour raid.
        """
        params = {
            "query": f"conversation_id:{conversation_id} is:reply",
            "max_results": 100,
            "tweet.fields": "author_id,created_at,conversation_id",
            "expansions": "author_id",
            "user.fields": "username",
        }
        if since_id:
            params["since_id"] = since_id
        replies: list[Reply] = []
        newest_id = None
        for _ in range(max_pages):
            data = await self._get("/tweets/search/recent", params)
            meta = data.get("meta") or {}
            if newest_id is None and meta.get("newest_id"):
                newest_id = meta["newest_id"]
            users = {u["id"]: u.get("username") for u in (data.get("includes") or {}).get("users", [])}
            for t in data.get("data") or []:
                replies.append(
                    Reply(
                        tweet_id=str(t["id"]),
                        author_id=str(t["author_id"]),
                        author_username=users.get(t["author_id"]),
                        created_at=_parse_time(t.get("created_at"), str(t["id"])),
                    )
                )
            token = meta.get("next_token")
            if not token:
                break
            params["next_token"] = token
        return replies, newest_id
