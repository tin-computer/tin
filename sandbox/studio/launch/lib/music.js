// Music runtime for seek(t) films. Pure functions of film time, so every frame renders the same.
//
//   const M = Music(CUE)               // CUE = the contents of cue.json, inlined in the page
//   M.at(t)        -> { bar, beat, beatPhase, barPhase, phrase, section, energy }
//   M.time(bar, beat = 1)              // film seconds of a bar/beat (beat may be fractional: 2.5)
//   M.pulse(t, { every: 'beat' | 'bar' | 'half', decay: 10 })  // 1 on the hit, decays to 0
//   M.env(t, 'all' | 'low' | 'mid' | 'high' | 'kick' | 'onset')  // 0..1, measured from the audio
//   M.since(t, type)  -> seconds since the last event of type ('break', 'bass_in', 'riser', 'hit')
//   M.section(t)   -> the section object containing t
(function (root) {
  function Music(cue) {
    const B = cue.bar, b = cue.beat, bpb = cue.beats_per_bar;
    const clamp = (x, a = 0, c = 1) => Math.min(c, Math.max(a, x));
    const sectionAt = t => cue.sections.find(s => t >= s.t && t < s.end) || cue.sections[cue.sections.length - 1];
    function at(t) {
      const barF = t / B;
      const bar = Math.floor(barF) + 1;
      const beatF = (t - (bar - 1) * B) / b;
      const beat = Math.floor(beatF) + 1;
      const s = sectionAt(t);
      const row = cue.bars[Math.min(cue.bars.length - 1, Math.max(0, bar - 1))];
      return {
        bar, beat, beatPhase: beatF - Math.floor(beatF), barPhase: barF - Math.floor(barF),
        phrase: Math.floor((bar - (cue.phrase_offset || 1)) / 4) + 1, section: s && s.name, energy: row ? row.energy : 0,
      };
    }
    const time = (bar, beat = 1) => (bar - 1) * B + (beat - 1) * b;
    function pulse(t, { every = 'beat', decay = 10 } = {}) {
      const len = every === 'bar' ? B : every === 'half' ? b / 2 : b;
      const ph = ((t % len) + len) % len;
      return Math.exp(-decay * ph);
    }
    function env(t, band = 'all') {
      const e = cue.envelope, v = e[band];
      if (!v) return 0;
      const x = t * e.fps, i = Math.floor(x), f = x - i;
      if (i < 0) return v[0];
      if (i >= v.length - 1) return v[v.length - 1];
      return v[i] * (1 - f) + v[i + 1] * f;
    }
    function since(t, type) {
      const list = type === 'hit' ? cue.hits : cue.events.filter(e => e.type === type);
      let best = Infinity;
      for (const e of list) {
        const et = type === 'break' && e.back_in != null ? e.back_in : e.t;
        if (et <= t) best = Math.min(best, t - et);
      }
      return best;
    }
    return { cue, at, time, pulse, env, since, section: sectionAt, clamp, bar: B, beat: b, beatsPerBar: bpb };
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = Music; else root.Music = Music;
})(typeof window !== 'undefined' ? window : globalThis);
