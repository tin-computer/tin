import assert from "node:assert/strict";
import test from "node:test";

import { DOMParser, parseHTML } from "linkedom";
import { diagramFixtures } from "./diagram-fixtures.js";
import { brandShapeFixtures, shapeSource } from "./diagram-brand-fixtures.js";
import { routingFixtures } from "./diagram-routing-fixtures.js";
import { planPorts } from "./diagram-ports.js";
import { diagramQuality, readDiagramGeometry, acceptableRefinement } from "./diagram-quality.js";

globalThis.DOMParser = DOMParser;
globalThis.XMLSerializer = class XMLSerializer {
  serializeToString(node) {
    return node.toString();
  }
};
globalThis.window = parseHTML("<html></html>").window;

const renderer = await import("./diagram-renderer.js");

test("approved corners preserve role hierarchy, semantic shapes and routed geometry", async () => {
  const baseline = new DOMParser().parseFromString(await renderer.renderSource(shapeSource), "image/svg+xml");
  const geometry = readDiagramGeometry(baseline);
  for (const [index, fixture] of brandShapeFixtures.entries()) {
    const [label, node, frame] = [[4,8,12], [0,0,0], [4,8,12], [6,12,16]][index];
    const doc = new DOMParser().parseFromString(await renderer.renderSource(fixture.source), "image/svg+xml");
    assert.deepEqual(readDiagramGeometry(doc), geometry, "corners do not change layout, labels or connectors");
    for (const rect of doc.querySelectorAll(".node:not(.tin-diagram-store) > rect")) {
      assert.equal(Number(rect.getAttribute("rx")), node);
      assert.equal(Number(rect.getAttribute("ry")), node);
    }
    for (const rect of doc.querySelectorAll(".edge-label > rect")) assert.equal(Number(rect.getAttribute("rx")), label);
    assert.equal(Number(doc.querySelector(".tin-diagram-group > rect").getAttribute("rx")), frame);
    for (const selector of [".tin-diagram-store path", ".tin-diagram-store ellipse", ".tin-diagram-gate circle", ".tin-diagram-wait circle", ".tin-diagram-receipt circle"]) {
      assert.equal(doc.querySelector(selector).outerHTML, baseline.querySelector(selector).outerHTML, "semantic silhouettes stay intact");
    }
  }
});

test("composition diagnostics measure painted routes and protect legibility", () => {
  const graph = {
    width: 200, height: 300,
    children: [0, 1, 2].map((i) => ({ id: String(i), x: 0, y: i * 100, width: 100, height: 40 })),
    edges: [0, 1].map((i) => ({ data: { from: String(i), to: String(i + 1), kind: "call" }, points: [{ x: 50, y: i * 100 + 40 }, { x: 50, y: (i + 1) * 100 }] })),
  };
  const q = diagramQuality(graph);
  assert.equal(q.bends, 0);
  assert.equal(q.crossings, 0);
  assert.equal(q.routeLength, 120);
  assert.equal(q.chainJoints, 1);
  assert.equal(q.chainDrift, 0);
  const drift = structuredClone(graph);
  drift.edges[1].points.forEach((p) => p.x += 16);
  assert.equal(diagramQuality(drift).chainDrift, 16);
  assert.equal(acceptableRefinement(drift, graph), false);
  assert.equal(acceptableRefinement({ ...graph, height: 1800 }, graph), false, "smaller overview cannot win on bend count");
  assert.equal(acceptableRefinement({ ...graph, height: 280 }, graph), true);
  const jog = structuredClone(graph);
  jog.edges[0].points = [{ x: 50, y: 40 }, { x: 50, y: 70 }, { x: 58, y: 70 }, { x: 58, y: 100 }];
  assert.equal(diagramQuality(jog).shortJogs, 1);
  assert.equal(acceptableRefinement(jog, graph), false);
});

