"use strict";
const status = document.querySelector("#status"), detail = document.querySelector("#detail");
async function send(type) {
  try {
    const result = await chrome.runtime.sendMessage({ namespace: "tin.linkedin.collection.v3", type, cloud_consent: document.querySelector("#cloud-consent").checked });
    if (!result?.ok) throw Error(result?.error || "extension_unavailable");
    const s = result.payload;
    const labels = {ready:"Ready. Start a collection in Tin.", collecting:"Collecting in Chrome",cloud_running:"Collecting in the cloud",paused:"Collection paused",finished:"Collection finished",completed:"Collection finished",partial:"Collection stopped. Saved results are kept.",stopped:"Collection stopped"};
    status.textContent = s.connected ? (labels[s.status] || "LinkedIn connected") : "Connect LinkedIn in Tin to get started.";
    const reasons = {open_linkedin_tab:"Open LinkedIn and sign in.",account_changed:"The LinkedIn account changed. Check your connection in Tin.",cloud_permission_required:"Allow cloud collection in Tin's LinkedIn settings.",session_expired:"Reconnect LinkedIn in Tin.",refresh_linkedin_context:"Open LinkedIn, then reconnect in Tin.",challenge:"LinkedIn needs your attention. Open LinkedIn in Chrome.",rate_limited:"LinkedIn asked us to wait. Your saved results are kept.",unsupported_layout:"Tin could not read this LinkedIn page. Your saved results are kept.",cloud_transfer_consent_required:"Finish LinkedIn setup in Tin to remember your cloud preference."};
    detail.textContent = reasons[s.reason] || (s.reason && !["no_pending_collection","cloud_unavailable"].includes(s.reason) ? "Check your LinkedIn connection in Tin." : "");
    document.querySelector("#setup").hidden = Boolean(s.permission?.version);
    const begin = document.querySelector("#begin");
    begin.hidden = !s.connected || (Boolean(s.permission?.version) && s.status !== "paused");
    begin.disabled = !s.connected;
    document.querySelector("#legacy-consent").hidden = Boolean(s.permission?.version) || !s.connected;
    document.querySelector("#stop").hidden = !s.run_id || ["completed","partial","finished","stopped"].includes(s.status);
    document.querySelector("#local-note").hidden = s.status !== "collecting";
    const link = new URL("https://app.tin.computer/integrations");
    if (s.project_id) link.searchParams.set("project",s.project_id);
    document.querySelector("#integrations").href = link.href;
  } catch (error) { detail.textContent = "Open Tin to check your LinkedIn connection."; }
}
document.querySelector("#begin").addEventListener("click", () => send("RESUME"));
document.querySelector("#stop").addEventListener("click", () => send("STOP"));
void send("STATUS");
