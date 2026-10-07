"use strict";
const status = document.querySelector("#status"), detail = document.querySelector("#detail");
async function send(type) {
  try {
    const result = await chrome.runtime.sendMessage({ namespace: "tin.linkedin.collection.v3", type, cloud_consent: document.querySelector("#cloud-consent").checked });
    if (!result?.ok) throw Error(result?.error || "extension_unavailable");
    const s = result.payload;
    status.textContent = s.connected ? `${s.actor || "LinkedIn"} · ${s.status.replaceAll("_"," ")}${s.page ? ` · page ${s.page}` : ""}` : "Connect the Tin extension from your project’s Integrations page.";
    detail.textContent = (s.reason || "").replaceAll("_"," ");
    document.querySelector("#begin").disabled = !s.connected;
    document.querySelector("#stop").disabled = !s.run_id || ["completed","partial","finished","stopped"].includes(s.status);
  } catch (error) { detail.textContent = error.message.replaceAll("_"," "); }
}
document.querySelector("#begin").addEventListener("click", () => send("RESUME"));
document.querySelector("#stop").addEventListener("click", () => send("STOP"));
void send("STATUS");
