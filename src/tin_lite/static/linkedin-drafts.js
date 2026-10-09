/* LinkedIn draft text is literal content. This reader has no publishing capability. */
(() => {
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const clone = value => JSON.parse(JSON.stringify(value));
  const sessions = new Map();
  let active;
  function open(context) {
    const key = `${context.actorId}:${context.projectId}:${context.path}`;
    if (active?.key === key && active.root.isConnected) return;
    active?.close();
    const root = document.createElement("section");
    root.className = "x-posts-reader"; root.setAttribute("aria-label", "LinkedIn post drafts");
    context.host.replaceChildren(root);
    const state = {context, key, root, selected:0, draft:null, revision:null, dirty:false, busy:false, editing:false, notes:false, message:"", feedback:"", feedbackOpen:false, ...sessions.get(key)};
    state.beforeUnload = event => {if(state.dirty){event.preventDefault();event.returnValue="";}};
    window.addEventListener("beforeunload", state.beforeUnload);
    state.close = () => {
      sessions.set(key, {draft:state.draft && clone(state.draft),revision:state.revision,dirty:state.dirty,selected:state.selected,saveRequest:state.saveRequest,sha256:state.sha256,revisionSource:state.revisionSource,feedback:state.feedback,revisionRequest:state.revisionRequest,revisionRun:state.revisionRun,revisedPath:state.revisedPath});
      clearTimeout(state.revisionTimer);
      state.closed=true; state.cleanup?.(); root.remove();
      window.removeEventListener("beforeunload",state.beforeUnload);
      if(active===state)active=null;
    };
    active=state;
    if(state.revisionRun)watchRevision(state);
    if(state.dirty && state.draft){state.message="Your unsaved edits are still here.";render(state);}else void load(state);
  }
  const url = state => `/api/projects/${encodeURIComponent(state.context.projectId)}/linkedin/drafts`;
  async function load(state) {
    state.message="Loading drafts…"; render(state);
    try {
      const data=await state.context.api(`${url(state)}?${new URLSearchParams({path:state.context.path})}`);
      if(state.closed)return;
      if(data.draft?.schema_version!=="tin.social.linkedin_draft.v1")throw Error("This file is not a LinkedIn draft.");
      state.draft=clone(data.draft);state.revision=data.revision;state.sha256=data.sha256;state.revisionSource=data.revision_source_run_id;state.message="";
      state.selected=Math.min(state.selected,Math.max(0,state.draft.posts.length-1));render(state);
    } catch(error){if(!state.closed){state.message=error.message;render(state);}}
  }
  async function save(state) {
    if(state.busy || !state.dirty)return;
    state.busy=true; status(state);
    try {
      state.saveRequest ||= crypto.randomUUID();
      const data=await state.context.api(url(state),{method:"PUT",body:JSON.stringify({path:state.context.path,expected_revision:state.revision,request_id:state.saveRequest,draft:state.draft})});
      if(state.closed)return;
      state.draft=clone(data.draft);state.revision=data.revision;state.sha256=data.sha256;state.dirty=false;state.saveRequest=null;
      state.message="Draft saved.";
    } catch(error){if(!state.closed)state.message=`${error.message} Your edits are still here. Copy them before reloading.`;}
    finally {state.busy=false;if(!state.closed)render(state);}
  }
  async function revise(state) {
    if(state.busy || state.dirty || state.revisionRun || !state.feedback.trim())return;
    state.busy=true;status(state);
    try {
      state.revisionRequest ||= crypto.randomUUID();
      const run=await state.context.api(`${url(state)}/revisions`,{method:"POST",body:JSON.stringify({path:state.context.path,expected_sha256:state.sha256,post_id:state.draft.posts[state.selected].id,feedback:state.feedback,request_id:state.revisionRequest})});
      if(state.closed)return;
      state.revisionRun=run.id;state.feedbackOpen=false;
      state.message="Revising this alternative. Your current batch stays unchanged.";
      render(state);watchRevision(state);
    } catch(error){if(!state.closed)state.message=error.message;}
    finally {state.busy=false;if(!state.closed)status(state);}
  }
  function watchRevision(state) {
    clearTimeout(state.revisionTimer);
    state.revisionTimer=setTimeout(async()=>{
      if(state.closed || !state.revisionRun)return;
      try {
        const run=await state.context.api(`/api/workflows/runs/${encodeURIComponent(state.revisionRun)}`);
        if(state.closed)return;
        if(run.status==="succeeded"){
          state.revisedPath=run.artifact_path;state.revisionRun=null;state.revisionRequest=null;
          state.message="Your revised batch is ready. This copy and your edits are unchanged.";render(state);
        } else if(["failed","stopped"].includes(run.status)){
          state.revisionRun=null;state.revisionRequest=null;state.message=run.error_message || "Revision stopped. Your original batch is unchanged.";render(state);
        } else watchRevision(state);
      } catch(error){if(!state.closed){state.message="Could not check the revision yet. It remains in Activity.";status(state);watchRevision(state);}}
    },2000);
  }
  function status(state) {
    state.root.querySelector("[role=status]").textContent=state.message;
    state.root.querySelectorAll("button, textarea").forEach(e=>{e.disabled=state.busy;});
    const revise=state.root.querySelector("[data-revise]");
    if(revise)revise.disabled=state.busy || state.dirty || !!state.revisionRun;
  }
  function render(state) {
    if(state.closed)return;
    state.cleanup?.();
    const post=state.draft?.posts[state.selected];
    const html=post ? `<h1>Post ${state.selected+1} of ${state.draft.posts.length}</h1><div class="x-posts-copy" data-linkedin-copy>${esc(post.text)}</div>` : state.draft ? `<h1>More context needed</h1>${state.draft.gaps.map(g=>`<p>${esc(g.reason)}</p>`).join("")}` : "";
    state.cleanup=window.TinMarkdownViewer.mount(state.root,{filename:state.context.path.split("/").at(-1),html,word_count:0,reading_minutes:0},{mode:"in-app",factsText:false,
      returnTo:{label:"files",onActivate:()=>{state.close();state.context.onClose();}},
      rawAction:post?{label:state.notes?"Close notes":"Draft notes",onActivate:()=>{state.notes=!state.notes;render(state);}}:null,
      secondaryAction:post?{label:state.editing?"Close editing":"Edit post",onActivate:()=>{state.editing=!state.editing;render(state);}}:null,
      primaryAction:post?{label:"Copy post",onActivate:async()=>{
        try{await navigator.clipboard.writeText(post.text);if(!state.closed){state.message="Post copied.";status(state);}}
        catch{if(!state.closed){state.message="Could not copy. Select the post text to copy it.";status(state);}}
      }}:null,
    });
    state.root.querySelector(".markdown-context-bar").classList.add("x-posts-context");
    state.root.querySelectorAll(".markdown-context-action").forEach((button,i)=>{button.className=i===0?"x-posts-link":"x-posts-button";});
    if(post && state.revisionSource){
      const button=document.createElement("button");button.type="button";button.className="x-posts-button";button.dataset.revise="";
      button.textContent=state.feedbackOpen?"Close feedback":"Request changes";
      button.onclick=()=>{state.feedbackOpen=!state.feedbackOpen;render(state);};
      state.root.querySelector(".markdown-context-bar").append(button);
    }
    const layout=state.root.querySelector(".markdown-reader-layout");
    layout.classList.remove("has-no-map");layout.classList.add("x-posts-layout");
    const nav=state.root.querySelector(".markdown-section-map");nav.hidden=false;nav.className="x-posts-nav";
    nav.setAttribute("aria-label","Draft posts");
    nav.innerHTML=(state.draft?.posts||[]).map((p,i)=>`<button type="button" data-select="${i}" ${i===state.selected?'aria-current="true"':""}>Post ${i+1}<span>${p.readiness==="needs_evidence"?"Needs evidence":p.readiness==="edited"?"Edited":"Draft"}</span></button>`).join("");
    nav.querySelectorAll("button").forEach(b=>b.onclick=()=>{state.selected=Number(b.dataset.select);state.feedbackOpen=false;state.feedback="";state.revisionRequest=null;render(state);});
    const article=state.root.querySelector(".markdown-document");article.classList.add("x-posts-document");
    const column=document.createElement("div");column.className="x-posts-column";
    article.replaceWith(column);
    column.innerHTML=`<p class="x-posts-status" role="status">${esc(state.message)}</p>`;
    if(state.revisedPath){
      const button=document.createElement("button");button.type="button";button.className="x-posts-button";button.textContent="Open revised batch";
      button.onclick=()=>state.context.openDraft(state.revisedPath);column.append(button);
    }
    if(post && state.feedbackOpen){
      const panel=document.createElement("div");panel.className="x-posts-editor";
      panel.innerHTML=`<label for="linkedin-feedback">What should change?</label><textarea id="linkedin-feedback" maxlength="4000">${esc(state.feedback)}</textarea><small>Creates a new batch with only this post revised. Uses normal Tin model credits.</small><button type="button" class="x-posts-button">Revise this post</button>`;
      panel.querySelector("textarea").oninput=e=>{state.feedback=e.target.value;state.revisionRequest=null;};
      panel.querySelector("button").onclick=()=>void revise(state);column.append(panel);
    }
    if(post && state.editing){
      const panel=document.createElement("div");panel.className="x-posts-editor";
      panel.innerHTML=`<label for="linkedin-text">Post text</label><textarea id="linkedin-text" maxlength="2500">${esc(post.text)}</textarea><small>Up to 2,500 characters. Check edited claims against your sources.</small><div class="x-posts-editor-actions"><button type="button" class="x-posts-button is-primary">Save draft</button></div>`;
      panel.querySelector("textarea").oninput=e=>{post.text=e.target.value;post.readiness="edited";state.dirty=true;state.saveRequest=null;state.message="Unsaved changes.";article.querySelector("[data-linkedin-copy]").textContent=post.text;status(state);};
      panel.querySelector("button").onclick=()=>void save(state);column.append(panel);
    }
    if(post && state.notes){
      const notes=document.createElement("div");notes.className="x-posts-notes";
      notes.innerHTML=`<h2>Draft notes</h2><p>${esc(post.angle)}</p><p>${esc(post.editor_notes)}</p><p>For: ${esc(state.draft.audience)}</p><p>Format: ${esc(post.template_id)}</p><p>${state.draft.style_path?"Uses the selected writing guide.":"No personal writing guide was applied."}</p>${post.support.map(s=>`<blockquote>${esc(s.excerpt)}</blockquote>`).join("")}${state.draft.gaps.map(g=>`<p>${esc(g.reason)}</p>`).join("")}`;
      column.append(notes);
    }
    column.append(article);status(state);
  }
  window.TinLinkedInDrafts={open,close:()=>active?.close()};
})();
