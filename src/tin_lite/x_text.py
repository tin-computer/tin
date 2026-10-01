"""Conservative X post length under twitter-text v3's weighted rules.

The official parser recognizes a wider set of URLs and emoji sequences. Unknown
cases retain their uncompressed code-point weights and possible URLs get at
least the 23-character t.co weight. This can reject a valid borderline post but
must not admit an overweight one. Keep the package copy byte-for-byte identical.

Reference: https://github.com/twitter/twitter-text/blob/master/config/v3.json
"""

import re
import unicodedata

MAX_WEIGHTED_LENGTH = 280
URL_WEIGHT = 23
_SCHEMED = re.compile(r"https?://[^\s<>\"“”]+", re.IGNORECASE)
_BARE = re.compile(r"(?<![\w@/])(?:[\w-]+\.)+[\w-]{2,}(?:/[^\s<>\"“”]*)?")
_KNOWN = re.compile(
    r"(?:https?://)?"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"(?:computer|com|org|net|edu|gov|mil|io|ai|dev|app|co|uk|us|ca|de|fr|jp|au)"
    r"(?:/[A-Za-z0-9/_~.\-]*)?",
    re.IGNORECASE,
)
_TRAILING = ".,!?;:'\"”’)}]"


def _point_weight(character):
    point = ord(character)
    if point <= 4351 or 8192 <= point <= 8205 or 8208 <= point <= 8223 or 8242 <= point <= 8247:
        return 1
    return 2


def _raw_weight(value):
    return sum(map(_point_weight, value))


def _possible_url_weight(value):
    pieces = re.split(r"([,;!?()\[\]{}])", value)
    if len(pieces) > 1:
        return sum(
            _possible_url_weight(piece)
            if _SCHEMED.fullmatch(piece) or _BARE.fullmatch(piece)
            else _raw_weight(piece)
            for piece in pieces
        )
    if _KNOWN.fullmatch(value):
        return URL_WEIGHT
    # The unknown host may still be a URL. If only its host is recognized,
    # X can count that host as 23 plus the remaining path text.
    after_scheme = re.sub(r"^https?://", "", value, flags=re.IGNORECASE)
    _, slash, suffix = after_scheme.partition("/")
    lower_bound = URL_WEIGHT + (_raw_weight(slash + suffix) if slash else 0)
    possible = max(_raw_weight(value), lower_bound)
    for end, char in enumerate(value):
        if char in ".,;:!?/" and _KNOWN.fullmatch(value[:end]):
            possible = max(possible, URL_WEIGHT + _raw_weight(value[end:]))
    return possible


def weighted_length(value):
    """Return a conservative upper bound for a standalone X post's length."""
    if not isinstance(value, str):
        raise ValueError("Post text must be a string")
    text = unicodedata.normalize("NFC", value)
    spans = [(m.start(), m.end()) for m in _SCHEMED.finditer(text)]
    for match in _BARE.finditer(text):
        if not any(start <= match.start() < end for start, end in spans):
            spans.append((match.start(), match.end()))
    spans.sort()
    total = cursor = 0
    for start, end in spans:
        if start < cursor:
            continue
        while end > start and text[end - 1] in _TRAILING:
            end -= 1
        if end <= start:
            continue
        total += _raw_weight(text[cursor:start])
        total += _possible_url_weight(text[start:end])
        cursor = end
    return total + _raw_weight(text[cursor:])
