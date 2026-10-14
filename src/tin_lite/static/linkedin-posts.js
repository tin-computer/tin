/* LinkedIn drafts are ordinary project files; publishing is a separate exact-preview effect.
   This reader follows Paper boards LI-07A to LI-15: read a post with its draft notes, edit it
   in place with images as rows, preview exactly what LinkedIn receives, then publish and
   follow the outcome. X's drafts keep their own reader in x-posts.js. */
(() => {
  const SCHEMA = "tin.social.linkedin_draft.v1";
  const MAX_TEXT = 3000;
  const MAX_IMAGES = 20;
  const MAX_BYTES = 5_000_000;
  const MEDIA_DIR = "social/linkedin/media";
  const EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg"};
  const IMAGE_PATH = /\.(png|jpe?g)$/i;
  const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi;
  const OUTCOME_LABELS = {publishing: "Publishing", published: "Published", uncertain: "Check the page", not_published: "Not published"};
  const ICON = {
    down: '<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M3 4.5L6 7.5L9 4.5" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    up: '<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M3 7.5L6 4.5L9 7.5" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    check: '<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M2.5 6.5L5 9L9.5 3.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    remove: '<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><path d="M3.5 3.5L10.5 10.5M10.5 3.5L3.5 10.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>',
    plus: '<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 3V13M3 8H13" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>',
    grip: '<svg width="10" height="16" viewBox="0 0 10 16" aria-hidden="true"><g fill="currentColor"><circle cx="2.5" cy="3" r="1.3"/><circle cx="7.5" cy="3" r="1.3"/><circle cx="2.5" cy="8" r="1.3"/><circle cx="7.5" cy="8" r="1.3"/><circle cx="2.5" cy="13" r="1.3"/><circle cx="7.5" cy="13" r="1.3"/></g></svg>',
    image: '<svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true"><rect x="3" y="4" width="14" height="12" rx="2" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M3.5 13.5L7.5 9.5L10.5 12.5L13 10L16.5 13.5" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>',
  };
  const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[char]));
  const copy = value => JSON.parse(JSON.stringify(value));
  const count = value => value.toLocaleString("en-US");
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  // Tin names uploads "<uuid>-name.png" in a run's folder; people read the name and folder.
  const shownName = path => path.split("/").at(-1).replace(new RegExp(`^${UUID.source}-`, "i"), "");
  const shownFolder = path => path.split("/").slice(0, -1).join("/").replace(UUID, id => id.slice(0, 8)) || "Files";
  const bytes = size => size >= 1_000_000 ? `${(size / 1_000_000).toFixed(1)} MB` : `${Math.max(1, Math.round(size / 1000))} KB`;
  const kindLabel = account => account?.kind === "organization" ? "page" : "profile";
  const initial = name => (String(name || "").trim().charAt(0) || "?").toUpperCase();
  const firstName = name => String(name || "").trim().split(/\s+/)[0] || "Its owner";
  const asWho = account => account.kind === "organization" ? `the ${account.name} page` : account.name;
  const pageUrl = account => {
    const org = /^urn:li:organization:(\d+)$/.exec(account?.urn || "");
    return org ? `https://www.linkedin.com/company/${org[1]}/` : "https://www.linkedin.com/in/me/recent-activity/all/";
  };
  const pageLabel = account => account?.kind === "organization" ? `Open the ${account.name} page ↗` : "Open your profile ↗";
  const sessions = new Map();
  let active = null;

  function open(context) {
    const key = `${context.actorId || ""}:${context.projectId}:${context.path}`;
    if (active?.key === key && active.root.isConnected) return;
    active?.close();
    const root = document.createElement("section");
    root.className = "li-reader";
    root.setAttribute("aria-label", "LinkedIn post drafts");
    if (context.host) context.host.replaceChildren(root);
    else document.body.append(root);
    const kept = sessions.get(key);
    const state = {
      root, context, key, closed: false, busy: false,
      revision: null, saved: null, draft: null, savedSha: null,
      connected: false, accounts: [], unavailable: [], publications: {},
      selected: 0, mode: "read", outcome: false, notes: false, menu: false, switcher: false,
      preview: null, publishRequest: null, saveRequest: null, message: null,
      uploads: [], media: new Map(), picker: null, ...kept,
    };
    state.close = () => {
      sessions.set(key, {
        saved: state.saved && copy(state.saved), draft: state.draft && copy(state.draft),
        revision: state.revision, savedSha: state.savedSha, selected: state.selected,
        mode: state.mode === "edit" ? "edit" : "read", saveRequest: state.saveRequest,
      });
      // Unsaved edits survive navigation; a preview, an upload or a picker never does.
      state.closed = true;
      for (const item of state.uploads) item.request?.abort();
      clearTimeout(state.followTimer);
      clearInterval(state.clock);
      for (const item of state.media.values()) if (item.url) URL.revokeObjectURL(item.url);
      window.removeEventListener("beforeunload", state.beforeUnload);
      root.remove();
      if (active === state) active = null;
    };
    state.beforeUnload = event => {
      if (dirty(state) || state.uploads.some(item => ["queued", "uploading"].includes(item.status))) {
        event.preventDefault(); event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", state.beforeUnload);
    root.addEventListener("click", event => {
      const target = event.target.closest("[data-li]");
      if (target && root.contains(target)) act(state, target.dataset.li, target);
      else if (state.menu && !event.target.closest(".li-menu")) {state.menu = false; render(state);}
    });
    root.addEventListener("input", event => typed(state, event.target));
    root.addEventListener("change", event => {
      if (event.target.matches("[data-li-upload]")) {
        const files = [...event.target.files]; event.target.value = "";
        queueUploads(state, files);
      }
    });
    root.addEventListener("keydown", event => keyed(state, event));
    bindDragging(state);
    active = state;
    if (state.draft) {
      if (dirty(state)) state.message = {text: "Your unsaved edits are still here."};
      render(state);
      refresh(state);
    } else load(state);
  }

  const apiUrl = (state, action) => `/api/projects/${encodeURIComponent(state.context.projectId)}/linkedin/${action}`;
  const dirty = state => !!state.draft && JSON.stringify(state.draft) !== JSON.stringify(state.saved);
  const posts = state => state.draft?.posts || [];
  const current = state => typeof state.selected === "number" ? posts(state)[state.selected] : null;
  const uploadsFor = (state, post) => state.uploads.filter(item => item.postId === post.id);

  async function load(state) {
    state.message = {text: "Loading drafts…"}; render(state);
    try {
      const result = await state.context.api(`${apiUrl(state, "drafts")}?${new URLSearchParams({path: state.context.path})}`);
      if (state.closed) return;
      if (result?.draft?.schema_version !== SCHEMA || !Array.isArray(result.draft.posts)) throw new Error("This file is not a LinkedIn draft. Open it in Files.");
      accept(state, result);
      state.saved = copy(result.draft); state.draft = copy(result.draft);
      state.savedSha = result.sha256; state.revision = result.revision;
      state.saveRequest = null; state.preview = null; state.message = null;
      if (typeof state.selected === "number" && state.selected >= state.draft.posts.length) state.selected = state.draft.posts.length ? 0 : "gaps";
      if (!state.draft.posts.length) state.selected = "gaps";
      state.mode = "read";
      state.outcome = showsOutcome(state);
      render(state); follow(state);
    } catch (error) {if (!state.closed) {state.message = {text: error.message, error: true}; render(state);}}
  }

  // Coming back to kept edits still reads who may post and what has been published.
  async function refresh(state) {
    try {
      const result = await state.context.api(`${apiUrl(state, "drafts")}?${new URLSearchParams({path: state.context.path})}`);
      if (state.closed || result?.draft?.schema_version !== SCHEMA) return;
      accept(state, result);
      if (state.mode !== "edit") render(state); else paintChrome(state);
      follow(state);
    } catch (_error) {/* The kept copy stays readable; saving reports any problem. */}
  }

  function accept(state, result) {
    state.connected = result.connected !== false && (result.connected === true || (result.accounts || []).length > 0);
    // Your own profile first, then the project's pages, as LinkedIn lists them.
    state.accounts = [...(result.accounts || [])].sort((a, b) => (a.kind === "organization") - (b.kind === "organization"));
    state.unavailable = result.unavailable || [];
    state.publications = result.publications || {};
  }

  // A post opens on its outcome while it publishes, once it's live or uncertain, and after a
  // refusal until it changes.
  function shownPublication(state, post) {
    const item = post && state.publications[post.id];
    if (!item || (item.state === "not_published" && item.text !== post.text)) return null;
    return item;
  }
  const showsOutcome = state => !!shownPublication(state, current(state));

  function whoPosts(state, post) {
    if (!state.connected) return {kind: "not_connected"};
    if (post.target) {
      const own = state.accounts.find(item => item.urn === post.target);
      if (own) return {kind: "chosen", account: own};
      const other = state.unavailable.find(item => item.urn === post.target);
      if (other?.reason === "another_member") return {kind: "another_member", account: other};
      return {kind: "disconnected", account: other || null};
    }
    if (state.accounts.length === 1) return {kind: "chosen", account: state.accounts[0]};
    return {kind: state.accounts.length ? "unchosen" : "none"};
  }

  function statusLabel(state, post, index) {
    if (index === state.selected && state.mode === "edit") return "Editing";
    if (index === state.selected && state.mode === "preview") return "Previewing";
    const published = shownPublication(state, post);
    if (published) return OUTCOME_LABELS[published.state];
    if (post.readiness === "needs_evidence") return "Needs evidence";
    if (post.readiness === "needs_asset" || post.missing_assets?.length) return "Needs media";
    return "Ready";
  }

  // ---- Rendering ----

  function render(state) {
    if (state.closed) return;
    const focused = document.activeElement?.closest?.("[data-focus]")?.dataset.focus;
    const view = viewOf(state);
    state.root.dataset.view = view;
    state.root.innerHTML = `${bar(state, view)}
      <div class="li-body">${rail(state)}<div class="li-column li-column-${view}">${column(state, view)}</div></div>
      ${dock(state, view)}`;
    if (focused) state.root.querySelector(`[data-focus="${CSS.escape(focused)}"]`)?.focus();
    state.root.querySelectorAll(".li-text").forEach(fit);
    loadMedia(state);
    tickPreview(state);
  }

  function viewOf(state) {
    if (!state.draft) return "loading";
    if (state.selected === "gaps") return "gaps";
    if (state.mode === "edit") return "edit";
    if (state.mode === "preview" && state.preview) return "preview";
    return state.outcome && shownPublication(state, current(state)) ? "outcome" : "read";
  }

  // Only the bar, rail and dock: typing, uploads and outcome updates keep the column intact.
  function paintChrome(state) {
    if (state.closed || !state.draft) return;
    const view = viewOf(state);
    const focused = document.activeElement?.closest?.("[data-focus]")?.dataset.focus;
    const swap = (selector, html) => {
      const node = state.root.querySelector(selector);
      if (!node) return;
      const next = document.createElement("div"); next.innerHTML = html;
      node.replaceWith(...next.childNodes);
    };
    swap(".li-bar", bar(state, view));
    swap(".li-rail", rail(state));
    swap(".li-dock", dock(state, view));
    if (focused && !state.root.contains(document.activeElement)) state.root.querySelector(`[data-focus="${CSS.escape(focused)}"]`)?.focus();
  }

  function actions(state, view) {
    const post = current(state);
    const busy = state.busy;
    if (view === "loading") return {};
    if (view === "gaps") {
      const source = sourcesPath(state);
      return {links: source ? [{label: "Sources", action: "sources"}] : []};
    }
    if (view === "edit") return {
      note: dirty(state) ? "Unsaved changes" : "",
      secondary: {label: "Cancel", action: "cancel", disabled: busy},
      primary: {label: "Save changes", action: "save", disabled: busy || !dirty(state)},
    };
    if (view === "preview") {
      const left = minutesLeft(state.preview);
      return {
        note: left > 0 ? `Preview good for ${plural(left, "minute")}` : "Preview expired",
        secondary: {label: "Edit post", action: "edit", disabled: busy},
        primary: left > 0
          ? {label: "Publish this exact post", action: "publish", disabled: busy}
          : {label: "Preview again", action: "preview", disabled: busy},
      };
    }
    if (view === "outcome") {
      const item = shownPublication(state, post);
      if (item.state === "publishing") return {
        note: "Publishing to LinkedIn…",
        secondary: {label: "Edit post", action: "edit", disabled: true},
        primary: {label: "Publish this exact post", action: "publish", disabled: true},
      };
      const back = {label: "Back to drafts", action: "back"};
      if (item.state === "published") return {note: `Published ${when(item.at)}`, secondary: back, primary: {label: "View on LinkedIn ↗", href: item.url}};
      if (item.state === "uncertain") return {note: "Check the page", secondary: back, primary: {label: pageLabel(item.account), href: pageUrl(item.account)}};
      return {note: "Not published", secondary: back, primary: {label: "Edit post", action: "edit"}};
    }
    return {
      note: dirty(state) ? "Unsaved changes" : "",
      links: post ? [{label: state.notes ? "Close notes" : "Draft notes", action: "notes", expanded: state.notes, wide: true}] : [],
      secondary: post ? {label: "Edit post", action: "edit", disabled: busy} : null,
      primary: post ? {label: "Preview post", action: "preview", disabled: busy} : null,
    };
  }

  function button(item, kind, focus = kind) {
    if (!item) return "";
    const label = esc(item.label);
    if (item.href) return `<a class="li-button is-${kind}" href="${esc(item.href)}" target="_blank" rel="noopener noreferrer" data-focus="${focus}">${label}</a>`;
    return `<button type="button" class="li-button is-${kind}" data-li="${item.action}" data-focus="${focus}" ${item.disabled ? "disabled" : ""}>${label}</button>`;
  }

  function bar(state, view) {
    const {note = "", links = [], secondary, primary} = actions(state, view);
    const label = state.context.returnLabel || "files";
    const post = current(state);
    const list = posts(state);
    const title = view === "gaps" ? `LinkedIn drafts${batchDay(state.context.path, true)}`
      : post ? `${batchDay(state.context.path)}Post ${state.selected + 1}${list.length > 1 ? ` of ${list.length}` : ""}` : shownName(state.context.path);
    const switchLabel = view === "gaps" ? "More context needed" : post ? `Post ${state.selected + 1}${list.length > 1 ? ` of ${list.length}` : ""}` : "";
    const switchStatus = post ? statusLabel(state, post, state.selected) : "";
    // At phone width the secondary action becomes the bar's text link and the primary moves
    // to the dock; the board LI-15 draws "Edit" for "Edit post".
    const short = secondary && {...secondary, label: secondary.label === "Edit post" ? "Edit" : secondary.label};
    return `<header class="li-bar">
      <button type="button" class="li-return" data-li="close" data-focus="close" aria-label="Back to ${esc(label)}"><span aria-hidden="true">←</span><span class="li-wide"> ${esc(label.charAt(0).toUpperCase() + label.slice(1))}</span></button>
      <span class="li-bar-rule" aria-hidden="true"></span>
      <span class="li-bar-title">${esc(title)}</span>
      ${switchLabel && (list.length > 1 || state.draft?.gaps?.length) ? `<button type="button" class="li-switch" data-li="switcher" data-focus="switcher" aria-expanded="${state.switcher}"><strong>${esc(switchLabel)}</strong>${switchStatus ? `<span>${esc(switchStatus)}</span>` : ""}${state.switcher ? ICON.up : ICON.down}</button>` : `<span class="li-switch is-static"><strong>${esc(switchLabel)}</strong>${switchStatus ? `<span>${esc(switchStatus)}</span>` : ""}</span>`}
      <span class="li-bar-note" role="status">${esc(note)}</span>
      ${links.map(item => `<button type="button" class="li-link li-bar-link${item.wide ? " li-wide" : ""}" data-li="${item.action}" data-focus="${item.action}" ${item.expanded === undefined ? "" : `aria-expanded="${item.expanded}"`}>${esc(item.label)}</button>`).join("")}
      <span class="li-wide-actions">${button(secondary, "secondary")}${button(primary, "primary")}</span>
      ${short ? (short.href ? "" : `<button type="button" class="li-link li-narrow-action" data-li="${short.action}" ${short.disabled ? "disabled" : ""}>${esc(short.label)}</button>`) : ""}
      ${switcher(state)}
    </header>`;
  }

  function dock(state, view) {
    const {primary} = actions(state, view);
    return `<footer class="li-dock"${primary ? "" : " hidden"}>${button(primary, "primary", "dock")}</footer>`;
  }

  function railItems(state) {
    const items = posts(state).map((post, index) => ({key: index, title: `Post ${index + 1}`, status: statusLabel(state, post, index)}));
    const gaps = state.draft?.gaps?.length || 0;
    if (gaps) items.push({key: "gaps", title: "More context needed", status: `${plural(gaps, "note")} from this run`, short: plural(gaps, "note"), gaps: true});
    return items;
  }

  function rail(state) {
    if (!state.draft) return '<nav class="li-rail" aria-label="Posts in this draft"></nav>';
    return `<nav class="li-rail" aria-label="Posts in this draft">${railItems(state).map(item => `<button type="button" class="li-rail-item${item.gaps && posts(state).length ? " is-gaps" : ""}" data-li="select" data-post="${item.key}" data-focus="rail-${item.key}" ${item.key === state.selected ? 'aria-current="true"' : ""}><span>${esc(item.title)}</span><small>${esc(item.status)}</small></button>`).join("")}</nav>`;
  }

  function switcher(state) {
    if (!state.switcher || !state.draft) return '<div class="li-switcher" hidden></div>';
    return `<div class="li-switcher"><div class="li-switcher-list" role="list">${railItems(state).map(item => `<button type="button" role="listitem" class="li-switcher-item" data-li="select" data-post="${item.key}" ${item.key === state.selected ? 'aria-current="true"' : ""}><span>${esc(item.title)}</span><small>${esc(item.short || item.status)}</small></button>`).join("")}</div><button type="button" class="li-scrim" data-li="switcher" aria-label="Close the post list"></button></div>`;
  }

  function line(state) {
    const message = state.message;
    return message ? `<p class="li-line${message.error ? " is-error" : ""}" role="status">${esc(message.text)}</p>` : "";
  }

  function column(state, view) {
    if (view === "loading") return line(state);
    if (view === "gaps") return gapsView(state);
    const post = current(state);
    if (view === "edit") return editView(state, post);
    if (view === "preview") return previewView(state);
    if (view === "outcome") return outcomeView(state, post);
    return readView(state, post);
  }

  // ---- Reading (LI-12) ----

  function readView(state, post) {
    const who = whoPosts(state, post);
    return `${line(state)}${state.notes ? notesCard(state, post) : ""}
      <div class="li-as is-read">${readPoster(state, who)}<button type="button" class="li-link li-narrow" data-li="notes" aria-expanded="${state.notes}">${state.notes ? "Close notes" : "Draft notes"}</button></div>
      <div class="li-copy">${esc(post.text)}</div>
      ${readImages(state, post.attachments || [])}`;
  }

  function readPoster(state, who) {
    if (who.kind === "chosen") return `<span class="li-mark is-square" aria-hidden="true">${esc(initial(who.account.name))}</span><span>Posting as ${esc(who.account.name)}</span>`;
    if (who.kind === "another_member") return `<span class="li-mark is-square" aria-hidden="true">${esc(initial(who.account.name))}</span><span>Posting as ${esc(who.account.name)}. Only ${esc(firstName(who.account.name))} can post as this profile.</span>`;
    if (who.kind === "not_connected") return '<span class="li-mark is-empty" aria-hidden="true"></span><span class="is-muted">LinkedIn posting isn\'t connected to this project yet.</span><button type="button" class="li-link" data-li="connect">Connect LinkedIn</button>';
    if (who.kind === "disconnected") return `<span class="li-mark is-empty" aria-hidden="true"></span><span class="is-error">${esc(disconnectedText(who.account))}</span>`;
    return '<span class="li-mark is-empty" aria-hidden="true"></span><span class="is-muted">Who posts isn\'t chosen yet.</span><button type="button" class="li-link" data-li="edit">Choose in Edit post</button>';
  }

  const disconnectedText = account => account ? `The ${account.name} ${kindLabel(account)} is no longer connected. Choose another account or reconnect it.` : "This post's LinkedIn account is no longer connected. Choose another account or reconnect it.";

  function readImages(state, items) {
    if (!items.length) return "";
    const described = items.filter(item => item.alt_text?.trim()).length;
    const which = items.length === 1 ? (described ? "described" : "not described")
      : described === items.length ? (items.length === 2 ? "both described" : "all described")
      : described === 0 ? (items.length === 2 ? "neither described" : "none described") : `${described} described`;
    return `<figure class="li-read-images${items.length === 1 ? " is-single" : ""}">
      <div class="li-read-grid">${items.map(item => `<div class="li-media" data-li-media="${esc(item.path)}" data-alt="${esc(item.alt_text || "")}"></div>`).join("")}</div>
      <figcaption>${plural(items.length, "image")} · ${which}</figcaption></figure>`;
  }

  // Drafting writes "Angle: …" and "Format: …" lines; any such line is a row, the rest are notes.
  function noteRows(post) {
    const rows = [], loose = [];
    for (const text of String(post.editor_notes || "").split("\n").map(item => item.trim()).filter(Boolean)) {
      const labelled = /^([A-Z][A-Za-z ]{1,23}):\s+(.+)$/.exec(text);
      if (labelled) rows.push([labelled[1], esc(labelled[2])]);
      else loose.push(esc(text));
    }
    if (loose.length) rows.push(["Notes", loose.join("<br>")]);
    return rows;
  }

  function notesCard(state, post) {
    const sources = new Map((state.draft.sources || []).map(item => [item.id, item]));
    const support = (post.support || []).map(item => {
      const id = String(item.source_path || "").split("#")[1];
      const source = sources.get(id);
      const label = source ? [source.label, source.author].filter(Boolean).join(" · ") : shownName(String(item.source_path || "").split("#")[0]);
      return `<div class="li-support"><span>${esc(label)}</span><q>${esc(item.excerpt)}</q></div>`;
    }).join("");
    const rows = noteRows(post);
    if (support) rows.push(["Supported by", support]);
    if (post.missing_assets?.length) rows.push(["Missing", post.missing_assets.map((item, index) => `<span class="li-missing">${esc(item)} <button type="button" class="li-link" data-li="resolve" data-asset="${index}">Mark resolved</button></span>`).join("")]);
    if (post.readiness !== "ready") rows.push(["Status", `${post.readiness === "needs_asset" ? "Needs media" : "Needs evidence"}. <button type="button" class="li-link" data-li="ready" ${post.missing_assets?.length ? "disabled" : ""}>Mark ready</button>`]);
    return `<section class="li-notes" aria-label="Draft notes"><h2>Draft notes</h2>
      ${rows.length ? `<dl>${rows.map(([label, value]) => `<div><dt>${esc(label)}</dt><dd>${value}</dd></div>`).join("")}</dl>` : '<p class="li-notes-empty">No notes for this post.</p>'}</section>`;
  }

  // ---- Editing (LI-07A, LI-08, LI-13, LI-14) ----

  function editView(state, post) {
    const length = post.text.length;
    return `${line(state)}<div class="li-edit-post">
      ${posterField(state, post)}
      <div class="li-text-box"><label class="li-hidden" for="li-text">Post text</label><textarea id="li-text" class="li-text" data-li-text data-focus="text" maxlength="${MAX_TEXT}" spellcheck="true">${esc(post.text)}</textarea></div>
      <p class="li-count${length > MAX_TEXT ? " is-error" : ""}" data-li-count>${count(length)} / ${count(MAX_TEXT)}</p></div>
      ${imagesField(state, post)}`;
  }

  function posterField(state, post) {
    const who = whoPosts(state, post);
    const mark = (name, muted) => `<span class="li-mark${muted ? " is-muted" : ""}" aria-hidden="true">${esc(initial(name))}</span>`;
    if (who.kind === "not_connected") return `<div class="li-as"><span class="li-mark is-empty" aria-hidden="true"></span><span class="is-muted">LinkedIn posting isn't connected to this project yet.</span><button type="button" class="li-link" data-li="connect">Connect LinkedIn</button></div>`;
    if (who.kind === "none") return `<div class="li-as"><span class="li-mark is-empty" aria-hidden="true"></span><span class="is-muted">There's no LinkedIn account here you can post as. Connect your profile or a page.</span><button type="button" class="li-link" data-li="connect">Connect LinkedIn</button></div>`;
    if (who.kind === "another_member") {
      const page = state.accounts.find(item => item.kind === "organization") || state.accounts[0];
      return `<div class="li-as-wrap"><div class="li-as">${mark(who.account.name)}<span class="is-muted">Posting as</span><span>${esc(who.account.name)}</span><span class="li-kind">profile</span></div>
        <p class="li-as-note">Only ${esc(firstName(who.account.name))} can post as this profile.${page ? ` <button type="button" class="li-link" data-li="target" data-urn="${esc(page.urn)}">Post as ${esc(page.kind === "organization" ? page.name : "yourself")} instead</button>` : ""}</p></div>`;
    }
    if (who.kind === "chosen" && state.accounts.length === 1) return `<div class="li-as">${mark(who.account.name)}<span class="is-muted">Posting as</span><span>${esc(who.account.name)}</span><span class="li-kind">${kindLabel(who.account)}</span></div>`;
    const lost = who.kind === "disconnected";
    const shown = who.account || null;
    const menu = state.menu ? `<ul class="li-menu" role="listbox" aria-label="Post as">${state.accounts.map(item => `<li role="option" aria-selected="${item.urn === post.target || (who.kind === "chosen" && who.account.urn === item.urn)}"><button type="button" data-li="target" data-urn="${esc(item.urn)}" data-focus="option-${esc(item.urn)}"><span class="li-check">${item.urn === shown?.urn && !lost ? ICON.check : ""}</span><span>${esc(item.name)}</span><span class="li-kind">${kindLabel(item)}</span></button></li>`).join("")}</ul>` : "";
    const picker = `<button type="button" class="li-picker${lost ? " is-lost" : ""}" data-li="menu" data-focus="picker" aria-haspopup="listbox" aria-expanded="${state.menu}" ${state.accounts.length ? "" : "disabled"}>${shown ? `<span>${esc(shown.name)}</span><span class="li-kind">${kindLabel(shown)}</span>` : '<span class="is-muted">Choose an account</span>'}${state.menu ? ICON.up : ICON.down}</button>`;
    return `<div class="li-as-wrap"><div class="li-as">${shown ? mark(shown.name, lost) : '<span class="li-mark is-empty" aria-hidden="true"></span>'}<span class="is-muted">Posting as</span><span class="li-picker-wrap">${picker}${menu}</span></div>
      ${lost ? `<p class="li-as-note is-error">${esc(disconnectedText(shown))} <button type="button" class="li-link" data-li="connect">Reconnect</button></p>` : ""}</div>`;
  }

  function imagesField(state, post) {
    const attached = post.attachments || [];
    const uploads = uploadsFor(state, post);
    const used = attached.length + uploads.filter(item => ["queued", "uploading"].includes(item.status)).length;
    return `<section class="li-images" aria-label="Images">
      <header><h2>Images</h2><span class="li-meta">${used} of ${MAX_IMAGES}</span></header>
      <ol class="li-rows">${attached.map((item, index) => imageRow(state, item, index, attached.length)).join("")}${uploads.map(uploadRow).join("")}
        ${state.picker ? pickerRows(state, post, used) : addRow(used)}</ol></section>`;
  }

  function imageRow(state, item, index, total) {
    const known = state.media.get(item.path);
    const facts = [shownName(item.path), known?.width ? `${known.width} × ${known.height}` : ""].filter(Boolean).join(" · ");
    return `<li class="li-row" data-li-row="${index}">
      <button type="button" class="li-grip" data-li-grip="${index}" data-focus="grip-${index}" aria-label="Image ${index + 1} of ${total}. Drag, or use the arrow keys, to change its place.">${ICON.grip}</button>
      <div class="li-thumb li-media" data-li-media="${esc(item.path)}" data-alt="${esc(item.alt_text || "")}"></div>
      <div class="li-row-body"><span class="li-meta" data-li-facts="${esc(item.path)}">${esc(facts)}</span>
        <label class="li-hidden" for="li-alt-${index}">Description of ${esc(shownName(item.path))}</label>
        <input id="li-alt-${index}" class="li-alt" data-li-alt="${index}" data-focus="alt-${index}" maxlength="1000" value="${esc(item.alt_text || "")}" placeholder="Describe this image for people who can't see it" /></div>
      <button type="button" class="li-remove" data-li="remove" data-index="${index}" aria-label="Remove ${esc(shownName(item.path))}">${ICON.remove}</button></li>`;
  }

  function uploadRow(item) {
    const remove = `<button type="button" class="li-remove" data-li="cancel-upload" data-upload="${item.id}" aria-label="Remove ${esc(item.name)}">${ICON.remove}</button>`;
    if (item.status === "queued" || item.status === "uploading") {
      const shown = item.status === "uploading" ? `uploading ${(item.loaded / 1_000_000).toFixed(1)} of ${bytes(item.size)}` : "waiting to upload";
      return `<li class="li-row is-uploading" data-li-upload-row="${item.id}"><span class="li-grip is-empty"></span>
        <div class="li-thumb-wrap"><div class="li-thumb"><img alt="" src="${esc(item.preview)}" /></div><span class="li-progress"><span style="width:${item.size ? Math.round(100 * item.loaded / item.size) : 0}%"></span></span></div>
        <div class="li-row-body"><span class="li-meta" data-li-progress-text>${esc(item.name)} · ${esc(shown)}</span>
          <label class="li-hidden" for="li-upload-alt-${item.id}">Description of ${esc(item.name)}</label>
          <input id="li-upload-alt-${item.id}" class="li-alt" data-li-upload-alt="${item.id}" data-focus="upload-alt-${item.id}" maxlength="1000" value="${esc(item.alt_text)}" placeholder="Describe this image for people who can't see it" /></div>${remove}</li>`;
    }
    const reason = {
      too_large: "Images can be 5 MB at most. Export a smaller copy and upload it again.",
      wrong_type: "LinkedIn posts from Tin take PNG or JPEG images. Save it as one of those and upload it again.",
      failed: item.error || "Tin couldn't store this image. Your connection may have dropped.",
    }[item.status];
    return `<li class="li-row is-refused" data-li-upload-row="${item.id}"><span class="li-grip is-empty"></span>
      <div class="li-thumb is-placeholder">${ICON.image}</div>
      <div class="li-row-body"><span class="li-meta">${esc(item.name)} · ${esc(bytes(item.size))}</span>
        <p class="li-refusal"><span>${esc(reason)}</span>${item.status === "failed" ? ` <button type="button" class="li-link" data-li="retry-upload" data-upload="${item.id}">Try again</button>` : ""}</p></div>${remove}</li>`;
  }

  function addRow(used) {
    if (used >= MAX_IMAGES) return `<li class="li-row is-add is-full"><span class="li-grip is-empty"></span><span class="li-thumb is-add" aria-hidden="true">${ICON.plus}</span>
      <div class="li-row-body"><span>This post has 20 images, LinkedIn's limit. Remove one to add another.</span><span class="li-meta">${used} of ${MAX_IMAGES}</span></div><span class="li-remove is-empty"></span></li>`;
    return `<li class="li-row is-add"><span class="li-grip is-empty"></span><label class="li-thumb is-add" aria-hidden="true">${ICON.plus}<input type="file" tabindex="-1" data-li-upload accept="image/png,image/jpeg" multiple /></label>
      <div class="li-row-body"><span class="li-add-actions"><label class="li-link li-upload">Upload an image<input type="file" data-li-upload data-focus="upload" accept="image/png,image/jpeg" multiple /></label><span class="is-muted">or</span><button type="button" class="li-link" data-li="pick" data-focus="pick">choose one from Files</button></span>
        <span class="li-meta">PNG or JPEG · up to 5 MB · 20 per post</span></div><span class="li-remove is-empty"></span></li>`;
  }

  function pickerRows(state, post, used) {
    const picker = state.picker;
    const filter = picker.filter.trim().toLowerCase();
    const shown = picker.files.filter(item => !filter || item.path.toLowerCase().includes(filter)).slice(0, 60);
    const attached = new Set((post.attachments || []).map(item => item.path));
    const rows = picker.loading ? '<p class="li-picker-empty">Loading images in Files…</p>'
      : !picker.files.length ? '<p class="li-picker-empty">No PNG or JPEG images in Files yet. Upload one instead.</p>'
      : !shown.length ? '<p class="li-picker-empty">No image names match.</p>'
      : shown.map(item => {
        const known = state.media.get(item.path);
        const facts = [shownFolder(item.path), known?.width ? `${known.width} × ${known.height}` : "", known?.size ? bytes(known.size) : ""].filter(Boolean).join(" · ");
        const added = attached.has(item.path);
        return `<li class="li-file"><div class="li-file-thumb li-media" data-li-media="${esc(item.path)}" data-lazy></div>
          <div class="li-file-body"><span>${esc(shownName(item.path))}</span><span class="li-meta" data-li-facts="${esc(item.path)}" data-folder="${esc(shownFolder(item.path))}">${esc(facts)}</span></div>
          <span class="li-file-action">${added ? '<span class="is-muted">Added</span>' : `<button type="button" class="li-link" data-li="add-file" data-path="${esc(item.path)}" ${used >= MAX_IMAGES ? "disabled" : ""}>Add</button>`}</span></li>`;
      }).join("");
    return `<li class="li-picker-block"><div class="li-picker-head"><span>Images in Files</span>
      <label class="li-hidden" for="li-filter">Filter images by name</label><input id="li-filter" class="li-filter" data-li-filter data-focus="filter" value="${esc(picker.filter)}" placeholder="Filter by name" autocomplete="off" />
      <button type="button" class="li-link" data-li="pick-done" data-focus="pick-done">Done</button></div><ul class="li-files">${rows}</ul></li>`;
  }

  // ---- Preview and outcome (LI-09, LI-10) ----

  // LinkedIn lays images out by count: one wide, two side by side, three as one over two,
  // more as one over three with the rest counted on the last.
  function card(account, text, attachments, exact) {
    const n = attachments.length;
    const layout = ["", "one", "two", "three"][n] || "many";
    const shown = n > 4 ? attachments.slice(0, 4) : attachments;
    return `<article class="li-card" aria-label="${exact ? "Exact LinkedIn preview" : "The post"}" ${exact ? "data-li-exact" : ""}>
      <div class="li-card-body"><div class="li-card-head"><span class="li-card-mark" aria-hidden="true">${esc(initial(account?.name))}</span><span><strong>${esc(account?.name || "LinkedIn")}</strong><small>${account?.kind === "organization" ? "Page" : "Profile"}${exact ? " · now" : ""}</small></span></div>
      <div class="li-card-text">${esc(text)}</div></div>
      ${n ? `<div class="li-card-images is-${layout}">${shown.map((item, index) => `<div class="li-media" data-li-media="${esc(item.path)}" data-alt="${esc(item.alt_text || "")}" ${exact && item.url ? `data-url="${esc(item.url)}"` : ""}>${index === 3 && n > 4 ? `<span class="li-more">+${n - 4}</span>` : ""}</div>`).join("")}</div>` : ""}</article>`;
  }

  function previewView(state) {
    const exact = state.preview;
    const n = exact.attachments?.length || 0;
    const parts = n === 0 ? "the text and the account" : n === 1 ? "the text, the image and the account" : `the text, ${n === 2 ? "both" : `all ${n}`} images in this order, and the account`;
    return `${line(state)}<p class="li-sentence">This is exactly what LinkedIn receives: ${parts}.</p>
      ${card(exact.account, exact.text, exact.attachments || [], true)}
      <p class="li-sentence">Publishing posts it now as ${esc(asWho(exact.account))}. Tin records your approval first, so trying again never posts twice, and saves a report in Files.</p>`;
  }

  function outcomeView(state, post) {
    const item = shownPublication(state, post);
    const account = item.account?.name ? item.account : whoPosts(state, post).account || {};
    const sentence = {
      publishing: `Sending the post as ${asWho(account)}. You can leave this page; it finishes on its own and shows up in Activity.`,
      published: `Published on LinkedIn as ${asWho(account)}. Tin saved a report with the link in Files.`,
      uncertain: "Tin didn't hear back from LinkedIn, so it won't send this post again. Check the page: if the post isn't there, preview it again.",
      not_published: item.message || "LinkedIn didn't publish this post. Change it, then preview it again.",
    }[item.state];
    const tone = item.state === "published" ? " is-strong" : ["uncertain", "not_published"].includes(item.state) ? " is-error" : "";
    const same = item.text === post.text;
    return `${line(state)}<p class="li-sentence${tone}" role="status">${esc(sentence)}</p>
      ${card(account, same ? post.text : item.text, same ? post.attachments || [] : [], false)}`;
  }

  // ---- More context needed (LI-11) ----

  function gapsView(state) {
    const gaps = state.draft.gaps || [];
    return `${line(state)}<div class="li-gaps"><h1>More context needed</h1>
      <p>${posts(state).length ? "What this run couldn't support. Add it to your selected notes and the next run can use it." : "This run wrote no posts. Add what's missing to your selected notes, then draft again."}</p>
      <ul>${gaps.map(gap => `<li>${esc(gap.reason)}</li>`).join("")}</ul></div>`;
  }

  function sourcesPath(state) {
    for (const post of posts(state)) for (const item of post.support || []) {
      const path = String(item.source_path || "").split("#")[0];
      if (path) return path;
    }
    return null;
  }

  // ---- Events ----

  async function act(state, action, target) {
    if (state.closed) return;
    const post = current(state);
    if (action === "close") {state.close(); state.context.onClose?.(); return;}
    if (action === "switcher") {state.switcher = !state.switcher; paintChrome(state); return;}
    if (action === "select") {
      const key = target.dataset.post === "gaps" ? "gaps" : Number(target.dataset.post);
      state.selected = key; state.switcher = false; state.notes = false; state.menu = false; state.picker = null;
      state.preview = null; state.publishRequest = null; state.message = null;
      // Editing carries on from post to post, except into one with an outcome to see first.
      state.outcome = showsOutcome(state);
      if (state.mode !== "edit" || key === "gaps" || state.outcome) state.mode = "read";
      render(state);
      // Another post starts at its top, wherever the last one was read to.
      state.root.closest("main")?.scrollTo?.({top: 0});
      window.scrollTo({top: 0});
      return;
    }
    if (state.busy && !["notes", "cancel-upload", "pick-done", "menu"].includes(action)) return;
    if (action === "notes") {state.notes = !state.notes; render(state); return;}
    if (action === "edit") {state.mode = "edit"; state.outcome = false; state.preview = null; state.message = null; render(state); state.root.querySelector(".li-text")?.focus(); return;}
    if (action === "back") {state.outcome = false; state.message = null; render(state); return;}
    if (action === "cancel") {
      for (const item of state.uploads) item.request?.abort();
      state.uploads = []; state.draft = copy(state.saved); state.saveRequest = null;
      state.mode = "read"; state.menu = false; state.picker = null; state.message = null;
      state.outcome = showsOutcome(state);
      render(state); return;
    }
    if (action === "save") {if (await save(state)) {state.mode = "read"; state.picker = null; render(state);} return;}
    if (action === "preview") return preview(state, post);
    if (action === "publish") return publish(state);
    if (action === "connect") {state.context.connect?.(); return;}
    if (action === "sources") {const path = sourcesPath(state); if (path) state.context.openFile?.(path, state.revision); return;}
    if (action === "menu") {state.menu = !state.menu; render(state); state.root.querySelector(state.menu ? '.li-menu [aria-selected="true"] button, .li-menu button' : ".li-picker")?.focus(); return;}
    if (action === "target") {
      post.target = target.dataset.urn; state.menu = false; edited(state);
      render(state); state.root.querySelector(".li-picker, .li-text")?.focus(); return;
    }
    if (action === "ready") {if (!post.missing_assets?.length) {post.readiness = "ready"; edited(state); render(state);} return;}
    if (action === "resolve") {post.missing_assets.splice(Number(target.dataset.asset), 1); edited(state); render(state); return;}
    if (action === "remove") {post.attachments.splice(Number(target.dataset.index), 1); edited(state); render(state); return;}
    if (action === "cancel-upload") {
      const item = state.uploads.find(entry => entry.id === target.dataset.upload);
      item?.request?.abort();
      state.uploads = state.uploads.filter(entry => entry !== item);
      if (item?.preview) URL.revokeObjectURL(item.preview);
      render(state); pump(state); return;
    }
    if (action === "retry-upload") {
      const item = state.uploads.find(entry => entry.id === target.dataset.upload);
      if (item) {item.status = "queued"; item.loaded = 0; item.error = null; render(state); pump(state);}
      return;
    }
    if (action === "pick") return openPicker(state);
    if (action === "pick-done") {state.picker = null; render(state); state.root.querySelector('[data-focus="pick"]')?.focus(); return;}
    if (action === "add-file") {
      const used = (post.attachments || []).length + uploadsFor(state, post).filter(item => ["queued", "uploading"].includes(item.status)).length;
      if (used >= MAX_IMAGES) return;
      post.attachments ||= [];
      post.attachments.push({type: "image", path: target.dataset.path, alt_text: ""});
      edited(state); render(state); state.root.querySelector('[data-focus="filter"]')?.focus();
    }
  }

  function typed(state, field) {
    const post = current(state);
    if (field.matches("[data-li-text]")) {
      post.text = field.value; fit(field);
      const counter = state.root.querySelector("[data-li-count]");
      if (counter) counter.textContent = `${count(field.value.length)} / ${count(MAX_TEXT)}`;
      edited(state, true);
    } else if (field.matches("[data-li-alt]")) {
      post.attachments[Number(field.dataset.liAlt)].alt_text = field.value; edited(state, true);
    } else if (field.matches("[data-li-upload-alt]")) {
      const item = state.uploads.find(entry => entry.id === field.dataset.liUploadAlt);
      if (item) item.alt_text = field.value;
    } else if (field.matches("[data-li-filter]")) {
      state.picker.filter = field.value;
      const list = state.root.querySelector(".li-files");
      const holder = document.createElement("div");
      holder.innerHTML = pickerRows(state, post, (post.attachments || []).length);
      list?.replaceWith(holder.querySelector(".li-files"));
      loadMedia(state);
    }
  }

  // An edit changes what a preview froze; the bar says so without redrawing the column.
  function edited(state, quiet = false) {
    state.preview = null; state.publishRequest = null; state.saveRequest = null;
    if (state.message?.saved) state.message = null;
    if (quiet) paintChrome(state);
  }

  function keyed(state, event) {
    if (event.key === "Escape" && (state.menu || state.switcher)) {
      state.menu = false; state.switcher = false; render(state);
      state.root.querySelector(".li-picker, .li-switch")?.focus(); return;
    }
    if (state.menu && ["ArrowDown", "ArrowUp"].includes(event.key) && event.target.closest(".li-menu")) {
      const options = [...state.root.querySelectorAll(".li-menu button")];
      const at = options.indexOf(event.target);
      options[(at + (event.key === "ArrowDown" ? 1 : -1) + options.length) % options.length]?.focus();
      event.preventDefault(); return;
    }
    const grip = event.target.closest("[data-li-grip]");
    if (grip && ["ArrowUp", "ArrowDown"].includes(event.key)) {
      const from = Number(grip.dataset.liGrip);
      const to = from + (event.key === "ArrowUp" ? -1 : 1);
      if (move(state, from, to)) state.root.querySelector(`[data-focus="grip-${to}"]`)?.focus();
      event.preventDefault();
    }
  }

  function move(state, from, to) {
    const items = current(state)?.attachments || [];
    if (from === to || to < 0 || to >= items.length) return false;
    const [item] = items.splice(from, 1);
    items.splice(to, 0, item);
    edited(state); render(state);
    return true;
  }

  // Rows drag by their grip; the arrow keys on the grip do the same without a pointer.
  function bindDragging(state) {
    let from = null;
    state.root.addEventListener("pointerdown", event => {
      const grip = event.target.closest("[data-li-grip]");
      const row = grip?.closest(".li-row");
      if (row) {row.draggable = true; from = Number(grip.dataset.liGrip);}
    });
    state.root.addEventListener("dragstart", event => {
      if (from === null) return;
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", String(from));
      event.target.closest(".li-row")?.classList.add("is-dragging");
    });
    state.root.addEventListener("dragover", event => {
      const row = event.target.closest("[data-li-row]");
      if (from === null || !row) return;
      event.preventDefault();
      state.root.querySelectorAll(".li-row.is-target").forEach(node => node.classList.remove("is-target"));
      row.classList.add("is-target");
    });
    state.root.addEventListener("drop", event => {
      const row = event.target.closest("[data-li-row]");
      if (from === null || !row) return;
      event.preventDefault();
      const to = Number(row.dataset.liRow);
      const start = from; from = null;
      move(state, start, to);
    });
    state.root.addEventListener("dragend", () => {
      from = null;
      state.root.querySelectorAll(".li-row").forEach(node => {node.draggable = false; node.classList.remove("is-dragging", "is-target");});
    });
  }

  // ---- Saving, previewing and publishing ----

  async function save(state) {
    if (!dirty(state)) return true;
    if (state.uploads.some(item => ["queued", "uploading"].includes(item.status))) {
      state.message = {text: "Wait for the images to finish uploading, then save.", error: true}; render(state); return false;
    }
    state.busy = true; paintChrome(state);
    try {
      state.saveRequest ||= crypto.randomUUID();
      const result = await state.context.api(apiUrl(state, "drafts"), {
        method: "PUT", body: JSON.stringify({path: state.context.path, expected_revision: state.revision, request_id: state.saveRequest, draft: state.draft}),
      });
      if (state.closed) return false;
      state.revision = result.revision; state.savedSha = result.sha256;
      state.saved = copy(result.draft); state.draft = copy(result.draft); state.saveRequest = null;
      state.uploads = state.uploads.filter(item => !["too_large", "wrong_type"].includes(item.status));
      state.message = {text: "Saved to Files.", saved: true};
      return true;
    } catch (error) {
      if (!state.closed) {state.message = {text: `${error.message} Your edits are still here.`, error: true}; render(state);}
      return false;
    } finally {state.busy = false; if (!state.closed) paintChrome(state);}
  }

  async function preview(state, post) {
    if (!post || state.busy) return;
    if (dirty(state) && !(await save(state))) return;
    state.busy = true; state.message = {text: "Preparing the exact preview…"}; render(state);
    try {
      const result = await state.context.api(apiUrl(state, "preview"), {method: "POST", body: JSON.stringify({path: state.context.path, post_id: post.id})});
      if (state.closed) return;
      state.preview = result; state.publishRequest = null; state.mode = "preview"; state.outcome = false;
      state.notes = false; state.picker = null; state.message = null;
    } catch (error) {
      if (state.closed) return;
      state.mode = state.mode === "preview" ? "read" : state.mode;
      state.message = {text: error.message, error: true};
      // A post that isn't ready says why in its notes.
      if (post.readiness !== "ready" || post.missing_assets?.length) state.notes = true;
    } finally {state.busy = false; render(state);}
  }

  async function publish(state) {
    const exact = state.preview;
    const post = current(state);
    if (state.busy || !exact?.preview_token || dirty(state)) return;
    state.busy = true; state.publishRequest ||= crypto.randomUUID(); paintChrome(state);
    try {
      const result = await state.context.api(apiUrl(state, "publish"), {
        method: "POST", body: JSON.stringify({preview_token: exact.preview_token, request_id: state.publishRequest}),
      });
      if (state.closed) return;
      state.publications[post.id] = {
        post_id: post.id, run_id: result.run_id, account: exact.account, text: exact.text,
        state: result.url ? "published" : "publishing", url: result.url || null, at: new Date().toISOString(), message: null,
      };
      state.preview = null; state.mode = "read"; state.outcome = true; state.message = null;
      render(state); follow(state);
      await state.context.onPublished?.();
    } catch (error) {
      if (!state.closed) {
        state.message = {text: `${error.message} It may still be on its way: retrying uses the same request, so it can't post twice.`, error: true};
        render(state);
      }
    } finally {state.busy = false; if (!state.closed) paintChrome(state);}
  }

  // While a post publishes, read its outcome from this file's publications.
  function follow(state, delay = 2500) {
    clearTimeout(state.followTimer);
    if (state.closed || !Object.values(state.publications).some(item => item.state === "publishing")) return;
    state.followTimer = setTimeout(async () => {
      try {
        const result = await state.context.api(`${apiUrl(state, "publications")}?${new URLSearchParams({path: state.context.path})}`);
        if (state.closed) return;
        state.publications = {...state.publications, ...(result.publications || {})};
        if (state.mode === "edit") paintChrome(state); else render(state);
      } catch (_error) {/* Keep following; Activity shows the run meanwhile. */}
      follow(state, Math.min(delay * 1.5, 15000));
    }, delay);
  }

  function minutesLeft(exact) {
    const until = Date.parse(exact?.valid_until || "");
    return Number.isFinite(until) ? Math.max(0, Math.ceil((until - Date.now()) / 60000)) : 15;
  }

  function tickPreview(state) {
    clearInterval(state.clock);
    if (state.mode === "preview" && state.preview) state.clock = setInterval(() => paintChrome(state), 30000);
  }

  function when(at) {
    const date = new Date(at);
    if (Number.isNaN(date.getTime())) return "";
    const time = date.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
    return date.toDateString() === new Date().toDateString() ? time : `${date.toLocaleDateString([], {month: "short", day: "numeric"})}, ${time}`;
  }

  function batchDay(path, short = false) {
    const day = /^(\d{4}-\d{2}-\d{2})/.exec(path.split("/").at(-1))?.[1];
    if (!day) return short ? "" : `${shownName(path).replace(/\.json$/, "")} · `;
    if (!short) return `${day} · `;
    return ` · ${new Date(`${day}T00:00:00Z`).toLocaleDateString("en-US", {month: "short", day: "numeric", timeZone: "UTC"})}`;
  }

  // ---- Images ----

  function queueUploads(state, files) {
    const post = current(state);
    if (!post) return;
    for (const file of files) {
      const used = (post.attachments || []).length + uploadsFor(state, post).filter(item => ["queued", "uploading"].includes(item.status)).length;
      const item = {id: crypto.randomUUID(), postId: post.id, file, name: file.name.split(/[\\/]/).at(-1), size: file.size, loaded: 0, alt_text: "", status: "queued"};
      if (!EXTENSIONS[file.type]) item.status = "wrong_type";
      else if (file.size > MAX_BYTES) item.status = "too_large";
      else if (used >= MAX_IMAGES) break;
      else item.preview = URL.createObjectURL(file);
      state.uploads.push(item);
    }
    state.picker = null;
    render(state); pump(state);
  }

  // One upload at a time: each lands on the revision the last one made.
  async function pump(state) {
    if (state.uploading || state.closed) return;
    const item = state.uploads.find(entry => entry.status === "queued");
    if (!item) return;
    state.uploading = true; item.status = "uploading";
    paintUpload(state, item, true);
    try {
      // The extension comes from the file's type: Tin stores a file only when the two match,
      // and a long or mislabelled name must not lose or contradict it.
      const base = item.name.replace(/\.[^.]*$/, "").replace(/[^a-zA-Z0-9._-]/g, "-").slice(0, 80) || "image";
      const path = `${MEDIA_DIR}/${state.context.runId || "manual"}/${crypto.randomUUID()}-${base}${EXTENSIONS[item.file.type]}`;
      const query = new URLSearchParams({path, expected_revision: state.revision, request_id: crypto.randomUUID()});
      const url = `/api/projects/${encodeURIComponent(state.context.projectId)}/files/upload?${query}`;
      const uploaded = state.context.upload
        ? await state.context.upload(url, item.file, {onProgress: loaded => {item.loaded = loaded; paintUpload(state, item);}, attach: request => {item.request = request;}})
        : await (await state.context.rawFetch(url, {method: "POST", headers: {"Content-Type": item.file.type, Accept: "application/json"}, body: item.file})).json();
      if (state.closed || !state.uploads.includes(item)) return;
      state.revision = uploaded.revision;
      const post = posts(state).find(entry => entry.id === item.postId);
      post.attachments ||= [];
      post.attachments.push({type: "image", path: uploaded.path, alt_text: item.alt_text});
      // The local preview stands in until the stored copy is read back.
      state.media.set(uploaded.path, {url: item.preview, size: item.size});
      state.uploads = state.uploads.filter(entry => entry !== item);
      edited(state);
      if (state.mode === "edit") render(state); else paintChrome(state);
    } catch (error) {
      if (state.closed || !state.uploads.includes(item)) return;
      item.status = "failed"; item.request = null;
      item.error = /409|changed|revision/i.test(error.message || "") ? "Files changed while this uploaded. Try again to upload it onto the newer version." : null;
      if (state.mode === "edit") render(state);
    } finally {
      state.uploading = false;
      if (!state.closed) pump(state);
    }
  }

  function paintUpload(state, item, full = false) {
    const row = state.root.querySelector(`[data-li-upload-row="${item.id}"]`);
    if (!row || full) {if (state.mode === "edit") render(state); return;}
    const bar = row.querySelector(".li-progress span");
    if (bar) bar.style.width = `${item.size ? Math.round(100 * item.loaded / item.size) : 0}%`;
    const text = row.querySelector("[data-li-progress-text]");
    if (text) text.textContent = `${item.name} · uploading ${(item.loaded / 1_000_000).toFixed(1)} of ${bytes(item.size)}`;
  }

  async function openPicker(state) {
    state.picker = {files: [], filter: "", loading: true};
    render(state); state.root.querySelector('[data-focus="filter"]')?.focus();
    try {
      const result = await state.context.api(`/api/projects/${encodeURIComponent(state.context.projectId)}/files?include_modified=true`);
      if (state.closed || !state.picker) return;
      state.picker.files = (result.files || []).filter(item => IMAGE_PATH.test(item.path))
        .sort((a, b) => String(b.modified_at || "").localeCompare(String(a.modified_at || "")) || a.path.localeCompare(b.path));
      state.picker.loading = false;
    } catch (error) {
      if (state.closed || !state.picker) return;
      state.picker = null; state.message = {text: error.message, error: true};
    }
    render(state); state.root.querySelector('[data-focus="filter"]')?.focus();
  }

  // Images are read from Files once; size and pixels fill in the rows that name them.
  function loadMedia(state) {
    state.observer?.disconnect();
    const observer = state.observer = "IntersectionObserver" in window ? new IntersectionObserver(entries => {
      for (const entry of entries) if (entry.isIntersecting) {observer.unobserve(entry.target); show(state, entry.target);}
    }, {root: null, rootMargin: "200px"}) : null;
    for (const node of state.root.querySelectorAll("[data-li-media]")) {
      if (node.dataset.lazy !== undefined && observer && !state.media.get(node.dataset.liMedia)?.url) observer.observe(node);
      else show(state, node);
    }
  }

  async function show(state, node) {
    const path = node.dataset.liMedia;
    const supplied = node.dataset.url;
    const allowed = supplied?.startsWith(`/api/projects/${encodeURIComponent(state.context.projectId)}/files/raw?`) ? supplied : null;
    let item = state.media.get(path);
    try {
      if (!item?.url) {
        const source = allowed || `/api/projects/${encodeURIComponent(state.context.projectId)}/files/raw?${new URLSearchParams({path, revision: state.revision})}`;
        item = state.media.get(path) || {};
        if (!item.loading) {
          item.loading = (async () => {
            const response = await state.context.rawFetch(source, {headers: {Accept: "image/*"}});
            if (!response.ok) throw new Error("unavailable");
            const blob = await response.blob();
            return {url: URL.createObjectURL(blob), size: blob.size};
          })();
          state.media.set(path, item);
        }
        Object.assign(item, await item.loading);
      }
      if (state.closed || !node.isConnected) return;
      const image = node.querySelector("img") || document.createElement("img");
      image.alt = node.dataset.alt || "";
      image.decoding = "async";
      if (image.getAttribute("src") !== item.url) image.src = item.url;
      if (!image.isConnected) node.prepend(image);
      if (!item.width) {
        await image.decode().catch(() => {});
        item.width = image.naturalWidth; item.height = image.naturalHeight;
      }
      for (const facts of state.root.querySelectorAll(`[data-li-facts="${CSS.escape(path)}"]`)) {
        const sized = item.width ? `${item.width} × ${item.height}` : "";
        facts.textContent = facts.dataset.folder !== undefined
          ? [facts.dataset.folder, sized, item.size ? bytes(item.size) : ""].filter(Boolean).join(" · ")
          : [shownName(path), sized].filter(Boolean).join(" · ");
      }
    } catch (_error) {
      if (node.isConnected) {node.classList.add("is-placeholder"); node.innerHTML = ICON.image; node.title = "Preview unavailable";}
    }
  }

  function fit(field) {
    field.style.height = "auto";
    field.style.height = `${Math.max(176, field.scrollHeight + 2)}px`;
  }

  window.TinLinkedInPosts = {open, close: () => active?.close()};
})();
