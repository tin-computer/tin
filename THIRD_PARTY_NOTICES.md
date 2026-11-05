# Third-party notices

The packaged UI and diagram fonts use Geist Sans and Geist Mono (SIL OFL-1.1).
Geist Sans regular/bold come from `vercel/geist-font` commit
`10dc7658f13c38a474cde201bb09a4617267545b`, `packages/next/dist/fonts/geist-sans/`.
Their license is in `third_party/licenses/geist-sans-OFL.txt`; the existing Mono
license stays at `src/tin_lite/static/fonts/Geist-OFL.txt`.

The hosted product optionally loads separately licensed FK Grotesk Neue from an
operator-controlled CDN. It is not included in Tin's source/package or licensed
under Apache 2.0. See `docs/font-serving.md`.

The browser bundle at `src/tin_lite/static/pierre-trees.js` includes:

- `@pierre/trees` 1.0.0-beta.6 (Apache-2.0)
- `@pierre/theming` 1.0.0 (Apache-2.0)
- `preact` 11.0.0-beta.0 (MIT)
- `preact-render-to-string` 6.6.5 (MIT)

Their license and notice texts are preserved in `third_party/licenses/`.

The browser bundle at `src/tin_lite/static/diagram-renderer.js` includes:

- `beautiful-mermaid` 1.1.3 (MIT)
- `elkjs` 0.11.1 (EPL-2.0)
- `entities` 7.0.1 (BSD-2-Clause)
- `libavoid-js` 0.5.0-beta.5 (LGPL-2.1-or-later), the JS bindings for the separately
  loaded `src/tin_lite/static/diagram-routing.wasm` routing library

Their license texts are preserved in `third_party/licenses/`.

The feature film recorder (`web/film/`, a development tool, not part of the package) adapts
Stageshow's virtual clock, cursor and cursor timing from `benjitaylor/skills` commit
`802057409787133703c56001080169e347305572`, `stageshow/kit/` (MIT, Copyright (c) 2026 Benji
Taylor). Its license is in `third_party/licenses/stageshow-LICENSE.txt`. The stage kit
(`web/film/stage/`) is written for Tin after Stageshow's framing, restraint and motion rules and
its two easing curves; it copies none of Stageshow's components.

The film recorder's checks (`web/film/vendor/mographc/`) are mographc 0.1.1's film model,
viewer and rules, copied from its published build, and `web/film/probe.js` adapts its probe
(MIT, Copyright (c) 2026 Replicas). Its license is in `third_party/licenses/mographc-LICENSE.txt`.

The AI Visibility page's engine marks in `src/tin_lite/static/ai-engines/` are unmodified SVGs
from `@lobehub/icons-static-svg` 1.102.0 (MIT). Its license text is preserved in
`third_party/licenses/lobe-icons-LICENSE.txt`. The marks themselves are their owners'
trademarks, shown only to name each answer engine.

The studio sandbox image (`sandbox/studio/`) ships:

- Montserrat Bold and ExtraBold (OFL-1.1), caption font for rendered demo videos
- `resvg` 0.48.1 (Apache-2.0 / MIT), fetched at template build time by pinned digest

The Montserrat license text is preserved in `third_party/licenses/`.


The libavoid JS bindings and WASM are unmodified upstream code. The exact wrapper
source is [libavoid-js commit 5062a42](https://github.com/Aksem/libavoid-js/tree/5062a42fbd82fff562afeebcbb7b1ed45eed8e75).
Its build instructions and `tools/generate.py` identify the Adaptagrams source and
Emscripten toolchain. The LGPL text is in `third_party/licenses/libavoid-js-LICENSE.txt`.
The WASM is a separately replaceable asset; an API-compatible rebuilt library can
replace it at `/assets/diagram-routing.wasm`. To rebuild the JS integration, install
the replacement package locally and run `npm run build:diagrams`. Tin's adapter
sources are under `web/diagram-*.js`; the normal pinned build is `npm ci` followed
by `npm run build:diagrams`.
