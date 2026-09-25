import { PREFIX, validateBrand, parseBrand } from "./diagram-brand.js";
// The same bounded source contract is validated on the server before publication.
const KINDS = new Set([
  "step",
  "surface",
  "store",
  "wait",
  "gate",
  "receipt",
  "ghost",
]);
const DIRECTIONS = new Set(["LR", "TD", "RL", "BT"]);
const ID = /^[a-z][a-z0-9_]*$/;
const NODE =
  /^\s*([a-z][a-z0-9_]*)(?:\[\("([^"]+)"\)\]|\["([^"]+)"\]):::(step|surface|store|wait|gate|receipt|ghost)\s*$/;
const EDGE =
  /^\s*([a-z][a-z0-9_]*)\s*(-->|-\.->)(?:\|([^|]+)\|)?\s*([a-z][a-z0-9_]*)\s*$/;
const GROUP = /^subgraph ([a-z][a-z0-9_]*)(?:\["([^"]+)"\])?$/;

function text(value, limit, empty = false) {
  if (
    typeof value !== "string" ||
    (!value && !empty) ||
    value !== value.trim() ||
    value.length > limit ||
    /[&<>"`{|};\r\n]/.test(value)
  )
    throw new Error("Diagram text is invalid.");
  return value;
}

function validateFlow(flow) {
  const composed = Boolean(flow?.groups);
  if (flow?.brand !== undefined) validateBrand(flow.brand);
  if (
    !flow ||
    !DIRECTIONS.has(flow.direction) ||
    (!composed && !["LR", "TD"].includes(flow.direction))
  )
    throw new Error("Diagram direction is invalid.");
  if (
    !Array.isArray(flow.nodes) ||
    flow.nodes.length < 2 ||
    flow.nodes.length > (composed ? 32 : 8)
  )
    throw new Error("Diagram node count is invalid.");
  if (
    !Array.isArray(flow.edges) ||
    flow.edges.length < 1 ||
    flow.edges.length > (composed ? 48 : 12)
  )
    throw new Error("Diagram edge count is invalid.");
  const ids = new Set();
  for (const node of flow.nodes) {
    if (
      typeof node.id !== "string" ||
      !ID.test(node.id) ||
      ids.has(node.id) ||
      !KINDS.has(node.kind)
    )
      throw new Error("Diagram node is invalid.");
    ids.add(node.id);
    text(node.label, composed ? 80 : 32);
    text(node.fact || "", composed ? 240 : 48, true);
  }
  if (composed) {
    if (
      !Array.isArray(flow.groups) ||
      flow.groups.length < 1 ||
      flow.groups.length > 16 ||
      !Array.isArray(flow.layout)
    )
      throw new Error("Diagram groups are invalid.");
    for (const group of flow.groups) {
      if (
        typeof group.id !== "string" ||
        !ID.test(group.id) ||
        ids.has(group.id) ||
        !DIRECTIONS.has(group.direction) ||
        !["frame", "lane", "layout"].includes(group.kind) ||
        !Array.isArray(group.children) ||
        !group.children.length
      )
        throw new Error("Diagram group is invalid.");
      ids.add(group.id);
      text(group.label || "", 80, group.kind === "layout");
    }
    const groups = new Map(flow.groups.map((g) => [g.id, g]));
    const visited = new Set();
    function visit(items, depth) {
      if (depth > 4) throw new Error("Diagram groups exceed four levels.");
      for (const id of items) {
        if (!ids.has(id) || visited.has(id))
          throw new Error("Diagram layout has duplicate or unknown children.");
        visited.add(id);
        if (groups.has(id)) visit(groups.get(id).children, depth + 1);
      }
    }
    visit(flow.layout, 0);
    if (visited.size !== ids.size)
      throw new Error("Every diagram item must belong to the layout.");
  }
  const endpoints = new Set(flow.nodes.map((node) => node.id));
  const pairs = new Set(),
    adjacent = new Map([...ids].map((id) => [id, []]));
  for (const edge of flow.edges) {
    const pair = `${edge.from}:${edge.to}`;
    if (
      !endpoints.has(edge.from) ||
      !endpoints.has(edge.to) ||
      (!composed && edge.from === edge.to) ||
      pairs.has(pair) ||
      !["call", "signal"].includes(edge.kind || "call")
    )
      throw new Error("Diagram edge is invalid.");
    text(edge.label || "", composed ? 96 : 40, true);
    pairs.add(pair);
    adjacent.get(edge.from).push(edge.to);
    adjacent.get(edge.to).push(edge.from);
  }
  if (!composed) {
    const seen = new Set(),
      queue = [flow.nodes[0].id];
    while (queue.length) {
      const id = queue.pop();
      if (seen.has(id)) continue;
      seen.add(id);
      queue.push(...adjacent.get(id));
    }
    if (seen.size !== ids.size) throw new Error("Diagram must be connected.");
  }
  return flow;
}

function sourceForFlow(flow) {
  validateFlow(flow);
  const lines = [`graph ${flow.direction}`];
  if (flow.brand) lines.push(`  ${PREFIX}${JSON.stringify(flow.brand)}`);
  const nodes = new Map(flow.nodes.map((node) => [node.id, node]));
  const groups = new Map((flow.groups || []).map((group) => [group.id, group]));
  if (flow.groups) lines.push("  %% tin:composition");
  function emit(id, indent) {
    const group = groups.get(id);
    if (group) {
      lines.push(
        `${indent}subgraph ${id}${group.label ? `["${group.label}"]` : ""}`,
        `${indent}  direction ${group.direction}`,
        `${indent}  %% tin:group ${group.kind}`,
      );
      for (const child of group.children) emit(child, `${indent}  `);
      lines.push(`${indent}end`);
      return;
    }
    const node = nodes.get(id),
      label = [node.label, node.fact].filter(Boolean).join("<br/>");
    lines.push(
      `${indent}${id}${node.kind === "store" ? `[("${label}")]` : `["${label}"]`}:::${node.kind}`,
    );
  }
  for (const id of flow.layout || flow.nodes.map((node) => node.id))
    emit(id, "  ");
  for (const edge of flow.edges)
    lines.push(
      `  ${edge.from} ${edge.kind === "signal" ? "-.->" : "-->"}${edge.label ? `|${edge.label}|` : ""} ${edge.to}`,
    );
  return `${lines.join("\n")}\n`;
}

function parseSource(source) {
  if (typeof source !== "string" || source.length > 64000)
    throw new Error("Diagram source is too large.");
  const lines = source.replace(/\r\n?/g, "\n").trimEnd().split("\n");
  const header = /^graph (LR|TD|RL|BT)$/.exec(lines.shift()?.trim() || "");
  if (!header) throw new Error("Tin diagrams start with graph LR or graph TD.");
  const flow = { direction: header[1], nodes: [], edges: [] },
    stack = [],
    layout = [];
  if (lines[0]?.trim().startsWith(PREFIX)) flow.brand = parseBrand(lines.shift().trim());
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) continue;
    if (
      line === "%% tin:composition" &&
      !flow.groups &&
      !flow.nodes.length &&
      !flow.edges.length
    ) {
      flow.groups = [];
      flow.layout = layout;
      continue;
    }
    const group = GROUP.exec(line);
    if (group && flow.groups) {
      const item = {
        id: group[1],
        label: group[2] || "",
        direction: flow.direction,
        kind: group[2] ? "frame" : "layout",
        children: [],
      };
      (stack.at(-1)?.children || layout).push(item.id);
      flow.groups.push(item);
      stack.push(item);
      continue;
    }
    if (line === "end" && stack.length) {
      stack.pop();
      continue;
    }
    const direction = /^direction (LR|TD|RL|BT)$/.exec(line);
    if (direction && stack.length && !stack.at(-1).children.length) {
      stack.at(-1).direction = direction[1];
      continue;
    }
    const kind = /^%% tin:group (frame|lane|layout)$/.exec(line);
    if (kind && stack.length && !stack.at(-1).children.length) {
      stack.at(-1).kind = kind[1];
      continue;
    }
    const node = NODE.exec(line);
    if (node) {
      if ((node[4] === "store") !== Boolean(node[2]))
        throw new Error("Diagram stores must use the cylinder shape.");
      const parts = (node[2] || node[3]).split(/<br\s*\/>/i);
      if (parts.length > 2)
        throw new Error("Diagram nodes may have at most two logical lines.");
      flow.nodes.push({
        id: node[1],
        kind: node[4],
        label: parts[0],
        fact: parts[1] || "",
      });
      (stack.at(-1)?.children || layout).push(node[1]);
      continue;
    }
    const edge = EDGE.exec(line);
    if (edge && !stack.length) {
      flow.edges.push({
        from: edge[1],
        to: edge[4],
        kind: edge[2] === "-.->" ? "signal" : "call",
        label: edge[3] || "",
      });
      continue;
    }
    throw new Error(
      "Diagram source uses syntax outside the Tin diagram vocabulary.",
    );
  }
  if (stack.length) throw new Error("Diagram group is not closed.");
  return validateFlow(flow);
}

export { parseSource, sourceForFlow, validateFlow };
