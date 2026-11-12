# Tin's card kit

Every LinkedIn post a Tin project sends from its company page goes out with an image or a video.
This kit makes the image: one card per post, built from the company's own CSS, fonts and
artwork, rendered and checked here, then uploaded to the project's draft.

## Before you start

Read these from the project (Tin gives you the list with the draft):

- the draft: each post's card line, card kind and brief, under its draft notes;
- brand/BRAND.md and DESIGN.md: colours, type and what the company never shows;
- the cards in `social/linkedin/cards/`, which earlier posts used. Start from them, so the
  page keeps one look.

Work in the company's own repository, where its CSS, fonts and artwork already are. Use them
as they are. Fonts stay on your machine; the upload holds only the images and their HTML.

## A card

- One HTML file per post, named after the post: `p1.html`.
- One `<main data-card>`, 1080 × 1350.
- The headline is the card line, word for word, in one element marked `data-card-line`. It is
  the only text the card carries.
- A logo goes in an element marked `data-card-logo`.
- A product screen or a terminal goes in an element marked `data-card-screen`. Its words are
  the product's. Use sample data, never a real customer's, and output a real command gives.

The kinds the draft asks for:

- **type:** the headline alone over the company's own artwork;
- **screen:** the product's own screen, with sample data;
- **terminal:** a coding agent or shell session, for a product that works through one;
- **artwork:** a drawing or photo the company already publishes.

Make the screen large enough to read in the feed, where a card shows at about half its width:
no text smaller than 22 px on the card, 2% of its width. That leaves room for about 480 px of a
product's screen, so a screen on a card is a small, simplified part of the product.

## Screens

A screen on a card is a picture of the product as the product draws it. Redrawing one by hand
fills every gap with a colour or a shape the company never made.

- Run the product, or load its own stylesheet and components, with sample data: invented people
  and companies, never a real customer's. Where the product's code can build that data, use it.
- Shoot the page, or the one part the post is about, at twice its size:

  ```
  tin-cards shoot http://localhost:3000/analytics screens/analytics.png --width 760 --height 1500
  tin-cards shoot https://example.com/ screens/people.png --selector ".people-list"
  ```

  It refuses a shot whose text fell back to a system font. `--scheme dark` shoots the dark theme.
- Simplify it for the feed, the way a launch film stages a screen. Keep real words only on what
  the post says, three to seven strings, plus the product's own chrome: its page title, its tabs,
  its navigation. Everything else keeps its place as a quiet bar, a rounded line in the product's
  own border colour where the words were. Hide what the post doesn't touch: other sections,
  columns it doesn't name, captions under numbers, hint lines, metadata lines, search and
  footers. Never shrink the product's type to fit more in; show less of the product instead.
- Do the simplifying in the page, with the product's own markup and styles, never in a redrawn
  copy: `--script` runs a script in the page before the shot, `--style` adds a stylesheet, and
  `--wait` waits for a part the page draws after loading. Hide with `display: none`, and put a
  bar where words were with the product's border colour. Drop any bar down a box's left edge,
  such as a quote's rule: the shot fails with one in it.
- Shoot it narrow, about 480 px wide, and say how much larger it shows on the card with
  `--card-scale`. The shot fails when any word in it would be under 22 px on the card:

  ```
  tin-cards shoot "http://localhost:3000/analytics?tab=week" screens/analytics.png \
    --width 486 --selector ".analytics-view" --script stage/analytics.js --card-scale 1.85
  ```

- A terminal is the company's own transcript or terminal component, from its site, with output a
  real command gave: keep the prompt and the lines the post names, and turn the rest into bars.
- Leave out other companies' logos; turn them off in the sample data when the product shows them.
  Leave out real people's names, including the signed-in member's.

## Colours

Every colour a card paints is one of the brand's. List the brand's colours from brand/BRAND.md
in `cards.json` as its palette; the check fails any other colour, at any opacity. Shots and
artwork keep their own colours.

## Composition

Start from the cards in `social/linkedin/cards/`, the layouts a member picked for the page. When
there are none yet, make a round first: the same posts in several compositions, each taken from
a layout the company already publishes (a section of its site, its hero, its link preview, its
dark theme), and let a member pick before making the batch.

A screen is a window with a hard edge: the company's hairline and radius, and a shadow only where
its own site uses one. Place it one of two ways, measured from its width, never by eye:

- closed: a gutter in on every side of the space below the headline, the window's hairline and
  radius all round;
- centred: equal space on its left and right, ending a gutter above the foot.

Decide the window's size from what the post needs to show, then fit the screen to it: give the
screen's own elements the room (a taller chart, roomier rows, the sentence the post is about set
larger) or show it larger, keeping its padding even on every side. Never fill a window with what
the post doesn't need: no extra sections, rows, cards or quiet lines added to reach an edge.
Quiet bars stand only where the product shows words nobody needs to read, one or two lines at a
time. A screen runs off an edge only when its own content is larger than the card, such as a
table too wide to show whole, and then its window runs off with it.

A window is exactly as large as the screen it holds. A window drawn larger than its screen leaves
an empty strip that reads as a box left open, and a screen nested in a second frame reads as
boxes inside boxes.

One smaller screen may sit over a corner of the main one, as a satellite: a terminal over the app,
say. It is placed against the main screen, and nothing the post says may be under it.

Nothing the post says may be cut by an edge.

## Never

- No eyebrows: no small uppercase, letter-spaced or mono label above a title, a card's words or
  a number.
- No words beyond the headline outside a screen.
- No customer names, prices or numbers the post doesn't give, unless they are sample data
  inside a screen.
- No art the company doesn't publish, and no third-party logos.
- No colour outside the brand's palette.
- No bar down a box's left edge, a quote's rule included, on the card or in a screen on it.

## Check

List the brand's palette and the cards in `cards.json` beside them:

```json
{
  "palette": {"paper": "#f7f5ef", "ink": "#26231c"},
  "cards": [{"post": "p1", "line": "The card line", "alt_text": "What the card shows, and its headline."}]
}
```

Then run:

```
tin-cards check <dir>
```

It renders each card to `<post>.png` and fails a card when:

- a font didn't load, or the text falls back to a system font;
- the headline runs past the card's edge or is clipped;
- there is text outside the headline, a logo or a screen;
- there is an eyebrow;
- a colour isn't in the palette;
- a box has a bar down its left edge;
- a screen floats with more space on one side than the other, neither centred nor running off;
- a window is larger than the shot it holds, leaving empty window inside the card;
- the headline is over nine words;
- the alt text is missing;
- the image is over 5 MB, even as a JPEG.

It notes, without failing, when the headline differs from the draft's card line, or a screen's
words run past the card's edge. Look at `sheet.png`, which lays the batch out together, before
you upload.
