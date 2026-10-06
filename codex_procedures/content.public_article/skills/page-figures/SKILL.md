---
name: page-figures
description: Add figures, diagrams, interactive pieces, videos and callouts to an article draft only where they show what the words can't, as files in its assets folder that the founder approves with the article.
---

# Figures, diagrams, interactive pieces and videos

Add one only where it shows something the words can't carry as well: a process, a comparison,
a structure, a measured result. Most articles need none, one or two. Never decorate.

Your output instructions name the article's assets folder. Write each file directly in it and
refer to it from the article with a path that starts `./<folder name>/`. Tin keeps only the files
the article refers to, and the founder approves them with the words.

## The project's look

Everything you draw should look like it belongs on the founder's site. When `brand/BRAND.md` and
`DESIGN.md` exist, read them first and follow them: the token palette in light and dark, the type
family, weight and size for each role, the corner radii and how the site frames figures, the
founder's written rules, and the way the product draws its recurring subjects (a mascot,
characters, its own objects or screens) in their own colours, never as a labelled placeholder
shape. Name the brand's font families with their fallbacks. Without brand files, keep a plain
look: system fonts and a quiet neutral palette.

Follow the site's theming. When the site has a dark theme (the brand has dark tokens, or DESIGN.md
describes one), leave the background transparent so the page shows through and give the colours a
dark variant; in an SVG, a `prefers-color-scheme: dark` block in its `<style>` does it. When the
site is light only, or you can't tell, keep it light: `color-scheme: light` and the site's paper
colour as its own background, so it reads on any page.

## Figures and diagrams

Make each one however suits it: an SVG file for a still figure or diagram, an interactive piece
when the reader should try something, or a `mermaid` block when the site's own pages render
Mermaid. Tin shows what you make; it doesn't redraw it. For an SVG file:

```markdown
![What the figure shows](./<folder name>/<file>.svg "What the reader should notice")
```

The alt text says what the figure shows; the caption says what to notice. Use real labels and the
article's own numbers. The SVG must have no script, no event handlers, no `foreignObject` and no
links, images or fonts from elsewhere; Tin leaves out an SVG that has any.

## Interactive piece

One self-contained HTML file in the assets folder, only when interaction teaches something a still
figure can't, such as a simulation or a calculator:

````markdown
```tin-embed
src: ./<folder name>/<file>.html
height: 420
title: What the reader can do with it
```
````

Inline its CSS and JavaScript. It runs with no network access: no requests, no outside
libraries or images. When DESIGN.md records the font files the site itself serves, you may point
`@font-face` at those site paths: they load on the site, and Tin's preview uses the fallbacks.
Keep it small, give it a sensible default state, and make it work with a mouse, a keyboard and a
touch screen.

Draw no panel, card or border around the piece; frame only the surface the reader plays with,
the way the site frames figures. Set colours as CSS variables, light on `:root` with
`color-scheme: light`. For a site with a dark theme, add the dark values with `color-scheme: dark`
in both `@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { … } }` and
`:root[data-theme="dark"] { … }`; Tin sets `data-theme` to match its reader. Let the content set
its height: Tin fits the frame to it, so `height` is only a first guess.

## Look at it before you finish

Look at every figure and piece the way a reader will, in light and in dark, and fix what you see,
then look again. Check each one against what it is meant to show: every shape, line and mark you
drew must be visible in both themes, and no text may touch other text or lines. The sandbox has an
offline Chromium for this. In a small Node script, `require("/opt/tin-lite/diagram/node_modules/playwright")`,
run it with `PLAYWRIGHT_BROWSERS_PATH=/opt/tin-lite/diagram/browsers`, open each file with
`colorScheme` set to `light` and then `dark`, and save screenshots under `/tmp`. View them with
your image viewing tool. Keep the screenshots out of the assets folder.

Make this draft's figures yourself. Don't copy them from an earlier draft that wasn't approved:
its problems come with it.

## Video

A video that already exists at a public address, on the founder's site or on YouTube, Vimeo, Loom
or Mux:

````markdown
```tin-video
url: https://www.youtube.com/watch?v=...
title: What it shows
```
````

Never invent a video or an address. A long video file is linked, never copied into the folder.

## Callout

`> [!NOTE]`, `> [!TIP]` or `> [!WARNING]` on the first line of a quote, for a short aside the
reader shouldn't miss. Use one or two at most.

## Existing pages

When the brief points to an existing page that has figures and you can read its source, keep
them: copy an inline SVG into the folder as its own file, or rebuild an interactive piece as an
embed with its script and data. Don't describe a figure in words instead of keeping it. If you
can't carry one over, say which and why in Generation notes.

List every figure, diagram, interactive piece and video in Generation notes with one line on what
each shows.
