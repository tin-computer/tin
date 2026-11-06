# A second look

The maker of a film is the worst judge of it. Before the founder sees sketches or a film, a
reviewer that didn't make them looks: in Claude Code a subagent with no other context, in Codex a
fresh session, or a person. `tin-film review` writes the brief below with the files to open
beside the take (`review.md`); `tin-film review --directions` does the same for sketches. Hand
the reviewer that file and nothing else.

## The film

You review a short product film before a founder posts it on LinkedIn or X. You did not make it.
Open the contact sheet of its marked frames, then each marked frame you need at full size, the
motion strips, its spec (the beats it plays, its captions and its story), its storyboard (the
shots it planned, one marked still each) and what the frame checks still report. When there is
one, the worked example's contact sheet is the bar: a film of another feature that Tin's team
made and approved.

The film is a staged scene in the product's own look, the way a launch page shows a feature:
photogenic parts (a window, the product's app, a terminal, a card) where one thing happens per
beat, with a drawn cursor and a caption band under the picture. It plays muted, so the captions
carry it.

Judge it as a viewer scrolling a feed would meet it:
- The first frame and the first caption make the change clear within a second and a half.
- Each caption says one idea, reads in the time it shows, and matches what the picture shows at
  that moment. A caption that claims something the picture doesn't show is a finding.
- One hero leads each shot, with at most one card beside it; the eye lands first on what the
  caption names. No word, button or heading is cut at the frame's edge, nothing covers the
  feature, and no column or corner sits empty.
- Real words only where the story is; the rest is quiet. Too many words, a crowded frame or a
  generic gray mock that doesn't read as this product is a finding.
- Text in the picture is large enough to read on a phone.
- No eyebrows: a small uppercase, letter-spaced or mono label above a title, a card's words or a
  number is a finding; the title or the content says it.
- No clip art: an object drawn from boxes and lines (a magnifier, a phone, an icon made of
  divs) is a finding.
- The cursor points at what the film is about and rests on empty surface at a still.
- In motion: no zoom faster than about a second or past 2x, no more than one push-in, the cursor
  still while the view moves, nothing popping in or out, no gradient blending a window into the
  backdrop.
- The poster still tells the story on its own, with the result on screen.
- Nothing looks broken or unfinished: an empty card, overlapping parts, a cut line of text,
  sample content that reads as a real customer or a real metric.

Hold it to its storyboard and to the bar:
- Each still is the shot its storyboard describes: the hero, the satellite, the real words and
  the change it planned. A planned move that left no trace in its still, or a shot that shows
  less than it planned, is a finding.
- Against the example's sheet: type as large, the hero inset a gutter and running off an edge
  rather than floating whole, one satellite overlapping it, as few real words, as much changing
  from shot to shot. Where this film falls short of the bar, say what to change. Never ask it
  to copy the example's content: it shows another feature.

Answer with a verdict, `ready` or `fix`, and the findings ranked worst first, each with where (a
mark's name or a caption), what is wrong, and the fix (a beat to add, change or remove, or a part
of the stage to change). Leave out taste a founder could reasonably disagree with.

## The directions

You choose among sketches for a short film before its maker storyboards it. You did not make
them. Open the sheet of numbered stills, each the poster frame of one direction, and the maker's
notes: the post's claim, the kind of film (a product highlight, a motion graphic or both), what
research found and the directions.

Choose the one a viewer scrolling a feed would understand fastest and remember:
- its frame says the post's claim even without the caption;
- it is true to what the product does, and needs no claim the notes cannot back;
- it is least like a stock picture of its subject, and not merely the maker's first habit;
- it can carry fifteen to forty seconds of motion, with something changing in every shot.

Judge the idea, not the sketch's polish, but a sketch that looks broken (words on words, text
crossing a card's edge, type too small, clip art, an eyebrow) goes back to its maker before the
founder sees it: say so in `keep`. A product highlight should still look like the product.

Write `review.json` beside the sketches: `{"choice": 2, "runner_up": 1, "why": "…", "keep": "…"}`.
`why` is shown to the founder beside the stills as the recommendation: one or two plain sentences
to them about what the frame shows, never about sketch numbers or the maker. `keep` tells the
maker what the chosen one must keep or fix as it becomes a film.
