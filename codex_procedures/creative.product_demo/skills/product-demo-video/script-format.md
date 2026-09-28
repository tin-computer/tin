# script.json format

```json
{
  "hook": "your AI agent *can't* text you?",
  "settleMs": 700,
  "steps": [
    {"goto": "https://example.com/", "label": "home", "caption": "meet *Example*",
     "hold": 1500, "vo": "[excited] Your agent can't text you? Meet Example."},
    {"scroll": {"by": 560}, "label": "feature", "caption": "real *iMessage*",
     "hold": 1400, "duration": 1000, "vo": "Real iMessage, straight from your agent."},
    {"scroll": {"to": 1900}, "label": "stats", "caption": "*119* agents live",
     "vo": "A hundred and nineteen agents are already texting through it."},
    {"click": "header a[href='/docs']", "label": "docs", "caption": "connect your *framework*",
     "vo": "Connect your framework in minutes."},
    {"click": "header a[href='/']", "scrollTo": 1780, "label": "pricing",
     "caption": "plans from *$5*", "vo": "Plans start at five dollars a month."},
    {"click": "a[href='/sign-up']", "label": "cta", "caption": "start your *free trial*",
     "hold": 1800, "vo": "[warm] Free trial on every plan. Go send your first message."}
  ]
}
```

- `hook`: shown for the first 2.6 seconds over the top of the frame. At most seven words.
- Every step needs `label`, and should have `caption` (at most five words, one `*word*`) and
  `vo` (the spoken line; captions on screen are generated word by word from `vo`, so keep it
  short and plain, digits are fine to write as words).
- Step kinds, exactly one per step:
  - `goto` a URL; optional `scrollTo` (css px) and `settleMs`.
  - `scroll` with `{"by": px}` or `{"to": px}` or a selector string to scroll into view;
    optional `duration` (motion ms, default 900).
  - `click` a selector string (from `tin-studio inspect` actions) or `{"x": px, "y": px}`;
    optional `scrollTo` after the click, `zoom` (default 1.12), `scrollDuration`.
  - `type` with `{"selector": "...", "text": "...", "every": 3}` to type into a field on a
    public page (a search box, never a form you would submit); its voice line plays on the
    focused field before the text is typed.
  - `hold` in ms to linger on the current screen.
- `hold` (ms) is the minimum time on the step's screen; with a voice line it stretches until the
  line finishes. The viewport is 390x693 css px at 2.77x, a true 9:16 phone frame.
- Prefer `scroll` steps to `click` steps: three or four scrolls, one or two clicks.
