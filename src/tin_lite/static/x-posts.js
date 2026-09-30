/* X drafts are ordinary project files. Publication is a separate exact-preview effect. */
(() => {
  const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
  const copy = value => JSON.parse(JSON.stringify(value));
  const mediaType = file => file.type === "image/png" || file.type === "image/jpeg" ? "image" : file.type === "video/mp4" ? "video" : null;
  const draftUrl = projectId => `/api/projects/${encodeURIComponent(projectId)}/x/drafts`;
  const readUrl = (projectId, path, revision) => `/api/projects/${encodeURIComponent(projectId)}/files/raw?${new URLSearchParams({path, revision})}`;
  let active = null;
  const sessions = new Map();
  const sessionKey = context => `${context.actorId || ""}:${context.projectId}:${context.path}`;

  function open(context) {
    const key = sessionKey(context);
    if (active?.key === key && active.root.isConnected) return;
    active?.close();
    const root = document.createElement("section");
    root.className = "x-posts-reader";
    root.setAttribute("aria-label", "X post drafts");
    if (context.host) context.host.replaceChildren(root);
    else document.body.append(root);
    const kept = sessions.get(key);
    const state = {root, context, key, revision: null, draft: null, dirty: false, busy: false,
      preview: null, previewPost: null, publishRequest: null, saveRequest: null, message: "",
      mediaUrls: new Map(), closed: false, selected: 0, editing: false, notes: false,
      picking: false, files: [], ...kept};
    state.close = () => {
      sessions.set(key, {draft: state.draft && copy(state.draft), revision: state.revision,
        dirty: state.dirty, selected: state.selected, saveRequest: state.saveRequest});
      // In-memory unsaved edits survive navigation, but a preview never survives a close.
      state.closed = true;
      for (const url of state.mediaUrls.values()) URL.revokeObjectURL(url);
      state.mediaUrls.clear();
      window.removeEventListener("beforeunload", state.beforeUnload);
      root.remove();
      if (active === state) active = null;
    };
    state.beforeUnload = event => {
      if (state.dirty) {event.preventDefault(); event.returnValue = "";}
    };
    window.addEventListener("beforeunload", state.beforeUnload);
    active = state;
    if (state.dirty && state.draft) {
      state.message = "Your unsaved edits are still here.";
      render(state);
    } else load(state);
  }

  async function load(state) {
    state.message = "Loading drafts…"; render(state);
    try {
      const result = await state.context.api(`${draftUrl(state.context.projectId)}?${new URLSearchParams({path: state.context.path})}`);
      if (state.closed) return;
      if (result?.draft?.schema_version !== "tin.social.x_draft.v1" || !Array.isArray(result.draft.posts)) throw new Error("This file is not an X draft. Open it in Files.");
      state.revision = result.revision;
      state.draft = copy(result.draft);
      state.selected = Math.min(state.selected, state.draft.posts.length - 1);
      state.dirty = false; state.preview = null; state.previewPost = null; state.saveRequest = null;
      state.message = ""; render(state);
    } catch (error) { if (!state.closed) {state.message = error.message; render(state);} }
  }

  function changed(state) {
    state.dirty = true;
    state.preview = null; state.previewPost = null; state.publishRequest = null; state.saveRequest = null;
    state.message = "Unsaved changes. Preview will save this draft first.";
    renderStatus(state);
    state.root.querySelector("[data-x-exact]")?.remove();
  }

  async function save(state) {
    if (!state.dirty) return true;
    if (state.busy) return false;
    state.busy = true; renderStatus(state);
    try {
      state.saveRequest ||= crypto.randomUUID();
      const result = await state.context.api(draftUrl(state.context.projectId), {
        method: "PUT", body: JSON.stringify({path: state.context.path, expected_revision: state.revision, request_id: state.saveRequest, draft: state.draft}),
      });
      if (state.closed) return false;
      state.revision = result.revision;
      state.draft = copy(result.draft);
      state.selected = Math.min(state.selected, state.draft.posts.length - 1);
      state.dirty = false; state.saveRequest = null;
      state.message = "Draft saved to Files.";
      render(state);
      return true;
    } catch (error) {
      if (!state.closed) {state.message = `${error.message} Your edits are still here. Reload only if you want to discard them.`; renderStatus(state);}
      return false;
    } finally {state.busy = false; if (!state.closed) renderStatus(state);}
  }

  async function preview(state, postId) {
    if (state.busy) return;
    if (state.dirty && !(await save(state))) return;
    state.busy = true; state.message = "Preparing exact preview…"; renderStatus(state);
    try {
      const result = await state.context.api(`/api/projects/${encodeURIComponent(state.context.projectId)}/x/preview`, {
        method: "POST", body: JSON.stringify({path: state.context.path, post_id: postId}),
      });
      if (state.closed) return;
      state.preview = result; state.previewPost = postId; state.publishRequest = null;
      state.editing = false; state.picking = false;
      state.message = "Check the account, text and attachments. Publishing sends this post now.";
      render(state);
    } catch (error) {if (!state.closed) {state.message = error.message; renderStatus(state);}}
    finally {state.busy = false; if (!state.closed) renderStatus(state);}
  }

  async function publish(state) {
    if (state.busy || !state.preview?.preview_token || state.dirty) return;
    state.busy = true;
    state.publishRequest ||= crypto.randomUUID();
    state.message = "Starting approved delivery…"; renderStatus(state);
    try {
      const result = await state.context.api(`/api/projects/${encodeURIComponent(state.context.projectId)}/x/publish`, {
        method: "POST", body: JSON.stringify({preview_token: state.preview.preview_token, request_id: state.publishRequest}),
      });
      if (state.closed) return;
      state.preview = null; state.previewPost = null;
      state.message = result.url ? `Published: ${result.url}` : `Approved delivery ${result.status || "queued"}. Follow its progress in Activity.`;
      render(state);
      await state.context.onPublished?.();
    } catch (error) {
      if (!state.closed) {state.message = `${error.message} Delivery status may be uncertain. Retrying uses the same request ID; check Activity before trying again.`; renderStatus(state);}
    } finally {state.busy = false; if (!state.closed) renderStatus(state);}
  }

  async function upload(state, post, file) {
    const type = mediaType(file);
    if (!type) {state.message = "Choose a PNG, JPEG or MP4 file."; renderStatus(state); return;}
    if (file.size > (type === "video" ? 64_000_000 : 5_000_000)) {
      state.message = type === "video" ? "Choose an MP4 no larger than 64 MB." : "Choose an image no larger than 5 MB.";
      renderStatus(state); return;
    }
    const attachments = post.attachments || [];
    if (type === "video" ? attachments.length > 0 : attachments.some(item => item.type === "video") || attachments.length >= 4) {
      state.message = "Use up to four images or one video per post."; renderStatus(state); return;
    }
    state.busy = true; state.message = "Uploading to this project's Files…"; renderStatus(state);
    try {
      const filename = file.name.split(/[\\/]/).at(-1).replace(/[^a-zA-Z0-9._-]/g, "-").slice(0, 100) || "attachment";
      const path = `social/x/media/${state.context.runId}/${crypto.randomUUID()}-${filename}`;
      const query = new URLSearchParams({path, expected_revision: state.revision, request_id: crypto.randomUUID()});
      const response = await state.context.rawFetch(`/api/projects/${encodeURIComponent(state.context.projectId)}/files/upload?${query}`, {
        method: "POST", headers: {"Content-Type": file.type, Accept: "application/json"}, body: file,
      });
      const uploaded = await response.json();
      if (state.closed) return;
      state.revision = uploaded.revision;
      post.attachments ||= [];
      post.attachments.push({type, path: uploaded.path, alt_text: ""});
      changed(state);
      state.message = "Media uploaded to Files. Save the draft to attach it to this post.";
      render(state);
    } catch (error) {if (!state.closed) {state.message = error.message; renderStatus(state);}}
    finally {state.busy = false; if (!state.closed) renderStatus(state);}
  }

  function renderStatus(state) {
    const status = state.root.querySelector("[data-x-status]");
    if (status) {status.textContent = state.message; status.hidden = !state.message;}
    state.root.querySelectorAll("button[data-x-action]").forEach(button => {button.disabled = state.busy || button.dataset.xEdgeDisabled === "true";});
    state.root.querySelectorAll("[data-x-text], [data-x-alt], [data-x-path], [data-x-upload]").forEach(field => {field.disabled = state.busy;});
  }

  function mediaLine(item, index, postIndex, count, supportsAltText) {
    return `<div class="x-posts-media" data-x-media-row="${postIndex}:${index}">
      <div class="x-posts-media-preview" data-x-media-preview data-path="${esc(item.path)}" data-type="${esc(item.type)}">${esc(item.type === "video" ? "Video" : "Image")}</div>
      <div class="x-posts-media-fields"><span>${esc(item.path)}</span>${item.type === "image" && (supportsAltText || item.alt_text) ? `<label>Alt text<input class="workflow-inline-input" data-x-alt="${postIndex}:${index}" value="${esc(item.alt_text || "")}" maxlength="1000" /></label>` : ""}${item.type === "image" && !supportsAltText ? `<small>X alt text is unavailable for this connection.${item.alt_text ? " Clear this text before previewing." : ""}</small>` : ""}</div>
      <div class="x-posts-media-actions"><button type="button" data-x-action="up" data-post="${postIndex}" data-media="${index}" data-x-edge-disabled="${index === 0}" aria-label="Move attachment up" ${index === 0 ? "disabled" : ""}>↑</button><button type="button" data-x-action="down" data-post="${postIndex}" data-media="${index}" data-x-edge-disabled="${index === count - 1}" aria-label="Move attachment down" ${index === count - 1 ? "disabled" : ""}>↓</button><button type="button" data-x-action="remove" data-post="${postIndex}" data-media="${index}">Remove</button></div>
    </div>`;
  }

  const statusLabel = post => post.readiness === "needs_evidence" ? "Needs evidence" : post.readiness === "needs_asset" || post.missing_assets?.length ? "Needs media" : "Draft";
  const fileName = path => path.split("/").at(-1);
  function readMedia(items, exact = false) {
    if (!items?.length) return "";
    return `<div class="x-posts-gallery">${items.map(item => `<figure><div class="x-posts-media-preview" data-x-media-preview data-path="${esc(item.path)}" data-type="${esc(item.type)}" data-alt="${esc(item.alt_text || "Attachment preview")}" ${exact ? `data-media-url="${esc(item.url || "")}"` : ""}>${esc(item.type === "video" ? "Video" : "Image")}</div><figcaption>${esc(fileName(item.path))}${item.alt_text ? ` · ${esc(item.alt_text)}` : ""}</figcaption></figure>`).join("")}</div>`;
  }

  function render(state) {
    if (state.closed) return;
    const post = state.draft?.posts[state.selected];
    const i = state.selected;
    const exact = state.preview;
    const editing = state.editing && post;
    const heading = `<h1>Post ${i + 1}${state.draft?.posts.length > 1 ? ` of ${state.draft.posts.length}` : ""}</h1>`;
    state.root.innerHTML = `<header class="markdown-context-bar x-posts-context">
      <button type="button" class="markdown-return" data-x-close>← ${esc(state.context.returnLabel || "files")}</button>
      <span class="markdown-context-separator" aria-hidden="true"></span>
      <span class="markdown-filename" title="${esc(state.context.path)}">${esc(fileName(state.context.path))}</span>
      <span class="markdown-context-spacer"></span>
      ${post ? `<button type="button" class="x-posts-link" data-x-action="notes" aria-expanded="${state.notes}">${state.notes ? "Close notes" : "Draft notes"}</button>
        <button type="button" class="x-posts-button" data-x-action="edit" aria-expanded="${!!editing}">${editing ? "Close editing" : "Edit post"}</button>
        ${exact ? `<button type="button" class="x-posts-button is-publish" data-x-action="publish">Publish this exact post</button>` : `<button type="button" class="x-posts-button is-primary" data-x-action="preview" data-post="${i}" data-x-preview>Preview post</button>`}` : ""}
    </header>
    <div class="markdown-reader-layout x-posts-layout" data-x-body>
      ${state.draft ? `<nav class="x-posts-nav" aria-label="Draft posts">${state.draft.posts.map((item, index) => `<button type="button" data-x-action="select" data-post="${index}" ${i === index ? 'aria-current="true"' : ""}>Post ${index + 1}<span>${esc(statusLabel(item))}</span></button>`).join("")}</nav>` : ""}
      <div class="x-posts-column">
        <p class="x-posts-status" role="status" data-x-status ${state.message ? "" : "hidden"}>${esc(state.message)}</p>
        ${post ? `<section class="x-posts-card" data-x-card="${i}">
          ${state.notes ? `<aside class="x-posts-notes" aria-label="Draft notes"><h2>Draft notes</h2>${post.editor_notes ? `<p>${esc(post.editor_notes)}</p>` : ""}
            ${post.missing_assets?.length ? `<p>Resolve these before publishing:</p><ul>${post.missing_assets.map((item, j) => `<li>${esc(item)} <button class="x-posts-link" type="button" data-x-action="resolve-asset" data-post="${i}" data-asset="${j}">Mark resolved</button></li>`).join("")}</ul>` : ""}
            ${post.support?.length ? `<details class="x-posts-support"><summary>Supporting facts · ${post.support.length}</summary>${post.support.map(item => `<p><code>${esc(item.source_path)}</code><br>${esc(item.excerpt)}</p>`).join("")}</details>` : ""}
            ${post.readiness !== "ready" ? `<p>Check the evidence and media before marking this post ready.</p><button class="x-posts-button" type="button" data-x-action="ready" data-post="${i}" data-x-edge-disabled="${!!post.missing_assets?.length}" ${post.missing_assets?.length ? "disabled" : ""}>Mark ready</button>` : ""}
            ${!post.editor_notes && !post.support?.length && !post.missing_assets?.length && post.readiness === "ready" ? "<p>No additional notes for this post.</p>" : ""}
          </aside>` : ""}
          ${editing ? `<section class="x-posts-editor" aria-label="Edit post">
            <label for="x-post-text">Post text</label><textarea id="x-post-text" data-x-text="${i}" rows="4" maxlength="10000">${esc(post.text)}</textarea>
            <small data-x-count="${i}">Links and emoji count toward X’s limit; checked when saved.</small>
            <div class="x-posts-attachments"><h2>Media</h2>${(post.attachments || []).map((item, j) => mediaLine(item, j, i, post.attachments.length, state.context.supportsAltText)).join("")}
              <div class="x-posts-add"><button type="button" class="x-posts-button" data-x-action="pick-files" data-post="${i}">Choose from Files</button><label class="x-posts-upload x-posts-button">Upload media<input type="file" data-x-upload="${i}" accept="image/png,image/jpeg,video/mp4" /></label></div>
              ${state.picking ? `<div class="x-posts-file-picker"><label for="x-media-file">Image or video in Files</label><select id="x-media-file" data-x-path="${i}"><option value="">Choose a file</option>${state.files.map(path => `<option value="${esc(path)}">${esc(path)}</option>`).join("")}</select><button type="button" class="x-posts-button" data-x-action="add-path" data-post="${i}">Attach file</button>${!state.files.length ? "<p>No supported media in Files yet. Upload an image or video.</p>" : ""}</div>` : ""}
              <p class="x-posts-limit">Up to four images (PNG or JPEG, 5 MB each) or one MP4 video (64 MB, 5 minutes).</p>
            </div><footer class="x-posts-editor-actions"><button type="button" class="x-posts-button is-primary" data-x-action="save">Save draft</button></footer>
          </section>` : ""}
          ${exact ? `<article class="markdown-document x-posts-document x-posts-exact" data-x-exact aria-label="Exact X preview">${heading}<p class="x-posts-account">Posting as <strong>${esc(exact.account?.username || exact.account?.id || "")}</strong></p><div class="x-posts-copy">${esc(exact.text)}</div>${readMedia(exact.attachments, true)}</article>` : `<article class="markdown-document x-posts-document" aria-label="Post text">${heading}<div class="x-posts-copy" data-x-copy>${esc(post.text)}</div>${readMedia(post.attachments)}</article>`}
          ${!state.notes && (post.readiness !== "ready" || post.missing_assets?.length) ? `<p class="x-posts-needs">${esc(statusLabel(post))}. <button type="button" class="x-posts-link" data-x-action="notes">Review draft notes</button></p>` : ""}
        </section>
        <footer class="x-posts-footer"><button type="button" class="x-posts-link" data-x-action="reload">Reload saved draft</button><a href="/activity?project=${encodeURIComponent(state.context.projectId)}">View Activity →</a></footer>` : ""}
      </div>
    </div>`;
    state.root.querySelector("[data-x-close]").onclick = () => {state.close(); state.context.onClose?.();};
    bind(state);
    loadMedia(state);
    renderStatus(state);
  }

  function fitText(field) {
    field.style.height = "auto";
    field.style.height = `${Math.min(360, Math.max(116, field.scrollHeight + 2))}px`;
  }

  function bind(state) {
    const body = state.root;
    body.querySelectorAll("[data-x-text]").forEach(field => fitText(field));
    body.querySelectorAll("[data-x-text]").forEach(field => field.oninput = () => {
      fitText(field);
      const index = Number(field.dataset.xText); state.draft.posts[index].text = field.value;
      body.querySelector(`[data-x-count="${index}"]`).textContent = "Links and emoji count toward X’s limit; checked when saved.";
      changed(state);
      body.querySelector("[data-x-copy]").textContent = field.value;
    });
    body.querySelectorAll("[data-x-alt]").forEach(field => field.oninput = () => {
      const [post, media] = field.dataset.xAlt.split(":").map(Number);
      state.draft.posts[post].attachments[media].alt_text = field.value; changed(state);
    });
    body.querySelectorAll("[data-x-upload]").forEach(field => field.onchange = () => {if (field.files[0]) upload(state, state.draft.posts[Number(field.dataset.xUpload)], field.files[0]);});
    body.querySelectorAll("[data-x-action]").forEach(button => button.onclick = async () => {
      if (state.busy) return;
      const action = button.dataset.xAction;
      const postIndex = Number(button.dataset.post);
      const post = state.draft.posts[postIndex];
      if (action === "notes") {state.notes = !state.notes; render(state); body.querySelector('[data-x-action="notes"]')?.focus(); return;}
      if (action === "edit") {
        state.editing = !state.editing; state.preview = null; state.previewPost = null; state.publishRequest = null;
        render(state); body.querySelector(state.editing ? "[data-x-text]" : '[data-x-action="edit"]')?.focus(); return;
      }
      if (action === "select") {
        state.selected = postIndex; state.editing = false; state.notes = false; state.picking = false;
        state.preview = null; state.previewPost = null; state.publishRequest = null; state.message = "";
        render(state); body.querySelector('[data-x-action="select"][aria-current]')?.focus(); return;
      }
      if (action === "ready") {if (!post.missing_assets?.length) post.readiness = "ready"; changed(state); render(state); return;}
      if (action === "pick-files") {
        state.busy = true; state.message = "Loading media from Files…"; renderStatus(state);
        try {
          const result = await state.context.api(`/api/projects/${encodeURIComponent(state.context.projectId)}/files`);
          if (state.closed) return;
          state.files = (result.files || []).map(file => file.path).filter(path => /\.(png|jpe?g|mp4)$/i.test(path));
          state.picking = true; state.message = ""; render(state);
        } catch (error) {if (!state.closed) {state.message = error.message; renderStatus(state);}}
        finally {state.busy = false; if (!state.closed) renderStatus(state);}
        return;
      }
      if (action === "save") return save(state);
      if (action === "preview") return preview(state, post.id);
      if (action === "publish") return publish(state);
      if (action === "reload") {if (!state.dirty || window.confirm("Discard unsaved X draft edits?")) await load(state); return;}
      if (action === "resolve-asset") {
        post.missing_assets.splice(Number(button.dataset.asset), 1);
        changed(state); render(state); return;
      }
      if (action === "add-path") {
        const field = body.querySelector(`[data-x-path="${postIndex}"]`);
        const path = field.value.trim();
        const type = /\.(png|jpe?g)$/i.test(path) ? "image" : /\.mp4$/i.test(path) ? "video" : null;
        if (!type || !path || path.startsWith("/") || path.includes("..") || path.includes("\\")) {state.message = "Choose a PNG, JPEG or MP4 from Files."; renderStatus(state); return;}
        const items = post.attachments || [];
        if (type === "video" ? items.length > 0 : items.some(item => item.type === "video") || items.length >= 4) {state.message = "Use up to four images or one video per post."; renderStatus(state); return;}
        post.attachments ||= []; post.attachments.push({type, path, alt_text: ""}); state.picking = false; changed(state); render(state); return;
      }
      if (["up", "down", "remove"].includes(action)) {
        const media = Number(button.dataset.media), items = post.attachments;
        if (action === "remove") items.splice(media, 1);
        else {const next = media + (action === "up" ? -1 : 1); if (next >= 0 && next < items.length) [items[media], items[next]] = [items[next], items[media]];}
        changed(state); render(state);
      }
    });
  }

  async function loadMedia(state) {
    for (const node of state.root.querySelectorAll("[data-x-media-preview]")) {
      const path = node.dataset.path;
      const supplied = node.dataset.mediaUrl;
      const allowedUrl = supplied?.startsWith(`/api/projects/${encodeURIComponent(state.context.projectId)}/files/raw?`) ? supplied : null;
      const source = allowedUrl || readUrl(state.context.projectId, path, state.revision);
      const key = source;
      try {
        let url = state.mediaUrls.get(key);
        if (!url) {
          const response = await state.context.rawFetch(source, {headers: {Accept: "*/*"}});
          const blob = await response.blob();
          if (state.closed) return;
          url = URL.createObjectURL(blob); state.mediaUrls.set(key, url);
        }
        if (node.isConnected) node.innerHTML = node.dataset.type === "video"
          ? `<video controls preload="metadata" src="${esc(url)}"></video>`
          : `<img alt="${esc(node.dataset.alt || "Attachment preview")}" src="${esc(url)}" />`;
      } catch (_error) {if (node.isConnected) node.textContent = "Preview unavailable";}
    }
  }

  window.TinXPosts = {open, close: () => active?.close()};
})();
