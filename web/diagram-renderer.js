import { renderMermaidASCII } from "beautiful-mermaid";
import ELK from "elkjs/lib/elk.bundled.js";
import { FONT_ADVANCES, FONT_VERTICAL } from "./diagram-font-metrics.js";

import { parseSource, sourceForFlow } from "./diagram-contract.js";
import { brandStyles } from "./diagram-brand.js";
import { layoutComposition } from "./diagram-composition.js";

const EDGE_COLOR = "var(--diagram-edge)";
const GEOMETRY = Object.freeze({ sourceGap: 6, targetGap: 10, arrowLength: 4.5, arrowHalfWidth: 3 });
const TYPE = Object.freeze({ maxWidth: 224, edgeMaxWidth: 160, padding: 20, markInset: 16, lineGap: 4, factGap: 6 });

function setAttributes(element, attributes) {
  for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, value);
}

const SVG_NS = "http://www.w3.org/2000/svg";
let layoutEngine;

function element(document, tag, attributes, text) {
  const node = document.createElementNS(SVG_NS, tag);
  setAttributes(node, attributes);
  if (text !== undefined) node.textContent = text;
  return node;
}

function textStyle(kind, fact = false) {
  if (!fact) return { face: kind === "gate" ? "mono" : "bold", size: kind === "gate" ? 10.5 : 12.5, weight: 700,
    color: kind === "gate" ? "var(--hot)" : kind === "ghost" ? "var(--ink-muted)" : "var(--ink)" };
  const mono = ["step", "receipt", "ghost", "wait"].includes(kind);
  return { face: mono ? "mono" : "regular", size: ["surface", "gate"].includes(kind) ? 11.5 : kind === "store" ? 11 : 10.5,
    weight: 400, color: kind === "gate" ? "var(--ink)" : ["surface", "store", "receipt"].includes(kind) ? "var(--ink-secondary)" : "var(--ink-muted)" };
}

function textWidth(text, style) {
  return [...text].reduce((width, character) => {
    // The finite vocabulary allows Unicode. Reserve an em for fallback glyphs,
    // two for emoji, and no extra advance for combining marks.
    const fallback = /\p{Mark}/u.test(character) ? 0 : /\p{Extended_Pictographic}/u.test(character) ? 2 : 1;
    const advance = style.face === "mono"
      ? (character.codePointAt(0) < 384 ? 0.6 : fallback)
      : (FONT_ADVANCES[style.face][character] ?? fallback);
    return width + advance * style.size;
  }, 0);
}

function wrapAt(text, style, maxWidth) {
  if (!text) return [];
  const lines = [];
  let line = "";
  for (const word of text.split(" ")) {
    if (line && textWidth(`${line} ${word}`, style) > maxWidth) { lines.push(line); line = ""; }
    // Split only an overlong token, at grapheme boundaries (never inside an accent
    // or emoji sequence). The canonical source keeps the original logical lines.
    const graphemes = [...new Intl.Segmenter(undefined, { granularity: "grapheme" }).segment(word)].map((s) => s.segment);
    if (line) line += " ";
    for (const grapheme of graphemes) {
      if (line && textWidth(line + grapheme, style) > maxWidth) { lines.push(line); line = ""; }
      line += grapheme;
    }
  }
  if (line) lines.push(line);
  return lines;
}

function wrapText(text, style, maxWidth) {
  let lines = wrapAt(text, style, maxWidth);
  // Balance word-wrapped lines without adding lines. Avoid a long first line
  // and a stranded last word, particularly in centered storage/surface facts.
  if (lines.length > 1 && text.includes(" ")) {
    let low = Math.max(...text.split(" ").map((word) => textWidth(word, style)));
    let high = maxWidth;
    const count = lines.length;
    while (high - low > 0.5) {
      const width = (low + high) / 2;
      const candidate = wrapAt(text, style, width);
      if (candidate.length > count) low = width;
      else { high = width; lines = candidate; }
    }
  }
  return lines;
}

function lineMetrics(style) {
  // hhea metrics of the packaged fonts. Baselines are computed from line boxes,
  // so one-line, wrapped, and mixed-face blocks share balanced vertical padding.
  const metrics = FONT_VERTICAL[style.face === "mono" && style.weight === 700 ? "monoBold" : style.face];
  return { ascent: style.size * metrics.ascent, descent: style.size * metrics.descent };
}

