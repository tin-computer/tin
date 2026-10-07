(function installPopup() {
  "use strict";

  const NAMESPACE = "tin.linkedin.popup.v2";
  const card = document.getElementById("status-card");
  const dot = document.getElementById("status-dot");
  const title = document.getElementById("status-title");
  const detail = document.getElementById("status-detail");
  const refresh = document.getElementById("refresh");
  const disconnect = document.getElementById("disconnect");
  const disconnectNote = document.getElementById("disconnect-note");
  const error = document.getElementById("error");

  function request(type) {
    return new Promise((resolve, reject) => {
      chrome.runtime.sendMessage({ namespace: NAMESPACE, type }, (response) => {
        const runtimeError = chrome.runtime.lastError;
        if (runtimeError || !response?.ok) {
          reject(
            new Error(response?.error?.message || "The Tin extension is unavailable."),
          );
          return;
        }
        resolve(response.payload);
      });
    });
  }

  function description(status) {
    switch (status.status) {
      case "connected":
        return {
          tone: "ready",
          title: "Connected to Tin",
          detail: status.actor?.display_name
            ? `Session synced as ${status.actor.display_name}. Tin can run delegated outreach while Chrome is closed.`
            : "Your session is ready. Tin can run delegated outreach while Chrome is closed.",
        };
      case "signed_out":
        return {
          tone: "error",
          title: "Sign in to LinkedIn",
          detail: "Sign in in this Chrome profile, then choose Refresh.",
        };
      case "checkpoint":
        return {
          tone: "error",
          title: "LinkedIn security check needed",
          detail:
            "Complete LinkedIn's security check in this Chrome profile, then choose Refresh.",
        };
      case "needs_reconnect":
        return {
          tone: "error",
          title: "Reconnect from Tin",
          detail: "Open the LinkedIn integration in Tin and connect this browser again.",
        };
      case "disconnect_pending":
        return {
          tone: "waiting",
          title: "Stopping browser sync",
          detail: "Tin will revoke this browser's refresh access when the network is available.",
        };
      case "connecting":
      case "refreshing":
        return {
          tone: "waiting",
          title: "Syncing LinkedIn",
          detail: "This should take only a moment.",
        };
      case "error":
        return {
          tone: "error",
          title: "Connection needs attention",
          detail: "Choose Refresh, or reconnect from Tin if the issue continues.",
        };
      default:
        return {
          tone: "waiting",
          title: "Not connected",
          detail: "Open Tin's LinkedIn integration to connect this Chrome profile.",
        };
    }
  }

  function render(status) {
    const copy = description(status || {});
    dot.dataset.tone = copy.tone;
    title.textContent = copy.title;
    detail.textContent = copy.detail;
    card.setAttribute("aria-busy", "false");
    disconnect.hidden = !status?.project_id;
    disconnectNote.hidden = !status?.project_id;
  }

  async function run(type) {
    error.hidden = true;
    error.textContent = "";
    card.setAttribute("aria-busy", "true");
    refresh.disabled = true;
    disconnect.disabled = true;
    try {
      render(await request(type));
    } catch (caught) {
      error.textContent = caught instanceof Error ? caught.message : "Request failed.";
      error.hidden = false;
    } finally {
      card.setAttribute("aria-busy", "false");
      refresh.disabled = false;
      disconnect.disabled = false;
    }
  }

  refresh.addEventListener("click", () => void run("REFRESH"));
  disconnect.addEventListener("click", () => void run("DISCONNECT"));
  void run("STATUS");
})();