test("unequal equivalent branches balance around their visible envelope in either order", async () => {
  for (const direction of ["lr", "rl"]) {
    const flow = routingFixtures.find((f) => f.id === `designer-peers-${direction}`).flow;
    for (const edges of [flow.edges, [...flow.edges].reverse()]) {
      const before = JSON.stringify({ ...flow, edges });
      const { svg } = await renderer.renderFlow({ ...flow, edges });
      const doc = new DOMParser().parseFromString(svg, "image/svg+xml");
      const [graph] = readDiagramGeometry(doc), q = diagramQuality(graph);
      assert.equal(q.branchImbalance, 0);
      assert.equal(q.crossings, 0);
      assert.equal(q.shortJogs, 0);
      assert.equal(graph.edges.length, 6);
      assert.equal(graph.children.length, 5);
      assert.equal(JSON.stringify({ ...flow, edges }), before, "layout cannot mutate source meaning");
    }
  }
});

test("an ongoing loop compacts without inventing a terminal or deleting its return", async () => {
  const flow = routingFixtures.find((f) => f.id === "designer-continuous-loop").flow;
  const { svg } = await renderer.renderFlow(flow);
  const [graph] = readDiagramGeometry(new DOMParser().parseFromString(svg, "image/svg+xml"));
  assert.ok(graph.width < 650, "return label should not enlarge every crossed gap");
  assert.equal(graph.children.length, 3);
  assert.equal(graph.edges.length, 3);
  assert.equal(graph.edges.filter((e) => e.data.kind === "signal").length, 1);
  assert.equal(diagramQuality(graph).crossings, 0);
});

const flow = {
  direction: "LR",
  nodes: [
    { id: "draft", kind: "step", label: "draft email", fact: "one bounded call" },
    { id: "review", kind: "gate", label: "needs you", fact: "approve the draft" },
    { id: "sent", kind: "receipt", label: "receipt", fact: "sent · 12" },
  ],
  edges: [
    { from: "draft", to: "review", kind: "call" },
    { from: "review", to: "sent", kind: "call" },
  ],
};

test("Tin flow compiles to the constrained Mermaid dialect", () => {
  assert.equal(
    renderer.sourceForFlow(flow),
    'graph LR\n  draft["draft email<br/>one bounded call"]:::step\n' +
      '  review["needs you<br/>approve the draft"]:::gate\n' +
      '  sent["receipt<br/>sent · 12"]:::receipt\n' +
      "  draft --> review\n  review --> sent\n",
  );
});

test("Tin renderer applies the design-system node meanings", async () => {
  const { svg } = await renderer.renderFlow(flow);
  assert.match(svg, /tin-diagram-step/);
  assert.match(svg, /tin-diagram-gate/);
  assert.match(svg, /tin-diagram-receipt/);
  assert.match(svg, /var\(--diagram-gate-wash\)/);
  assert.match(svg, /var\(--accent-deep\)/);
  const document = new DOMParser().parseFromString(svg, "image/svg+xml");
  assert.equal(document.querySelector("polyline.edge").getAttribute("stroke"), "var(--diagram-edge)");
  const arrowhead = document.querySelector("polyline.tin-diagram-arrow");
  assert.equal(arrowhead.getAttribute("fill"), "none");
  assert.equal(arrowhead.getAttribute("stroke"), document.querySelector("polyline.edge").getAttribute("stroke"));
  assert.doesNotMatch(svg, /stroke="var\(--_line\)"/);
  assert.doesNotMatch(svg, /(?:fill|stroke)="var\(--_arrow\)"/);
  assert.doesNotMatch(svg, /fonts\.googleapis\.com/);
  assert.doesNotMatch(svg, /<style|<marker|marker-end=/);
});

function points(element) {
  return element.getAttribute("points").split(/\s+/).map((pair) => pair.split(",").map(Number));
}

