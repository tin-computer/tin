"use strict";

(function defineTinMarkdownViewer() {
  const NUMERIC_CELL = /^[-+]?[$€£]?\d[\d,]*(?:\.\d+)?%?(?:\s+of\s+\d+)?$/i;

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

    const filename = makeElement(
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
    bar.append(filename, spacer, facts);
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

  function decorateImages(article) {
    article.querySelectorAll("img").forEach((image) => {
      const showFallback = () => {
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
      };
      image.addEventListener("error", showFallback, { once: true });
      if (image.complete && image.naturalWidth === 0) showFallback();
    });
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
