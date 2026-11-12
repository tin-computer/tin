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
- Keep the product's type and spacing as they are: hide, replace words with bars, and size what
  the product itself sizes (a chart's height, a list's length), but never change a font size, a
  margin, a padding or a gap inside the screen. The shot fails when the script did.
- Shoot it narrow, about 480 px wide, and say how much larger it shows on the card with
  `--card-scale`, and how large the headline is with `--headline`. The shot fails when any word
  in it would be under 22 px on the card, or near the headline's size:

  ```
  tin-cards shoot "http://localhost:3000/analytics?tab=week" screens/analytics.png \
    --width 486 --selector ".analytics-view" --script stage/analytics.js --card-scale 1.85 --headline 70
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
its own site uses one. The screen decides its size and the layout follows, in this order:

1. **Stage the screen at the product's own proportions.** Its type, and the space between and
   inside its parts, stay as the product draws them, so what the product groups stays grouped:
   a label with what it labels, a question with its answer. Change only what the product itself
   sizes to the page it is on: a chart's height, a list's length, a column's width. `shoot`
   fails a script that changes type or spacing inside the screen.
2. **Enlarge the whole screen by one scale**, the same for every part, so its smallest words are
   at least 22 px on the card and its largest stay well under the headline. `shoot` fails both.
3. **Choose the layout from the screen's shape**, measured at that scale. A page, a chart or a
   list of several rows is a window under the headline. A single part (an answer, a few rows, a
   button) is enlarged into a corner beside a larger headline. A post with no screen sets the
   headline alone, or over the company's artwork. Choose per post: a batch mixes layouts because
   its screens differ.
4. **Place the window at its measured size**, never stretched or padded to a size the layout
   wanted. It closes a gutter in, with its hairline and radius all round, centred in the space
   under the headline. What is left over is the card's paper around the window, never air inside
   it; when the screen is short, set the headline larger or choose the corner layout.
5. **One container where the post allows.** When the post is about one box inside a panel, a
   quote or a row, show that box and leave the panel out.
6. **Run off an edge only with content.** A screen runs off when its own content is larger than
   the card (a table too wide to show whole, a single part enlarged into a corner), and then its
   window runs off with it. Nothing is added to reach an edge: no extra section, row, card or
   quiet line. Quiet bars stand only where the product shows words nobody needs to read, one or
   two lines at a time.

7. **Stand the screen off its ground.** A light screen goes on the brand's dark ground or over
   its artwork, with the shadow its site uses there; a dark screen, the product's dark theme, goes
   on a light ground. A pale screen on pale paper reads as one muted surface, and `check` fails a
   window whose edge differs from the ground around it by less than 1.5 : 1.
8. **Mark the one thing the post says.** The words or the number the post is about may carry one
   mark the product doesn't draw, in the colour the product already uses for that meaning, such
   as Tin's "recommended" green behind the sentence that recommends. One mark per card; the rest
   of the screen stays as the product draws it.
9. **Anchor a single part.** One answer, a few rows or a button sits centred over a ground, a band
   of artwork or a panel. It is the subject of the card, so it isn't tucked against an edge.

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
- No filler: no section, row, card or quiet line added to fill a window or reach an edge.
- No type or spacing changed inside a screen; enlarge the whole screen instead.

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

`tin-cards shoot` checks each screen before it reaches a card, and fails the shot when:

- its text fell back to a system font;
- a box in it has a bar down its left edge;
- a word would be under 22 px on the card, or near the headline's size;
- the stage script changed a font size, a margin, a padding or a gap inside the screen.

`tin-cards check` renders each card to `<post>.png` and fails a card when:

- a font didn't load, or the text falls back to a system font;
- the headline runs past the card's edge or is clipped;
- there is text outside the headline, a logo or a screen;
- there is an eyebrow;
- a colour isn't in the palette;
- a box has a bar down its left edge;
- a screen floats with more space on one side than the other, neither centred nor running off;
- a window is larger than the shot it holds, leaving empty window inside the card;
- a window barely stands off the ground around it, under 1.5 : 1;
- the headline is over nine words;
- the alt text is missing;
- the image is over 5 MB, even as a JPEG.

It notes, without failing, when the headline differs from the draft's card line, or a screen's
words run past the card's edge. Look at `sheet.png`, which lays the batch out together, before
you upload.
