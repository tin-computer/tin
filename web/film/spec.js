// A film as data. A film lists its beats, each one of the director's moves with its options, and
// `perform` plays them: {"click": "[data-view=files]", "to": [0.3, 0.5]} is
// f.click("[data-view=files]", {to: [0.3, 0.5]}). The list is plain JSON, so an agent can write a
// film without writing code; a hand-made film may add named values and hooks for what the beats
// can't say.
//
// - A beat's first key is its action, named as the director's method (`term.<method>` is the
//   terminal's); some options share an action's name, as in {"open": "/people", "ready": "…"}.
//   The action's value is its first argument (`true` for none), and every other key is an option.
//   For `wait`, `show`, `scroll` and `term.work` the value is the list of arguments.
// - `when` plays a beat only in some takes: "wide" or "tall" (the format), "full" or "brisk"
//   (a desk scene's pace). `if` plays it only while a selector shows.
// - `{"beats": [...]}` groups beats under one `when`. `{"morph": [...], "caption": "…"}` plays
//   them as one change shown in place.
// - "@name" anywhere in a value is the film's named value; a function value is called with the
//   director when the beat plays, so it can measure the page. `{"run": "name", "args": [...]}`
//   calls a hook for what no action covers, such as changing the world.

const ACTIONS = new Set([
  "address", "begin", "camera", "click", "do", "endCard", "end", "focus", "follow", "gone", "hide",
  "highlight", "hold", "look", "mark", "move", "open", "raise", "read", "ready", "say",
  "scroll", "scrollPage", "show", "still", "type", "wait", "caption",
  "term.box", "term.clear", "term.enter", "term.paste", "term.print", "term.prompt",
  "term.type", "term.work",
]);
const LISTS = new Set(["wait", "show", "scroll", "term.work", "do"]);
const CONTROLS = new Set(["when", "if"]);
const WHEN = {
  wide: f => f.wide,
  tall: f => !f.wide,
  full: f => !f.brisk,
  brisk: f => Boolean(f.brisk),
};

export async function perform(f, beats, values = {}) {
  for (const beat of beats) await play(f, beat, values);
}

async function play(f, beat, values) {
  if (beat.when && !WHEN[beat.when](f)) return;
  if (beat.if && !(await f.shows(beat.if))) return;
  if (beat.beats) return perform(f, beat.beats, values);
  if (beat.morph) {
    const options = await resolve(f, rest(beat, "morph"), values);
    return f.morph(() => perform(f, beat.morph, values), options);
  }
  if (beat.run) return values[beat.run](f, ...(await resolve(f, beat.args || [], values)));
  const action = actionOf(beat);
  const options = rest(beat, action);
  const raw = beat[action];
  const value = raw === true ? undefined : await resolve(f, raw, values);
  const args = raw === true ? [] : LISTS.has(action) && Array.isArray(value) ? value : [value];
  if (Object.keys(options).length) args.push(await resolve(f, options, values));
  const [owner, method] = action.startsWith("term.") ? [f.term, action.slice(5)] : [f, action];
  return owner[method](...args);
}

function actionOf(beat) {
  const action = Object.keys(beat).find(key => !CONTROLS.has(key));
  if (!ACTIONS.has(action)) throw new Error(`A beat starts with its action, one of ${[...ACTIONS].join(", ")}: ${JSON.stringify(beat)}`);
  return action;
}

function rest(beat, action) {
  return Object.fromEntries(Object.entries(beat).filter(([key]) => key !== action && !CONTROLS.has(key)));
}

async function resolve(f, value, values) {
  if (typeof value === "string" && value.startsWith("@")) {
    const named = values[value.slice(1)];
    return typeof named === "function" ? named(f) : named;
  }
  if (Array.isArray(value)) {
    const out = [];
    for (const item of value) out.push(await resolve(f, item, values));
    return out;
  }
  if (value && typeof value === "object") {
    const out = {};
    for (const [key, item] of Object.entries(value)) out[key] = await resolve(f, item, values);
    return out;
  }
  return value;
}

// Check a film's beats before anything is filmed, so a mistake shows at once with the beat that
// has it: an unknown action, a `when` that names no take, a named value or hook the film lacks.
export function check(beats, values = {}, where = "beats") {
  const problems = [];
  const names = new Set(Object.keys(values));
  const refs = value => {
    if (typeof value === "string" && value.startsWith("@") && !names.has(value.slice(1))) return [value];
    if (Array.isArray(value)) return value.flatMap(refs);
    if (value && typeof value === "object") return Object.values(value).flatMap(refs);
    return [];
  };
  beats.forEach((beat, index) => {
    const at = `${where}[${index}]`;
    if (!beat || typeof beat !== "object" || Array.isArray(beat)) return problems.push(`${at} is not a beat`);
    if (beat.when && !WHEN[beat.when]) problems.push(`${at}: when "${beat.when}" names no take (${Object.keys(WHEN).join(", ")})`);
    if (beat.beats) return problems.push(...check(beat.beats, values, `${at}.beats`));
    if (beat.morph) return problems.push(...check(beat.morph, values, `${at}.morph`));
    if (beat.run) {
      if (typeof values[beat.run] !== "function") problems.push(`${at}: the film has no hook "${beat.run}"`);
      return;
    }
    const action = Object.keys(beat).find(key => !CONTROLS.has(key));
    if (!ACTIONS.has(action)) problems.push(`${at} starts with "${action}", which is no action (${[...ACTIONS].join(", ")})`);
    for (const ref of refs(beat)) problems.push(`${at}: the film has no value ${ref}`);
  });
  return problems;
}

// A film's beats as the director plays them: its `beats` (or a scene's), or its own code.
export function player(source, values = {}, where = "beats") {
  if (typeof source.play === "function") return source.play;
  const problems = check(source.beats || [], values, where);
  if (!source.beats || problems.length) throw new Error(`The film can't play: ${problems.join("; ") || "it has neither beats nor play"}`);
  return f => perform(f, source.beats, values);
}
