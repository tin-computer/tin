# GitHub search response validation

Run this fixed Python block before evaluating GitHub results. It deliberately returns a
limitation instead of raising for a malformed or incomplete provider response, so the procedure
can record the source limitation and continue with other bounded searches. The returned rows are
only candidates; they are not strong matches until the skill's relevance rules are met.

```python
from datetime import datetime


def github_search_candidates(payload):
    """Return (safe candidate rows, limitation) for one bounded GitHub search response."""
    if not isinstance(payload, dict):
        return (), "GitHub search returned malformed JSON."
    items = payload.get("items")
    if not isinstance(items, list):
        return (), "GitHub search response omitted its items list."
    if len(items) > 10:
        return (), "GitHub search response exceeded the requested result bound."

    candidates = []
    for item in items:
        if not isinstance(item, dict):
            return (), "GitHub search response contained a malformed result."
        title = item.get("title")
        link = item.get("html_url")
        updated_at = item.get("updated_at")
        if not all(isinstance(value, str) and value.strip() for value in (title, link, updated_at)):
            return (), "GitHub search response omitted required result fields."
        if not link.startswith("https://github.com/"):
            return (), "GitHub search response contained a non-canonical public link."
        try:
            datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        except ValueError:
            return (), "GitHub search response contained an invalid update timestamp."
        if "pull_request" in item:
            continue
        candidates.append(
            {
                "title": title.strip(),
                "link": link,
                "updated_at": updated_at,
                "body": item.get("body") if isinstance(item.get("body"), str) else "",
                "thread_type": "issue",
            }
        )
    return tuple(candidates), None
```
