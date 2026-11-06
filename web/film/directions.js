// Directions for a film, dealt before any is chosen: a spread of ways to show one claim, drawn
// at random from outside a model's habits so the first idea that comes is one of several, not the
// film. `tin-film directions` deals them; the agent writes one direction per card, sketches each
// as a still, and a second look picks among the stills.
//
// A film is a product highlight (the post shows what the product does: staged in its own look,
// as the worked examples are), a motion graphic (the post argues a general idea), or both (an
// idea that lands on the product). Each kind is dealt from its own deck.

// Ways to show an idea, each with examples from different subjects so none primes one answer.
export const FAMILIES = [
  {name: "Physical metaphor", move: "an object whose shape is the idea", examples: "an iceberg for what is hidden, an hourglass for a narrow neck, a dam for pressure held back"},
  {name: "Scale move", move: "the camera pulls back or pushes in, and size is the reveal", examples: "one dot that becomes a crowd, a street that becomes the city"},
  {name: "Flow", move: "quantities that move, merge, split or leak", examples: "streams that split at a fork, a Sankey of where people go, a pipe that leaks"},
  {name: "Rings and distance", move: "things arranged by how close they are to a centre", examples: "an orbit around you, ripples from one event, a target"},
  {name: "One record's story", move: "one person, order or page followed through time", examples: "a timeline of one customer's month, the path of one parcel"},
  {name: "Typography", move: "one number or sentence carries it; the rest is small", examples: "a numeral that dwarfs the old one, a sentence that rewrites itself"},
  {name: "Field of units", move: "one mark per thing, in a field that moves, sorts or lights up", examples: "dots that sort themselves by fit, a crowd that parts (never a waffle grid of squares)"},
  {name: "Network", move: "who connects to whom", examples: "one post and the people it reached, links lighting up between teams"},
  {name: "Comparison", move: "the same frame twice, or two worlds side by side", examples: "before and after, what you see beside what is there"},
  {name: "Map or space", move: "where things are", examples: "a map, a floor plan, a shelf, a calendar as a place"},
  {name: "Chart in motion", move: "a real chart whose change is the point", examples: "a line that bends at the moment it matters, bars that re-sort"},
];

// Ways to tell what a product does, in its own look; the worked examples are two of them.
export const STRUCTURES = [
  {name: "Problem to product", move: "open where the founder's problem lives (their terminal, inbox, post), then travel to the product answering it"},
  {name: "Result first", move: "open on the result, then show the one action that made it"},
  {name: "One record", move: "follow a single item (a person, a draft, an order) through the feature"},
  {name: "Before and after", move: "the same screen without the feature, then with it"},
  {name: "Quiet hero, loud card", move: "the product's window stays still while one card beside it carries the point and changes"},
  {name: "Close to wide", move: "start on one detail, then pull back to the whole screen"},
  {name: "A week in seconds", move: "the feature's effect accumulating over days, on a calendar or a count"},
];

export const LOOKS = {
  ground: ["the product's own ground", "black", "warm paper", "pure white"],
  type: ["the product's own sans", "an editorial serif headline", "giant numerals", "the product's sans, with mono only for data"],
  camera: ["a still frame", "a push in", "a pull back", "the scene travelling up", "a pan across"],
  accent: ["the result", "the thing that changes", "the one person"],
};

// A small seeded generator, so a deal can be repeated from its seed.
function random(seed) {
  let h = 1779033703 ^ seed.length;
  for (let i = 0; i < seed.length; i++) h = Math.imul(h ^ seed.charCodeAt(i), 3432918353), h = (h << 13) | (h >>> 19);
  return () => {
    h = Math.imul(h ^ (h >>> 16), 2246822507);
    h = Math.imul(h ^ (h >>> 13), 3266489909);
    return ((h ^= h >>> 16) >>> 0) / 4294967296;
  };
}

const shuffle = (items, next) => {
  const out = [...items];
  for (let i = out.length - 1; i > 0; i--) {
    const j = Math.floor(next() * (i + 1));
    [out[i], out[j]] = [out[j], out[i]];
  }
  return out;
};

// Looks as far from each other as the deck allows: each next look differs most from those dealt.
// A product keeps its own ground (and, for a product highlight, its own type).
function looks(count, next, {ownGround = false, ownType = false} = {}) {
  const all = [];
  for (const ground of ownGround ? [LOOKS.ground[0]] : LOOKS.ground) {
    for (const type of ownType ? [LOOKS.type[0]] : LOOKS.type) for (const camera of LOOKS.camera) for (const accent of LOOKS.accent) all.push({ground, type, camera, accent});
  }
  const pool = shuffle(all, next);
  const dealt = [pool[0]];
  const distance = (a, b) => ["ground", "type", "camera", "accent"].filter(key => a[key] !== b[key]).length;
  while (dealt.length < count) {
    let best = null;
    let score = -1;
    for (const look of pool) {
      const nearest = Math.min(...dealt.map(other => distance(look, other)));
      if (nearest > score) [best, score] = [look, nearest];
    }
    dealt.push(best);
  }
  return dealt;
}

// Deal `count` directions of a kind: "idea", "product" or "both" (ideas that land on the product).
export function deal(kind, {count = 5, seed = String(Date.now())} = {}) {
  const next = random(seed);
  if (kind === "product") {
    const dealtLooks = looks(count, next, {ownGround: true, ownType: true});
    return shuffle(STRUCTURES, next).slice(0, count).map((card, i) => ({...card, look: dealtLooks[i]}));
  }
  const cards = shuffle(FAMILIES, next).slice(0, count);
  const dealtLooks = looks(count, next, {ownGround: kind === "both"});
  return cards.map((card, i) => ({...card, look: dealtLooks[i], lands: kind === "both" ? "then lands on the product's own screen" : undefined}));
}

export function describe(card) {
  const look = ` Look: ${card.look.ground}, ${card.look.type}, ${card.look.camera}; the accent marks ${card.look.accent}.`;
  const examples = card.examples ? ` (for instance ${card.examples})` : "";
  return `${card.name}: ${card.move}${examples}${card.lands ? `, ${card.lands}` : ""}.${look}`;
}
