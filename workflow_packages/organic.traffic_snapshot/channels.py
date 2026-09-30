"""Traffic channels: one fixed map for the traffic snapshot and its weekly readout.

The order and the domain patterns are product.analytics_brief's (its CALCULATIONS.md), so a
channel means the same thing in both reports; tests/test_traffic_snapshot.py checks that they
stay identical. UTM medium decides paid, email and social first; then the lowercased
referring domain without a leading "www." is tested in order. The snapshot adds one thing the
brief does not do: when the referrer is empty, a utm_source names the source.
"""

import re

CHANNELS = ("Paid", "Email", "Social", "Direct", "Internal", "AI assistants", "Search", "Referral")
# Beyond the fixed channels: no referrer property at all, or a session whose first pageview
# ties between two channels.
EXTRA = ("Unknown", "Ambiguous")
PAID_MEDIUM = r"^(?:cpc|ppc|cpm|cpv|cpa|paid.*|display|retargeting|ads?)$"
EMAIL_MEDIUM = r"^(?:e-?mail|newsletter)$"
SOCIAL_MEDIUM = r"^(?:social|social[-_ ]?media|organic[-_ ]?social|sm)$"
AI_DOMAINS = (
    r"(?:^|\.)(?:chatgpt\.com|chat\.openai\.com|perplexity\.ai|claude\.ai"
    r"|gemini\.google\.com|copilot\.microsoft\.com)$"
)
SEARCH_DOMAINS = (
    r"^(?:(?:search|m)\.)?(?:google|bing|duckduckgo|yahoo|yandex|baidu|ecosia|naver|qwant"
    r"|startpage|seznam)\.[a-z]{2,3}(?:\.[a-z]{2})?$|^search\.brave\.com$"
)
SOCIAL_DOMAINS = (
    r"(?:^|\.)(?:facebook\.com|fb\.com|instagram\.com|t\.co|twitter\.com|x\.com|linkedin\.com"
    r"|lnkd\.in|reddit\.com|youtube\.com|youtu\.be|tiktok\.com|pinterest\.com|threads\.net"
    r"|bsky\.app|mastodon\.social|news\.ycombinator\.com)$"
)
# Webmail opened from a link is email, not search (mail.google.com would otherwise read as a
# Google domain in some maps). Tested before the domain patterns.
EMAIL_DOMAINS = r"^(?:mail|webmail|outlook)\.|^com\.google\.android\.gm|^app\.fastmail\.com$"
# A bare utm_source word, read only when the referrer is empty.
SOURCE_WORDS = {
    "google": "Search",
    "bing": "Search",
    "duckduckgo": "Search",
    "chatgpt": "AI assistants",
    "openai": "AI assistants",
    "perplexity": "AI assistants",
    "claude": "AI assistants",
    "gemini": "AI assistants",
    "copilot": "AI assistants",
    "newsletter": "Email",
    "email": "Email",
    "gmail": "Email",
}
# Assistant names for the report's per-assistant rows.
ASSISTANTS = {
    "chatgpt.com": "ChatGPT",
    "chat.openai.com": "ChatGPT",
    "perplexity.ai": "Perplexity",
    "claude.ai": "Claude",
    "gemini.google.com": "Gemini",
    "copilot.microsoft.com": "Copilot",
}


def domain(value):
    """The lowercased referring domain without a leading www."""
    return re.sub(r"^www\.", "", str(value or "").strip().lower())


def classify(medium, referrer, own_hosts, *, source=None):
    """The channel of one entry. `referrer` None means the property was absent (Unknown)."""
    med = str(medium or "").strip().lower()
    if re.search(PAID_MEDIUM, med):
        return "Paid"
    if re.search(EMAIL_MEDIUM, med):
        return "Email"
    if re.search(SOCIAL_MEDIUM, med):
        return "Social"
    if referrer is None:
        return "Unknown"
    dom = domain(referrer)
    if dom in ("", "$direct"):
        word = domain(source)
        return SOURCE_WORDS.get(word, "Direct") if word and "." not in word else "Direct"
    own = {domain(host) for host in own_hosts}
    if dom in own or any(dom.endswith("." + host) for host in own):
        return "Internal"
    if re.search(EMAIL_DOMAINS, dom):
        return "Email"
    if re.search(AI_DOMAINS, dom):
        return "AI assistants"
    if re.search(SEARCH_DOMAINS, dom):
        return "Search"
    if re.search(SOCIAL_DOMAINS, dom):
        return "Social"
    return "Referral"


def assistant(referrer):
    dom = domain(referrer)
    return next((name for host, name in ASSISTANTS.items() if dom == host), None)


def quote(value):
    """A HogQL string literal. Only code-owned patterns and validated hosts reach here."""
    text = str(value)
    if len(text) > 1000 or any(ord(char) < 32 for char in text):
        raise ValueError("unsafe literal")
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def hogql(medium, referrer, source, own_hosts):
    """The same map as classify(), as one HogQL expression over three string expressions.

    `referrer` must be NULL when the property is absent and '' or '$direct' when direct.
    """
    dom = f"replaceRegexpOne(lower({referrer}), '^www\\\\.', '')"
    word = f"replaceRegexpOne(lower(ifNull({source}, '')), '^www\\\\.', '')"
    own = sorted({domain(host) for host in own_hosts if host})
    internal = (
        " OR ".join([f"{dom} = {quote(h)} OR endsWith({dom}, {quote('.' + h)})" for h in own])
        or "0 = 1"
    )
    words = "multiIf(" + ", ".join(
        f"{word} = {quote(key)}, {quote(value)}" for key, value in SOURCE_WORDS.items()
    )
    med = f"lower(ifNull({medium}, ''))"
    return (
        f"multiIf(match({med}, {quote(PAID_MEDIUM)}), 'Paid', "
        f"match({med}, {quote(EMAIL_MEDIUM)}), 'Email', "
        f"match({med}, {quote(SOCIAL_MEDIUM)}), 'Social', "
        f"{referrer} IS NULL, 'Unknown', "
        f"{dom} IN ('', '$direct'), {words}, 'Direct'), "
        f"{internal}, 'Internal', "
        f"match({dom}, {quote(EMAIL_DOMAINS)}), 'Email', "
        f"match({dom}, {quote(AI_DOMAINS)}), 'AI assistants', "
        f"match({dom}, {quote(SEARCH_DOMAINS)}), 'Search', "
        f"match({dom}, {quote(SOCIAL_DOMAINS)}), 'Social', 'Referral')"
    )