function textBlock(parts, maxWidth) {
  const lines = [];
  let height = 0;
  for (const [part, { value, style }] of parts.entries()) {
    for (const [index, text] of wrapText(value, style, maxWidth).entries()) {
      if (lines.length) height += part && index === 0 ? TYPE.factGap : TYPE.lineGap;
      const { ascent, descent } = lineMetrics(style);
      lines.push({ text, style, baseline: height + ascent });
      height += ascent + descent;
    }
  }
  return { lines, height, width: Math.max(...lines.map((line) => textWidth(line.text, line.style))) };
}

function nodeSize(node) {
  const marked = ["gate", "receipt", "wait"].includes(node.kind);
  const block = textBlock([
    { value: node.label, style: textStyle(node.kind) },
    { value: node.fact || "", style: textStyle(node.kind, true) },
  ], TYPE.maxWidth);
  const cap = node.kind === "store" ? 12 : 0;
  return { width: Math.ceil(Math.max(96, block.width + TYPE.padding * 2 + (marked ? TYPE.markInset : 0)) / 8) * 8,
    height: Math.ceil((block.height + 26 + cap) / 8) * 8, block, cap };
}

function drawNode(document, node, data) {
  const { x, y, width, height } = node;
  const { kind } = data;
  const group = element(document, "g", { class: `node tin-diagram-${kind}`, "data-id": data.id,
    "aria-label": [data.label, data.fact].filter(Boolean).join(" · ") });
  // Neutral node roles are conveyed by labels and shape, not unexplained shades.
  const fills = { step: "var(--paper-card)", surface: "var(--paper-card)", store: "var(--paper-card)",
    gate: "var(--diagram-gate-wash)", receipt: "var(--diagram-receipt-wash)", wait: "none", ghost: "none" };
  const strokes = { step: "var(--card-border)", surface: "var(--card-border)", ghost: "var(--diagram-ghost-border)" };
  const shape = element(document, "rect", { x, y, width, height, rx: 8, ry: 8,
    fill: kind === "store" ? "none" : fills[kind], stroke: strokes[kind] || "none", "stroke-width": 0.75 });
  if (kind === "ghost") shape.setAttribute("stroke-dasharray", "3 3");
  group.append(shape);
  if (kind === "store") {
    group.append(element(document, "path", {
      d: `M${x},${y + 6} L${x},${y + height - 6} A${width / 2},6 0 0 0 ${x + width},${y + height - 6} L${x + width},${y + 6}`,
      fill: fills.store, stroke: "var(--card-border)", "stroke-width": 0.75,
    }));
    group.append(element(document, "ellipse", { cx: x + width / 2, cy: y + 6, rx: width / 2, ry: 6,
      fill: fills.store, stroke: "var(--card-border)", "stroke-width": 0.75 }));
  }
  const marked = ["gate", "receipt", "wait"].includes(kind);
  const { block, cap } = nodeSize(data);
  const textX = marked ? x + TYPE.padding + TYPE.markInset : x + width / 2;
  const textTop = y + cap + (height - cap - block.height) / 2;
  const labelY = textTop + block.lines[0].baseline;
  if (marked) {
    const color = kind === "gate" ? "var(--hot)" : kind === "receipt" ? "var(--accent-deep)" : "var(--ink-muted)";
    group.append(element(document, "circle", { cx: x + TYPE.padding, cy: labelY - 4, r: kind === "wait" ? 4 : 3.5,
      fill: kind === "wait" ? "none" : color, stroke: kind === "wait" ? color : "none", "stroke-width": 1.2 }));
  }
  const text = element(document, "text", { x: textX, "text-anchor": marked ? "start" : "middle" });
  for (const { text: value, style, baseline } of block.lines) {
    text.append(element(document, "tspan", { x: textX, y: textTop + baseline,
      "font-family": style.face === "mono" ? "var(--diagram-mono)" : "var(--diagram-sans)",
      "font-size": style.size, "font-weight": style.weight, fill: style.color }, value));
  }
  group.append(text);
  return group;
}

function readPoints(edge) {
  return edge.getAttribute("points").trim().split(/\s+/).map((pair) => {
    const [x, y] = pair.split(",").map(Number);
    return { x, y };
  }).filter((point, index, points) => index === 0 || point.x !== points[index - 1].x || point.y !== points[index - 1].y);
}

function distance(a, b) { return Math.hypot(b.x - a.x, b.y - a.y); }

