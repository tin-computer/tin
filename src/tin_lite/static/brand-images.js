"use strict";

// Brand > Images: the project's image library (image_library.py), kept in project Files under
// brand/illustrations/. A grid filtered by kind, a search box, and on each image a download and
// a discard. Members of the project see it, the Tin team and the client's team alike; there is
// no public link. Images are fetched with the member's session: PNG and JPEG as typed Blobs,
// SVG as a data URL so it never runs as a page, and diagrams drawn by Tin's diagram renderer.
window.TinBrandImages = (() => {
  const KINDS = [
    { key: "", label: "All" },
    { key: "illustration", label: "Illustrations" },
    { key: "icon", label: "Icons" },
    { key: "photo", label: "Photos" },
    { key: "diagram", label: "Diagrams" },
  ];
  const TYPES = { png: "image/png", jpg: "image/jpeg", svg: "image/svg+xml" };
  const UNDO_MS = 48 * 3600 * 1000;

  let view = null;

  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function mount(main, options) {
    const fresh = !view || view.projectId !== options.projectId;
    if (fresh) {
      const query = new URLSearchParams(window.location.search);
      const kind = query.get("kind") || "";
      view = {
        ...options,
        kind: KINDS.some((item) => item.key === kind) ? kind : "",
        query: (query.get("q") || "").slice(0, 300),
        data: null,
        error: null,
        busy: new Set(),
        urls: new Map(),
        loadToken: 0,
      };
    } else {
      Object.assign(view, options);
    }
    view.main = main;
    view.active = true;
    render();
    load();
  }

  async function load() {
    const current = view;
    const token = ++current.loadToken;
    const query = new URLSearchParams();
    if (current.query) query.set("q", current.query);
    if (current.kind) query.set("kind", current.kind);
    try {
      const data = await current.api(`/api/projects/${encodeURIComponent(current.projectId)}/images?${query}`);
      if (view !== current || !current.active || token !== current.loadToken) return;
      current.data = data;
      current.error = null;
    } catch (error) {
      if (view !== current || !current.active || token !== current.loadToken) return;
      current.error = error.message;
    }
    render();
  }

  function remember() {
    const url = new URL(window.location.href);
    if (view.kind) url.searchParams.set("kind", view.kind);
    else url.searchParams.delete("kind");
    if (view.query) url.searchParams.set("q", view.query);
    else url.searchParams.delete("q");
    window.history.replaceState(null, "", url);
  }

  function render() {
    const main = view.main;
    if (!main.querySelector(".brand-view")) {
      main.innerHTML = `<section class="product-view workspace-view brand-view">
        <header class="workspace-header"><h1>Brand</h1><div class="header-spacer"></div></header>
        <div class="analytics-tabs brand-tabs">
          <div class="workflow-sections analytics-sections" role="tablist" aria-label="Brand">
            <button class="workflow-section is-active" type="button" role="tab" aria-selected="true">Images</button>
          </div>
          <input class="brand-search" type="search" placeholder="Search by subject or tag" aria-label="Search images" maxlength="300" data-brand-search />
        </div>
        <div class="brand-kinds" role="group" aria-label="Kind"></div>
        <div class="brand-body"></div>
      </section>`;
      const input = main.querySelector("[data-brand-search]");
      input.value = view.query;
      input.addEventListener("input", () => {
        window.clearTimeout(view.searchTimer);
        view.searchTimer = window.setTimeout(() => {
          view.query = input.value.trim();
          remember();
          load();
        }, 250);
      });
    }
    main.querySelector(".brand-kinds").innerHTML = KINDS.map((item) => `<button class="brand-kind${view.kind === item.key ? " is-active" : ""}" type="button" aria-pressed="${view.kind === item.key}" data-brand-kind="${item.key}">${item.label}</button>`).join("");
    main.querySelectorAll("[data-brand-kind]").forEach((button) => button.addEventListener("click", () => {
      view.kind = button.dataset.brandKind;
      remember();
      render();
      load();
    }));
    const body = main.querySelector(".brand-body");
    const data = view.data;
    if (view.error && !data) {
      body.innerHTML = `<p class="analytics-empty">${escape(view.error)}</p>`;
    } else if (!data) {
      body.innerHTML = `<p class="analytics-empty">Reading the library…</p>`;
    } else if (!data.images.length) {
      body.innerHTML = `<div class="analytics-empty"><p>${data.total ? "No image matches." : "No images yet."}</p><p>Your agent searches this library before it draws, and saves each new image here (search_images, generate_image).</p></div>`;
    } else {
      body.innerHTML = `<ul class="brand-grid">${data.images.map(card).join("")}</ul>`;
      bind(body, data);
    }
  }

  function card(image) {
    const created = Date.parse(image.created_at || "");
    const undo = Number.isFinite(created) && Date.now() - created < UNDO_MS;
    const retired = image.status === "retired";
    const date = Number.isFinite(created) ? new Date(created).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "";
    const busy = view.busy.has(image.id);
    return `<li class="brand-card${retired ? " is-retired" : ""}" data-brand-image="${escape(image.id)}">
      <div class="brand-thumb" data-brand-thumb="${escape(image.id)}"><span class="brand-thumb-wait">${escape(image.kind)}</span></div>
      <div class="brand-card-text">
        <strong title="${escape(image.subject)}">${escape(image.subject)}</strong>
        <span>${escape([image.kind, date, retired ? "retired" : ""].filter(Boolean).join(" · "))}</span>
      </div>
      <div class="brand-card-actions">
        <button class="button-quiet" type="button" data-brand-download="${escape(image.id)}">Download</button>
        ${retired ? "" : `<button class="button-quiet" type="button" data-brand-discard="${escape(image.id)}" ${busy ? "disabled" : ""} title="${undo ? "Removes it from the library. Files history keeps it." : "Past 48 hours: Tin keeps the file and marks it retired."}">${undo ? "Discard" : "Retire"}</button>`}
      </div>
    </li>`;
  }

  function mainFile(image) {
    const files = image.files || {};
    const extension = ["png", "jpg", "svg", "mmd"].find((item) => files[item]);
    return extension ? { extension, path: files[extension] } : null;
  }

  function bind(body, data) {
    data.images.forEach((image) => thumbnail(body, image, data.revision));
    body.querySelectorAll("[data-brand-download]").forEach((button) => button.addEventListener("click", () => {
      const image = data.images.find((item) => item.id === button.dataset.brandDownload);
      const file = image && mainFile(image);
      if (file) view.download({ path: file.path, revision: data.revision });
    }));
    body.querySelectorAll("[data-brand-discard]").forEach((button) => button.addEventListener("click", () => discard(button.dataset.brandDiscard)));
  }

  async function thumbnail(body, image, revision) {
    const slot = body.querySelector(`[data-brand-thumb="${CSS.escape(image.id)}"]`);
    const file = mainFile(image);
    if (!slot || !file) return;
    const key = `${revision}:${file.path}`;
    try {
      if (file.extension === "mmd") {
        const source = await view.rawText({ path: file.path, revision });
        const renderer = await view.loadDiagram();
        const svg = await renderer.renderSource(source);
        if (slot.isConnected) slot.innerHTML = `<div class="brand-diagram">${svg}</div>`;
        return;
      }
      let url = view.urls.get(key);
      if (!url) {
        const bytes = await view.rawBytes({ path: file.path, revision });
        url = await view.mediaUrl(new Blob([bytes], { type: TYPES[file.extension] }));
        view.urls.set(key, url);
      }
      if (slot.isConnected) slot.innerHTML = `<img src="${escape(url)}" alt="${escape(image.subject)}" loading="lazy" />`;
    } catch (_error) {
      if (slot.isConnected) slot.innerHTML = `<span class="brand-thumb-wait">Tin could not load this image.</span>`;
    }
  }

  async function discard(imageId) {
    const image = view.data?.images.find((item) => item.id === imageId);
    if (!image || view.busy.has(imageId)) return;
    const created = Date.parse(image.created_at || "");
    const undo = Number.isFinite(created) && Date.now() - created < UNDO_MS;
    const question = undo
      ? `Discard “${image.subject}”? Tin removes it from the library; Files history keeps it.`
      : `Retire “${image.subject}”? Past 48 hours Tin keeps the file and marks it retired.`;
    if (!window.confirm(question)) return;
    view.busy.add(imageId);
    render();
    try {
      const result = await view.api(`/api/projects/${encodeURIComponent(view.projectId)}/images/discard`, {
        method: "POST",
        body: JSON.stringify({ image_id: imageId }),
      });
      view.toast(result?.retired ? "Image retired." : "Image discarded.");
    } catch (error) {
      view.toast(`Tin could not discard it: ${error.message}`);
    } finally {
      view.busy.delete(imageId);
    }
    load();
  }

  function close() {
    if (!view) return;
    view.active = false;
    window.clearTimeout(view.searchTimer);
    for (const url of view.urls.values()) if (url.startsWith("blob:")) URL.revokeObjectURL(url);
    view.urls.clear();
  }

  return { mount, close };
})();
