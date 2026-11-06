# Directions

A storyboard is the second decision. The first is the direction: the one visual idea the film is
built on. The first idea that comes is almost always the obvious one, the product's window with
the feature in it, and a film built on it looks like every other product film. Write several
directions, far apart, before you choose.

## Write five

In `storyboard.md`, under `## Directions` (after the story, before the shots), write five
directions, each from a different family below. For each, one line:

- the poster frame: what is on screen, and the one number, word or shape that carries the claim;
- the one move the film is built on (a band rises, the camera pulls back, a stream fades);
- the look: its ground (the product's own, black, paper, white), its type (the product's sans, a
  serif for a story, giant numerals) and what the one accent colour marks.

Keep them far apart. If two would make the same storyboard in different colours, replace one.
Vary the idea first, then the camera, then the look.

## Choose one

Choose the direction whose poster says the post's claim with no caption and that the product can
truthfully back; say why in one line, and why the runner-up lost in another. A film may join
two: an idea that opens on the founder's problem and lands on the product answering it (an
iceberg, then People ranked). Then write the shots for that direction only.

## Families

Ways to show one idea, each with how a "your funnel is larger than you think" film would use it:

| Family | What carries it | Funnel example |
| --- | --- | --- |
| Product scene | The product's own window, the feature happening in it | People ranked by fit, the fits queued (the worked examples) |
| Physical metaphor | An object whose shape is the idea | An iceberg: what you measure is the tip above the line; an hourglass: few reach the neck |
| Scale move | The camera pulls back or pushes in; size is the reveal | A small funnel turns out to be the tip of a cone of people |
| Flow | Streams that merge, split or fade | A wide stream of engagement fades out before the line marked "your site" |
| Rings or orbit | Distance from a centre | Customers at the centre, then signups, visits, and a wide outer ring of people who engaged |
| One person's story | One record's timeline | Her funnel started a month before her first visit |
| Typographic | One huge number or sentence; the rest small | 1,140 above 380 · 41 · 7 |
| Units | One mark per person: dots, a waffle of squares | One square each; your analytics sees the dark rows |
| Network or map | Who connects to whom, or where | One post at the centre, the people it reached around it |
| Comparison | The same frame twice, before and after | The funnel your analytics draws beside the one Tin sees |

## Building one

The stage kit's parts cover product scenes. For another family, build its parts on the stage in
HTML and CSS: boxes, dots (`radial-gradient` fields), bands, rings (`border-radius` with a dotted
border) and shapes (`clip-path`). Keep words as real text. Use SVG only for lines: the checks
measure an SVG as one block, so motion inside one goes unchecked. Mark a diagram that sits whole
in the frame, with air around it, with `data-diagram`, so the check for a floating hero leaves it
alone; a product window still runs off an edge.
