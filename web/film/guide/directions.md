# Directions

A storyboard is the second decision. The first is the direction: the one visual idea the film is
built on. The idea a model reaches for first is its habit (for a product, its window with the
feature in it; for an idea, a chart of it), and a film built on a habit looks like every other.
So stay abstract for longer: decide the kind of film, research how such a claim has been shown,
deal directions from outside your habits, sketch each, and choose among the sketches.

## The kind of film

- **Product highlight**: the post shows what the product does. Stage it in the product's own
  look, as the worked examples do. The directions are ways to tell it: which opening, which
  moment, which card carries the point.
- **Motion graphic**: the post argues an idea bigger than one screen (a market, a habit, a hidden
  number). The directions are ways to picture the idea; the product may not appear at all.
- **Both**: an idea that opens the film and lands on the product.

Write `Kind:` and one line why at the top of `WORK/storyboard.md`.

## Research

Before dealing, look at how this kind of claim has been shown, and write what you found under
`## Research`, a line each, with where you saw it:

- for a motion graphic: explainers, data-visualisation galleries, other products' launch films
  and essays on the subject;
- for a product highlight: the product's own pages, screens and wording, and how comparable
  products film a feature like it.

Write down moves, never content: what moved, what carried the number, what the poster showed,
what the first second did. Nothing of theirs goes in the film. Read public pages only, with
`tin-film inspect` or a plain fetch; never sign in, submit or buy.

## Deal, write, sketch, choose

1. `tin-film directions --kind idea|product|both` deals five cards at random: ways to picture an
   idea with a look each, or ways to tell a product's story in its own look. Write one direction
   per card under `## Directions`: its poster frame (what is on screen, and the number, word or
   shape that carries the claim), its one move, and its caption. You may swap one card for a
   way you found in research, with a reason.
2. Sketch each as a stage page: `WORK/sketches/<n>-<name>/index.html`, its caption in
   `caption.txt` and, in `about.txt`, one sentence for the founder on what this way of showing
   it is (they pick from the stills and these sentences; the folder's name is the direction's
   name, so name it as they would read it: `2-flow`, `4-one-person`). Only its poster frame,
   built with the kit, the product's tokens and its type (copy `WORK/look/fonts.css` and
   `WORK/look/fonts/` into the folder and link it). When the poster needs more than every part
   shown (a flow drawn, a value set), define `stage.poster`. Rough is fine; the idea must read
   in one look, and it must pass the checks: no eyebrows (a label above a title or a card is
   the first thing that makes a sketch look generated), no words on words, no text crossing a
   card's edge, nothing too small to read on a phone, no object drawn from boxes and lines.
   `tin-film sketch` fails a sketch that breaks one.
3. `tin-film sketch WORK/sketches` films each for a moment, checks it, and puts the stills side
   by side, in order, in `WORK/sketches/sketches.jpg`. Fix what it prints, then open the sheet
   once.
4. `tin-film review --directions WORK/sketches` writes a brief for a reviewer that didn't make
   them (`tin-film guide review`). It chooses the sketch whose frame says the claim with no
   caption, that the product can truthfully back and that is least like a stock picture of the
   subject, names the runner-up and says why, in a sentence the founder reads beside it, in
   `review.json`. Its choice is the recommendation; the founder picks in Decisions, and the
   picked page grows into the film's stage.

## Families

Ways to picture an idea. The examples are from different subjects on purpose; none of them is the
answer to your claim.

| Family | What carries it | For instance |
| --- | --- | --- |
| Physical metaphor | An object whose shape is the idea | An iceberg for what is hidden; an hourglass for a narrow neck; a dam for pressure held back |
| Scale move | The camera pulls back or pushes in; size is the reveal | One dot that becomes a crowd; a street that becomes the city |
| Flow | Quantities that move, merge, split or leak | Streams that split at a fork; where a budget goes; a pipe that leaks |
| Rings and distance | Things arranged by closeness to a centre | An orbit around you; ripples from one event; a target |
| One record's story | One person, order or page through time | One customer's month; one parcel's path |
| Typography | One number or sentence carries it; the rest is small | A numeral that dwarfs the old one; a sentence that rewrites itself |
| Field of units | One mark per thing, in a field that moves, sorts or lights | Dots that sort themselves; a crowd that parts |
| Network | Who connects to whom | One message and everyone it reached; teams linking up |
| Comparison | The same frame twice, or two worlds side by side | Before and after; what you see beside what is there |
| Map or space | Where things are | A map; a floor plan; a calendar read as a place |
| Chart in motion | A real chart whose change is the point | A line that bends where it matters; bars that re-sort |

Ways to tell a product's story: problem to product (open where the founder's problem lives, then
the product answering it), result first, one record through the feature, before and after,
quiet hero with a loud card, close to wide, a week in seconds.

Never a waffle chart (a grid of squares, one per unit): it reads as a stock infographic.

## Building a diagram

A diagram is data, then a layout, then marks, the way an Observable notebook builds one. The kit
carries D3 7.9.0, d3-sankey 0.12.3 and Observable Plot 0.6.17 for when a direction needs them;
none is required, and plain HTML and CSS serve many directions better:

```html
<script src="/__stage/kit/vendor/d3.min.js"></script>
<script src="/__stage/kit/vendor/d3-sankey.min.js"></script>
<script src="/__stage/kit/vendor/plot.umd.min.js"></script>
```

Put a diagram inside an element with `data-diagram` and give each mark an id: the checks then
measure each mark, follow it through a change by its id, and hold the diagram to the alignment
rules in `stage-kit.md`; the check for a floating hero leaves it alone. Draw lines and flows with
`draw`, count numbers up with `count`; D3's transitions play on the film's clock too. When data
arrives, lay the diagram out again and ease the old marks to their new places, so it stays
centred. Keep labels as HTML text beside the marks. Numbers are plainly made up and add up.
