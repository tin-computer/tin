"use strict";

// A workflow's presentation flow, read top to bottom along one vertical spine,
// for the narrow diagram panel. Nodes are ordinary HTML in reading order, so
// text wraps and a screen reader hears the run in sequence. Each row holds one
// node on the spine, or two that run side by side either side of it. The
// connectors are drawn over the laid-out rows: straight down the spine, split
// and merged around a pair, a loop back up the left rail, and a skip down the
// right one.
(() => {
  const SVG_NS = "http://www.w3.org/2000/svg";
  const SOURCE_GAP = 6; // a line starts this far below its source
  const TARGET_GAP = 10; // and its arrow stops this far above its target
  const CORNER = 6;
  const PAIR = 2;

  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);

  // Rows from the graph: loops are found first (an edge back to a node still
  // being walked), the rest is layered by its longest path from the start, and
  // a layer wider than a pair continues on the next row.
  function layout(flow) {
    const nodes = flow.nodes;
    const outgoing = new Map(nodes.map((node) => [node.id, []]));
    const incoming = new Map(nodes.map((node) => [node.id, []]));
    for (const edge of flow.edges) {
      outgoing.get(edge.from)?.push(edge);
      incoming.get(edge.to)?.push(edge);
    }
    const loops = new Set();
    const walked = new Map();
    const walk = (id) => {
      walked.set(id, "open");
      for (const edge of outgoing.get(id)) {
        const seen = walked.get(edge.to);
        if (seen === "open") loops.add(edge);
        else if (!seen) walk(edge.to);
      }
      walked.set(id, "done");
    };
    const starts = nodes.filter((node) => !incoming.get(node.id).length);
    for (const node of [...starts, ...nodes]) if (!walked.has(node.id)) walk(node.id);

    const forward = flow.edges.filter((edge) => !loops.has(edge));
    const depth = new Map(nodes.map((node) => [node.id, 0]));
    const pending = new Map(nodes.map((node) => [node.id, 0]));
    for (const edge of forward) pending.set(edge.to, pending.get(edge.to) + 1);
    const ready = nodes.filter((node) => !pending.get(node.id)).map((node) => node.id);
    while (ready.length) {
      const id = ready.shift();
      for (const edge of forward.filter((item) => item.from === id)) {
        depth.set(edge.to, Math.max(depth.get(edge.to), depth.get(id) + 1));
        pending.set(edge.to, pending.get(edge.to) - 1);
        if (!pending.get(edge.to)) ready.push(edge.to);
      }
    }

    const layers = [];
    nodes.forEach((node, index) => {
      const at = depth.get(node.id);
      (layers[at] ||= []).push({ node, index });
    });
    const column = new Map();
    const rows = [];
    for (const layer of layers.filter(Boolean)) {
      // Keep a branch on the side its parent sits.
      layer.sort((a, b) => {
        const lean = (item) => {
          const parents = forward.filter((edge) => edge.to === item.node.id && column.has(edge.from));
          return parents.length ? parents.reduce((sum, edge) => sum + column.get(edge.from), 0) / parents.length : 0;
        };
        return lean(a) - lean(b) || a.index - b.index;
      });
      for (let start = 0; start < layer.length; start += PAIR) {
        const row = layer.slice(start, start + PAIR).map((item) => item.node);
        row.forEach((node, place) => column.set(node.id, row.length === 1 ? 0 : place === 0 ? -1 : 1));
        rows.push(row);
      }
    }
    const rowOf = new Map();
    rows.forEach((row, index) => row.forEach((node) => rowOf.set(node.id, index)));
    return { rows, rowOf, loops };
  }

  function nodeHtml(node) {
    const fact = node.fact ? `<code>${escapeHtml(node.fact)}</code>` : "";
    const label = `<strong>${escapeHtml(node.label)}</strong>`;
    const attributes = `class="spine-node is-${escapeHtml(node.kind)}" data-spine-node="${escapeHtml(node.id)}"`;
    if (node.kind === "wait") {
      return `<div ${attributes}><span class="spine-wait-mark" aria-hidden="true"></span><span class="spine-wait-text">${label}${fact}</span></div>`;
    }
    if (node.kind === "gate") {
      // The only bermellón: a dot before the title, never a label above it.
      return `<div ${attributes}><strong><span class="spine-dot" aria-hidden="true"></span>${escapeHtml(node.label)}</strong>${fact}</div>`;
    }
    if (node.kind === "receipt") {
      return `<div ${attributes}><strong><span class="spine-dot" aria-hidden="true"></span>${escapeHtml(node.label)}</strong>${fact}</div>`;
    }
    if (node.kind === "store") {
      // A store is an ordinary card with a file mark in its corner: something Tin keeps.
      return `<div ${attributes}><svg class="spine-node-icon" viewBox="0 0 12 14" aria-hidden="true"><path d="M2.5 0.75h4.75l3.5 3.5v8a1 1 0 0 1-1 1h-7.25a1 1 0 0 1-1-1v-10.5a1 1 0 0 1 1-1z"/><path d="M7.25 0.75v3.5h3.5"/></svg>${label}${fact}</div>`;
    }
    return `<div ${attributes}>${label}${fact}</div>`;
  }

  function render(flow, { trigger = "" } = {}) {
    const { rows, rowOf, loops } = layout(flow);
    const root = document.createElement("div");
    root.className = "spine-diagram";
    const triggerRow = trigger
      ? `<div class="spine-row is-trigger"><div class="spine-trigger" data-spine-node="__trigger"><span class="spine-trigger-mark" aria-hidden="true"></span>${escapeHtml(trigger)}</div></div>`
      : "";
    const labelled = new Set(flow.edges.filter((edge) => edge.label && !loops.has(edge)).map((edge) => rowOf.get(edge.to)));
    root.innerHTML = `<ol class="spine-rows" role="list">${triggerRow}${rows.map((row, index) => `<li class="spine-row ${row.length > 1 ? "is-pair" : "is-single"}${labelled.has(index) ? " has-edge-label" : ""}">${row.map(nodeHtml).join("")}</li>`).join("")}</ol>
      <svg class="spine-edges" aria-hidden="true"><defs></defs></svg>
      <div class="spine-labels" aria-hidden="true"></div>`;
    const edges = flow.edges.map((edge) => ({ ...edge, loop: loops.has(edge) }));
    if (trigger) {
      for (const node of rows[0] || []) edges.unshift({ from: "__trigger", to: node.id, kind: "signal", label: "", loop: false, trigger: true });
    }
    const triggerOffset = trigger ? 1 : 0;
    const draw = () => {
      // A label that wraps moves its row down, so draw again once the rows settle.
      for (let pass = 0; pass < 3; pass += 1) {
        if (!drawEdges(root, edges, (id) => id === "__trigger" ? 0 : rowOf.get(id) + triggerOffset)) break;
      }
    };
    const observer = new ResizeObserver(draw);
    observer.observe(root);
    return { element: root, redraw: draw, dispose: () => observer.disconnect() };
  }

  function box(root, id, origin) {
    const element = root.querySelector(`[data-spine-node="${CSS.escape(id)}"]`);
    if (!element) return null;
    const rect = (element.querySelector(".spine-wait-mark") || element).getBoundingClientRect();
    return {
      id,
      // A wait is joined at its dot, but its words start higher and reach further right.
      textTop: element.getBoundingClientRect().top - origin.top,
      textRight: element.getBoundingClientRect().right - origin.left,
      left: rect.left - origin.left,
      right: rect.right - origin.left,
      top: rect.top - origin.top,
      bottom: rect.bottom - origin.top,
      x: rect.left - origin.left + rect.width / 2,
      y: rect.top - origin.top + rect.height / 2,
    };
  }

  // An orthogonal path with softened corners.
  function route(points) {
    let path = `M ${points[0][0]} ${points[0][1]}`;
    for (let index = 1; index < points.length; index += 1) {
      const [x, y] = points[index];
      const next = points[index + 1];
      if (!next) {
        path += ` L ${x} ${y}`;
        break;
      }
      const [px, py] = points[index - 1];
      const inLength = Math.hypot(x - px, y - py);
      const outLength = Math.hypot(next[0] - x, next[1] - y);
      const radius = Math.min(CORNER, inLength / 2, outLength / 2);
      if (radius < 0.5) {
        path += ` L ${x} ${y}`;
        continue;
      }
      const before = [x - ((x - px) / inLength) * radius, y - ((y - py) / inLength) * radius];
      const after = [x + ((next[0] - x) / outLength) * radius, y + ((next[1] - y) / outLength) * radius];
      path += ` L ${before[0]} ${before[1]} Q ${x} ${y} ${after[0]} ${after[1]}`;
    }
    return path;
  }

  function arrow(x, y, direction) {
    // An open chevron, drawn with the same stroke as the line it ends.
    const size = 4.5;
    if (direction === "down") return `M ${x - size} ${y - size} L ${x} ${y} L ${x + size} ${y - size}`;
    if (direction === "right") return `M ${x - size} ${y - size} L ${x} ${y} L ${x - size} ${y + size}`;
    return `M ${x + size} ${y - size} L ${x} ${y} L ${x + size} ${y + size}`;
  }

  function drawEdges(root, edges, rowIndex) {
    const svg = root.querySelector(".spine-edges");
    const labels = root.querySelector(".spine-labels");
    if (!svg || !root.isConnected) return false;
    const origin = root.getBoundingClientRect();
    svg.setAttribute("viewBox", `0 0 ${origin.width} ${origin.height}`);
    svg.setAttribute("width", String(origin.width));
    svg.setAttribute("height", String(origin.height));
    const rowElements = [...root.querySelectorAll(".spine-row")];
    const rowBoxes = rowElements.map((row) => {
      const rect = row.getBoundingClientRect();
      return { top: rect.top - origin.top, bottom: rect.bottom - origin.top };
    });
    const nodeBoxes = [...root.querySelectorAll("[data-spine-node]")].map((element) => ({
      ...box(root, element.dataset.spineNode, origin),
      row: rowIndex(element.dataset.spineNode),
    }));
    const leftmost = Math.min(...nodeBoxes.map((item) => item.left));
    const rightmost = Math.max(...nodeBoxes.map((item) => item.textRight));
    const leftRail = Math.max(4, leftmost - 16);
    const rightRail = Math.min(origin.width - 4, rightmost + 16);
    const hasLoops = edges.some((edge) => edge.loop);
    const skips = (edge) => !edge.loop && rowIndex(edge.to) > rowIndex(edge.from) + 1;
    const usesRightRail = edges.some((edge) => skips(edge) && box(root, edge.from, origin)?.x >= origin.width / 2 - 1);
    // A label beside a line keeps to its own lane: short of the next line in that row,
    // and of the right rail or the panel edge.
    const roomBeside = (to) => {
      const next = nodeBoxes.filter((item) => item.row === rowIndex(to.id) && item.x > to.x + 1).map((item) => item.x - 10);
      return Math.min(...next, usesRightRail ? rightRail - 8 : origin.width - 8) - (to.x + 8);
    };
    const wrapped = [];
    let paths = "";
    let heads = "";
    labels.innerHTML = "";
    const label = (text, x, y, side = "right") => {
      if (!text) return null;
      const item = document.createElement("span");
      item.className = `spine-edge-label is-${side}`;
      item.textContent = text;
      item.style.left = `${x}px`;
      item.style.top = `${y}px`;
      labels.append(item);
      return item;
    };
    for (const edge of edges) {
      const from = box(root, edge.from, origin);
      const to = box(root, edge.to, origin);
      if (!from || !to) continue;
      const dashed = edge.kind === "signal" ? ' stroke-dasharray="3 4"' : "";
      const sourceRow = rowIndex(edge.from);
      const targetRow = rowIndex(edge.to);
      if (edge.loop) {
        // Back up the left rail into the left side of the earlier node.
        const startY = from.y;
        const endY = to.y;
        paths += `<path d="${route([[from.left - SOURCE_GAP, startY], [leftRail, startY], [leftRail, endY], [to.left - TARGET_GAP, endY]])}"${dashed} />`;
        heads += `<path d="${arrow(to.left - TARGET_GAP, endY, "right")}" />`;
        label(edge.label, leftRail, (startY + endY) / 2, "rail");
        continue;
      }
      if (skips(edge)) {
        // Down the rail on the source's side, past the rows in between, and in
        // from that side.
        const onLeft = from.x < origin.width / 2 - 1;
        const rail = onLeft ? leftRail - (hasLoops ? 8 : 0) : rightRail;
        const startX = onLeft ? from.left - SOURCE_GAP : from.textRight + SOURCE_GAP;
        const endX = onLeft ? to.left - TARGET_GAP : to.textRight + TARGET_GAP;
        paths += `<path d="${route([[startX, from.y], [rail, from.y], [rail, to.y], [endX, to.y]])}"${dashed} />`;
        heads += `<path d="${arrow(endX, to.y, onLeft ? "right" : "left")}" />`;
        label(edge.label, rail, (from.y + to.y) / 2, "skip");
        continue;
      }
      const startY = from.bottom + SOURCE_GAP;
      const endY = to.top - TARGET_GAP;
      const middle = rowBoxes[sourceRow].bottom + SOURCE_GAP + 8;
      const points = Math.abs(from.x - to.x) < 1
        ? [[from.x, startY], [to.x, endY]]
        : [[from.x, startY], [from.x, middle], [to.x, middle], [to.x, endY]];
      paths += `<path d="${route(points)}"${dashed} />`;
      heads += `<path d="${arrow(to.x, endY, "down")}" />`;
      const top = Math.max(startY, middle);
      const bottom = Math.min(endY, to.textTop - 4);
      const item = label(edge.label, to.x + 8, (top + bottom) / 2 + 1, "right");
      if (item) {
        item.style.maxWidth = `${Math.max(56, roomBeside(to))}px`;
        wrapped.push({ item, row: targetRow, space: bottom - top });
      }
    }
    svg.innerHTML = `<g class="spine-lines">${paths}</g><g class="spine-heads">${heads}</g>`;

    // A label that wraps to more lines than the gap holds pushes its row down by the
    // difference; the gap it measured already includes any room given last time.
    const room = new Map();
    for (const { item, row, space } of wrapped) {
      const given = parseFloat(rowElements[row].style.getPropertyValue("--spine-label-room")) || 0;
      const needed = Math.ceil(item.getBoundingClientRect().height + 4 - (space - given));
      if (needed > 0) room.set(row, Math.max(room.get(row) || 0, needed));
    }
    let moved = false;
    rowElements.forEach((row, index) => {
      const value = room.has(index) ? `${room.get(index)}px` : "";
      if (row.style.getPropertyValue("--spine-label-room") === value) return;
      if (value) row.style.setProperty("--spine-label-room", value);
      else row.style.removeProperty("--spine-label-room");
      moved = true;
    });
    return moved;
  }

  // What the panel's footer says about a flow.
  function summary(flow) {
    const count = (kind) => flow.nodes.filter((node) => node.kind === kind).length;
    const steps = flow.nodes.filter((node) => !["wait", "receipt", "ghost"].includes(node.kind)).length;
    return [
      `${steps} ${steps === 1 ? "step" : "steps"}`,
      count("gate") ? `${count("gate")} ${count("gate") === 1 ? "approval" : "approvals"}` : null,
      count("wait") ? `${count("wait")} ${count("wait") === 1 ? "wait" : "waits"}` : null,
    ].filter(Boolean).join(" · ");
  }

  window.TinWorkflowSpine = Object.freeze({ layout, render, summary });
})();
