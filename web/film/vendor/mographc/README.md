# mographc, vendored

The film model, the simulated viewer and the rules of [mographc](https://github.com/replicas-group/mographc)
0.1.1 (MIT, Copyright (c) 2026 Replicas), copied from the package's published build (`dist/`)
so the film kit can check its takes without installing Remotion, which mographc's npm package
pulls in as a required peer.

- Copied unchanged: `film.js`, `viewer.js`, `rule.js`, `report.js`, `lint.js` and `rules/*.js`.
- `config.js` without `loadConfig` and its imports (`node:fs`, `node:path`, `./load.js`): the kit
  builds its configs in code (`web/film/checks.js`) and never loads a `mographc.config.ts`.
- Not copied: the Remotion capture and probe, the CLI and the test runner. The kit measures its own
  frames (`web/film/probe.js`, adapted from mographc's probe) and builds the same `RawFilm`.

To update, copy the same files from a newer release's `dist/` and make the same change to
`config.js`. `LICENSE` is the package's licence.
