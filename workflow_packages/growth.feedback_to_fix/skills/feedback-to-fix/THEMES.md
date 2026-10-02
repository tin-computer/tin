# What counts as a theme and what to do with it

Group the feedback you read into themes, record each theme's facts, then call
`choose_action(themes)` from the single Python block in this file, unchanged. It counts the
different people behind each theme from the quotes you recorded, so a count can never be larger
than the evidence. It decides; you implement. If it returns anything other than `patch`, change
no files.

A theme is one thing several people said about the product. Its `kind` is one of:

- `unclear_what_it_is`: people describe the product wrongly, or say they cannot tell what it does.
- `objection`: people give a reason not to try or use it that the page could address.
- `unanswered_question`: people ask something a visitor should find answered on the page.
- `product_change`: people want the product itself to behave differently. Copy cannot fix it,
  so it is reported, never patched.

Facts for each theme, each backed by something you read:

- `id`: a short unique label such as `t1`.
- `quotes`: every quote behind the theme. Each has `author` (a label you give each person in
  this run, `a1`, `a2`; never a username), `url` (the https link to the comment or thread where
  you read it) and `text` (their words, verbatim, at most 400 characters).
- `copy_files`: the existing files in the repository whose text carries this (the hero, the
  FAQ already on the page, the meta description). Empty when no existing copy does.
- `supported`: true only when the Feature map or the code shows the product does what the fix
  would say. When the fix would need a claim the product does not back, it is false.

The code merges themes of the same kind that name the same copy file: two people who raise the
same kind of problem on the same copy are one theme even when they suggest different fixes. Record
each point as its own theme and name the copy files exactly, so the merge has something to match.
A merged theme is supported only when every theme in it is. Themes of different kinds, themes
about different files and every `product_change` theme are never merged.

One person who says it in three comments is one person. A quote with no link, no text or a
second "author" that is really the same person is an error in your facts, not a theme.

```python
from urllib.parse import urlparse

KINDS = ("unclear_what_it_is", "objection", "unanswered_question", "product_change")
PRIORITY = {kind: rank for rank, kind in enumerate(KINDS)}
MIN_AUTHORS = 2
MAX_QUOTE = 400
MAX_FILES = 3


def _https(url):
    if not isinstance(url, str) or any(char.isspace() for char in url):
        return False
    parts = urlparse(url)
    return parts.scheme == "https" and any(char.isalnum() for char in parts.hostname or "")


def _people(theme):
    quotes = theme.get("quotes")
    if not isinstance(quotes, list) or not quotes:
        raise ValueError("every theme needs at least one quote")
    authors = set()
    for quote in quotes:
        if not isinstance(quote, dict):
            raise ValueError("a quote must be an object")
        author, url, text = quote.get("author"), quote.get("url"), quote.get("text")
        if not isinstance(author, str) or not author.strip():
            raise ValueError("a quote needs an author label")
        if not _https(url):
            raise ValueError("a quote needs the https link, with a host, where it was read")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_QUOTE:
            raise ValueError(f"a quote needs verbatim text of at most {MAX_QUOTE} characters")
        authors.add(author.strip())
    return len(authors)


def _blocker(theme):
    if theme["kind"] == "product_change":
        return "it asks for a product change, which different wording cannot fix"
    if not theme["supported"]:
        return "the product does not show it does what a fix would have to say"
    if not theme["copy_files"]:
        return "no existing page copy in the repository carries it"
    if len(theme["copy_files"]) > MAX_FILES:
        return f"fixing it would touch more than {MAX_FILES} files"
    return ""


def _merge(themes):
    # ponytail: one pass, so a theme that would bridge two earlier ones is not chained
    merged = []
    for theme in themes:
        target = None
        if theme["kind"] != "product_change" and theme["copy_files"]:
            for other in merged:
                same_kind = other["kind"] == theme["kind"]
                if same_kind and set(other["copy_files"]) & set(theme["copy_files"]):
                    target = other
                    break
        if target is None:
            merged.append(
                {**theme, "quotes": list(theme["quotes"]), "copy_files": list(theme["copy_files"])}
            )
            continue
        target["quotes"] += theme["quotes"]
        target["copy_files"] += [f for f in theme["copy_files"] if f not in target["copy_files"]]
        target["supported"] = target["supported"] and theme["supported"]
    return merged


def choose_action(themes):
    if not isinstance(themes, list):
        raise ValueError("themes must be a list")
    seen, valid = set(), []
    for theme in themes:
        if not isinstance(theme, dict):
            raise ValueError("a theme must be an object")
        theme_id = theme.get("id")
        if not isinstance(theme_id, str) or not theme_id or theme_id in seen:
            raise ValueError("theme ids must be unique non-empty strings")
        seen.add(theme_id)
        if theme.get("kind") not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        if type(theme.get("supported")) is not bool:
            raise ValueError("supported must be true or false")
        files = theme.get("copy_files")
        if not isinstance(files, list) or any(not isinstance(f, str) or not f for f in files):
            raise ValueError("copy_files must be a list of file paths")
        _people(theme)
        valid.append(theme)

    counted = [(theme, _people(theme)) for theme in _merge(valid)]
    real = [(theme, people) for theme, people in counted if people >= MIN_AUTHORS]
    if not real:
        return {
            "outcome": "insufficient_data",
            "theme": None,
            "reported": [],
            "reason": f"no theme has {MIN_AUTHORS} different people behind it yet; "
            "run again after more feedback",
        }
    real.sort(key=lambda pair: (-pair[1], PRIORITY[pair[0]["kind"]], pair[0]["id"]))
    for theme, _ in real:
        if not _blocker(theme):
            others = [other["id"] for other, _ in real if other is not theme]
            return {"outcome": "patch", "theme": theme["id"], "reported": others, "reason": ""}
    return {
        "outcome": "report_only",
        "theme": None,
        "reported": [theme["id"] for theme, _ in real],
        "reason": _blocker(real[0][0]),
    }
```
