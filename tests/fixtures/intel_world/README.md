Small excerpts of public feeds (BBC, Google News, Google Trends, Techmeme, Wikimedia) trimmed to
a few items, and synthetic Hacker News and GDELT answers. `expected.json` was first what the
prototype world fetcher produced from these same bodies. It was re-recorded once from Tin's
methods when the world feed moved onto the shared feed helpers (`feeds.py`): only the Google
News items changed (`source` is the outlet, `author` empty, the cluster text's spacing).
`tests/test_intel_fetch_world.py` holds the methods to it.
