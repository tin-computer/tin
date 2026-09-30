"""Conservative X post length under twitter-text v3's weighted rules.

The official parser recognizes a wider set of URLs and emoji sequences. Unknown
cases retain their uncompressed code-point weights, which can reject a valid
borderline post but must not admit an overweight one. Keep the package copy
byte-for-byte identical; the isolated package cannot import trusted code.

Reference: https://github.com/twitter/twitter-text/blob/master/config/v3.json
"""

import re
import unicodedata

MAX_WEIGHTED_LENGTH = 280
URL_WEIGHT = 23
_URL = re.compile(
    r"(?<![\w@])https?://"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"(?:computer|com|org|net|edu|gov|mil|io|ai|dev|app|co|uk|us|ca|de|fr|jp|au)"
    r"(?![A-Za-z0-9-])(?:/[A-Za-z0-9/_~.\-]*)?",
    re.IGNORECASE,
)
_TRAILING = ".,!?;:'\"”’)}]"


def _point_weight(character):
    point = ord(character)
    if point <= 4351 or 8192 <= point <= 8205 or 8208 <= point <= 8223 or 8242 <= point <= 8247:
        return 1
    return 2


def weighted_length(value):
    """Return a safe upper bound for X's weighted length of a standalone post.

    Known ordinary HTTP(S) URLs use t.co's 23-character weight. Everything else
    uses twitter-text v3 code-point ranges. Complex emoji deliberately remain
    uncompressed unless a trusted parser is later introduced.
    """
    if not isinstance(value, str):
        raise ValueError("Post text must be a string")
    text = unicodedata.normalize("NFC", value)
    total = 0
    cursor = 0
    for match in _URL.finditer(text):
        end = match.end()
        while end > match.start() and text[end - 1] in _TRAILING:
            end -= 1
        if end <= match.start():
            continue
        total += sum(map(_point_weight, text[cursor : match.start()]))
        total += URL_WEIGHT
        cursor = end
    total += sum(map(_point_weight, text[cursor:]))
    return total
