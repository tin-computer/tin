# film.json

A film is one JSON object. `tin-film new` writes the worked example; keep its `stage`, `screens`
and `ready`, and write the rest for this film.

```json
{
  "title": "Set up Tin from your coding agent",
  "story": {
    "before": "Growing a product means a blank calendar and no one to say what to do first.",
    "after": "You tell your coding agent to set up Tin, and a marketing system starts running."
  },
  "stage": "stage/index.html",
  "screens": {"16:9": 1080},
  "now": "2026-11-02T10:00:00Z",
  "format": "16:9",
  "ready": ".stage",
  "beats": [
    {"camera": "body", "cut": true, "pad": 0},
    {"caption": "Tell your coding agent to set up Tin."},
    {"begin": true},
    {"do": ["type", "#cmd", "claude \"Set up Tin\"", {"cps": 20}]},
    {"do": ["print", "#term", ["<span class='muted'>&gt;</span> Set up Tin"], {"gap": 0.16}]},
    {"caption": "It reads your code, so there's no form to fill."},
    {"do": ["set", "#v-product", "aupilot.com"]},
    {"hold": 0.8},
    {"mark": "poster"},
    {"click": "#nav-decisions", "to": [0.4, 0.5]},
    {"mark": "payoff"},
    {"hold": 1.0},
    {"end": true}
  ],
  "notes": []
}
```

## Fields

- `title`: the note's title and the film's name. A sentence about the change, at most 80
  characters.
- `story`: `before` and `after`, one sentence each. The note opens with them.
- `stage`: the stage page, beside `film.json`.
- `screens`: the screen per format. `{"16:9": 1080}` for a 16:9 film; leave 4:5 and 1:1 at their
  default (720).
- `now`: the time the stage's clock shows. A calendar in the stage should agree with it.
- `format`: the run's `format`: `16:9`, `4:5` or `1:1`.
- `theme`: `dark` (the default) puts the stage on black with white captions; `light` on white with
  dark ones. Pick the one closest to the product's own ground.
- `ready`: a selector the stage shows once it has loaded; `.stage`.
- `beats`: the film, in order.
- `notes`: sentences for the note's checks section, one per finding you left and why.

## Beats

A beat's first key is its action; the other keys are its options. A selector is CSS on the stage.

| Beat | What it does |
| --- | --- |
| `{"camera": "body", "cut": true, "pad": 0}` | The first beat: frame the whole stage. A staged film needs no other camera. |
| `{"caption": "…"}` | Change the caption under the picture. One at a time; it stays until the next. |
| `{"begin": true}` | Fade in. Once, after the first camera and caption. |
| `{"do": ["print", "#term", ["…"]]}` | Call the stage's runtime (`stage-kit.md`); the film holds while it plays. |
| `{"hold": 2}` | Let seconds pass, filmed. |
| `{"move": "selector"}` | The hand travels to a target. `to: [x, y]` is where in it, as fractions (0.5, 0.5); past 1 is beside it. |
| `{"click": "selector"}` | Move there and press. The stage answers through `data-go` and `data-then`. |
| `{"mark": "poster"}` | Keep this frame as a still. `poster` is required; mark the payoff too. |
| `{"end": true}` | Fade out. The last beat. |

## Pacing

Every take checks it and prints a warning when it is off:

- The first caption shows within a second and a half.
- A caption shows long enough to read, one second for every 17 characters and at least a second
  and a half, and at most six and a half seconds.
- The picture never stands frozen for four seconds.
- 15 to 40 seconds in all; the film must run 8 to 90.