for (const direction of ["LR", "TD"]) {
  test(`${direction} arrow tips leave 10px of air and lines start 6px after nodes`, async () => {
    const doc = new DOMParser().parseFromString((await renderer.renderFlow({ ...flow, direction })).svg, "image/svg+xml");
    for (const edge of doc.querySelectorAll("polyline.edge")) {
      const source = doc.querySelector(`g[data-id="${edge.getAttribute("data-from")}"] rect`);
      const target = doc.querySelector(`g[data-id="${edge.getAttribute("data-to")}"] rect`);
      const axis = direction === "LR" ? 0 : 1;
      const coordinate = direction === "LR" ? "x" : "y";
      const size = direction === "LR" ? "width" : "height";
      const start = points(edge)[0][axis];
      const tip = points(edge.nextElementSibling)[1][axis];
      assert.ok(Math.abs(start - Number(source.getAttribute(coordinate)) - Number(source.getAttribute(size)) - 6) < 0.001);
      assert.ok(Math.abs(Number(target.getAttribute(coordinate)) - tip - 10) < 0.001);
    }
  });
}

test("branches, signals, and return calls preserve routes and meaning", async () => {
  const branch = {
    direction: "LR",
    nodes: [
      { id: "draft", kind: "step", label: "Draft" },
      { id: "review", kind: "gate", label: "Review" },
      { id: "wait", kind: "wait", label: "Wait", fact: "until reply" },
      { id: "receipt", kind: "receipt", label: "Receipt" },
    ],
    edges: [
      { from: "draft", to: "review", kind: "call" },
      { from: "review", to: "wait", kind: "signal", label: "approved" },
      { from: "review", to: "draft", kind: "call", label: "revise" },
      { from: "draft", to: "receipt", kind: "call" },
      { from: "wait", to: "receipt", kind: "signal" },
    ],
  };
  const { source, svg } = await renderer.renderFlow(branch);
  const doc = new DOMParser().parseFromString(svg, "image/svg+xml");
  assert.equal(doc.querySelectorAll("polyline.edge").length, branch.edges.length);
  assert.equal(doc.querySelectorAll("polyline.tin-diagram-arrow").length, branch.edges.length);
  assert.equal(doc.querySelectorAll('polyline[stroke-dasharray]').length, 2);
  assert.equal(doc.querySelector(".tin-diagram-wait circle").getAttribute("fill"), "none");
  assert.ok(doc.querySelector('polyline[data-from="review"][data-to="draft"]'));
  assert.doesNotMatch(svg, /NaN|Infinity/);
  assert.equal(renderer.sourceForFlow(renderer.parseSource(source)), source);
  assert.match(renderer.renderASCII(source), /Draft/);
});

test("Tin renderer rejects Mermaid features outside the finite vocabulary", async () => {
  await assert.rejects(
    () => renderer.renderSource("graph LR\n  A[one] --> B[two]\n  click A javascript:alert(1)\n"),
    /outside the Tin diagram vocabulary/,
  );
  await assert.rejects(
    () => renderer.renderSource('graph LR\n  one["&lt;script&gt;"]:::step\n  two["done"]:::receipt\n  one --> two\n'),
    /Diagram text is invalid/,
  );
});

test("long text wraps within bounded nodes while preserving the full source and accessible labels", async () => {
  for (const fixture of diagramFixtures.filter((item) => /^(wide|editorial|unicode)-lr$/.test(item.id))) {
    const { source, svg } = await renderer.renderFlow(fixture.flow);
    const doc = new DOMParser().parseFromString(svg, "image/svg+xml");
    assert.equal(renderer.sourceForFlow(renderer.parseSource(source)), source);
    for (const node of fixture.flow.nodes) {
      const rendered = doc.querySelector(`g.node[data-id="${node.id}"]`);
      assert.equal(rendered.getAttribute("aria-label"), [node.label, node.fact].filter(Boolean).join(" · "));
      assert.ok(Number(rendered.querySelector("rect").getAttribute("width")) <= 280);
    }
    if (fixture.id === "wide-lr") assert.ok(doc.querySelectorAll("tspan").length > fixture.flow.nodes.length * 2);
  }
});

