"""Rank captured Reddit search results and draft replies without posting them."""

import json
import math
import re
from collections.abc import Iterable
from urllib.parse import quote


REDDIT_SEARCH_URL = "https://www.reddit.com/search.json?q={}"
STOP_WORDS = {
    "about",
    "after",
    "being",
    "could",
    "from",
    "have",
    "into",
    "that",
    "their",
    "there",
    "these",
    "they",
    "this",
    "with",
}


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]{3,}", value.lower())
        if token not in STOP_WORDS
    }


def _clean(value: object, limit: int) -> str:
    value = value if isinstance(value, str) else ""
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit].rstrip()


def _permalink(value: object) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if value.startswith("/r/"):
        return "https://www.reddit.com" + value
    if value.startswith("https://www.reddit.com/") or value.startswith("https://reddit.com/"):
        return value
    return ""


def _post_rows(payload: object, keyword: str) -> Iterable[dict[str, object]]:
    if not isinstance(payload, dict):
        raise ValueError("Each Reddit response must be a JSON object")
    data = payload.get("data")
    children = data.get("children") if isinstance(data, dict) else None
    if not isinstance(children, list):
        raise ValueError("Reddit response must contain data.children")
    keyword_tokens = _tokens(keyword)
    for child in children:
        post = child.get("data") if isinstance(child, dict) else None
        if not isinstance(post, dict):
            continue
        title = _clean(post.get("title"), 240)
        body = _clean(post.get("selftext"), 500)
        link = _permalink(post.get("permalink"))
        if not title or not link:
            continue
        post_tokens = _tokens(f"{title} {body}")
        matches = len(keyword_tokens & post_tokens)
        if matches == 0:
            continue
        score = post.get("score", 0) if isinstance(post.get("score"), int) else 0
        comments = (
            post.get("num_comments", 0) if isinstance(post.get("num_comments"), int) else 0
        )
        relevance = matches * 10 + math.log1p(max(score, 0)) + math.log1p(max(comments, 0))
        yield {
            "keyword": keyword,
            "title": title,
            "body": body,
            "link": link,
            "subreddit": _clean(post.get("subreddit"), 80),
            "score": score,
            "comments": comments,
            "relevance": relevance,
        }


def _draft_reply(post: dict[str, object]) -> str:
    title = str(post["title"])
    body = str(post["body"])
    context = _clean(body or title, 180)
    return (
        f"I have run into this too, especially when {context.lower().rstrip('.')}. "
        "One approach that helped was to define the smallest useful outcome first, then test "
        "one change at a time so it is clear what actually improved. What have you already "
        "tried, and where does the process still break down?"
    )


def _render(posts: list[dict[str, object]], keywords: list[str]) -> str:
    lines = [
        "# Reddit pain-point interceptor",
        "",
        "Keywords: " + ", ".join(keywords),
        "",
        "These are drafts for founder review. They do not claim to represent every customer "
        "and must not be posted without checking the thread context.",
        "",
    ]
    for index, post in enumerate(posts, 1):
        lines.extend(
            [
                f"## {index}. {post['title']}",
                "",
                f"- Thread: [{post['link']}]({post['link']})",
                f"- Search keyword: {post['keyword']}",
                f"- Community: r/{post['subreddit'] or 'unknown'}",
                f"- Discussion: {post['score']} score, {post['comments']} comments",
                "",
                "### Draft reply",
                "",
                _draft_reply(post),
                "",
            ]
        )
    return "\n".join(lines)


def run(ctx, inputs):
    keywords = inputs["keywords"]
    responses = inputs["reddit_search_json"]
    if len(keywords) != len(responses):
        raise ValueError("Provide one captured Reddit response for each keyword")
    if len(set(keyword.casefold() for keyword in keywords)) != len(keywords):
        raise ValueError("Keywords must be unique")

    posts = []
    for keyword, response in zip(keywords, responses):
        try:
            payload = json.loads(response)
        except json.JSONDecodeError as exc:
            raise ValueError("Each Reddit response must contain valid JSON") from exc
        posts.extend(_post_rows(payload, keyword))

    unique = {}
    for post in posts:
        unique.setdefault(post["link"], post)
    ranked = sorted(unique.values(), key=lambda post: post["relevance"], reverse=True)[:3]
    if len(ranked) < 3:
        requested = ", ".join(quote(keyword) for keyword in keywords)
        raise ValueError(
            "Need at least three relevant Reddit threads in the supplied responses. "
            f"Capture search results from {REDDIT_SEARCH_URL.format(requested)}"
        )
    return {
        "path": "reports/REDDIT_PAIN_POINT_INTERCEPTOR.md",
        "content": _render(ranked, keywords),
    }