// Walk the route rather than assuming the first/last segment is long enough.
function trimStart(points, gap) {
  while (points.length > 1) {
    const length = distance(points[0], points[1]);
    if (length > gap) {
      points[0] = {
        x: points[0].x + (points[1].x - points[0].x) * gap / length,
        y: points[0].y + (points[1].y - points[0].y) * gap / length,
      };
      break;
    }
    points.shift();
    gap -= length;
  }
  return points;
}

function styleEdge(edge) {
  let points = readPoints(edge);
  points = trimStart(points, GEOMETRY.sourceGap);
  points = trimStart(points.reverse(), GEOMETRY.targetGap).reverse();
  if (points.length < 2) throw new Error("Diagram connector has insufficient clearance.");
  const tip = points.at(-1);
  const before = points.at(-2);
  const length = distance(before, tip);
  const dx = (tip.x - before.x) / length;
  const dy = (tip.y - before.y) / length;
  const base = { x: tip.x - dx * GEOMETRY.arrowLength, y: tip.y - dy * GEOMETRY.arrowLength };
  const arrowhead = edge.ownerDocument.createElementNS("http://www.w3.org/2000/svg", "polyline");
  setAttributes(arrowhead, {
    class: "tin-diagram-arrow", "data-from": edge.getAttribute("data-from"), "data-to": edge.getAttribute("data-to"),
    points: `${base.x - dy * GEOMETRY.arrowHalfWidth},${base.y + dx * GEOMETRY.arrowHalfWidth} ${tip.x},${tip.y} ${base.x + dy * GEOMETRY.arrowHalfWidth},${base.y - dx * GEOMETRY.arrowHalfWidth}`,
    fill: "none", stroke: EDGE_COLOR, "stroke-width": "1",
    "stroke-linecap": "round", "stroke-linejoin": "round",
  });
  edge.removeAttribute("marker-start");
  edge.removeAttribute("marker-end");
  setAttributes(edge, {
    points: points.map(({ x, y }) => `${x},${y}`).join(" "),
    stroke: EDGE_COLOR, "stroke-width": "1", "stroke-linejoin": "round", "stroke-linecap": "round",
  });
  edge.after(arrowhead);
}