test("cycles retain the authored progression and route revisions backwards", async () => {
  for (const direction of ["lr", "td"]) {
    const fixture = diagramFixtures.find((item) => item.id === `dense-${direction}`);
    const doc = new DOMParser().parseFromString((await renderer.renderFlow(fixture.flow)).svg, "image/svg+xml");
    const axis = direction === "lr" ? "x" : "y";
    const position = (id) => Number(doc.querySelector(`g.node[data-id="${id}"] rect`).getAttribute(axis));
    // The review loop must not move the draft after its own approval gate.
    assert.ok(position("b") < position("d"));
    assert.ok(position("d") < position("e"));
    assert.ok(position("e") < position("g"));
  }
});

test("all seven composed references preserve groups, paths, text, and shorter open arrowheads", async () => {
  const fs = await import("node:fs/promises");
  const studies = JSON.parse(await fs.readFile("docs/diagram-studies/index.json", "utf8"));
  for (const study of studies) {
    const source = await fs.readFile(`docs/diagram-studies/${study.file}`, "utf8");
    const flow = renderer.parseSource(source);
    assert.equal(renderer.sourceForFlow(flow), source);
    const svg = new DOMParser().parseFromString(await renderer.renderSource(source), "image/svg+xml");
    assert.equal(svg.querySelectorAll("g.node").length, flow.nodes.length);
    assert.equal(svg.querySelectorAll(".tin-diagram-group").length, flow.groups.filter((g) => g.kind !== "layout").length);
    assert.equal(svg.querySelectorAll(".edge").length, flow.edges.length);
    const ascii = renderer.renderASCII(source);
    for (const node of flow.nodes) assert.ok(ascii.includes(node.label));
    for (const group of flow.groups.filter((g) => g.label)) assert.ok(ascii.includes(group.label));
    for (const arrow of svg.querySelectorAll(".tin-diagram-arrow")) {
      const [a,tip,b] = arrow.getAttribute("points").split(" ").map((pair) => pair.split(",").map(Number));
      assert.ok(Math.abs(Math.hypot(a[0]-b[0],a[1]-b[1])-6) < 0.001);
      assert.ok(Math.abs(Math.hypot((a[0]+b[0])/2-tip[0],(a[1]+b[1])/2-tip[1])-4.5) < 0.001);
      assert.equal(arrow.getAttribute("fill"),"none");
    }
  }
});

test("composition grammar stays bounded and rejects executable or ambiguous source", () => {
  const valid = 'graph TD\n  %% tin:composition\n  subgraph lane["Input"]\n    direction RL\n    %% tin:group lane\n    a["Ask"]:::step\n    b["Done"]:::receipt\n  end\n  a --> b\n';
  assert.equal(renderer.parseSource(valid).groups[0].direction,"RL");
  for (const source of [valid.replace('  end\n',''),valid.replace('a --> b','a --> lane'),valid.replace('a --> b','a --> missing'),valid+'  click a javascript:alert(1)',valid.replace(':::receipt',':::unknown'),valid.replace('%% tin:group lane','%% tin:group unsafe'),valid.replace('b["Done"]','a["Done"]'),valid.replace('%% tin:composition','%% arbitrary comment')]) assert.throws(()=>renderer.parseSource(source));
});


test("compositions render identically after other graphs and concurrent requests", async () => {
  const fs=await import("node:fs/promises");
  const studies=JSON.parse(await fs.readFile("docs/diagram-studies/index.json","utf8"));
  const sources=await Promise.all(studies.map(s=>fs.readFile(`docs/diagram-studies/${s.file}`,"utf8")));
  const first=await Promise.all(sources.map(s=>renderer.renderSource(s)));
  const repeated=await Promise.all([...sources].reverse().map(s=>renderer.renderSource(s)));
  assert.deepEqual(repeated.reverse(),first);
});

