/* X drafts are ordinary project files. Publication is a separate exact-preview effect. */
(() => {
  const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
  const copy = value => JSON.parse(JSON.stringify(value));
  const mediaType = file => file.type === "image/png" || file.type === "image/jpeg" ? "image" : file.type === "video/mp4" ? "video" : null;
  const draftUrl = projectId => `/api/projects/${encodeURIComponent(projectId)}/x/drafts`;
  const readUrl = (projectId, path, revision) => `/api/projects/${encodeURIComponent(projectId)}/files/raw?${new URLSearchParams({path, revision})}`;
  let active = null;

  function open(context) {
    active?.close();
    const dialog = document.createElement("dialog");
    dialog.className = "x-posts-dialog";
    dialog.innerHTML = `<div class="x-posts-shell"><header class="x-posts-header"><div><span class="x-posts-kicker">Project Files · X</span><h2>X post drafts</h2><p>Refine a post, preview its exact text and attachments, then choose when to publish.</p></div><button type="button" class="button-quiet" data-x-close aria-label="Close X drafts">Close</button></header><div data-x-body></div></div>`;
    document.body.append(dialog);
    const state = {dialog, context, revision: null, draft: null, dirty: false, busy: false, preview: null, previewPost: null, publishRequest: null, saveRequest: null, message: "", mediaUrls: new Map(), closed: false};
    state.close = () => {
      state.closed = true;
      for (const url of state.mediaUrls.values()) URL.revokeObjectURL(url);
      state.mediaUrls.clear();
      dialog.close(); dialog.remove();
      if (active === state) active = null;
    };
    active = state;
    dialog.querySelector("[data-x-close]").onclick = state.close;
    dialog.addEventListener("cancel", event => {event.preventDefault(); state.close();});
    dialog.showModal();
    render(state);
    load(state);
  }

  async function load(state) {
    state.message = "Loading drafts…"; render(state);
    try {
      const result = await state.context.api(`${draftUrl(state.context.projectId)}?${new URLSearchParams({path: state.context.path})}`);
      if (state.closed) return;
      if (result?.draft?.schema_version !== "tin.social.x_draft.v1" || !Array.isArray(result.draft.posts)) throw new Error("This file is not an X draft. Open it in Files.");
      state.revision = result.revision;
      state.draft = copy(result.draft);
      state.dirty = false; state.preview = null; state.previewPost = null; state.saveRequest = null;
      state.message = ""; render(state);
    } catch (error) { if (!state.closed) {state.message = error.message; render(state);} }
  }

  function changed(state) {
    state.dirty = true;
    state.preview = null; state.previewPost = null; state.publishRequest = null; state.saveRequest = null;
    state.message = "Unsaved changes. Preview will save this draft first.";
    renderStatus(state);
    state.dialog.querySelectorAll("[data-x-preview], [data-x-save]").forEach(button => {button.disabled = false;});
    state.dialog.querySelector("[data-x-exact]")?.remove();
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
      state.message = "Check the account, text and every attachment before publishing.";
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
    const status = state.dialog.querySelector("[data-x-status]");
    if (status) status.textContent = state.message;
    state.dialog.querySelectorAll("button[data-x-action]").forEach(button => {button.disabled = state.busy || button.dataset.xEdgeDisabled === "true";});
    state.dialog.querySelectorAll("[data-x-text], [data-x-readiness], [data-x-alt], [data-x-path], [data-x-upload]").forEach(field => {field.disabled = state.busy;});
  }

  function mediaLine(item, index, postIndex, count, supportsAltText) {
    return `<div class="x-posts-media" data-x-media-row="${postIndex}:${index}">
      <div class="x-posts-media-preview" data-x-media-preview data-path="${esc(item.path)}" data-type="${esc(item.type)}">${esc(item.type === "video" ? "Video" : "Image")}</div>
      <div class="x-posts-media-fields"><span>${esc(item.path)}</span>${item.type === "image" && (supportsAltText || item.alt_text) ? `<label>Alt text<input class="workflow-inline-input" data-x-alt="${postIndex}:${index}" value="${esc(item.alt_text || "")}" maxlength="1000" /></label>` : ""}${item.type === "image" && !supportsAltText ? `<small>X alt text is unavailable for this connection.${item.alt_text ? " Clear this text before previewing." : ""}</small>` : ""}</div>
      <div class="x-posts-media-actions"><button type="button" data-x-action="up" data-post="${postIndex}" data-media="${index}" data-x-edge-disabled="${index === 0}" aria-label="Move attachment up" ${index === 0 ? "disabled" : ""}>↑</button><button type="button" data-x-action="down" data-post="${postIndex}" data-media="${index}" data-x-edge-disabled="${index === count - 1}" aria-label="Move attachment down" ${index === count - 1 ? "disabled" : ""}>↓</button><button type="button" data-x-action="remove" data-post="${postIndex}" data-media="${index}">Remove</button></div>
    </div>`;
  }

  function render(state) {
    if (state.closed) return;
    const body = state.dialog.querySelector("[data-x-body]");
    if (!state.draft) {
      body.innerHTML = `<p class="x-posts-status" role="status" data-x-status>${esc(state.message)}</p>`;
      return;
    }
    body.innerHTML = `<p class="x-posts-status" role="status" data-x-status>${esc(state.message)}</p>
      <div class="x-posts-list">${state.draft.posts.map((post, i) => `<section class="x-posts-card" data-x-card="${i}">
        <div class="x-posts-card-head"><strong>Post ${i + 1}</strong><span>${esc(post.id)}</span></div>
        <label>Post text<textarea data-x-text="${i}" rows="5" maxlength="10000">${esc(post.text)}</textarea></label>
        <div class="x-posts-card-meta"><span data-x-count="${i}">${[...post.text].length} Unicode characters · Links and emoji count differently; checked when saved</span><label>Ready to publish <select data-x-readiness="${i}"><option value="ready" ${post.readiness === "ready" ? "selected" : ""}>Ready</option><option value="needs_evidence" ${post.readiness === "needs_evidence" ? "selected" : ""}>Needs evidence</option><option value="needs_asset" ${post.readiness === "needs_asset" ? "selected" : ""}>Needs an asset</option></select></label></div>
        ${post.editor_notes ? `<p class="x-posts-note"><strong>Draft note</strong> ${esc(post.editor_notes)}</p>` : ""}
        ${post.missing_assets?.length ? `<div class="x-posts-note"><strong>Missing assets · resolve each after attaching or confirming it is no longer needed</strong><ul>${post.missing_assets.map((item, j) => `<li>${esc(item)} <button type="button" data-x-action="resolve-asset" data-post="${i}" data-asset="${j}">Mark resolved</button></li>`).join("")}</ul></div>` : ""}
        ${post.support?.length ? `<details class="x-posts-support"><summary>Supporting facts · ${post.support.length}</summary>${post.support.map(item => `<p><code>${esc(item.source_path)}</code> ${esc(item.excerpt)}</p>`).join("")}</details>` : ""}
        <div class="x-posts-attachments"><strong>Attachments</strong>${(post.attachments || []).length ? post.attachments.map((item, j) => mediaLine(item, j, i, post.attachments.length, state.context.supportsAltText)).join("") : `<p>No images or video attached.</p>`}
          <div class="x-posts-add"><label>File path<input class="workflow-inline-input" data-x-path="${i}" placeholder="images/demo.png" /></label><button type="button" data-x-action="add-path" data-post="${i}">Add from Files</button><label class="x-posts-upload">Upload media<input type="file" data-x-upload="${i}" accept="image/png,image/jpeg,video/mp4" /></label></div><p class="x-posts-limit">PNG or JPEG up to 5 MB; H.264/AAC MP4 up to 64 MB and 5 minutes. One video or up to four images.</p></div>
        <div class="x-posts-card-actions"><button type="button" data-x-action="save">Save draft</button><button type="button" class="button-secondary" data-x-action="preview" data-post="${i}" data-x-preview>Preview exact post</button></div>
      </section>`).join("")}</div>
      ${state.preview ? `<section class="x-posts-exact" data-x-exact><h3>Exact X preview</h3><p><strong>Account</strong> ${esc(state.preview.account?.username || "")} <code>${esc(state.preview.account?.id || "")}</code></p><div class="x-posts-exact-text">${esc(state.preview.text)}</div><div class="x-posts-exact-media">${(state.preview.attachments || []).map(item => `<div><div class="x-posts-media-preview" data-x-media-preview data-media-url="${esc(item.url || "")}" data-path="${esc(item.path)}" data-type="${esc(item.type)}">${esc(item.type)}</div><span>${esc(item.type)} · ${esc(item.path)}${item.alt_text ? ` · alt: ${esc(item.alt_text)}` : ""}</span></div>`).join("") || "No attachments"}</div><p>Publishing sends this post now. A later edit requires a new preview.</p><button type="button" class="button-primary" data-x-action="publish">Publish this exact post</button></section>` : ""}
      <footer class="x-posts-footer"><button type="button" class="button-quiet" data-x-action="reload">Reload saved draft</button><a href="/activity?project=${encodeURIComponent(state.context.projectId)}">View Activity</a></footer>`;
    bind(state);
    loadMedia(state);
    renderStatus(state);
  }

  function bind(state) {
    const body = state.dialog.querySelector("[data-x-body]");
    body.querySelectorAll("[data-x-text]").forEach(field => field.oninput = () => {
      const index = Number(field.dataset.xText); state.draft.posts[index].text = field.value;
      body.querySelector(`[data-x-count="${index}"]`).textContent = `${[...field.value].length} Unicode characters · Links and emoji count differently; checked when saved`;
      changed(state);
    });
    body.querySelectorAll("[data-x-readiness]").forEach(field => field.onchange = () => {state.draft.posts[Number(field.dataset.xReadiness)].readiness = field.value; changed(state);});
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
        if (!type || !path || path.startsWith("/") || path.includes("..") || path.includes("\\")) {state.message = "Enter a project Files path ending in .png, .jpg, .jpeg or .mp4."; renderStatus(state); return;}
        const items = post.attachments || [];
        if (type === "video" ? items.length > 0 : items.some(item => item.type === "video") || items.length >= 4) {state.message = "Use up to four images or one video per post."; renderStatus(state); return;}
        post.attachments ||= []; post.attachments.push({type, path, alt_text: ""}); changed(state); render(state); return;
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
    for (const node of state.dialog.querySelectorAll("[data-x-media-preview]")) {
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
          : `<img alt="Attachment preview" src="${esc(url)}" />`;
      } catch (_error) {if (node.isConnected) node.textContent = "Preview unavailable";}
    }
  }

  window.TinXPosts = {open};
})();