async function renderSource(source) {
  const flow = parseSource(source);
  layoutEngine ||= new ELK();
  const edges = new Map(flow.edges.map((edge, index) => [`@edge:${index}`, edge]));
  const labelBlocks = new Map([...edges].filter(([, edge]) => edge.label).map(([id, edge]) =>
    [id, textBlock([{ value: edge.label, style: { face: "mono", size: 10, weight: 400 } }], TYPE.edgeMaxWidth)]));
  // Measure Tin typography before layout. Flat flows use the public ELK API;
  // compositions keep their authored hierarchy and use the libavoid adapter.
  const graph = flow.groups ? await layoutComposition(flow, nodeSize, labelBlocks, (label) => textWidth(label, { face: "bold", size: 13 })) : await layoutEngine.layout({
    id: "@diagram",
    layoutOptions: {
      "elk.algorithm": "layered", "elk.direction": flow.direction === "LR" ? "RIGHT" : "DOWN",
      "elk.edgeRouting": "ORTHOGONAL", "elk.padding": "[top=8,left=8,bottom=8,right=8]",
      "elk.spacing.nodeNode": "32", "elk.spacing.edgeNode": "24",
      "elk.layered.spacing.nodeNodeBetweenLayers": "48",
      "elk.layered.spacing.edgeNodeBetweenLayers": "20",
      "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
      "elk.layered.cycleBreaking.strategy": "MODEL_ORDER",
      "elk.layered.nodePlacement.bk.fixedAlignment": "BALANCED",
    },
    children: flow.nodes.map((node) => { const { width, height } = nodeSize(node); return { id: node.id, width, height }; }),
    edges: [...edges].map(([id, edge]) => ({ id, sources: [edge.from], targets: [edge.to],
      labels: edge.label ? [{ text: edge.label, width: labelBlocks.get(id).width + 20, height: labelBlocks.get(id).height + 12,
        layoutOptions: { "elk.edgeLabels.inline": "true" } }] : [] })),
  });
  const document = new DOMParser().parseFromString(`<svg xmlns="${SVG_NS}"/>`, "image/svg+xml");
  const svg = document.documentElement;
  setAttributes(svg, { viewBox: `0 0 ${graph.width} ${graph.height}`, role: "img", "aria-label": "Workflow diagram",
    "data-composed": flow.groups ? "true" : "false", preserveAspectRatio: "xMidYMid meet", style: `--diagram-width:${graph.width}px` });
  if (flow.brand) {
    svg.setAttribute("data-tin-brand", flow.brand.sha256);
    svg.setAttribute("data-brand-revision", flow.brand.revision);
    svg.setAttribute("style", `${svg.getAttribute("style")};${brandStyles(flow.brand)}`);
  }
  for (const group of graph.groups || []) {
    const frame = element(document, "g", { class: "tin-diagram-group", "data-group-id": group.id });
    if (group.kind === "frame") frame.append(element(document, "rect", { x: group.x, y: group.y, width: group.width, height: group.height, rx: 12, fill: "var(--diagram-frame-fill)", stroke: "var(--card-border)", "stroke-width": 0.75 }));
    frame.append(element(document, "text", { x: group.x + (group.kind === "frame" ? 24 : 0), y: group.y + (group.kind === "frame" ? 36 : 20), "font-family": "var(--diagram-sans)", "font-size": 13, "font-weight": 700, fill: "var(--ink-secondary)" }, group.label));
    svg.append(frame);
  }
  // Draw connectors behind labels and nodes; open chevrons avoid document-wide
  // marker IDs and inherit the theme of this SVG when multiple diagrams coexist.
  for (const edge of graph.edges) {
    const data = edges.get(edge.id);
    const section = edge.sections?.[0];
    if (!section) throw new Error("Diagram layout returned an unrouted connector.");
    const route = [section.startPoint, ...(section.bendPoints || []), section.endPoint];
    const line = element(document, "polyline", { class: "edge", "data-from": data.from, "data-to": data.to,
      fill: "none", points: route.map(({ x, y }) => `${x},${y}`).join(" ") });
    if (data.kind === "signal") line.setAttribute("stroke-dasharray", "4 3");
    svg.append(line);
    styleEdge(line);
  }
  for (const edge of graph.edges) {
    for (const label of edge.labels || []) {
      const data = edges.get(edge.id);
      const group = element(document, "g", { class: "edge-label", "aria-label": label.text,
        "data-from": data.from, "data-to": data.to });
      group.append(element(document, "rect", { x: label.x, y: label.y, width: label.width, height: label.height,
        rx: 4, fill: "var(--paper-card)" }));
      const text = element(document, "text", { x: label.x + label.width / 2,
        "text-anchor": "middle", "font-family": "var(--diagram-mono)", "font-size": 10, "font-weight": 400,
        fill: "var(--ink-secondary)" });
      for (const line of labelBlocks.get(edge.id).lines) text.append(element(document, "tspan", {
        x: label.x + label.width / 2, y: label.y + 6 + line.baseline }, line.text));
      group.append(text);
      svg.append(group);
    }
  }
  const nodes = new Map(flow.nodes.map((node) => [node.id, node]));
  for (const node of graph.children) svg.append(drawNode(document, node, nodes.get(node.id)));
  return new XMLSerializer().serializeToString(svg);
}

async function renderFlow(flow) {
  const source = sourceForFlow(flow);
  return { source, svg: await renderSource(source) };
}

function renderASCII(source) {
  const flow = parseSource(source);
  if (flow.groups) {
    // The ASCII view preserves group membership and every relationship. The
    // dependency's compound ASCII layout cannot route all nested return paths.
    const items = new Map([...flow.nodes, ...flow.groups].map((item) => [item.id, item]));
    const lines = [];
    function outline(ids, prefix = "") {
      ids.forEach((id, index) => {
        const item = items.get(id), last = index === ids.length - 1;
        lines.push(`${prefix}${last ? "└─" : "├─"} ${item.children ? `${item.label || item.id} [${item.direction}]` : `${item.label} [${item.kind}]${item.fact ? ` · ${item.fact}` : ""}`}`);
        if (item.children) outline(item.children, `${prefix}${last ? "   " : "│  "}`);
      });
    }
    outline(flow.layout); lines.push("", "Connections");
    for (const edge of flow.edges) lines.push(`${items.get(edge.from).label} ${edge.kind === "signal" ? "┄┄▷" : "──▷"} ${items.get(edge.to).label}${edge.label ? ` · ${edge.label}` : ""}`);
    return lines.join("\n");
  }
  return renderMermaidASCII(sourceForFlow({ ...flow, brand: undefined }), { useAscii: false });
}

const TinDiagramRenderer = Object.freeze({
  parseSource,
  renderASCII,
  renderFlow,
  renderSource,
  sourceForFlow,
});

if (typeof window !== "undefined") window.TinDiagramRenderer = TinDiagramRenderer;

export { parseSource, renderASCII, renderFlow, renderSource, sourceForFlow };
