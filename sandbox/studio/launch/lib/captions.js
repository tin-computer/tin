// Timed speech phrases for seek(t) films; styling stays with the film.
(function (root) {
  function create(element, lines) {
    if (!element || typeof element.replaceChildren !== 'function') {
      throw new TypeError('Captions need a DOM element');
    }
    if (!Array.isArray(lines)) throw new TypeError('Captions need timed phrases');
    let end = 0;
    const cues = lines.map(line => {
      if (!line || typeof line.text !== 'string' || !line.text.trim() ||
          !Number.isFinite(line.t0) || !Number.isFinite(line.t1) ||
          line.t0 < end || line.t1 <= line.t0) {
        throw new TypeError('Caption phrases need text and ordered, non-overlapping times in seconds');
      }
      end = line.t1;
      return { text: line.text, t0: line.t0, t1: line.t1 };
    });
    element.setAttribute('data-film-review-overflow', '');
    function paint(t) {
      if (!Number.isFinite(t)) throw new TypeError('Caption time must be finite');
      const cue = cues.find(line => t >= line.t0 && t < line.t1);
      element.hidden = !cue;
      element.textContent = cue ? cue.text : '';
    }
    paint(-1);
    return paint;
  }
  const api = { create };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.FilmCaptions = api;
})(typeof window !== 'undefined' ? window : globalThis);
