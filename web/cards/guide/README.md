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
body text no smaller than 20 px on the card.

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
- A narrow width lets the product's own layout reflow, so the shot reads at about 1.3 times its
  size on the card.
- A terminal is the company's own transcript or terminal component, from its site, with output a
  real command gave.
- Leave out other companies' logos; turn them off in the sample data when the product shows them.

## Colours

Every colour a card paints is one of the brand's. List the brand's colours from brand/BRAND.md
in `cards.json` as its palette; the check fails any other colour, at any opacity. Shots and
artwork keep their own colours.

## Composition

Start from the cards in `social/linkedin/cards/`, the layouts a member picked for the page. When
there are none yet, make a round first: the same posts in several compositions, each taken from
a layout the company already publishes (a section of its site, its hero, its link preview, its
dark theme), and let a member pick before making the batch.

## Never

- No eyebrows: no small uppercase, letter-spaced or mono label above a title, a card's words or
  a number.
- No words beyond the headline outside a screen.
- No customer names, prices or numbers the post doesn't give, unless they are sample data
  inside a screen.
- No art the company doesn't publish, and no third-party logos.
- No colour outside the brand's palette.
- No bar down a box's left edge to mark it.

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
- the headline is over nine words;
- the alt text is missing;
- the image is over 5 MB, even as a JPEG.

It notes, without failing, when the headline differs from the draft's card line, or a screen's
words run past the card's edge. Look at `sheet.png`, which lays the batch out together, before
you upload.
