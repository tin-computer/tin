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
| Units | One mark per person in a field that moves | A field of 1,140 dots where 380 drift into the site |
| Network or map | Who connects to whom, or where | One post at the centre, the people it reached around it |
| Comparison | The same frame twice, before and after | The funnel your analytics draws beside the one Tin sees |

Never a waffle chart (a grid of squares, one per unit): it reads as a stock infographic.

## Building one

The stage kit's parts cover product scenes. For a diagram, write it the way an Observable
notebook would: data, then a layout, then SVG. The kit carries D3 7.9.0, d3-sankey 0.12.3 and
Observable Plot 0.6.17; load them on the stage before the kit's runtime:

```html
<script src="/__stage/kit/vendor/d3.min.js"></script>
<script src="/__stage/kit/vendor/d3-sankey.min.js"></script>
<script src="/__stage/kit/vendor/plot.umd.min.js"></script>
```

Put the diagram inside an element with `data-diagram`, and give each mark an id (`.attr("id",
…)`): the checks then measure each mark as they do the stage's other parts, follow it through a
change by its id, and hold the diagram to the alignment rules in `stage-kit.md`; the check for a
floating hero leaves it alone. When data arrives, lay the diagram out again and ease the old marks
to their new places with a D3 transition, so it stays centred. Draw its flows and lines with `draw` and count its numbers up with `count`; D3's own
transitions also play on the film's clock. Keep long labels as HTML text beside the SVG, so they
read at the kit's sizes. Use plainly made-up numbers, and keep them consistent: a Sankey's flows
add up at every node.
