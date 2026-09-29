"use strict";

(function defineTinDelimitedViewer() {
  const DELIMITERS = [
    { character: ",", label: "csv" },
    { character: "\t", label: "tsv" },
    { character: ";", label: "csv · semicolon" },
    { character: "|", label: "csv · pipe" },
  ];
  const MAX_ROWS = 500;
  const MAX_INLINE_BYTES = 2 * 1024 * 1024;
  const NUMERIC_CELL = /^[-+]?[$€£]?\d[\d,]*(?:\.\d+)?%?$/;
  const ISO_VALUE = /^\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+-]+Z?)?$/;
  const EMAIL_VALUE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  const URL_VALUE = /^https?:\/\/\S+$/i;
  const TOKEN_VALUE = /^[^\s]+$/;

  function makeElement(tagName, className, text) {
    const element = document.createElement(tagName);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function createCandidate(delimiter, rowLimit) {
    let currentRow = [];
    let field = "";
    let inQuotes = false;
    let quotePending = false;
    let afterQuote = false;
    let swallowLf = false;
    let rowTouched = false;
    let expectedColumns = null;
    let totalRows = 0;
    let failed = false;
    const rows = [];

    function pushField() {
      currentRow.push(field);
      field = "";
      afterQuote = false;
    }

    function pushRow() {
      pushField();
      if (expectedColumns === null) expectedColumns = currentRow.length;
      if (currentRow.length !== expectedColumns) failed = true;
      if (rows.length < rowLimit + 1) rows.push(currentRow);
      totalRows += 1;
      currentRow = [];
      rowTouched = false;
    }

    function processOutside(character) {
      if (swallowLf) {
        swallowLf = false;
        if (character === "\n") return;
      }
      if (afterQuote) {
        if (character === delimiter) {
          pushField();
          rowTouched = true;
          return;
        }
        if (character === "\r" || character === "\n") {
          pushRow();
          swallowLf = character === "\r";
          return;
        }
        failed = true;
        return;
      }
      if (character === delimiter) {
        pushField();
        rowTouched = true;
      } else if (character === "\r" || character === "\n") {
        pushRow();
        swallowLf = character === "\r";
      } else if (character === '"') {
        if (field.length) {
          failed = true;
          return;
        }
        inQuotes = true;
        rowTouched = true;
      } else {
        field += character;
        rowTouched = true;
      }
    }

    return {
      feed(chunk) {
        if (failed) return;
        for (const character of chunk) {
          if (inQuotes) {
            if (quotePending) {
              if (character === '"') {
                field += '"';
                quotePending = false;
                continue;
              }
              inQuotes = false;
              quotePending = false;
              afterQuote = true;
              processOutside(character);
              continue;
            }
            if (character === '"') quotePending = true;
            else field += character;
            continue;
          }
          processOutside(character);
        }
      },
      finish() {
        if (inQuotes && quotePending) {
          inQuotes = false;
          quotePending = false;
          afterQuote = true;
        } else if (inQuotes) {
          failed = true;
        }
        if (!failed && (rowTouched || field.length || currentRow.length)) pushRow();
        if (expectedColumns === null || expectedColumns < 2 || totalRows < 1) failed = true;
        return {
          delimiter,
          failed,
          columns: expectedColumns || 0,
          rows,
          totalRows,
        };
      },
    };
  }

  function createDetector(rowLimit = MAX_ROWS) {
    const candidates = DELIMITERS.map((item) => ({
      ...item,
      parser: createCandidate(item.character, rowLimit),
    }));
    let firstChunk = true;
    return {
      feed(chunk) {
        let value = chunk;
        if (firstChunk) {
          firstChunk = false;
          if (value.startsWith("\uFEFF")) value = value.slice(1);
        }
        candidates.forEach((candidate) => candidate.parser.feed(value));
      },
      finish() {
        const valid = candidates
          .map((candidate) => ({ ...candidate, result: candidate.parser.finish() }))
          .filter((candidate) => !candidate.result.failed);
        if (!valid.length) return { error: "Tin couldn't read this as a table" };
        const mostColumns = Math.max(...valid.map((candidate) => candidate.result.columns));
        const strongest = valid.filter((candidate) => candidate.result.columns === mostColumns);
        if (strongest.length !== 1) return { error: "Tin couldn't read this as a table" };
        const selected = strongest[0];
        const [headers, ...rows] = selected.result.rows;
        return {
          delimiter: selected.character,
          delimiterLabel: selected.label,
          headers,
          rows,
          totalRows: Math.max(0, selected.result.totalRows - 1),
          truncated: selected.result.totalRows - 1 > rowLimit,
        };
      },
    };
  }

  function parseText(text, rowLimit = MAX_ROWS) {
    const detector = createDetector(rowLimit);
    detector.feed(text);
    return detector.finish();
  }

  async function readResponse(response) {
    const detector = createDetector();
    const decoder = new TextDecoder("utf-8", { fatal: true });
    const declaredBytes = Number(response.headers.get("content-length")) || 0;
    let keepRaw = !declaredBytes || declaredBytes <= MAX_INLINE_BYTES;
    let rawText = "";
    let bytes = 0;
    try {
      if (!response.body) {
        const buffer = await response.arrayBuffer();
        bytes = buffer.byteLength;
        const text = decoder.decode(buffer);
        detector.feed(text);
        return {
          kind: "delimited",
          bytes,
          contentType: response.headers.get("content-type") || "",
          rawText: bytes <= MAX_INLINE_BYTES ? text : null,
          table: detector.finish(),
        };
      }
      const reader = response.body.getReader();
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        bytes += value.byteLength;
        const text = decoder.decode(value, { stream: true });
        detector.feed(text);
        if (keepRaw && bytes <= MAX_INLINE_BYTES) rawText += text;
        else {
          keepRaw = false;
          rawText = "";
        }
      }
      const tail = decoder.decode();
      if (tail) {
        detector.feed(tail);
        if (keepRaw) rawText += tail;
      }
    } catch (_error) {
      return {
        kind: "file",
        bytes: bytes || declaredBytes,
        contentType: response.headers.get("content-type") || "",
        text: null,
      };
    }
    return {
      kind: "delimited",
      bytes,
      contentType: response.headers.get("content-type") || "",
      rawText: keepRaw ? rawText : null,
      table: detector.finish(),
    };
  }

  function isNumeric(value) {
    return NUMERIC_CELL.test(value.trim());
  }

  function cellVoice(value) {
    const trimmed = value.trim();
    if (!trimmed) return "is-empty";
    if (isNumeric(trimmed)) return "is-mono is-numeric";
    if (
      EMAIL_VALUE.test(trimmed) ||
      URL_VALUE.test(trimmed) ||
      ISO_VALUE.test(trimmed) ||
      TOKEN_VALUE.test(trimmed)
    ) return "is-mono";
    return "is-prose";
  }

  function numericColumns(table) {
    return table.headers.map((_, index) => {
      const values = table.rows.map((row) => row[index].trim()).filter(Boolean);
      return Boolean(values.length) && values.filter(isNumeric).length / values.length >= 0.9;
    });
  }

  function measureColumnWidths(table) {
    if (typeof document === "undefined") return table.headers.map(() => 160);
    const canvas = document.createElement("canvas");
    const context = canvas.getContext("2d");
    const proseMaximum = window.innerWidth < 720 ? 240 : 360;
    const proseFont = getComputedStyle(document.documentElement).getPropertyValue("--brand-sans") || '"Geist Sans", sans-serif';
    return table.headers.map((header, index) => {
      context.font = '700 12px "Geist Mono", monospace';
      const headerWidth = context.measureText(header || "—").width + 24;
      let contentWidth = 0;
      let hasProse = false;
      table.rows.forEach((row) => {
        const value = row[index] || "";
        const prose = cellVoice(value) === "is-prose";
        hasProse ||= prose;
        context.font = prose
          ? `400 14px ${proseFont}`
          : '400 12.5px "Geist Mono", monospace';
        contentWidth = Math.max(contentWidth, context.measureText(value || "—").width + 24);
      });
      return Math.ceil(Math.max(100, headerWidth, Math.min(hasProse ? proseMaximum : 240, contentWidth)));
    });
  }

  function makeContextBar(file, options, mode) {
    const bar = makeElement("header", "project-file-context is-tabular");
    const back = makeElement("button", "project-file-return", `← ${options.returnLabel || "files"}`);
    back.type = "button";
    back.addEventListener("click", options.onReturn);
    const separator = makeElement("span", "project-file-context-separator");
    const path = options.pathElement || makeElement("code", "project-file-path", options.contextLabel);
    const facts = makeElement("code", "project-file-facts", options.factsText);
    const spacer = makeElement("span", "project-file-context-spacer");
    const toggle = makeElement(
      "button",
      "project-file-mode-toggle",
      mode === "raw" ? "view table" : "view raw",
    );
    toggle.type = "button";
    toggle.addEventListener("click", () => options.onToggleMode(toggle));
    const copy = makeElement("button", "project-file-action", "Copy");
    copy.type = "button";
    copy.addEventListener("click", () => options.onCopy(copy));
    const download = makeElement("button", "project-file-action", "Download raw");
    download.type = "button";
    download.addEventListener("click", options.onDownload);
    bar.append(back, separator, path, facts, spacer, toggle, copy, download);
    return bar;
  }

  function makeRawBody(file) {
    const body = makeElement("div", "project-file-body is-delimited-raw");
    if (file.table.error) {
      body.append(makeElement(
        "p",
        "project-file-parse-note",
        "Tin couldn't read this as a table · showing the file as text",
      ));
    }
    const article = makeElement("article", "project-file-text");
    const pre = makeElement("pre", "", file.rawText || "");
    article.append(pre);
    body.append(article);
    return body;
  }

  function makeTable(file, options) {
    const tableData = file.table;
    const widths = measureColumnWidths(tableData);
    const numeric = numericColumns(tableData);
    const body = makeElement("div", "project-delimited-body");
    const viewport = makeElement("div", "project-delimited-viewport");
    const table = makeElement("table", "tin-table project-delimited-table");
    const colgroup = document.createElement("colgroup");
    const indexColumn = document.createElement("col");
    indexColumn.style.width = "42px";
    colgroup.append(indexColumn);
    widths.forEach((width) => {
      const column = document.createElement("col");
      column.style.width = `${width}px`;
      colgroup.append(column);
    });
    table.style.width = `${42 + widths.reduce((total, width) => total + width, 0)}px`;
    const head = document.createElement("thead");
    const headerRow = document.createElement("tr");
    const indexHeader = makeElement("th", "is-index", "#");
    headerRow.append(indexHeader);
    tableData.headers.forEach((header, index) => {
      const cell = makeElement("th", numeric[index] ? "is-numeric" : "", header || "—");
      headerRow.append(cell);
    });
    head.append(headerRow);
    const rows = document.createElement("tbody");
    tableData.rows.forEach((values, rowIndex) => {
      const row = document.createElement("tr");
      row.append(makeElement("td", "is-index", String(rowIndex + 1)));
      values.forEach((value) => {
        const cell = makeElement("td", cellVoice(value));
        cell.append(makeElement("span", "", value.trim() ? value : "—"));
        row.append(cell);
      });
      rows.append(row);
    });
    table.append(colgroup, head, rows);
    viewport.append(table);

    const scrollDoctrine = makeElement("div", "project-delimited-scroll");
    const track = makeElement("span", "project-delimited-scroll-track");
    const thumb = makeElement("span", "project-delimited-scroll-thumb");
    track.append(thumb);
    scrollDoctrine.append(track, makeElement("span", "project-delimited-scroll-hint", "table scrolls →"));

    const status = tableData.truncated
      ? `first column pinned · header sticks while you scroll · showing ${tableData.rows.length} of ${tableData.totalRows.toLocaleString()} rows · `
      : `first column pinned · header sticks while you scroll · ${tableData.totalRows} of ${tableData.totalRows} rows`;
    const statusLine = makeElement("p", "project-delimited-status", status);
    if (tableData.truncated) {
      const downloadAll = makeElement("button", "", "download for all");
      downloadAll.type = "button";
      downloadAll.addEventListener("click", options.onDownload);
      statusLine.append(downloadAll);
    }
    body.append(viewport, scrollDoctrine, statusLine);

    function updateScrollDoctrine() {
      const overflows = viewport.scrollWidth > viewport.clientWidth + 1;
      scrollDoctrine.hidden = !overflows;
      if (!overflows) return;
      const ratio = viewport.clientWidth / viewport.scrollWidth;
      const trackWidth = track.clientWidth;
      const thumbWidth = Math.max(30, trackWidth * ratio);
      const travel = Math.max(0, trackWidth - thumbWidth);
      const progress = viewport.scrollLeft / Math.max(1, viewport.scrollWidth - viewport.clientWidth);
      thumb.style.width = `${thumbWidth}px`;
      thumb.style.transform = `translateX(${travel * progress}px)`;
    }

    function markExpandableRows() {
      rows.querySelectorAll("tr").forEach((row) => {
        const truncated = [...row.querySelectorAll("td.is-prose > span")].some(
          (cell) => cell.scrollWidth > cell.clientWidth + 1,
        );
        row.classList.toggle("is-expandable", truncated);
        row.tabIndex = truncated ? 0 : -1;
      });
    }

    function toggleRow(row) {
      if (!row?.classList.contains("is-expandable")) return;
      row.classList.toggle("is-expanded");
    }

    rows.addEventListener("click", (event) => toggleRow(event.target.closest("tr")));
    rows.addEventListener("keydown", (event) => {
      if (!['Enter', ' '].includes(event.key)) return;
      const row = event.target.closest("tr");
      if (!row?.classList.contains("is-expandable")) return;
      event.preventDefault();
      toggleRow(row);
    });
    viewport.addEventListener("scroll", updateScrollDoctrine, { passive: true });
    window.requestAnimationFrame(() => {
      markExpandableRows();
      updateScrollDoctrine();
    });
    return { body, onResize: updateScrollDoctrine };
  }

  function mount(container, file, options) {
    const mode = file.mode === "raw" || file.table.error ? "raw" : "table";
    const root = makeElement("section", "project-file-view is-delimited");
    root.append(makeContextBar(file, options, mode));
    let resize = null;
    if (mode === "raw") root.append(makeRawBody(file));
    else {
      const rendered = makeTable(file, options);
      root.append(rendered.body);
      resize = rendered.onResize;
      window.addEventListener("resize", resize);
    }
    container.replaceChildren(root);
    return () => {
      if (resize) window.removeEventListener("resize", resize);
    };
  }

  window.TinDelimitedViewer = {
    MAX_INLINE_BYTES,
    mount,
    parseText,
    readResponse,
  };
})();
