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

## Diagram

A `mermaid` block for a flow, a sequence, a hierarchy or a timeline. Tin draws it in the
project's brand colours, so don't set colours or styles in it. Keep it to the few nodes the point
needs.

## Figure

An SVG file in the assets folder:

```markdown
![What the figure shows](./<folder name>/<file>.svg "What the reader should notice")
```

The alt text says what the figure shows; the caption says what to notice. Draw it plainly, with
real labels and the article's own numbers. The SVG must have no script, no event handlers, no
`foreignObject` and no links or images from elsewhere; Tin leaves out an SVG that has any.

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

Inline its CSS and JavaScript. It runs with no network access: no requests, no outside fonts,
libraries or images. Keep it small, give it a sensible default state, and make it work with a
mouse, a keyboard and a touch screen.

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
