"use strict";

(function defineTinMarkdownViewer() {
  const NUMERIC_CELL = /^[-+]?[$€£]?\d[\d,]*(?:\.\d+)?%?(?:\s+of\s+\d+)?$/i;
  // An embed is untrusted: it runs in an opaque-origin frame that can't reach the network.
  const EMBED_POLICY =
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; " +
    "img-src data: blob:; font-src data:; media-src data: blob:";
  const VIDEO_FILE = /\.(?:mp4|webm|m4v|mov)(?:[?#]|$)/i;

  function makeElement(tagName, className, text) {
    const element = document.createElement(tagName);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function makeSeparator() {
    return makeElement("span", "markdown-context-separator");
  }

  function mount(container, documentData, options = {}) {
    const root = makeElement(
      "section",
      `markdown-viewer ${options.mode === "in-app" ? "is-in-app" : "is-standalone"}`,
    );
    const bar = makeElement("header", "markdown-context-bar");
    if (options.mode !== "in-app") {
      const logo = makeElement("a", "markdown-logo");
      logo.href = "/";
      logo.setAttribute("aria-label", "Tin home");
      const logoImage = document.createElement("img");
      logoImage.src = "/assets/tin-logotype-ink.png";
      logoImage.alt = "";
      logo.append(logoImage);
      bar.append(logo);
    }

    if (options.returnTo) {
      const returnButton = makeElement(
        "button",
        "markdown-return",
        `← ${options.returnTo.label}`,
      );
      returnButton.type = "button";
      returnButton.addEventListener("click", options.returnTo.onActivate);
      if (bar.childElementCount) bar.append(makeSeparator());
      bar.append(returnButton, makeSeparator());
    }

    // The app may pass its own path element, with links to each folder.
    const filename = options.pathElement || makeElement(
      "span",
      "markdown-filename",
      options.contextLabel || documentData.filename,
    );
    const spacer = makeElement("span", "markdown-context-spacer");
    const facts = makeElement(
      "span",
      "markdown-reading-facts",
      options.factsText ||
        `markdown · ${documentData.word_count} words · ${documentData.reading_minutes} min`,
    );
    facts.dataset.mobileText = `${documentData.reading_minutes} min`;
    bar.append(filename, spacer);
    if (options.factsText !== false) bar.append(facts);
    for (const document of (documentData.related_documents || []).slice(0, 3)) {
      // Trusted product context, not links or instructions parsed from Markdown.
      if (typeof document.url !== "string" || !(document.url.startsWith("/?project=") || document.url.startsWith("/file?project="))) continue;
      const link = makeElement("a", "markdown-related-document", document.label);
      link.href = document.url;
      bar.append(link);
    }
    if (options.rawAction) {
      const rawAction = makeElement(
        "button",
        "markdown-context-action",
        options.rawAction.label,
      );
      rawAction.type = "button";
      rawAction.addEventListener("click", () => options.rawAction.onActivate(rawAction));
      bar.append(rawAction);
    }
    if (options.secondaryAction) {
      const secondaryAction = makeElement(
        "button",
        "markdown-context-action is-secondary",
        options.secondaryAction.label,
      );
      secondaryAction.type = "button";
      secondaryAction.addEventListener("click", () =>
        options.secondaryAction.onActivate(secondaryAction),
      );
      bar.append(secondaryAction);
    }
    if (options.primaryAction) {
      const action = makeElement(
        "button",
        "markdown-context-action",
        options.primaryAction.label,
      );
      action.type = "button";
      action.addEventListener("click", () => options.primaryAction.onActivate(action));
      bar.append(action);
    }

    const body = makeElement("div", "markdown-viewer-body");
    const layout = makeElement("div", "markdown-reader-layout");
    const map = makeElement("nav", "markdown-section-map");
    map.setAttribute("aria-label", "Document sections");
    const mapInner = makeElement("div", "markdown-section-map-inner");
    map.append(mapInner);
    const article = makeElement("article", "markdown-document");
    article.innerHTML = documentData.html;
    layout.append(map, article);
    body.append(layout);
    root.append(bar, body);
    container.replaceChildren(root);

    decorateTables(article);
    decorateImages(article);
    decorateBundle(article, documentData, options.loadAsset);
    decorateDiagrams(article);
    const cleanupMap = buildSectionMap(article, map, mapInner);
    return () => cleanupMap();
  }

  function decorateTables(article) {
    article.querySelectorAll("table").forEach((table) => {
      table.classList.add("tin-table");
      if (!table.parentElement?.classList.contains("md-table-scroll")) {
        const scroller = makeElement("div", "md-table-scroll");
        table.before(scroller);
        scroller.append(table);
      }
      table.querySelectorAll("td").forEach((cell) => {
        if (NUMERIC_CELL.test(cell.textContent.trim())) cell.classList.add("is-numeric");
      });
    });
  }

  function showImageFallback(image) {
    if (!image.isConnected) return;
    const frame = makeElement("div", "md-image-fallback");
    frame.setAttribute("role", "img");
    const alt = image.alt.trim();
    const filename = image.dataset.fallbackName || "image";
    frame.setAttribute("aria-label", alt || filename);
    frame.innerHTML =
      '<svg aria-hidden="true" viewBox="0 0 28 24"><rect x="1" y="1" width="26" height="22" rx="2" /><circle cx="8" cy="8" r="2.5" /><path d="M2 19L10 12L15 16L21 10L26 14" /></svg>';
    const label = makeElement(
      "span",
      "",
      [alt, filename].filter((value, index, values) => value && values.indexOf(value) === index).join(" · "),
    );
    frame.append(label);
    image.replaceWith(frame);
  }

  function decorateImages(article) {
    // Bundle figures have no src yet; decorateBundle loads them.
    article.querySelectorAll("img:not(.md-asset)").forEach((image) => {
      image.addEventListener("error", () => showImageFallback(image), { once: true });
      if (image.complete && image.naturalWidth === 0) showImageFallback(image);
    });
  }

  // A draft's figures, embeds and videos (page bundles). Files load with the member's session
  // through `loadAsset(path)`; nothing in the article is fetched by URL.
  function decorateBundle(article, documentData, loadAsset) {
    const assets = new Map((documentData.assets || []).map((item) => [item.path, item.media_type]));
    if (documentData.asset_notes?.length) {
      // Under the title: what the draft referred to that Tin left out, and why.
      const notes = makeElement("p", "md-asset-notes", documentData.asset_notes.join(" "));
      const title = article.querySelector("h1");
      if (title) title.after(notes);
      else article.prepend(notes);
    }
    article.querySelectorAll("img.md-asset").forEach((image) => {
      wrapFigure(image);
      const type = assets.get(image.dataset.asset);
      if (!type || !loadAsset) {
        showImageFallback(image);
        return;
      }
      loadAsset(image.dataset.asset)
        .then((bytes) => imageUrl(new Blob([bytes], { type })))
        .then((url) => {
          image.addEventListener("error", () => showImageFallback(image), { once: true });
          image.src = url;
        })
        .catch(() => showImageFallback(image));
    });
    article.querySelectorAll("figure.md-embed").forEach((figure) => {
      const title = figure.dataset.title || "";
      if (!assets.has(figure.dataset.asset) || !loadAsset) {
        figure.append(makeElement("div", "md-embed-missing", "This interactive piece isn't available."));
        return;
      }
      loadAsset(figure.dataset.asset)
        .then((bytes) => {
          const frame = document.createElement("iframe");
          frame.className = "md-embed-frame";
          // Scripts run, but in an opaque origin: no Tin cookies, storage, page or network.
          frame.setAttribute("sandbox", "allow-scripts");
          frame.setAttribute("referrerpolicy", "no-referrer");
          frame.title = title || "Interactive piece";
          // The article's height is a first guess; the piece then reports its own.
          frame.style.height = `${embedHeight(Number(figure.dataset.height) || 420)}px`;
          frame.srcdoc = embedDocument(new TextDecoder().decode(bytes), readerTheme());
          watchEmbed(frame);
          figure.prepend(frame);
          if (title) figure.append(makeElement("figcaption", "", title));
        })
        .catch(() => {
          figure.append(makeElement("div", "md-embed-missing", "This interactive piece couldn't load."));
        });
    });
    article.querySelectorAll("figure.md-video").forEach((figure) => {
      const url = figure.dataset.url || "";
      const title = figure.dataset.title || "";
      const player = videoPlayer(url, figure.dataset.poster, title);
      if (!player) return; // The link the server rendered stays.
      figure.replaceChildren(player);
      if (title) figure.append(makeElement("figcaption", "", title));
    });
  }

  function wrapFigure(image) {
    const parent = image.parentElement;
    if (parent?.tagName !== "P" || parent.childNodes.length !== 1) return;
    const figure = makeElement("figure", "md-figure");
    parent.replaceWith(figure);
    figure.append(image);
    if (image.title) figure.append(makeElement("figcaption", "", image.title));
  }

  function imageUrl(blob) {
    if (blob.type !== "image/svg+xml") return Promise.resolve(URL.createObjectURL(blob));
    // As in Files: an SVG becomes a data URL, inert in <img> and opaque if opened directly.
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error("Tin could not load this figure."));
      reader.readAsDataURL(blob);
    });
  }

  function embedDocument(html, theme) {
    const policy = `<meta http-equiv="Content-Security-Policy" content="${EMBED_POLICY}">`;
    const head = policy + embedBridge(theme);
    const doctype = html.match(/^\s*<!doctype[^>]*>/i);
    return doctype ? doctype[0] + head + html.slice(doctype[0].length) : head + html;
  }

  // Runs first in every embed: it takes the reader's theme as `data-theme` on its root and
  // reports the piece's height. The piece only ever sends a number; the reader, a theme name.
  function embedBridge(theme) {
    return `<script>(() => {
  const root = document.documentElement;
  root.dataset.theme = ${JSON.stringify(theme)};
  addEventListener("message", (event) => {
    const theme = event.source === parent && event.data ? event.data.tinTheme : null;
    if (theme === "light" || theme === "dark") root.dataset.theme = theme;
  });
  let reported = 0;
  const report = () => {
    const height = Math.ceil(root.getBoundingClientRect().height);
    if (height && height !== reported) parent.postMessage({ tinEmbedHeight: (reported = height) }, "*");
  };
  addEventListener("load", report);
  new ResizeObserver(report).observe(root);
})();</script>`;
  }

  function readerTheme() {
    return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
  }

  function embedHeight(height) {
    return Math.min(1600, Math.max(120, Math.ceil(height)));
  }

  const embedFrames = new Set();

  function watchEmbed(frame) {
    if (!embedFrames.size) {
      window.addEventListener("message", resizeEmbed);
      themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    }
    embedFrames.add(frame);
  }

  function resizeEmbed(event) {
    const height = event.data?.tinEmbedHeight;
    if (typeof height !== "number" || !Number.isFinite(height)) return;
    for (const frame of embedFrames) {
      if (!frame.isConnected) embedFrames.delete(frame);
      else if (frame.contentWindow === event.source) frame.style.height = `${embedHeight(height)}px`;
    }
  }

  const themeObserver = new MutationObserver(() => {
    const theme = readerTheme();
    for (const frame of embedFrames) {
      if (!frame.isConnected) embedFrames.delete(frame);
      else frame.contentWindow?.postMessage({ tinTheme: theme }, "*");
    }
  });

  // Players from a short list of hosts; a video file plays in place and loads only on play.
  function videoPlayer(url, poster, title) {
    let parsed;
    try {
      parsed = new URL(url);
    } catch (_error) {
      return null;
    }
    if (!["https:", "http:"].includes(parsed.protocol)) return null;
    if (VIDEO_FILE.test(parsed.pathname)) {
      const video = document.createElement("video");
      video.className = "md-video-file";
      video.controls = true;
      video.preload = "none";
      video.playsInline = true;
      video.src = parsed.href;
      if (poster?.startsWith("https://")) video.poster = poster;
      return video;
    }
    const src = providerEmbed(parsed);
    if (!src) return null;
    const frame = document.createElement("iframe");
    frame.className = "md-video-frame";
    frame.src = src;
    frame.title = title || "Video";
    frame.loading = "lazy";
    frame.setAttribute("sandbox", "allow-scripts allow-same-origin allow-presentation allow-popups");
    frame.setAttribute("allow", "fullscreen; picture-in-picture; encrypted-media");
    frame.setAttribute("referrerpolicy", "strict-origin-when-cross-origin");
    return frame;
  }

  function providerEmbed(url) {
    const host = url.hostname.replace(/^www\./, "");
    const segments = url.pathname.split("/").filter(Boolean);
    const id = (value) => (/^[A-Za-z0-9_-]{4,64}$/.test(value || "") ? value : null);
    if (host === "youtube.com" || host === "m.youtube.com") {
      const video = id(url.searchParams.get("v")) || (segments[0] === "shorts" ? id(segments[1]) : null);
      return video && `https://www.youtube-nocookie.com/embed/${video}`;
    }
    if (host === "youtu.be") return id(segments[0]) && `https://www.youtube-nocookie.com/embed/${segments[0]}`;
    if (host === "vimeo.com") return /^\d+$/.test(segments[0] || "") && `https://player.vimeo.com/video/${segments[0]}`;
    if (host === "loom.com" && segments[0] === "share") return id(segments[1]) && `https://www.loom.com/embed/${segments[1]}`;
    if (host === "stream.mux.com" || host === "player.mux.com") {
      const playback = id((segments[0] || "").replace(/\.m3u8$/, ""));
      return playback && `https://player.mux.com/${playback}`;
    }
    return null;
  }

  function decorateDiagrams(article) {
    const blocks = [...article.querySelectorAll('.md-code-block[data-language="mermaid"]')];
    if (!blocks.length) return;
    window.TinDiagramLoader.load().then(async (renderer) => {
      for (const block of blocks) {
        if (!block.isConnected) continue;
        const source = block.querySelector("code")?.textContent || "";
        try {
          const figure = makeElement("figure", "md-diagram");
          const stage = makeElement("div", "md-diagram-stage tin-diagram");
          stage.innerHTML = await renderer.renderSource(source);
          if (!block.isConnected) continue;
          const details = makeElement("details", "md-diagram-source");
          const summary = makeElement("summary", "", "Mermaid source");
          const pre = makeElement("pre", "");
          const code = makeElement("code", "", source);
          pre.append(code);
          details.append(summary, pre);
          figure.append(stage, details);
          block.replaceWith(figure);
        } catch (_error) {
          block.classList.add("is-diagram-invalid");
        }
      }
    }).catch(() => {
      for (const block of blocks) block.classList.add("is-diagram-invalid");
    });
  }

  function buildSectionMap(article, map, mapInner) {
    const headings = [...article.querySelectorAll("h2[id]")];
    if (headings.length < 3) {
      map.hidden = true;
      map.closest(".markdown-reader-layout")?.classList.add("has-no-map");
      return () => {};
    }

    const buttons = headings.map((heading) => {
      const button = makeElement("button", "markdown-section-link", heading.textContent.trim());
      button.type = "button";
      button.addEventListener("click", () => {
        heading.scrollIntoView({ behavior: prefersReducedMotion() ? "auto" : "smooth" });
      });
      mapInner.append(button);
      return button;
    });

    function updateCurrentSection() {
      let current = 0;
      for (let index = 0; index < headings.length; index += 1) {
        if (headings[index].getBoundingClientRect().top <= 150) current = index;
        else break;
      }
      buttons.forEach((button, index) => {
        const active = index === current;
        button.classList.toggle("is-current", active);
        if (active) button.setAttribute("aria-current", "location");
        else button.removeAttribute("aria-current");
      });
    }

    updateCurrentSection();
    window.addEventListener("scroll", updateCurrentSection, { passive: true });
    return () => window.removeEventListener("scroll", updateCurrentSection);
  }

  function prefersReducedMotion() {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  window.TinMarkdownViewer = { mount };
})();