test("assembly aligns columns and uses straight, spaced reciprocal connections", async () => {
  const fs=await import("node:fs/promises");
  const source=await fs.readFile("docs/diagram-studies/computer-assembly.mmd","utf8");
  const doc=new DOMParser().parseFromString(await renderer.renderSource(source),"image/svg+xml");
  const edge=(from,to)=>points(doc.querySelector(`.edge[data-from="${from}"][data-to="${to}"]`));
  for(const [from,to] of [["code","toolchain"],["state","sessions"],["sessions","state"],["codex","gateway"]]) assert.equal(edge(from,to).length,2,`${from} → ${to} should be straight`);
  assert.ok(Math.abs(edge("state","sessions")[0][1]-edge("sessions","state")[0][1])>=16);
  const centers=["codex","toolchain","sessions"].map(id=>{const n=doc.querySelector(`.node[data-id="${id}"] rect`);return +n.getAttribute("x")+(+n.getAttribute("width"))/2;});
  assert.ok(Math.max(...centers)-Math.min(...centers)<.01,"the sandbox's column stays aligned");
});

test("large fan-out and unrelated nested frames remain routable", async () => {
  for(const fixture of routingFixtures) {
    const {svg}=await renderer.renderFlow(fixture.flow);
    const doc=new DOMParser().parseFromString(svg,"image/svg+xml");
    assert.equal(doc.querySelectorAll(".edge").length,fixture.flow.edges.length);
    assert.equal(doc.querySelectorAll(".node").length,fixture.flow.nodes.length);
    for(const line of doc.querySelectorAll(".edge")) {
      const route=points(line);
      for(let i=1;i<route.length;i++) assert.ok(route[i][0]===route[i-1][0]||route[i][1]===route[i-1][1]);
    }
    if(fixture.id==="composed-foreign-frame") {
      const {hits}=await import("./diagram-geometry.js");
      const frame=doc.querySelector('[data-group-id="boundary"] rect');
      const box=Object.fromEntries(["x","y","width","height"].map(k=>[k,+frame.getAttribute(k)]));
      for(const line of doc.querySelectorAll('.edge[data-from="source"],.edge[data-from="target"]')) {
        const route=points(line).map(([x,y])=>({x,y}));
        for(let i=1;i<route.length;i++) assert.equal(hits(route[i-1],route[i],box),false,"a route must not imply entry into the unrelated frame");
      }
    }
  }
});

for (const direction of ['TD', 'LR']) {
  test(`peer inputs keep their ${direction === 'TD' ? 'vertical' : 'horizontal'} centerline under connector refinement`, async () => {
    const row = direction === 'TD' ? 'LR' : 'TD';
    const source = `graph ${row}
  %% tin:composition
  subgraph clients
    direction ${direction}
    %% tin:group layout
    browser["Browser"]:::step
    chat["Chat"]:::step
    agent["Coding agent<br/>Over MCP"]:::step
  end
  hub["Switchboard<br/>The only writer of canonical state"]:::surface
  output[("Project repository")]:::store
  browser --> hub
  chat --> hub
  agent --> hub
  hub --> output
`;
    const doc = new DOMParser().parseFromString(await renderer.renderSource(source), 'image/svg+xml');
    const axis = direction === 'TD' ? 'x' : 'y';
    const size = direction === 'TD' ? 'width' : 'height';
    const centers = ['browser', 'chat', 'agent'].map(id => {
      const box = doc.querySelector(`.node[data-id="${id}"] rect`);
      return Number(box.getAttribute(axis)) + Number(box.getAttribute(size)) / 2;
    });
    assert.ok(Math.max(...centers) - Math.min(...centers) < 0.001, `Peer centers drifted: ${centers}`);
    assert.equal(doc.querySelectorAll('.edge').length, 4);
    assert.equal(doc.querySelectorAll('.tin-diagram-arrow').length, 4);
  });
}

