"use strict";

(function defineTinJsonViewer() {
  // Match the canonical project-state text publication cap.
  const MAX_INLINE_BYTES = 1_000_000;
  const EXPANDED_LINE_LIMIT = 400;

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function parseText(text) {
    let value;
    try {
      value = JSON.parse(text);
    } catch (_error) {
      return { error: "not valid JSON, shown as text" };
    }
    // Breadth-first indexing and a reverse count avoid recursive traversal of
    // untrusted, deeply nested files. Count the expanded lines only once.
    const nodes = [{ value, key: null, depth: 0, comma: false }];
    for (let index = 0; index < nodes.length; index += 1) {
      const node = nodes[index];
      node.id = index;
      node.type = Array.isArray(node.value) ? "array"
        : node.value !== null && typeof node.value === "object" ? "object" : "scalar";
      node.children = [];
      if (node.type === "scalar") continue;
      const keys = Object.keys(node.value);
      keys.forEach((key, position) => {
        node.children.push(nodes.length);
        nodes.push({ value: node.value[key], key: node.type === "array" ? null : key,
          depth: node.depth + 1, comma: position < keys.length - 1 });
      });
    }
    for (let index = nodes.length - 1; index >= 0; index -= 1) {
      const node = nodes[index];
      node.lines = node.children.length
        ? 2 + node.children.reduce((sum, child) => sum + nodes[child].lines, 0) : 1;
    }
    const defaultFolded = [];
    let visibleLines = nodes[0].lines;
    const queue = [0];
    for (let index = 0; index < queue.length && visibleLines > EXPANDED_LINE_LIMIT; index += 1) {
      const node = nodes[queue[index]];
      if (node.type === "array" && node.children.length) {
        defaultFolded.push(node.id);
        visibleLines -= node.lines - 1;
      } else {
        node.children.forEach((child) => queue.push(child));
      }
    }
    return { nodes, lines: nodes[0].lines, defaultFolded };
  }

  async function readResponse(response) {
    const declaredBytes = Number(response.headers.get("content-length")) || 0;
    const file = { kind: "json", bytes: declaredBytes, exactSize: true, text: null, json: null, tooLarge: false };
    if (declaredBytes > MAX_INLINE_BYTES) {
      file.tooLarge = true;
      await response.body?.cancel();
      return file;
    }
    const chunks = [];
    let bytes = 0;
    if (response.body) {
      const reader = response.body.getReader();
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          bytes += value.byteLength;
          if (bytes > MAX_INLINE_BYTES) {
            await reader.cancel();
            return { ...file, bytes: MAX_INLINE_BYTES, exactSize: false, tooLarge: true };
          }
          chunks.push(value);
        }
      } finally {
        reader.releaseLock();
      }
    } else {
      const buffer = await response.arrayBuffer();
      bytes = buffer.byteLength;
      if (bytes > MAX_INLINE_BYTES) return { ...file, bytes, tooLarge: true };
      chunks.push(new Uint8Array(buffer));
    }
    file.bytes = bytes;
    const buffer = new Uint8Array(bytes);
    let offset = 0;
    chunks.forEach((chunk) => { buffer.set(chunk, offset); offset += chunk.byteLength; });
    try {
      // Preserve a BOM, whitespace and line endings for the raw view and Copy.
      file.text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(buffer);
    } catch (_error) {
      return file;
    }
    file.json = parseText(file.text);
    return file;
  }

  function createViewState(file) {
    return { mode: "formatted", folded: new Set(file.json?.defaultFolded || []) };
  }

  function scalar(value) {
    return element("span", typeof value === "string" ? "project-json-string" : "project-json-scalar", JSON.stringify(value));
  }

  function row(depth) {
    const root = element("div", "project-json-row");
    const gutter = element("span", "project-json-gutter");
    const text = element("span", "project-json-text");
    // Clamp only pathological depths to leave a readable wrapping measure.
    text.style.paddingLeft = `min(${depth * 2}ch, 60%)`;
    root.append(gutter, text);
    return { root, gutter, text };
  }

  function drawTree(article, model, view, onFold) {
    const fragment = document.createDocumentFragment();
    const pending = [{ id: 0 }];
    while (pending.length) {
      const { id, closing } = pending.pop();
      const node = model.nodes[id];
      const rendered = row(node.depth);
      const { root, gutter, text } = rendered;
      const open = node.type === "array" ? "[" : "{";
      const close = node.type === "array" ? "]" : "}";
      if (closing) {
        text.textContent = close + (node.comma ? "," : "");
        fragment.append(root);
        continue;
      }
      if (node.key !== null) {
        text.append(element("span", "project-json-key", JSON.stringify(node.key)), ": ");
      }
      const foldable = node.children.length > 0;
      const folded = view.folded.has(id);
      if (foldable) {
        const button = element("button", "project-json-caret", folded ? "▸" : "▾");
        button.type = "button";
        button.dataset.jsonNode = String(id);
        button.setAttribute("aria-expanded", String(!folded));
        button.setAttribute("aria-label", `${folded ? "Expand" : "Collapse"} ${node.key === null ? node.type : node.key}`);
        button.addEventListener("click", () => {
          if (view.folded.has(id)) view.folded.delete(id);
          else view.folded.add(id);
          drawTree(article, model, view, onFold);
          onFold();
          article.querySelector(`[data-json-node="${id}"]`)?.focus({ preventScroll: true });
        });
        gutter.append(button);
      }
      if (node.type === "scalar") text.append(scalar(node.value));
      else if (!foldable) text.append(open + close);
      else if (folded) {
        text.append(open + " ");
        if (node.type === "object") {
          const first = model.nodes[node.children[0]];
          text.append(element("span", "project-json-key", JSON.stringify(first.key)), ": ");
          text.append(first.type === "scalar" ? scalar(first.value)
            : first.type === "array" ? "[…]" : "{…}");
          text.append(" ");
        }
        text.append(`… ${node.children.length.toLocaleString("en-US")} ${node.type === "array" ? "items" : "keys"} ${close}`);
      } else {
        text.append(open);
        pending.push({ id, closing: true });
        for (let index = node.children.length - 1; index >= 0; index -= 1) {
          pending.push({ id: node.children[index] });
        }
      }
      if ((!foldable || folded) && node.comma) text.append(",");
      fragment.append(root);
    }
    article.replaceChildren(fragment);
  }

  function mount(container, file, options) {
    const view = options.view;
    const formatted = Boolean(file.json && !file.json.error);
    const root = element("section", "project-file-view is-json");
    const bar = element("header", "project-file-context is-json");
    const location = element("div", "project-json-location");
    const back = element("button", "project-file-return", `← ${options.returnLabel || "files"}`);
    back.type = "button";
    back.addEventListener("click", options.onReturn);
    const separator = element("span", "project-file-context-separator");
    const path = options.pathElement || element("code", "project-file-path", options.contextLabel);
    path.title = options.contextLabel;
    location.append(back, separator, path);
    const controls = element("div", "project-json-controls");
    const facts = element("code", "project-file-facts");
    function updateFacts() {
      const detail = file.tooLarge ? "too large to preview" : file.json?.error
        || (file.text === null ? "preview unavailable" : view.mode === "raw" ? "raw"
          : `${file.json.lines.toLocaleString("en-US")} lines${view.folded.size ? " · folded" : ""}`);
      facts.textContent = `json · ${options.sizeLabel} · ${detail}`;
      const revision = element("span", "project-json-revision", ` · ${options.revisionLabel}`);
      facts.append(revision);
      facts.title = facts.textContent;
    }
    updateFacts();
    const actions = element("div", "project-json-actions");
    const body = element("div", "project-file-body project-json-body");
    const article = element("article", "project-file-text");
    const toggle = element("button", "project-file-mode-toggle");
    function drawBody() {
      const tree = formatted && view.mode !== "raw";
      article.classList.toggle("project-json-tree", tree);
      if (tree) drawTree(article, file.json, view, updateFacts);
      else article.replaceChildren(element("pre", "", file.text));
      toggle.textContent = tree ? "view raw" : "view formatted";
      updateFacts();
    }
    if (formatted) {
      toggle.type = "button";
      toggle.addEventListener("click", () => {
        view.mode = view.mode === "raw" ? "formatted" : "raw";
        drawBody();
      });
      actions.append(toggle);
    }
    if (file.text !== null) {
      const copy = element("button", "project-file-action project-json-copy", "Copy");
      copy.type = "button";
      copy.addEventListener("click", () => options.onCopy(copy));
      actions.append(copy);
      drawBody();
      body.append(article);
    } else {
      const information = element("article", "project-file-information");
      information.append(element("span", "", file.tooLarge
        ? "This file is too large to preview. Download raw to read it."
        : "This file cannot be previewed as text. Download raw to read it."));
      body.append(information);
    }
    const download = element("button", "project-file-action project-json-download");
    download.type = "button";
    download.append(element("span", "project-json-download-wide", "Download raw"),
      element("span", "project-json-download-narrow", "download"));
    download.setAttribute("aria-label", "Download raw");
    download.addEventListener("click", options.onDownload);
    actions.append(download);
    controls.append(facts, actions);
    bar.append(location, controls);
    root.append(bar, body);
    container.replaceChildren(root);
    return () => {};
  }

  window.TinJsonViewer = { MAX_INLINE_BYTES, parseText, readResponse, createViewState, mount };
})();