test("successive review connectors share one axis and keep the side outcome separate", async () => {
  const fixture = routingFixtures.find((f) => f.id === "composed-review-chain");
  const doc = new DOMParser().parseFromString((await renderer.renderFlow(fixture.flow)).svg, "image/svg+xml");
  const routes = [["projection", "whose"], ["whose", "decisions"], ["decisions", "effect"]]
    .map(([from, to]) => points(doc.querySelector(`.edge[data-from="${from}"][data-to="${to}"]`)));
  const axis = routes[0][0][0];
  assert.ok(routes.every((route) => route.length === 2 && route.every(([x]) => Math.abs(x - axis) < 0.001)), "the chain must not drift at each node");
  const centers = ["projection", "whose", "decisions", "effect"].map((id) => {
    const box = doc.querySelector(`.node[data-id="${id}"] rect`);
    return +box.getAttribute("x") + +box.getAttribute("width") / 2;
  });
  assert.ok(Math.max(...centers) - Math.min(...centers) <= 24, "chain remains near-aligned after compaction");
  const side = points(doc.querySelector('.edge[data-from="whose"][data-to="done"]'));
  assert.ok(side.at(-1)[0] < axis - 24, "the side outcome stays separate from the review chain");
  assert.equal(doc.querySelectorAll('.node').length, fixture.flow.nodes.length);
  assert.equal(doc.querySelectorAll('.edge').length, fixture.flow.edges.length);
});

function chainGraph(direction = "TD") {
  const horizontal = ["LR", "RL"].includes(direction), reverse = ["BT", "RL"].includes(direction);
  const children = ["a", "b", "c"].map((id, i) => ({
    id, x: horizontal ? (reverse ? 2 - i : i) * 200 : i * 16,
    y: horizontal ? i * 16 : (reverse ? 2 - i : i) * 200,
    width: horizontal ? 64 : 120, height: horizontal ? 120 : 64,
  }));
  return { children, edges: [{ from: "a", to: "b", kind: "call" }, { from: "b", to: "c", kind: "call" }] };
}

for (const direction of ["TD", "BT", "LR", "RL"]) {
  test(`${direction} near-aligned chains use one axis independent of edge declaration order`, () => {
    const graph = chainGraph(direction), axis = ["TD", "BT"].includes(direction) ? "x" : "y";
    for (const edges of [graph.edges, [...graph.edges].reverse()]) {
      const plans = planPorts(graph, { edges }, []);
      assert.ok(plans.every((p) => p.start[axis] === 76 && p.end[axis] === 76));
    }
  });
}

test("signals, returns, and ambiguous forward branches do not acquire an inferred chain axis", () => {
  for (const scenario of ["signal", "return", "fork"]) {
    const graph = chainGraph();
    if (scenario === "signal") graph.edges[1].kind = "signal";
    if (scenario === "return") graph.children[2].y = -200;
    if (scenario === "fork") {
      graph.children.push({ id: "d", x: 0, y: 600, width: 120, height: 64 });
      graph.edges.push({ from: "b", to: "d", kind: "call" });
    }
    const first = planPorts(graph, graph, [])[0];
    assert.equal(first.start.x, 68, `${scenario} must retain pairwise placement`);
    assert.equal(first.end.x, 68);
  }
});

test("a blocked common corridor falls back without nudging nodes or ignoring port spacing", () => {
  const graph = chainGraph();
  graph.children.push({ id: "obstacle", x: 40, y: 300, width: 80, height: 40 });
  const before = structuredClone(graph.children);
  const plans = planPorts(graph, graph, []);
  assert.equal(plans[0].start.x, 68, "the unobstructed first edge keeps its normal midpoint");
  assert.deepEqual(graph.children, before);
  const atB = [plans[0].end, plans[1].start];
  assert.ok(atB[0].side !== atB[1].side || Math.hypot(atB[0].x - atB[1].x, atB[0].y - atB[1].y) >= 16);
});
