"use strict";

const main = document.querySelector("#main");
const toast = document.querySelector("#toast");
const authRoot = document.querySelector("#auth-root");
const authView = document.querySelector("#auth-view");
const appShell = document.querySelector("#app-shell");
const projectName = document.querySelector("#project-name");
const projectSummary = document.querySelector("#project-summary");
const workflowCount = document.querySelector("#workflow-count");
const decisionCount = document.querySelector("#decision-count");
const agentRail = document.querySelector("#agent-rail");
const agentRailToggle = document.querySelector("#agent-rail-toggle");
const agentRailBody = document.querySelector("#agent-rail-body");
const agentCommand = document.querySelector("#agent-command");
const agentLastUsed = document.querySelector("#agent-last-used");
const agentRailStatus = document.querySelector(".agent-rail-status");
const copyAgentCommand = document.querySelector("#copy-agent-command");
const userAvatar = document.querySelector("#user-avatar");
const userName = document.querySelector("#user-name");
document.querySelector("#sign-out")?.addEventListener("click", () => signOutTin());
const projectSwitcher = document.querySelector("#project-switcher");
const projectMenu = document.querySelector("#project-menu");
const integrationProjectDialog = document.querySelector("#integration-project-dialog");
const integrationProjectForm = document.querySelector("#integration-project-form");
const integrationProjectTitle = document.querySelector("#integration-project-title");
const integrationProjectCopy = document.querySelector("#integration-project-copy");
const integrationProjectOptions = document.querySelector("#integration-project-options");
const projectCreateDialog = document.querySelector("#project-create-dialog");
const projectCreateForm = document.querySelector("#project-create-form");
const projectCreateCopy = document.querySelector("#project-create-copy");
const projectCreateName = document.querySelector("#project-create-name");
const projectDeleteDialog = document.querySelector("#project-delete-dialog");
const projectDeleteForm = document.querySelector("#project-delete-form");
const projectDeleteCopy = document.querySelector("#project-delete-copy");
const projectDeleteName = document.querySelector("#project-delete-name");
const projectDeletePrompt = document.querySelector("#project-delete-prompt");
const projectInviteDialog = document.querySelector("#project-invite-dialog");
const projectInviteForm = document.querySelector("#project-invite-form");
const projectInviteCopy = document.querySelector("#project-invite-copy");
const projectInviteEmail = document.querySelector("#project-invite-email");
const projectInviteResult = document.querySelector("#project-invite-result");
const projectInviteUrl = document.querySelector("#project-invite-url");
const copyProjectInvite = document.querySelector("#copy-project-invite");

const RUNNING_STATES = new Set(["pending", "running"]);
const ACTIVE_TASK_STATES = new Set(["pending", "running", "needs_input", "paused"]);
const BILLING_ENABLED = document.documentElement.dataset.billingEnabled === "true";
// Browser sign-ups see a locked dashboard until their coding agent sets up the first
// workflow or starts the first run. Server setting TIN_LITE_BROWSER_LOCK_ENABLED;
// unlocks on the next reload.
const BROWSER_LOCK_ENABLED = document.documentElement.dataset.browserLockEnabled === "true";
// Server-owned addresses: changing the dashboard must not move MCP's OAuth resource.
const APP_URL = configuredOrigin(document.documentElement.dataset.appUrl);
const MCP_URL = document.documentElement.dataset.mcpUrl?.startsWith("http")
  ? document.documentElement.dataset.mcpUrl : `${window.location.origin}/mcp`;

function configuredOrigin(value) {
  return value && !value.startsWith("{{") ? new URL(value).origin : window.location.origin;
}
const ALLOWED_VIEWS = new Set(["chat", "workflows", "activity", "decisions", "files", "integrations"]);
if (BILLING_ENABLED) ALLOWED_VIEWS.add("billing");
// One small History API router. Legacy bookmarks are input-only compatibility;
// new links use paths. Document heading fragments are not application routes.
const DASHBOARD_ROUTE = /^(?:system|workflows|chat|activity|decisions|files|integrations|connect|billing|file|(?:document|task|compare)\/[^/?]+)(?:\?|$)/;
function routeUrl(route, base = window.location.href) {
  const url = new URL(base);
  const [path, query = ""] = route.replace(/^#?\/?/, "").split("?", 2);
  url.pathname = `/${path === "workflows" ? "system" : path}`;
  for (const key of ["return", "taskPath", "source", "path", "revision", "reviewRun", "compareRun", "back"]) url.searchParams.delete(key);
  for (const [key, value] of new URLSearchParams(query)) url.searchParams.set(key, value);
  url.hash = "";
  return `${url.pathname}${url.search}`;
}
function normalizeDashboardUrl() {
  const url = new URL(window.location.href);
  const legacy = url.hash.replace(/^#\/?/, "");
  if (url.pathname === "/" && DASHBOARD_ROUTE.test(legacy)) {
    window.history.replaceState(null, "", routeUrl(legacy));
  } else if (url.pathname === "/" || url.pathname === "/workflows") {
    url.pathname = "/system";
    window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
  }
}
function currentRoute() {
  return `${window.location.pathname.replace(/^\//, "")}${window.location.search}`;
}
function goToRoute(route) {
  const target = routeUrl(route);
  if (`${window.location.pathname}${window.location.search}${window.location.hash}` !== target) window.history.pushState(null, "", target);
  routeChanged();
}
normalizeDashboardUrl();
const RESOURCE_SCOPED_INTEGRATIONS = new Set(["analytics.gsc", "infra.github", "analytics.posthog"]);

// The one resource a scoped connection uses in this project, or null while unchosen.
function integrationSelection(integration) {
  const config = integration?.configuration || {};
  return config.selected_site_url || config.selected_repository || config.selected_project_id || null;
}

function integrationResourceNoun(providerKey) {
  if (providerKey === "infra.github") return "repository";
  if (providerKey === "analytics.posthog") return "PostHog project";
  return "Search Console property";
}
// Rendered as the last available row; saving creates a real custom.api.<name> connection.
const CUSTOM_API_TEMPLATE = Object.freeze({
  key: "custom.api",
  name: "Custom API",
  badge: "API",
  description: "Project API connection. Credentials stay in Tin.",
  unlocks: ["private code workflows"],
  connection_id: null,
  status: "available",
});
const CONNECT_REQUEST_KEY = "tin-lite:connect-providers";
const CONNECT_PROVIDERS = new Set(["infra.github", "analytics.gsc", "workspace.google", "ads.google", "payments.stripe", "analytics.posthog"]);
let pendingConnectRequest = null;

function rememberConnectRequest(projectId, providers) {
  if (!projectId) return;
  pendingConnectRequest = { projectId, providers: [...new Set(providers.filter((key) => CONNECT_PROVIDERS.has(key)))] };
  try {
    window.sessionStorage.setItem(CONNECT_REQUEST_KEY, JSON.stringify(pendingConnectRequest));
  } catch (_error) {}
}

// A connect request from the founder's agent: /connect?project=…&providers=a,b opens the
// Integrations view filtered to those providers and keeps that filter across the OAuth round
// trips until the person leaves the view.
function adoptConnectRequest() {
  const url = new URL(window.location.href);
  if (url.pathname.replace(/\/$/, "") !== "/connect") return;
  const providers = (url.searchParams.get("providers") || "").split(",").map((s) => s.trim()).filter(Boolean);
  clearConnectRequest();
  rememberConnectRequest(url.searchParams.get("project"), providers);
  url.pathname = "/integrations";
  url.searchParams.delete("providers");
  url.hash = "";
  window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
  state.view = "integrations";
}

function connectRequest(projectId = state.project?.id) {
  let request = pendingConnectRequest;
  try {
    const stored = window.sessionStorage.getItem(CONNECT_REQUEST_KEY);
    if (stored) request = JSON.parse(stored);
  } catch (_error) {}
  if (!projectId || request?.projectId !== projectId || !Array.isArray(request.providers)) return [];
  return request.providers.filter((key) => CONNECT_PROVIDERS.has(key));
}

function clearConnectRequest() {
  pendingConnectRequest = null;
  try {
    window.sessionStorage.removeItem(CONNECT_REQUEST_KEY);
  } catch (_error) {}
}

const TIN_FILE_ICON_SPRITE = `<svg aria-hidden="true" width="0" height="0" style="position:absolute;overflow:hidden">
  <symbol id="file-tree-icon-chevron" viewBox="0 0 16 16"><path d="M5.5 3.5 10 8l-4.5 4.5" fill="none" stroke="currentColor" stroke-width=".95" stroke-linecap="round" stroke-linejoin="round"/></symbol>
  <symbol id="file-tree-icon-file" viewBox="0 0 16 16"><path d="M4 1.75h5.25l3 3v9.5a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V2.75a1 1 0 0 1 1-1z" fill="var(--file-icon-surface)" stroke="var(--file-icon-stroke)" stroke-width=".95" stroke-linejoin="round"/><path d="M9.25 1.75v3h3" fill="none" stroke="var(--file-icon-stroke)" stroke-width=".95" stroke-linejoin="round"/></symbol>
  <symbol id="file-tree-icon-dot" viewBox="0 0 16 16"><circle cx="8" cy="8" r="1" fill="currentColor"/></symbol>
  <symbol id="file-tree-icon-lock" viewBox="0 0 16 16"><path d="M5 7V5.5a3 3 0 0 1 6 0V7m-7 0h8v6H4z" fill="none" stroke="currentColor" stroke-width=".95"/></symbol>
  <symbol id="tin-folder" viewBox="0 0 16 16"><path d="M1.75 5.25A1.25 1.25 0 0 1 3 4h3l1.5 1.5H13a1.25 1.25 0 0 1 1.25 1.25v5.5A1.25 1.25 0 0 1 13 13.5H3a1.25 1.25 0 0 1-1.25-1.25z" fill="var(--file-icon-folder)" stroke="var(--file-icon-stroke)" stroke-width=".95" stroke-linejoin="round"/></symbol>
  <symbol id="tin-folder-open" viewBox="0 0 16 16"><path d="M1.75 8V5.25A1.25 1.25 0 0 1 3 4h3l1.5 1.5H13a1.25 1.25 0 0 1 1.25 1.25V8" fill="none" stroke="var(--file-icon-stroke)" stroke-width=".95" stroke-linejoin="round"/><path d="M3.4 8.25H14l-1.4 4.6a.9.9 0 0 1-.86.65H2.75a.85.85 0 0 1-.82-1.08z" fill="var(--file-icon-folder)" stroke="var(--file-icon-stroke)" stroke-width=".95" stroke-linejoin="round"/></symbol>
  <symbol id="tin-file" viewBox="0 0 16 16"><path d="M4 1.75h5.25l3 3v9.5a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V2.75a1 1 0 0 1 1-1z" fill="var(--file-icon-surface)" stroke="var(--file-icon-stroke)" stroke-width=".95" stroke-linejoin="round"/><path d="M9.25 1.75v3h3" fill="none" stroke="var(--file-icon-stroke)" stroke-width=".95" stroke-linejoin="round"/></symbol>
  <symbol id="tin-markdown" viewBox="0 0 16 16"><path d="M4 1.75h5.25l3 3v9.5a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V2.75a1 1 0 0 1 1-1z" fill="var(--file-icon-surface)" stroke="var(--file-icon-stroke)" stroke-width=".95" stroke-linejoin="round"/><path d="M9.25 1.75v3h3M5.5 8.5h5M5.5 11h3.25" fill="none" stroke="var(--file-icon-stroke)" stroke-width=".95" stroke-linecap="round" stroke-linejoin="round"/></symbol>
  <symbol id="tin-json" viewBox="0 0 16 16"><path d="M4 1.75h5.25l3 3v9.5a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V2.75a1 1 0 0 1 1-1z" fill="var(--file-icon-surface)" stroke="var(--file-icon-stroke)" stroke-width=".95" stroke-linejoin="round"/><path d="M9.25 1.75v3h3" fill="none" stroke="var(--file-icon-stroke)" stroke-width=".95" stroke-linejoin="round"/><path d="M6.7 7.6c-.6 0-.9.3-.9.8v.7c0 .4-.25.6-.6.65.35.05.6.25.6.65v.7c0 .5.3.8.9.8M9.3 7.6c.6 0 .9.3.9.8v.7c0 .4.25.6.6.65-.35.05-.6.25-.6.65v.7c0 .5-.3.8-.9.8" fill="none" stroke="var(--file-icon-stroke)" stroke-width=".8" stroke-linecap="round" stroke-linejoin="round"/></symbol>
  <symbol id="tin-skill" viewBox="0 0 16 16"><path d="M4 1.75h5.25l3 3v9.5a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V2.75a1 1 0 0 1 1-1z" fill="var(--file-icon-surface)" stroke="var(--file-icon-stroke)" stroke-width=".95" stroke-linejoin="round"/><path d="M9.25 1.75v3h3" fill="none" stroke="var(--file-icon-stroke)" stroke-width=".95" stroke-linejoin="round"/><path d="m8 7.5.65 1.35L10 9.5l-1.35.65L8 11.5l-.65-1.35L6 9.5l1.35-.65z" fill="var(--file-icon-stroke)"/></symbol>
</svg>`;

function tinFilesTheme() {
  const dark = document.documentElement.dataset.theme === "dark";
  return {
    name: dark ? "tin-coal" : "tin-paper",
    type: dark ? "dark" : "light",
    colors: {
      "focusBorder": "var(--tree-focus)",
      "foreground": "var(--ink)",
      "input.background": "var(--input-bg)",
      "input.foreground": "var(--ink)",
      "list.activeSelectionBackground": "var(--tree-active)",
      "list.activeSelectionForeground": "var(--ink)",
      "list.focusBackground": "var(--tree-active)",
      "list.hoverBackground": "var(--tree-hover)",
      "sideBar.background": "var(--paper-bg)",
      "sideBar.border": "var(--tree-border)",
      "sideBar.foreground": "var(--ink)",
      "sideBarSectionHeader.foreground": "var(--ink-muted)",
    },
    tokenColors: [],
  };
}

const TIN_AUTH_APPEARANCE = window.TinAuth.appearance;
const TIN_AUTH_LOCALIZATION = window.TinAuth.localization;

const state = {
  projects: [],
  project: null,
  projectAccess: "loading",
  projectDeleteProjectId: null,
  projectDeleteRequestId: null,
  projectDeleteName: null,
  signedInName: null,
  signedInUserId: null,
  workflows: [],
  runWorkflows: new Map(),
  projectWorkflows: [],
  systemSummary: null,
  runs: [],
  activity: [],
  messages: [],
  integrations: [],
  decisions: [],
  view: viewFromLocation(),
  documentRoute: documentRouteFromLocation(),
  taskRoute: taskRouteFromLocation(),
  fileRoute: fileRouteFromLocation(),
  compareRoute: compareRouteFromLocation(),
  comparePage: null,
  compareSessions: new Map(),
  compareReturnFocus: null,
  taskDetail: null,
  taskLoadingRunId: null,
  documentCache: new Map(),
  documentLoadingRunId: null,
  documentCleanup: null,
  filesSnapshot: null,
  filesLoading: false,
  filesError: null,
  filesSearch: "",
  filesDirectory: "",
  filesTree: null,
  filesTreeObserver: null,
  filesTreeSubscription: null,
  filesUpdated: false,
  fileCache: new Map(),
  fileLoadingKey: null,
  fileError: null,
  workflowFilter: "all",
  workflowSection: "yours",
  templateView: "all",
  workflowSearch: "",
  workflowEditor: null,
  expandedRun: null,
  runDetails: new Map(),
  activityFilter: "all",
  activityHasMore: false,
  activityLoading: false,
  decisionId: null,
  agentTab: "claude",
  // The lock page starts on Codex like the website hero; a click moves both surfaces.
  lockTab: null,
  integrationFilter: "all",
  integrationSearch: "",
  expandedIntegration: null,
  integrationOptions: new Map(),
  integrationLoading: null,
  integrationConnectIntent: null,
  githubInstallationChoice: null,
  repositoryChoice: null,
  stripeKeyChoice: null,
  projectCreateWorkspaceId: null,
  projectCreateRequestId: null,
  projectCreatePersonal: false,
  chatDraft: "",
  chatDrafts: new Map(),
  sending: false,
  pendingReviewRunIds: new Set(),
  deferredReviewRunIds: [],
  pollTimer: null,
  pollInFlight: false,
  projectGeneration: 0,
};

function viewFromLocation() {
  if (compareRouteFromLocation()) return "compare";
  if (documentRouteFromLocation()) return "document";
  if (taskRouteFromLocation()) return "task";
  if (fileRouteFromLocation()) return "file";
  const path = window.location.pathname.slice(1);
  const view = path === "system" ? "workflows" : path;
  return ALLOWED_VIEWS.has(view) ? view : "workflows";
}

function fileRouteFromLocation() {
  const raw = currentRoute();
  const [path, query = ""] = raw.split("?", 2);
  if (path !== "file") return null;
  const values = new URLSearchParams(query);
  const filePath = values.get("path");
  const revision = values.get("revision");
  const reviewRunId = values.get("reviewRun");
  if (!filePath || !/^[0-9a-f]{40}$/.test(revision || "")) return null;
  const compareRun = values.get("compareRun");
  return { path: filePath, revision, reviewRunId: reviewRunId || null,
    compareRun: /^[0-9a-f-]{36}$/i.test(compareRun || "") ? compareRun : null,
    compareReturn: ["decisions", "activity", "workflows", "chat"].includes(values.get("return")) ? values.get("return") : "decisions",
    backTo: ALLOWED_VIEWS.has(values.get("back") || "") ? values.get("back") : null,
    backToDecision: values.get("back") === "decisions",
    source: values.get("source") === "retained" && compareRun ? "retained" : "canonical",
  };
}

function compareRouteFromLocation() {
  const [path, query = ""] = currentRoute().split("?", 2);
  const match = path.match(/^compare\/([0-9a-f-]{36})$/i);
  if (!match) return null;
  const returnView = new URLSearchParams(query).get("return");
  return { runId: match[1], returnView: ["decisions", "activity", "workflows", "chat"].includes(returnView) ? returnView : "decisions" };
}

function taskRouteFromLocation() {
  const raw = window.location.pathname.slice(1);
  const match = raw.match(/^task\/([^/]+)$/);
  if (!match) return null;
  try {
    return { runId: decodeURIComponent(match[1]) };
  } catch (_error) {
    return null;
  }
}

function documentRouteFromLocation() {
  const raw = currentRoute();
  const [path, query = ""] = raw.split("?", 2);
  const match = path.match(/^document\/([^/]+)$/);
  if (!match) return null;
  const returnView = new URLSearchParams(query).get("return");
  const taskPath = new URLSearchParams(query).get("taskPath");
  let runId;
  try {
    runId = decodeURIComponent(match[1]);
  } catch (_error) {
    return null;
  }
  return {
    runId,
    returnView: taskPath ? "task" : ALLOWED_VIEWS.has(returnView) ? returnView : "workflows",
    taskPath: taskPath || null,
    source: new URLSearchParams(query).get("source") === "retained" ? "retained" : "canonical",
  };
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatWorkflowKey(key) {
  return String(key || "workflow").replaceAll("_", "-");
}

function shortRunId(id) {
  return `run_${String(id || "").slice(0, 4)}`;
}

function humanize(value) {
  const words = String(value || "").replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function timeLabel(value) {
  if (!value) return "now";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "now";
  const seconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
  if (seconds < 60) return "now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function clockTime(date = new Date()) {
  const value = date instanceof Date ? date : new Date(date);
  if (Number.isNaN(value.getTime())) return "";
  return value.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function ledgerTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "--:--";
  return date.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function dayKey(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "unknown";
  return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
}

function dayLabel(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "date unknown";
  const today = new Date();
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  const calendar = date.toLocaleDateString(undefined, { month: "short", day: "numeric" }).toLowerCase();
  if (dayKey(date) === dayKey(today)) return `today · ${calendar}`;
  if (dayKey(date) === dayKey(yesterday)) return `yesterday · ${calendar}`;
  return calendar;
}

async function authorizedFetch(path, options = {}) {
  const token = await window.Clerk?.session?.getToken();
  if (!token) throw new Error("Your Tin session has ended. Sign in again.");
  const response = await fetch(path, {
    ...options,
    headers: {
      Accept: "application/json",
      Authorization: `Bearer ${token}`,
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    let code = null;
    let detail = null;
    try {
      const payload = await response.json();
      if (typeof payload.detail === "string") message = payload.detail;
      if (payload.detail?.message) message = payload.detail.message;
      code = payload.detail?.code || null;
      detail = payload.detail ?? null;
    } catch (_error) {
      // Keep the HTTP status when the server did not return JSON.
    }
    const error = new Error(message);
    error.status = response.status;
    error.code = code;
    error.detail = detail;
    throw error;
  }
  return response;
}

async function api(path, options = {}) {
  if (window.TinRunPaymentCard) {
    const cardContext = currentProjectContext();
    options = await window.TinRunPaymentCard.prepare(path, options, {
      workflows: state.workflows, projectWorkflows: state.projectWorkflows,
      assertContext() { if (!isCurrentProjectContext(cardContext)) throw new Error("Project changed. Run not started."); },
    });
  }
  if (state.billing?.enabled && options.method === "POST") {
    const context = currentProjectContext();
    const response = await window.TinBilling.paidStart({
      path, options, projectId: context.projectId, actor: state.signedInUserId,
      fetch: authorizedFetch,
      assertContext() { if (!isCurrentProjectContext(context)) throw new Error("Project changed. Run not started."); },
    });
    return response.status === 204 ? null : response.json();
  }
  const response = await authorizedFetch(path, options);
  return response.status === 204 ? null : response.json();
}

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("is-visible");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("is-visible"), 3200);
}

async function signOutTin() {
  await window.Clerk.signOut();
  window.location.assign("/");
}

function currentProjectContext() {
  return { projectId: state.project?.id || null, generation: state.projectGeneration };
}

function isCurrentProjectContext(context) {
  return context.projectId === state.project?.id && context.generation === state.projectGeneration;
}

function projectStorageKey() {
  return state.signedInUserId ? `tin-lite:project:${state.signedInUserId}` : null;
}

function storedProjectId() {
  const key = projectStorageKey();
  if (!key) return null;
  try {
    return window.localStorage.getItem(key);
  } catch (_error) {
    return null;
  }
}

function persistProjectSelection(projectId) {
  const key = projectStorageKey();
  if (key) {
    try {
      window.localStorage.setItem(key, projectId);
    } catch (_error) {
      // The URL remains the durable browser fallback when storage is unavailable.
    }
  }
  const url = new URL(window.location.href);
  url.searchParams.set("project", projectId);
  window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
}

function selectedProject(projects, invitedProjectId = null) {
  const byId = new Map(projects.map((project) => [project.id, project]));
  const urlProjectId = new URL(window.location.href).searchParams.get("project");
  return byId.get(invitedProjectId) || byId.get(urlProjectId) || byId.get(storedProjectId()) || projects[0];
}

function renderProjectMenu() {
  projectMenu.replaceChildren();
  const groups = new Map();
  for (const project of state.projects) {
    const key = project.workspace_id || `project:${project.id}`;
    if (!groups.has(key)) {
      groups.set(key, {
        id: project.workspace_id,
        name: project.workspace_name || "Workspace",
        canCreate: false,
        projects: [],
      });
    }
    const group = groups.get(key);
    group.canCreate ||= Boolean(project.can_create_project_in_workspace);
    group.projects.push(project);
  }
  const list = document.createElement("div");
  list.className = "project-menu-list";
  const showWorkspaceNames = groups.size > 1;
  for (const group of groups.values()) {
    const section = document.createElement("div");
    section.className = "project-menu-group";
    if (showWorkspaceNames) {
      const heading = document.createElement("div");
      heading.className = "project-menu-heading";
      heading.innerHTML = `<span>Workspace · ${escapeHtml(group.name)}</span>`;
      if (group.canCreate && group.id) {
        const create = document.createElement("button");
        create.type = "button";
        create.className = "project-menu-create";
        create.textContent = "New project";
        create.addEventListener("click", () => openProjectCreate(group.id, group.name));
        heading.append(create);
      }
      section.append(heading);
    }
    for (const project of group.projects) {
      const item = document.createElement("button");
      const current = project.id === state.project?.id;
      item.type = "button";
      item.className = `project-menu-item${current ? " is-current" : ""}`;
      item.dataset.projectId = project.id;
      item.title = project.name;
      if (current) item.setAttribute("aria-current", "true");
      const memberCount = Math.max(1, Number(project.member_count) || 1);
      item.innerHTML = `<span>${escapeHtml(project.name)}</span><small>${memberCount} ${memberCount === 1 ? "member" : "members"}</small>`;
      item.addEventListener("click", () => switchProject(project.id));
      section.append(item);
    }
    list.append(section);
  }
  const actions = document.createElement("div");
  actions.className = "project-menu-actions";
  const invite = document.createElement("button");
  invite.type = "button";
  invite.textContent = "Invite someone →";
  invite.addEventListener("click", openProjectInvite);
  actions.append(invite);
  if (state.billing?.enabled) {
    const billing = document.createElement("button");
    billing.type = "button";
    billing.textContent = state.billing.is_admin ? "Billing →" : "Spending →";
    billing.addEventListener("click", () => { closeProjectMenu(); navigate("billing"); });
    actions.append(billing);
  }
  const currentGroup = [...groups.values()].find((group) =>
    group.projects.some((project) => project.id === state.project?.id));
  if (!showWorkspaceNames && currentGroup?.canCreate && currentGroup.id) {
    const create = document.createElement("button");
    create.type = "button";
    create.textContent = "New project →";
    create.addEventListener("click", () => openProjectCreate(currentGroup.id, currentGroup.name));
    actions.append(create);
  } else if (state.projects.length && !state.projects.some((project) => project.can_create_project_in_workspace)) {
    const createPersonal = document.createElement("button");
    createPersonal.type = "button";
    createPersonal.textContent = "Create my workspace →";
    createPersonal.addEventListener("click", openPersonalWorkspaceCreate);
    actions.append(createPersonal);
  }
  if (state.project?.can_delete) {
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "project-menu-danger";
    remove.textContent = "Delete project →";
    remove.addEventListener("click", openProjectDelete);
    actions.append(remove);
  }
  list.append(actions);
  projectMenu.append(list);
}

function openProjectCreate(workspaceId, workspaceName) {
  closeProjectMenu();
  state.projectCreateWorkspaceId = workspaceId;
  state.projectCreateRequestId = window.crypto.randomUUID();
  state.projectCreatePersonal = false;
  projectCreateCopy.textContent = `Create it in ${workspaceName}. Its files and access stay project-scoped.`;
  projectCreateName.value = "";
  projectCreateDialog.showModal();
  window.requestAnimationFrame(() => projectCreateName.focus());
}

function openPersonalWorkspaceCreate() {
  closeProjectMenu();
  state.projectCreateWorkspaceId = null;
  state.projectCreateRequestId = null;
  state.projectCreatePersonal = true;
  projectCreateCopy.textContent = "Create your personal workspace and its first project.";
  projectCreateName.value = personalProjectName();
  projectCreateDialog.showModal();
  window.requestAnimationFrame(() => projectCreateName.select());
}

function openProjectInvite() {
  if (!state.project) return;
  closeProjectMenu();
  projectInviteForm.reset();
  projectInviteCopy.textContent = `Invite a verified email address to ${state.project.name}. Every member has the same project access.`;
  projectInviteResult.hidden = true;
  projectInviteUrl.textContent = "";
  projectInviteDialog.showModal();
  window.requestAnimationFrame(() => projectInviteEmail.focus());
}

function openProjectDelete() {
  if (!state.project?.can_delete) return;
  closeProjectMenu();
  state.projectDeleteProjectId = state.project.id;
  state.projectDeleteRequestId = window.crypto.randomUUID();
  state.projectDeleteName = state.project.name;
  projectDeleteCopy.textContent = `Deleting ${state.project.name} stops its running work, removes its schedules, disconnects its integrations and removes its files for every member. Billing history stays. This cannot be undone.`;
  projectDeletePrompt.replaceChildren("Type ", Object.assign(document.createElement("strong"), { textContent: state.project.name }), " to confirm");
  projectDeleteName.placeholder = state.project.name;
  projectDeleteName.value = "";
  updateProjectDeleteConfirm();
  projectDeleteDialog.showModal();
  window.requestAnimationFrame(() => projectDeleteName.focus());
}

function updateProjectDeleteConfirm() {
  const confirm = projectDeleteForm.querySelector("[data-confirm-project-delete]");
  confirm.disabled = projectDeleteName.value.trim() !== state.projectDeleteName;
}

function forgetProjectSelection(projectId) {
  const key = projectStorageKey();
  try {
    if (key && window.localStorage.getItem(key) === projectId) window.localStorage.removeItem(key);
  } catch (_error) {
    // Storage may be unavailable; the URL is cleared below either way.
  }
  const url = new URL(window.location.href);
  if (url.searchParams.get("project") === projectId) {
    url.searchParams.delete("project");
    window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
  }
}

function projectDeletedMessage(deleted) {
  const stopped = Number(deleted?.stopped_runs) || 0;
  const base = `${deleted.name} deleted.`;
  return stopped ? `${base} ${stopped} running ${stopped === 1 ? "run" : "runs"} stopped.` : base;
}

async function deleteProject() {
  const projectId = state.projectDeleteProjectId;
  const requestId = state.projectDeleteRequestId;
  const name = state.projectDeleteName;
  if (!projectId || !requestId || projectDeleteName.value.trim() !== name) return;
  const confirm = projectDeleteForm.querySelector("[data-confirm-project-delete]");
  confirm.disabled = true;
  confirm.textContent = "Deleting…";
  try {
    const params = new URLSearchParams({ request_id: requestId, confirm_name: name });
    const deleted = await api(`/api/projects/${encodeURIComponent(projectId)}?${params}`, { method: "DELETE" });
    await afterProjectDeleted(deleted);
  } catch (error) {
    showToast(`Could not delete ${name}: ${error.message}`);
    confirm.disabled = false;
  } finally {
    confirm.textContent = "Delete project";
  }
}

async function afterProjectDeleted(deleted) {
  projectDeleteDialog.close();
  forgetProjectSelection(deleted.project_id);
  state.projects = await api("/api/projects");
  if (state.projects.length) {
    await loadProject(selectedProject(state.projects));
  } else {
    state.project = null;
    await bootstrap();
  }
  showToast(projectDeletedMessage(deleted));
}

function closeProjectMenu({ restoreFocus = false } = {}) {
  projectMenu.hidden = true;
  projectSwitcher.setAttribute("aria-expanded", "false");
  projectSwitcher.classList.remove("is-open");
  if (restoreFocus) projectSwitcher.focus();
}

function openProjectMenu({ focus = "current" } = {}) {
  if (projectSwitcher.disabled || !state.projects.length) return;
  renderProjectMenu();
  projectMenu.hidden = false;
  projectSwitcher.setAttribute("aria-expanded", "true");
  projectSwitcher.classList.add("is-open");
  const items = [...projectMenu.querySelectorAll("[data-project-id]")];
  const target = focus === "last"
    ? items.at(-1)
    : focus === "first"
      ? items[0]
      : items.find((item) => item.getAttribute("aria-current") === "true") || items[0];
  target?.focus();
}

function handleProjectMenuKeydown(event) {
  const items = [...projectMenu.querySelectorAll("[data-project-id]")];
  const index = items.indexOf(document.activeElement);
  let next = null;
  if (event.key === "ArrowDown") next = items[(index + 1 + items.length) % items.length];
  if (event.key === "ArrowUp") next = items[(index - 1 + items.length) % items.length];
  if (event.key === "Home") next = items[0];
  if (event.key === "End") next = items.at(-1);
  if (event.key === "Escape") {
    event.preventDefault();
    closeProjectMenu({ restoreFocus: true });
    return;
  }
  if ((event.key === "Enter" || event.key === " ") && index >= 0) {
    event.preventDefault();
    items[index].click();
    return;
  }
  if (next) {
    event.preventDefault();
    next.focus();
  }
}

function activeUnsavedWorkflowRuns() {
  return state.runs.filter(
    (run) =>
      !run.project_workflow_id &&
      run.workflow_name !== "project.task" &&
      RUNNING_STATES.has(run.status),
  );
}

function registryWorkflows() {
  return state.workflows.filter((workflow) => workflow.definition?.kind !== "task");
}

const UNASSIGNED_WORKFLOW_SYSTEM = "__unassigned__";

function registrySystemKey(workflow) {
  return workflow.system_id && workflow.system_name
    ? `system:${workflow.system_id}`
    : UNASSIGNED_WORKFLOW_SYSTEM;
}

function registrySystemGroups(workflows) {
  const groups = new Map();
  for (const workflow of workflows) {
    const key = registrySystemKey(workflow);
    if (!groups.has(key)) {
      const unassigned = key === UNASSIGNED_WORKFLOW_SYSTEM;
      groups.set(key, {
        key,
        name: unassigned ? "Not in a system" : workflow.system_name,
        order: unassigned ? Number.MAX_SAFE_INTEGER : workflow.system_order,
        unassigned,
        workflows: [],
      });
    }
    groups.get(key).workflows.push(workflow);
  }
  return [...groups.values()].sort((left, right) => {
    if (left.unassigned !== right.unassigned) return left.unassigned ? 1 : -1;
    const order = Number(left.order ?? Number.MAX_SAFE_INTEGER) - Number(right.order ?? Number.MAX_SAFE_INTEGER);
    return order || left.name.localeCompare(right.name);
  });
}

function workflowHasRegistryState(workflow, status) {
  if (status === "paused") {
    return state.projectWorkflows.some(
      (configured) => configured.workflow_id === workflow.id && configured.status === "paused",
    ) || state.runs.some(
      (run) => run.workflow_id === workflow.id && run.status === "paused",
    );
  }
  if (status === "running") {
    return state.runs.some(
      (run) => run.workflow_id === workflow.id && RUNNING_STATES.has(run.status),
    );
  }
  return state.runs.some(
    (run) => run.workflow_id === workflow.id && run.status === "needs_input",
  );
}

function workflowCountLabel(count) {
  return `${count} ${count === 1 ? "workflow" : "workflows"}`;
}

function registrySystemFact(group, total, searching) {
  if (searching) return `${group.workflows.length} of ${total}`;
  const states = [
    ["needs_input", "needs you"],
    ["running", "running"],
    ["paused", "paused"],
  ];
  const live = states
    .map(([status, label]) => [
      group.workflows.filter((workflow) => workflowHasRegistryState(workflow, status)).length,
      label,
    ])
    .find(([count]) => count > 0);
  return live
    ? `${workflowCountLabel(group.workflows.length)} · ${live[0]} ${live[1]}`
    : workflowCountLabel(group.workflows.length);
}

function workflowSearchMatch(value, query, className) {
  if (!query) return escapeHtml(value);
  const lower = value.toLowerCase();
  let cursor = 0;
  let html = "";
  while (cursor < value.length) {
    const match = lower.indexOf(query, cursor);
    if (match < 0) {
      html += escapeHtml(value.slice(cursor));
      break;
    }
    html += escapeHtml(value.slice(cursor, match));
    html += `<span class="${className}">${escapeHtml(value.slice(match, match + query.length))}</span>`;
    cursor = match + query.length;
  }
  return html;
}

function renderRegistryWorkflows(registry, visibleRegistry, query, rawQuery) {
  if (!visibleRegistry.length) {
    const displayQuery = rawQuery.trim();
    const ask = displayQuery.length >= 3
      ? `<button type="button" data-ask-luna-workflow>Ask Luna for a ${escapeHtml(displayQuery)} workflow →</button>`
      : "";
    return `<div class="registry-search-empty">
      <strong>No workflows match “${escapeHtml(displayQuery)}”.</strong>
      <span>Search looks at workflow names and ids across all ${registry.length} in the registry.</span>
      <div><button type="button" data-clear-workflow-search>Clear search</button>${ask}</div>
    </div>`;
  }
  const totals = new Map(
    registrySystemGroups(registry).map((group) => [group.key, group.workflows.length]),
  );
  const groups = registrySystemGroups(visibleRegistry);
  const searching = Boolean(query);
  const resultCount = searching
    ? `<div class="registry-search-count">${visibleRegistry.length} ${visibleRegistry.length === 1 ? "match" : "matches"} in ${groups.length} ${groups.length === 1 ? "group" : "groups"}</div>`
    : "";
  return `${resultCount}${groups.map((group) => `<section class="registry-system-group">
    <header class="registry-system-header ${group.unassigned ? "is-unassigned" : ""}">
      <strong>${escapeHtml(group.name)}</strong>
      <code>${escapeHtml(registrySystemFact(group, totals.get(group.key), searching))}</code>
    </header>
    <div class="registry-system-workflows">
      ${group.workflows.map((workflow) => workflowEditor(workflow) || registryWorkflowCard(workflow, query)).join("")}
    </div>
  </section>`).join("")}`;
}

function workflowRequirementState(workflow) {
  const requirements = workflow.definition?.integration_requirements || [];
  return requirements.map((requirement) => {
    const integration = state.integrations.find((item) => item.key === requirement.provider_key);
    const granted = new Set(
      integration?.configuration?.granted_capabilities || integration?.capabilities || [],
    );
    const configured = Boolean(integration?.connection_id) && integration.status === "connected";
    const capable = (requirement.capabilities || []).every((capability) => granted.has(capability));
    const selected = integrationSelection(integration);
    const resourceReady = !RESOURCE_SCOPED_INTEGRATIONS.has(requirement.provider_key) || Boolean(selected);
    return { requirement, integration, ready: configured && capable && resourceReady };
  });
}

function systemTemplateCard(workflow, query) {
  if (state.workflowEditor?.workflowId === workflow.id && !state.workflowEditor.projectWorkflowId) {
    return systemTemplateSetupCard(workflow);
  }
  const saved = Boolean(workflow.saved);
  const count = Number(workflow.project_workflow_count || 0);
  const contextLabel = count
    ? `${count} ${count === 1 ? "workflow" : "workflows"} in My system`
    : saved ? "not set up yet" : "";
  return `<article class="system-template-card">
    <span class="system-template-identity">
      <strong>${workflowSearchMatch(workflow.title, query, "workflow-search-title-match")}</strong>
      <span class="system-template-description">${escapeHtml(workflow.description)}</span>
      <code>${workflowSearchMatch(workflow.key, query, "workflow-search-id-match")}${contextLabel ? ` · ${escapeHtml(contextLabel)}` : ""}</code>
    </span>
    <span class="system-template-actions ${state.templateView === "saved" ? "is-saved-view" : ""}">
      ${saved
        ? `<span class="system-saved-mark"><i aria-hidden="true"></i>Saved</span>${state.templateView === "saved" ? `<button class="system-quiet-action" type="button" data-remove-saved-template="${escapeHtml(workflow.id)}">Remove from saved</button>` : ""}`
        : `<button class="system-quiet-action" type="button" data-save-template="${escapeHtml(workflow.id)}">Save</button>`}
      <button class="button-secondary" type="button" data-configure-workflow="${escapeHtml(workflow.id)}">Setup</button>
    </span>
  </article>`;
}

function systemTemplateSetupCard(workflow) {
  const schema = workflow.definition?.input_schema || {};
  const required = new Set(schema.required || []);
  const fields = workflow.key === "content.deliver" ? window.TinContentDelivery.fields() : workflow.key === "content.generate" ? window.TinContentDraft.fields({}, schema) : workflow.key === "style.capture" ? window.TinStyleCapture.fields() : workflow.key === "content.plan" ? window.TinContentPlan.fields() : orderedWorkflowFields(schema).map(([name, definition]) => {
    const label = definition.title || humanize(name);
    const fieldId = `template-input-${String(name).replace(/[^a-z0-9_-]/gi, "-")}`;
    return `<label class="system-setting"><strong>${escapeHtml(label)}</strong>${workflowInputControl(`input:${name}`, definition, definition.default ?? "", label, fieldId, required.has(name))}${workflowFieldHelp(definition, fieldId)}</label>`;
  }).join("");
  const requirements = workflowRequirementState(workflow);
  const requirementRows = requirements.map(({ requirement, integration, ready }) => `<div class="system-requirement ${ready ? "is-ready" : "is-missing"}">
    <span class="system-card-dot ${ready ? "is-running" : "is-pending"}" aria-hidden="true"></span>
    <span><strong>${escapeHtml(integration?.name || humanize(requirement.provider_key))}</strong><small>${ready ? "Connected and ready" : "Connection required"}</small></span>
    ${ready ? "" : '<button type="button" data-open-integrations>Connect →</button>'}
  </div>`).join("");
  const blocked = requirements.some((item) => !item.ready);
  return `<form class="system-template-card is-open workflow-config-form ${workflow.key === "content.plan" ? "is-weekly" : "is-manual"}" data-workflow-id="${escapeHtml(workflow.id)}">
    <header class="system-template-open-header">
      <span class="system-template-identity"><strong>${escapeHtml(workflow.title)}</strong><span class="system-template-description">${escapeHtml(workflow.description)}</span><code>${escapeHtml(workflow.key)} · v${escapeHtml(workflow.version_label)}</code></span>
      <span class="system-template-actions">${workflow.saved ? '<span class="system-saved-mark"><i aria-hidden="true"></i>Saved</span>' : ""}<button class="system-quiet-action" type="button" data-cancel-workflow-editor>Cancel</button></span>
    </header>
    <div class="system-template-setup-body">
      <section>
        <code class="system-config-kicker">what it works on</code>
        <label class="system-setting"><strong>Name</strong><input class="workflow-inline-input" name="workflow_name" required maxlength="120" value="${escapeHtml(workflow.title)}" /></label>
        ${fields || '<span class="system-config-note">No additional choices.</span>'}
        ${requirementRows ? `<div class="system-requirements"><code class="system-config-kicker">connections</code>${requirementRows}</div>` : ""}
      </section>
      <section class="system-config-when">
        <code class="system-config-kicker">when</code>
        ${workflowScheduleControls(workflow.id, workflow.key === "content.plan" ? {cadence: "weekly", weekdays: ["monday"], local_time: "09:00", timezone: state.project?.timezone || "UTC"} : null, false, workflow.definition?.schedule_modes)}
        <p>Saving pins v${escapeHtml(workflow.version_label)}. You can change the schedule later.</p>
      </section>
    </div>
    <footer class="system-config-footer">
      <span class="system-config-note">Goes to My system when you finish.</span>
      <button class="button-secondary" type="submit" data-save-workflow ${blocked ? "disabled" : ""}>Finish setup</button>
      <button class="button" type="submit" data-save-and-run ${blocked ? "disabled" : ""}>Finish setup and run now</button>
    </footer>
  </form>`;
}

function systemAddWorkflowsHtml(registry, query) {
  const selected = state.templateView === "saved"
    ? registry.filter((workflow) => workflow.saved)
    : registry;
  const visible = selected.filter((workflow) =>
    `${workflow.title} ${workflow.key} ${workflow.description}`.toLowerCase().includes(query),
  );
  const switcher = `<div class="system-template-switch" role="group" aria-label="Workflow templates">
    <button type="button" data-template-view="saved" class="${state.templateView === "saved" ? "is-active" : ""}">Saved ${registry.filter((item) => item.saved).length}</button>
    <button type="button" data-template-view="all" class="${state.templateView === "all" ? "is-active" : ""}">All ${registry.length}</button>
  </div>`;
  if (!visible.length) {
    const empty = query
      ? `<strong>No workflows match “${escapeHtml(state.workflowSearch.trim())}”.</strong><span>Try another name or workflow id.</span><button class="button-quiet" type="button" data-clear-workflow-search>Clear search</button>`
      : '<strong>No saved templates yet.</strong><span>Save useful templates here without adding them to My system.</span><button class="button-secondary" type="button" data-template-view="all">Browse all</button>';
    return `${switcher}<div class="system-template-empty">${empty}</div>`;
  }
  return `${switcher}${registrySystemGroups(visible).map((group) => `<section class="system-template-group">
    <header><strong>${escapeHtml(group.name)}</strong><code>${group.workflows.length}</code></header>
    <div>${group.workflows.map((workflow) => systemTemplateCard(workflow, query)).join("")}</div>
  </section>`).join("")}`;
}

function workflowSearchProjection(registry = registryWorkflows()) {
  const rawQuery = state.workflowSearch;
  const query = rawQuery.trim().toLowerCase();
  const activeUnsavedRuns = activeUnsavedWorkflowRuns();
  const visibleLiveRuns = activeUnsavedRuns.filter((run) => {
    const workflow = workflowForRun(run);
    return `${workflow?.title || ""} ${workflow?.key || run.workflow_name} ${shortRunId(run.id)}`
      .toLowerCase()
      .includes(query);
  });
  const visibleRegistry = registry.filter((workflow) =>
    `${workflow.title} ${workflow.key}`.toLowerCase().includes(query),
  );
  const visibleConfigured = state.projectWorkflows.filter((configured) => {
    const run = state.runs.find((item) => item.id === configured.last_run_id);
    const runStatus = run?.status || configured.last_run_status;
    const matchesSearch = `${configured.name} ${configured.workflow_key} ${configured.workflow_description}`
      .toLowerCase()
      .includes(query);
    const matchesFilter =
      state.workflowFilter === "all" ||
      (state.workflowFilter === "scheduled" && configured.schedule && configured.status === "active") ||
      (state.workflowFilter === "paused" && configured.status === "paused") ||
      (state.workflowFilter === "running" && RUNNING_STATES.has(runStatus)) ||
      (state.workflowFilter === "needs_you" && runStatus === "needs_input");
    return matchesSearch && matchesFilter;
  });
  return {
    rawQuery,
    query,
    activeUnsavedRuns,
    visibleLiveRuns,
    visibleRegistry,
    visibleConfigured,
  };
}

function reviewPolicyFor(workflow) {
  const policy = workflow?.definition?.human_review;
  return policy && policy.eligible === true ? policy : {};
}

function isCampaignRevisionReview(run) {
  return run?.workflow_name === "outreach.email_campaign" &&
    run?.status === "needs_input" &&
    run?.review_decision === "approved";
}

function pendingReviewQueue() {
  const waiting = state.runs.filter((run) => {
    const isTaskGate = run.workflow_name === "project.task";
    return run.status === "needs_input" && (run.review_required || isTaskGate);
  });

  const deferred = new Map(
    state.deferredReviewRunIds.map((runId, index) => [runId, index]),
  );
  return waiting.sort((left, right) => {
    const leftDeferred = deferred.has(left.id);
    const rightDeferred = deferred.has(right.id);
    if (leftDeferred !== rightDeferred) return leftDeferred ? 1 : -1;
    if (leftDeferred && rightDeferred) return deferred.get(left.id) - deferred.get(right.id);
    const leftAt = Date.parse(left.review_requested_at || 0) || 0;
    const rightAt = Date.parse(right.review_requested_at || 0) || 0;
    return leftAt - rightAt;
  });
}

function waitingLabel(value) {
  const requestedAt = new Date(value);
  if (Number.isNaN(requestedAt.getTime())) return "now";
  const seconds = Math.max(0, Math.floor((Date.now() - requestedAt.getTime()) / 1000));
  if (seconds < 60) return "now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

function workflowForRun(run) {
  return state.workflows.find((workflow) => workflow.id === run.workflow_id)
    || state.runWorkflows.get(run.workflow_id) || null;
}

async function loadRunWorkflows(runs) {
  // Discovery can hide a template without removing existing runs' identity or controls.
  const context = currentProjectContext();
  const ids = [...new Set(runs.filter(run => !workflowForRun(run)).map(run => run.workflow_id))];
  const workflows = await Promise.all(ids.map(id =>
    api(`/api/workflows/${encodeURIComponent(id)}`).catch(() => null)
  ));
  if (!isCurrentProjectContext(context)) return false;
  for (const workflow of workflows) {
    if (workflow) state.runWorkflows.set(workflow.id, workflow);
  }
  return workflows.some(Boolean);
}

function isMarkdownPath(path) {
  const lower = String(path || "").toLowerCase();
  return lower.endsWith(".md") || lower.endsWith(".markdown");
}

function isMarkdownArtifact(run) {
  return isMarkdownPath(run?.artifact_path);
}

function updateRail() {
  if (!state.project) return;
  projectName.textContent = state.project.name;
  projectSwitcher.setAttribute("aria-label", `Current project: ${state.project.name}`);
  const reviewQueue = pendingReviewQueue();
  const active = state.runs.filter((run) => RUNNING_STATES.has(run.status)).length;
  const needsYou = reviewQueue.length;
  projectSummary.textContent = [
    state.project.workspace_name,
    `${active} running`,
    needsYou ? `${needsYou} needs you` : null,
  ].filter(Boolean).join(" · ");
  const runningCount = state.systemSummary?.running_count ?? state.runs.filter(
    (run) => run.workflow_name !== "project.task" && RUNNING_STATES.has(run.status),
  ).length;
  workflowCount.textContent = runningCount ? String(runningCount) : "";
  decisionCount.textContent = state.decisions.length ? String(state.decisions.length) : "";
  updateAgentRail();
}

const AGENT_PROMPT = "Use Tin to grow my project like a pro!";

function agentCommandFor(tab) {
  const mcpUrl = MCP_URL;
  if (tab === "codex") return [
    `codex mcp add tin --url ${mcpUrl}`,
    "",
    "# A browser tab opens once to sign in. Then start Codex in",
    `# your project and say: ${AGENT_PROMPT}`,
  ].join("\n");
  if (tab === "api") return `POST ${mcpUrl}\nAuthorization: Bearer <token>`;
  return [
    `claude mcp add -t http tin ${mcpUrl}`,
    "",
    "# Run /mcp once to sign in. Then, in your project, say:",
    `# ${AGENT_PROMPT}`,
  ].join("\n");
}

function updateAgentRail() {
  if (agentRail) agentRail.hidden = state.projectAccess === "locked";
  if (!agentCommand || !agentLastUsed) return;
  agentCommand.textContent = agentCommandFor(state.agentTab);
  const usedAt = state.systemSummary?.last_mcp_used_at;
  const tool = state.systemSummary?.last_mcp_tool_name;
  agentLastUsed.textContent = usedAt && tool
    ? `last used ${timeLabel(usedAt)} · ${humanize(tool)}`
    : "not connected yet";
  agentRailStatus?.classList.toggle("is-connected", Boolean(usedAt && tool));
  agentRail?.querySelectorAll("[data-agent-tab]").forEach((button) => {
    button.setAttribute("aria-selected", String(button.dataset.agentTab === state.agentTab));
  });
}

// The locked dashboard. One install line per agent, no comment lines: the sign-in opens
// by itself and the rail block carries the longer version once the project unlocks.
const LOCK_PAGE_TABS = [
  { tab: "codex", label: "Codex" },
  { tab: "claude", label: "Claude Code" },
  { tab: "api", label: "API" },
];

function lockPageLine(tab) {
  if (tab === "api") return { prompt: "GET", command: `${new URL(MCP_URL).origin}/api/workflows?project_id={project_id}` };
  return { prompt: "$", command: agentCommandFor(tab).split("\n")[0] };
}

function renderLockPage() {
  const tab = LOCK_PAGE_TABS.some((item) => item.tab === state.lockTab) ? state.lockTab : "codex";
  const line = lockPageLine(tab);
  main.innerHTML = `
    <section class="lock-page" aria-labelledby="lock-page-title">
      <div class="lock-page-column">
        <h1 id="lock-page-title">Set up Tin from your coding agent</h1>
        <p>Your coding agent already knows your repo and how you write. Connect Tin, and it will pick the workflows that fit and get the first ones running for you in a few minutes.</p>
        <p><strong>Browser setup is not available yet.</strong></p>
        <div class="lock-page-install">
          <div class="lock-page-tabs" role="tablist" aria-label="Coding agent">
            ${LOCK_PAGE_TABS.map((item) => `<button type="button" role="tab" data-lock-tab="${item.tab}" aria-selected="${String(item.tab === tab)}">${item.label}</button>`).join("")}
          </div>
          <div class="lock-page-line">
            <span aria-hidden="true">${escapeHtml(line.prompt)}</span>
            <code id="lock-page-command">${escapeHtml(line.command)}</code>
            <button type="button" id="copy-lock-command" aria-label="Copy">
              <svg aria-hidden="true" viewBox="0 0 16 16"><rect x="5" y="5" width="9" height="9" rx="1.5" /><path d="M11 5V3.5A1.5 1.5 0 0 0 9.5 2h-6A1.5 1.5 0 0 0 2 3.5v6A1.5 1.5 0 0 0 3.5 11H5" /></svg>
            </button>
          </div>
        </div>
      </div>
    </section>`;
  main.querySelectorAll("[data-lock-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      state.lockTab = button.dataset.lockTab;
      state.agentTab = button.dataset.lockTab;
      renderLockPage();
      updateAgentRail();
    });
  });
  main.querySelector("#copy-lock-command").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(line.command);
    } catch (_error) {
      showToast("Copy did not work. Select the command and copy it from here.");
      return;
    }
    showToast(tab === "api" ? "Copied." : `Copied. Run it, then ask your coding agent: “${AGENT_PROMPT}”`);
    recordLockPageEvent("install_copied", tab);
  });
}

function recordLockPageEvent(action, agent = null) {
  if (!state.project) return;
  api("/api/events/lock-page", {
    method: "POST",
    body: JSON.stringify({ action, agent, project_id: state.project.id }),
  }).catch(() => {});
}

function navigate(view) {
  if (!ALLOWED_VIEWS.has(view)) return;
  if (view !== "integrations") clearConnectRequest();
  goToRoute(view);
  main.focus({ preventScroll: true });
}

function openDocument(runId, returnView = state.view, source = "canonical") {
  window.TinWorkflowReview?.close(state.project?.id, runId);
  const safeReturn = ALLOWED_VIEWS.has(returnView) ? returnView : "workflows";
  goToRoute(`document/${encodeURIComponent(runId)}?return=${encodeURIComponent(safeReturn)}${source === "retained" ? "&source=retained" : ""}`);
}

function openTaskDocument(runId, path) {
  const query = new URLSearchParams({ return: "task", taskPath: path });
  goToRoute(`document/${encodeURIComponent(runId)}?${query.toString()}`);
}

function openTask(runId) {
  goToRoute(`task/${encodeURIComponent(runId)}`);
}

function openProjectFile(path, revision, reviewRunId = null) {
  const query = new URLSearchParams({ path, revision });
  if (reviewRunId) query.set("reviewRun", reviewRunId);
  goToRoute(`file?${query.toString()}`);
}

function availableRunOutput(run) {
  if (run?.artifact_path && run?.canonical_commit_sha) {
    return { path: run.artifact_path, revision: run.canonical_commit_sha, source: "canonical" };
  }
  if (run?.retained_output) {
    return { path: run.retained_output.artifact_path, revision: run.retained_output.revision, source: "retained" };
  }
  return null;
}

function outputReadUrl(runId, source = "canonical") {
  return `/api/workflows/runs/${encodeURIComponent(runId)}/artifact${source === "retained" ? "?source=retained" : ""}`;
}

function retainedOutputLabel(run) {
  return hasOutputConflict(run) ? "Compare" : run?.retained_output?.reason === "execution_interrupted" ? "Partial result" : "Saved result";
}

function retainedOutputMessage(run) {
  if (!run?.retained_output) return "";
  if (run.retained_output.reason === "execution_interrupted") {
    return "This workflow stopped before finishing. Its partial result is saved for reading and has not been applied to Files.";
  }
  return run.retained_output.reason === "output_conflict"
    ? "The result was saved because this file changed while the workflow ran. The current file was left alone."
    : "The result is saved. Tin has not yet confirmed whether it reached Files.";
}

// Any output that is not Markdown (diagrams, images, video, audio, PDF, data, plain text)
// opens in the project file viewer, which reads the raw bytes and picks a renderer by type.
function openRunOutputFile(run, output, returnView = state.view) {
  const query = new URLSearchParams({ path: output.path, revision: output.revision });
  if (output.source === "retained") {
    query.set("compareRun", run.id);
    query.set("source", "retained");
  } else if (run.status === "needs_input") {
    query.set("reviewRun", run.id);
  }
  if (ALLOWED_VIEWS.has(returnView)) query.set("back", returnView);
  goToRoute(`file?${query.toString()}`);
}

function openRunArtifact(runId, returnView = state.view) {
  const run = state.runs.find((item) => item.id === runId);
  if (hasOutputConflict(run)) { openOutputComparison(runId, returnView); return; }
  const output = availableRunOutput(run);
  if (!output || isMarkdownPath(output.path)) {
    openDocument(runId, returnView, output?.source || "canonical");
    return;
  }
  openRunOutputFile(run, output, returnView);
}

function hasOutputConflict(run) {
  return run?.retained_output?.reason === "output_conflict" && !run.canonical_commit_sha
    && ["failed", "stopped"].includes(run.status);
}

function openOutputComparison(runId, returnView = state.view) {
  const opener = document.activeElement;
  if (!state.compareReturnFocus?.includes(runId)) state.compareReturnFocus = `[data-decision-id="${runId}"]`;
  for (const attribute of ["data-output-compare", "data-activity-artifact", "data-run-detail-artifact", "data-artifact-run"]) {
    if (opener?.getAttribute(attribute) === runId) state.compareReturnFocus = `[${attribute}="${runId}"]`;
  }
  goToRoute(`compare/${encodeURIComponent(runId)}?return=${encodeURIComponent(returnView)}`);
}

let comparisonRendererPromise;
function loadComparisonRenderer() {
  if (window.TinOutputComparison) return Promise.resolve(window.TinOutputComparison);
  if (!comparisonRendererPromise) comparisonRendererPromise = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    const version = new URL(document.querySelector('script[src*="/assets/app.js"]').src).search;
    script.src = `/assets/output-comparison.js${version}`;
    script.onload = () => resolve(window.TinOutputComparison);
    script.onerror = () => { comparisonRendererPromise = null; script.remove(); reject(new Error("Diff renderer unavailable")); };
    document.head.appendChild(script);
  });
  return comparisonRendererPromise;
}

function renderOutputComparison() {
  const route = state.compareRoute;
  if (!route || !state.project) return;
  const context = currentProjectContext();
  const run = state.runs.find((item) => item.id === route.runId);
  const sessionKey = `${state.signedInUserId}:${context.projectId}:${route.runId}`;
  if (!state.compareSessions.has(sessionKey)) state.compareSessions.set(sessionKey, {});
  state.comparePage = window.TinOutputComparisonPage.mount(main, {
    ...route, projectId: context.projectId, actorId: state.signedInUserId,
    workflowTitle: run?.workflow_title, path: run?.retained_output?.artifact_path,
    returnLabel: route.returnView === "workflows" ? "system" : route.returnView,
    session: state.compareSessions.get(sessionKey), api, loadRenderer: loadComparisonRenderer,
    onReturn: (view = route.returnView) => {
      if (!isCurrentProjectContext(context)) return;
      state.decisionId = route.runId;
      navigate(view);
      window.setTimeout(() => main.querySelector(state.compareReturnFocus || "[data-decision-id]")?.focus(), 0);
    },
    onRead: async (side, comparison) => {
      try {
        if (side === "saved") {
          const output = availableRunOutput(run || await api(`/api/workflows/runs/${route.runId}`));
          if (!isCurrentProjectContext(context)) return;
          if (output?.source !== "retained") throw new Error("The saved result is unavailable.");
          const query = new URLSearchParams({ path: output.path, revision: output.revision,
            compareRun: route.runId, return: route.returnView, source: "retained" });
          goToRoute(`file?${query}`);
          return;
        }
        if (!comparison) comparison = await api(`/api/workflows/runs/${route.runId}/output-comparison`);
        if (!isCurrentProjectContext(context)) return;
        const snapshot = side === "current" ? comparison.current : comparison.saved;
        const query = new URLSearchParams({ path: comparison.path, revision: snapshot.revision,
          compareRun: route.runId, return: route.returnView, source: side === "saved" ? "retained" : "canonical" });
        goToRoute(`file?${query}`);
      } catch (error) { showToast(`Could not open this version: ${error.message}`); }
    },
    onOutcome: async () => {
      if (!isCurrentProjectContext(context)) return;
      // Do not wait for the capped run list or a status transition: a resolution is separate.
      try {
        const [decisions, summary, activity, updated] = await Promise.all([
          api(`/api/projects/${context.projectId}/decisions`), api(`/api/projects/${context.projectId}/system`),
          api(`/api/projects/${context.projectId}/activity?limit=100`), api(`/api/workflows/runs/${route.runId}`),
        ]);
        if (!isCurrentProjectContext(context)) return;
        state.decisions = decisions; state.systemSummary = summary; state.activity = activity;
        upsertRun(updated); updateRail();
      } catch { schedulePolling({ immediate: true }); }
    },
    onFiles: (path) => openCurrentComparisonFile(path, context),
    onActivity: () => { if (isCurrentProjectContext(context)) navigate("activity"); },
  });
}

async function openCurrentComparisonFile(path, context = currentProjectContext()) {
  try {
    const snapshot = await api(`/api/projects/${context.projectId}/files`);
    if (!isCurrentProjectContext(context)) return;
    if (path) openProjectFile(path, snapshot.revision);
    else navigate("files");
  } catch (error) { showToast(`Could not open Files: ${error.message}`); }
}

function returnFromComparisonFile(route) {
  if (route.compareRun && route.backToDecision) { state.decisionId = route.compareRun; navigate("decisions"); }
  else if (route.backTo) navigate(route.backTo);
  else if (route.compareRun) openOutputComparison(route.compareRun, route.compareReturn);
  else navigate("files");
}

function comparisonFileReturnLabel(route, reviewRun = null) {
  if (route.backTo) return route.backTo;
  if (route.compareRun) return "comparison";
  return reviewRun ? "workflows" : "files";
}

function comparisonFileLabel(route) {
  return route.compareRun ? route.source === "retained" ? "saved result" : "current file at comparison" : "canonical";
}

function render() {
  state.comparePage?.destroy();
  state.comparePage = null;
  disposeDocument();
  disposeFilesTree();
  const hasProject = state.projectAccess === "ready";
  projectSwitcher.disabled = !state.projects.length || state.projectAccess === "loading";
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.disabled = !hasProject;
    const active = item.classList.toggle(
      "is-active",
      item.dataset.view === state.view ||
        (state.view === "task" && item.dataset.view === "workflows") ||
        (state.view === "document" && state.documentRoute?.returnView === item.dataset.view) ||
        (state.view === "file" && item.dataset.view === "files"),
    );
    // On the mobile strip a deep link can land on a tab that sits past the edge; bring it into view.
    if (active && item.parentElement.scrollWidth > item.parentElement.clientWidth) item.scrollIntoView({ block: "nearest", inline: "nearest" });
  });
  updateRail();
  if (state.projectAccess === "locked") {
    if (state.view === "integrations" && connectRequest().length) renderIntegrations();
    else renderLockPage();
    return;
  }
  if (!hasProject) return;
  if (state.view === "chat") renderChat();
  if (state.view === "workflows" && !document.querySelector(".system-config-form")) renderWorkflows();
  if (state.view === "activity") renderActivity();
  if (state.view === "decisions") renderDecisions();
  if (state.view === "files") renderFiles();
  if (state.view === "integrations") renderIntegrations();
  if (state.view === "billing") window.TinBilling.mount(main, {
    api, projectId: state.project.id, projectName: state.project.name,
    workspaceName: state.project.workspace_name || "Workspace",
    onChange: (value) => { state.billing = value; renderProjectMenu(); },
  });
  if (state.view === "document") renderDocument();
  if (state.view === "task") renderTask();
  if (state.view === "file") renderFile();
  if (state.view === "compare") {
    document.querySelector('[data-view="decisions"]').classList.add("is-active");
    renderOutputComparison();
  }
}

function personalProjectName() {
  const name = String(state.signedInName || "").trim().slice(0, 60);
  return name ? `${name}’s project` : "My project";
}

function renderChat({ scrollToLatest = false } = {}) {
  const hasTurns = state.messages.length || state.sending;
  const turns = hasTurns
    ? `${state.messages.map(renderChatTurn).join("")}${state.sending ? renderTypingTurn() : ""}`
    : `<div class="empty-chat">
        <h1>What should we move forward?</h1>
      </div>`;

  let thread = document.querySelector("#chat-thread");
  let form = document.querySelector("#chat-form");
  let field = document.querySelector("#message");
  let sendButton = form?.querySelector(".send-button");
  const previousScrollTop = thread?.scrollTop || 0;
  const followLatest = scrollToLatest || !thread
    || thread.scrollHeight - thread.clientHeight - previousScrollTop <= 2;
  if (!thread || !form || !field || !sendButton) {
    main.innerHTML = `<section class="chat-view">
      <div class="chat-sheet">
        <div class="chat-thread" id="chat-thread" aria-live="polite" aria-busy="${state.sending}">${turns}</div>
        <form class="chat-composer" id="chat-form">
          <textarea class="composer-field" id="message" rows="1" maxlength="4000" placeholder="Message Tin…" aria-label="Message Tin">${escapeHtml(state.chatDraft)}</textarea>
          <button class="send-button" type="submit" aria-label="Send message" ${state.sending ? "disabled" : ""}>
            <svg aria-hidden="true" viewBox="0 0 18 18"><path d="M9 15V4M4.5 8L9 3.5 13.5 8" /></svg>
          </button>
        </form>
      </div>
    </section>`;
    thread = document.querySelector("#chat-thread");
    form = document.querySelector("#chat-form");
    field = document.querySelector("#message");
    sendButton = form.querySelector(".send-button");
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      sendMessage(field.value);
    });
    field.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        form.requestSubmit();
      }
    });
    field.addEventListener("input", () => {
      state.chatDraft = field.value;
      if (state.project) state.chatDrafts.set(state.project.id, field.value);
      resizeChatComposer(field);
    });
    resizeChatComposer(field);
  } else {
    thread.innerHTML = turns;
    thread.setAttribute("aria-busy", String(state.sending));
    sendButton.disabled = state.sending;
    if (field.value !== state.chatDraft) {
      field.value = state.chatDraft;
      resizeChatComposer(field);
    }
  }
  document.querySelectorAll("[data-open-run]").forEach((button) => {
    button.addEventListener("click", () => navigate("workflows"));
  });
  document.querySelectorAll("[data-open-task]").forEach((button) => {
    button.addEventListener("click", () => openTask(button.dataset.openTask));
  });
  document.querySelectorAll("[data-artifact-run]").forEach((button) => {
    button.addEventListener("click", () => openRunArtifact(button.dataset.artifactRun, "chat"));
  });
  // Polling and arriving replies must not interrupt someone reading earlier text.
  thread.scrollTo({
    top: followLatest ? thread.scrollHeight : previousScrollTop,
    behavior: "instant",
  });
}

function resizeChatComposer(field) {
  field.style.height = "auto";
  field.style.height = `${Math.min(field.scrollHeight, 140)}px`;
}

function renderChatTurn(turn) {
  if (turn.role === "user") {
    return `<article class="chat-turn is-user">
      <p class="turn-text">${escapeHtml(turn.message)}</p>
      <time class="turn-time">${escapeHtml(turn.time)}</time>
    </article>`;
  }
  const run = turn.run;
  let receipt = "";
  if (run) {
    const workflow = workflowForRun(run);
    let action = run.workflow_name === "project.task"
      ? `<button type="button" data-open-task="${escapeHtml(run.id)}">Open task →</button>`
      : `<button type="button" data-open-run="${escapeHtml(run.id)}">Watch →</button>`;
    if (run.retained_output && !run.canonical_commit_sha) {
      action = `<button type="button" data-artifact-run="${escapeHtml(run.id)}">${retainedOutputLabel(run)} →</button>`;
    } else if (availableRunOutput(run)) {
      action = `<button type="button" data-artifact-run="${escapeHtml(run.id)}">${run.status === "needs_input" && isMarkdownArtifact(run) ? "Review" : "Open"} →</button>`;
    }
    receipt = `<div class="run-receipt">
      <span class="status-dot is-${escapeHtml(run.status)}"></span>
      <code>${escapeHtml(shortRunId(run.id))} · ${escapeHtml(workflow?.key || run.workflow_name)} · ${escapeHtml(run.status)}</code>
      ${action}
    </div>`;
  }
  return `<article class="chat-turn is-agent">
    <div class="tin-avatar" aria-hidden="true">t</div>
    <p class="turn-text">${escapeHtml(turn.message)}</p>
    ${receipt}
  </article>`;
}

function renderTypingTurn() {
  return `<article class="chat-turn is-agent">
    <div class="tin-avatar" aria-hidden="true">t</div>
    <span class="turn-typing" role="status" aria-label="Tin is typing">
      <span aria-hidden="true"></span>
      <span aria-hidden="true"></span>
      <span aria-hidden="true"></span>
    </span>
  </article>`;
}

async function sendMessage(rawMessage) {
  const draft = String(rawMessage || "");
  const message = draft.trim();
  if (!message || state.sending || !state.project) return;
  const projectId = state.project.id;
  const generation = state.projectGeneration;
  const requestId = window.crypto.randomUUID();
  state.messages.push({ role: "user", message, time: clockTime(), requestId });
  state.chatDraft = "";
  state.chatDrafts.set(projectId, "");
  state.sending = true;
  renderChat({ scrollToLatest: true });
  try {
    const result = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({
        project_id: projectId,
        request_id: requestId,
        message,
      }),
    });
    if (generation !== state.projectGeneration || state.project?.id !== projectId) return;
    if (result.run) upsertRun(result.run);
    state.messages.push({
      role: "agent",
      message: result.message,
      run: result.run,
      responseId: result.response_id,
      requestId: result.request_id,
    });
    schedulePolling();
  } catch (error) {
    if (generation !== state.projectGeneration || state.project?.id !== projectId) return;
    if (!state.chatDraft) {
      state.chatDraft = draft;
      state.chatDrafts.set(projectId, draft);
    }
    state.messages.push({
      role: "agent",
      message: `I couldn't complete that request: ${error.message}`,
      run: null,
    });
  } finally {
    if (generation !== state.projectGeneration || state.project?.id !== projectId) return;
    state.sending = false;
    if (state.view === "chat") renderChat();
  }
}

function chatTurnFromMessage(item) {
  if (item.role === "user") {
    return {
      role: "user",
      message: item.content,
      time: clockTime(item.created_at),
      requestId: item.request_id,
    };
  }
  return {
    role: "agent",
    message: item.content,
    run: state.runs.find((run) => run.id === item.run_id) || null,
    responseId: item.response_id,
    requestId: item.request_id,
  };
}

function clearWorkflowSearch() {
  state.workflowSearch = "";
  const search = document.querySelector("#workflow-search");
  if (search) search.value = "";
  updateWorkflowSearchResults();
  search?.focus();
}

function bindWorkflowResultControls(root) {
  if (state.project) window.TinContentDraft?.mount(root, {api, projectId: state.project.id});
  if (state.project) window.TinContentDelivery?.bindPickers(root, {api, projectId: state.project.id});
  root.querySelectorAll("[data-stop-content-run]").forEach(button => button.onclick = async () => {
    button.disabled = true;
    try {
      await api(`/api/projects/${state.project.id}/content-programs/${button.dataset.programId}/runs/${button.dataset.stopContentRun}/stop`, {method: "POST"});
      schedulePolling({immediate: true}); showToast("Planning stopped. Pending revision holds can be discarded.");
    } catch (error) {button.disabled = false; showToast(error.message);}
  });
  if (state.project && window.TinContentPlan) window.TinContentPlan.mount(root, {
    api, projectId: state.project.id, toast: showToast,
    openFile: (path) => openCurrentComparisonFile(path), poll: () => schedulePolling({immediate: true}),
    openRun: (runId) => openRunArtifact(runId),
  });
  root.querySelectorAll("[data-template-view]").forEach((button) => {
    button.addEventListener("click", () => {
      state.templateView = button.dataset.templateView;
      state.workflowEditor = null;
      renderWorkflows();
    });
  });
  root.querySelectorAll("[data-save-template]").forEach((button) => {
    button.addEventListener("click", () => setTemplateSaved(button.dataset.saveTemplate, true, button));
  });
  root.querySelectorAll("[data-remove-saved-template]").forEach((button) => {
    button.addEventListener("click", () => setTemplateSaved(button.dataset.removeSavedTemplate, false, button));
  });
  root.querySelectorAll("[data-open-integrations]").forEach((button) => {
    button.addEventListener("click", () => navigate("integrations"));
  });
  root.querySelectorAll("[data-browse-registry]").forEach((button) => {
    button.addEventListener("click", () => {
      state.workflowSection = "registry";
      state.workflowFilter = "all";
      renderWorkflows();
    });
  });
  root.querySelectorAll("[data-clear-workflow-search]").forEach((button) => {
    button.addEventListener("click", clearWorkflowSearch);
  });
  root.querySelectorAll("[data-ask-luna-workflow]").forEach((button) => {
    button.addEventListener("click", () => {
      const workflowQuery = state.workflowSearch.trim();
      state.chatDraft = `Help me find or create a workflow for ${workflowQuery}.`;
      navigate("chat");
      window.requestAnimationFrame(() => {
        const field = document.querySelector("#message");
        field?.focus();
        field?.setSelectionRange(field.value.length, field.value.length);
      });
    });
  });
  root.querySelectorAll("[data-start-workflow]").forEach((button) => {
    button.addEventListener("click", () => startWorkflow(button.dataset.startWorkflow, button));
  });
  const openSystemWorkflowEditor = (target) => {
    state.expandedRun = null;
    state.workflowEditor = {
      workflowId: target.dataset.configureWorkflow,
      projectWorkflowId: target.dataset.projectWorkflowId || null,
      runId: target.dataset.editorRunId || null,
      field: null,
    };
    renderWorkflows();
    if (target.hasAttribute("data-run-intent")) {
      window.requestAnimationFrame(() => {
        document.querySelector(".workflow-config-form [required]")?.focus();
      });
    }
  };
  root.querySelectorAll("[data-configure-workflow]").forEach((button) => {
    button.addEventListener("click", () => {
      openSystemWorkflowEditor(button);
    });
  });
  root.querySelectorAll("[data-open-system-workflow]").forEach((row) => {
    row.addEventListener("click", (event) => {
      if (event.target.closest("button, a, input, select, textarea")) return;
      openSystemWorkflowEditor(row);
    });
  });
  root.querySelectorAll("[data-cancel-workflow-editor]").forEach((button) => {
    button.addEventListener("click", () => {
      state.workflowEditor = null;
      renderWorkflows();
    });
  });
  root.querySelectorAll("[data-close-system-workflow]").forEach((row) => {
    row.addEventListener("click", (event) => {
      if (event.target.closest("button, a, input, select, textarea")) return;
      state.workflowEditor = null;
      renderWorkflows();
    });
  });
  root.querySelectorAll("[data-edit-workflow-field]").forEach((button) => {
    button.addEventListener("click", () => {
      if (!state.workflowEditor) return;
      state.workflowEditor.field = button.dataset.editWorkflowField;
      renderWorkflows();
    });
  });
  root.querySelectorAll("[data-cancel-workflow-field]").forEach((button) => {
    button.addEventListener("click", () => {
      if (!state.workflowEditor) return;
      state.workflowEditor.field = null;
      renderWorkflows();
    });
  });
  root.querySelectorAll(".workflow-config-form:not(.workflow-config-ledger)").forEach((form) => {
    form.addEventListener("submit", saveProjectWorkflow);
    window.TinStyleCapture.bind(form, styleCaptureServices());
    bindTinControls(form);
    bindWorkflowFieldValidation(form);
    form.querySelector("[name=schedule_mode]")?.addEventListener("change", () => {
      form.classList.toggle("is-weekly", form.elements.schedule_mode.value === "weekly");
      form.classList.toggle("is-manual", form.elements.schedule_mode.value === "manual");
    });
  });
  root.querySelectorAll(".system-config-form").forEach((form) => {
    form.addEventListener("submit", saveSystemWorkflowSettings);
    bindTinControls(form);
    bindWorkflowFieldValidation(form);
    form.querySelector("[name=schedule_mode]")?.addEventListener("change", () => {
      form.classList.toggle("is-weekly", form.elements.schedule_mode.value === "weekly");
      form.classList.toggle("is-manual", form.elements.schedule_mode.value === "manual");
    });
  });
  root.querySelectorAll(".workflow-ledger-form").forEach((form) => {
    form.addEventListener("submit", saveProjectWorkflowField);
    bindTinControls(form);
    form.querySelector("[name=schedule_mode]")?.addEventListener("change", () => {
      form.classList.toggle("is-weekly", form.elements.schedule_mode.value === "weekly");
      form.classList.toggle("is-manual", form.elements.schedule_mode.value === "manual");
    });
    bindWorkflowFieldValidation(form);
  });
  root.querySelectorAll(".workflow-config-form, .system-config-form").forEach((form) => {
    const workflow = state.workflows.find((item) => item.id === form.dataset.workflowId);
    if (workflow?.definition?.executor !== "workflow.code") return;
    const configured = state.projectWorkflows.find((item) => item.id === form.dataset.projectWorkflowId);
    const context = currentProjectContext();
    window.TinCodeSetup.bind(form, {
      workflow, configured, projectId: context.projectId, fetch: authorizedFetch,
      isCurrent: () => isCurrentProjectContext(context),
      readInputs: () => readWorkflowInputs(form, configured?.input_schema || workflow.definition.input_schema),
    });
  });
  if (state.billing?.enabled && state.billing.run_billing_enabled !== false) {
    const context = currentProjectContext();
    root.querySelectorAll(".workflow-config-form, .system-config-form").forEach((form) => {
      const workflow = state.workflows.find((item) => item.id === form.dataset.workflowId);
      if (!workflow || workflow.definition?.executor === "workflow.code") return;
      window.TinBilling.bindEstimate(form, {
        projectId: context.projectId, fetch: authorizedFetch,
        isCurrent: () => isCurrentProjectContext(context),
        request() {
          // Saved cards preview their pinned saved configuration, matching Manual
          // run. Saving changed inputs updates the preview on the next render.
          if (form.dataset.projectWorkflowId) return {project_workflow_id: form.dataset.projectWorkflowId};
          return {workflow_id: workflow.id, inputs: readWorkflowInputs(form, workflow.definition?.input_schema || {})};
        },
      });
    });
  }
  root.querySelectorAll("[data-run-project-workflow]").forEach((button) => {
    button.addEventListener("click", () => runProjectWorkflow(button.dataset.runProjectWorkflow, button));
  });
  root.querySelectorAll("[data-retry-project-workflow]").forEach((button) => {
    button.addEventListener("click", () => retryProjectWorkflow(button.dataset.retryProjectWorkflow, button));
  });
  root.querySelectorAll("[data-toggle-project-workflow]").forEach((button) => {
    button.addEventListener("click", () => toggleProjectWorkflow(button.dataset.toggleProjectWorkflow, button.dataset.action, button));
  });
  root.querySelectorAll("[data-skip-project-workflow]").forEach((button) => {
    button.addEventListener("click", () => skipProjectWorkflow(button.dataset.skipProjectWorkflow, button));
  });
  root.querySelectorAll("[data-remove-project-workflow]").forEach((button) => {
    button.addEventListener("click", () => removeProjectWorkflow(button.dataset.removeProjectWorkflow, button));
  });
  root.querySelectorAll("[data-artifact-run]").forEach((button) => {
    button.addEventListener("click", () => {
      openRunArtifact(button.dataset.artifactRun, "workflows");
    });
  });
}

function updateWorkflowSearchResults() {
  const search = document.querySelector("#workflow-search");
  const workflowResults = document.querySelector("#workflow-results");
  if (!search || !workflowResults) return;

  const registry = registryWorkflows();
  const projection = workflowSearchProjection(registry);
  const searchField = search.closest(".search-field");
  searchField?.classList.toggle(
    "is-active",
    state.workflowSection === "registry" && Boolean(projection.query),
  );
  const headerClear = searchField?.querySelector("[data-clear-workflow-search]");
  if (headerClear) headerClear.hidden = !projection.query;

  if (state.workflowSection === "yours") {
    workflowResults.innerHTML = systemMySystemHtml();
    bindWorkflowResultControls(workflowResults);
    bindWorkflowRunControls(workflowResults);
    return;
  }
  if (state.workflowSection === "registry") {
    workflowResults.innerHTML = systemAddWorkflowsHtml(registry, projection.query);
    bindWorkflowResultControls(workflowResults);
    return;
  }
  updateWorkflowRunRegions();
  workflowResults.innerHTML = systemActivityHtml();
  bindWorkflowResultControls(workflowResults);
  bindActivityControls(workflowResults, "workflows");
}

function renderWorkflows() {
  const registry = registryWorkflows();
  const projection = workflowSearchProjection(registry);
  const query = projection.query;
  const runningCount = state.systemSummary?.running_count ?? state.runs.filter(
    (run) => run.workflow_name !== "project.task" && RUNNING_STATES.has(run.status),
  ).length;
  const sectionContent = state.workflowSection === "yours"
    ? `<div id="workflow-results" class="system-workflow-list">${systemMySystemHtml()}</div>`
    : state.workflowSection === "registry"
      ? `<div id="workflow-results" class="system-template-list">${systemAddWorkflowsHtml(registry, query)}</div>`
      : `<div id="workflow-results" class="system-activity">${systemActivityHtml()}</div>`;
  main.innerHTML = `<section class="product-view workspace-view system-view">
    <header class="workspace-header system-header">
      <h1>System</h1>
      <span class="header-spacer"></span>
      <div class="search-field is-registry ${query ? "is-active" : ""}" ${state.workflowSection === "activity" ? "hidden" : ""}>
        <svg aria-hidden="true" viewBox="0 0 13 13"><circle cx="5.5" cy="5.5" r="4" /><path d="M8.5 8.5L12 12" /></svg>
        <input id="workflow-search" type="search" aria-label="Search workflows" placeholder="Search…" value="${escapeHtml(state.workflowSearch)}" />
        <button class="search-clear" type="button" data-clear-workflow-search aria-label="Clear workflow search" ${query ? "" : "hidden"}>✕</button>
      </div>
    </header>
    <div class="workflow-sections system-sections" aria-label="System sections">
      <button class="workflow-section ${state.workflowSection === "yours" ? "is-active" : ""}" type="button" data-workflow-section="yours">My system ${runningCount ? `<i aria-hidden="true"></i><span>${runningCount}</span>` : ""}</button>
      <button class="workflow-section ${state.workflowSection === "registry" ? "is-active" : ""}" type="button" data-workflow-section="registry">Add workflows</button>
      <button class="workflow-section ${state.workflowSection === "activity" ? "is-active" : ""}" type="button" data-workflow-section="activity">Activity</button>
      <span class="system-pace">${escapeHtml(state.workflowSection === "activity" ? systemActivityPace() : systemPaceLine())}</span>
    </div>
    ${sectionContent}
  </section>`;
  document.querySelectorAll("[data-workflow-section]").forEach((button) => {
    button.addEventListener("click", () => {
      state.workflowSection = button.dataset.workflowSection;
      state.workflowFilter = "all";
      state.workflowEditor = null;
      state.expandedRun = null;
      renderWorkflows();
    });
  });
  document.querySelector("#workflow-search")?.addEventListener("input", (event) => {
    state.workflowSearch = event.target.value;
    updateWorkflowSearchResults();
  });
  bindWorkflowResultControls(document);
  bindWorkflowRunControls(main);
  if (state.workflowSection === "activity") bindActivityControls(main, "workflows");
}

function systemActivityPace() {
  const count = state.systemSummary?.activity_count_30_days ?? state.activity.length;
  return `live · ${count} ${count === 1 ? "event" : "events"} · 30 days kept`;
}

function systemActivityHtml() {
  const groups = groupActivityByDay(state.activity);
  if (!groups.length) {
    return '<div class="system-activity-empty"><strong>Nothing has happened yet.</strong><span>Runs, reviews, changes, and connections will appear here.</span></div>';
  }
  return `<div class="activity-ledger">${groups.map(activityGroup).join("")}</div>
    <p class="activity-boundary">${state.activityLoading ? "loading earlier activity…" : state.activityHasMore ? "scroll for earlier activity" : "30-day activity boundary"}</p>`;
}

function systemPaceLine() {
  const summary = state.systemSummary;
  const workflowCountValue = summary?.workflow_count ?? state.projectWorkflows.length;
  const runningCount = summary?.running_count ?? state.runs.filter((run) => RUNNING_STATES.has(run.status)).length;
  const monthStart = new Date();
  monthStart.setDate(1);
  monthStart.setHours(0, 0, 0, 0);
  const runsThisMonth = summary?.runs_this_month ?? state.runs.filter(
    (run) => run.workflow_name !== "project.task" && new Date(run.created_at) >= monthStart,
  ).length;
  const nextRunAt = summary?.next_run_at || state.projectWorkflows
    .map((item) => item.next_run_at)
    .filter(Boolean)
    .sort()[0];
  const next = nextRunAt ? `next ${systemDateTime(nextRunAt)}` : "nothing scheduled";
  const setUpAt = summary?.set_up_at ? new Date(summary.set_up_at) : null;
  const justSetUp = setUpAt && Date.now() - setUpAt.getTime() < 24 * 60 * 60 * 1000;
  const firstRun = justSetUp
    ? `Set up today: ${workflowCountValue} ${workflowCountValue === 1 ? "role" : "roles"}. First results ${nextRunAt ? systemDateTime(nextRunAt) : "within the hour"}. · `
    : "";
  return `${firstRun}${workflowCountValue} saved ${workflowCountValue === 1 ? "workflow" : "workflows"} · ${runningCount} running · ${runsThisMonth} runs this month · ${next}`;
}

function systemDateTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "later";
  const timeZone = state.systemSummary?.timezone || state.project?.timezone;
  try {
    const weekday = date.toLocaleDateString(undefined, { weekday: "short", timeZone }).toLowerCase();
    const time = date.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
      timeZone,
    });
    return `${weekday} ${time}`;
  } catch {
    return `${date.toLocaleDateString(undefined, { weekday: "short" }).toLowerCase()} ${ledgerTime(date)}`;
  }
}

function systemShortDate(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  try {
    return date.toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
      timeZone: state.systemSummary?.timezone || state.project?.timezone,
    }).toLowerCase();
  } catch {
    return date.toLocaleDateString(undefined, { month: "short", day: "numeric" }).toLowerCase();
  }
}

function systemScheduleLabel(configured) {
  const schedule = configured?.schedule;
  if (!schedule) return "manual";
  if (schedule.cadence === "daily") return `day · ${schedule.local_time}`;
  const days = (schedule.weekdays || []).map((day) => day.slice(0, 3)).join(", ");
  return `${days} · ${schedule.local_time}`;
}

function systemNextLabel(configured) {
  if (configured.content_revision) return configured.content_revision.preview_revision ? "review revision" : "revision held";
  if (configured.schedule?.end_at && new Date(configured.schedule.end_at) <= new Date()) return "program ended";
  if (configured.status === "paused") return "paused";
  if (!configured.schedule) return "—";
  return configured.next_run_at ? systemDateTime(configured.next_run_at) : "scheduling";
}

function systemLastLabel(configured) {
  if (!configured) return "No earlier run";
  const date = configured.last_finished_at ? systemShortDate(configured.last_finished_at) : "";
  if (configured.last_run_status === "failed") {
    const duration = systemRunDuration(configured.last_started_at, configured.last_finished_at);
    return `Failed${duration ? ` after ${duration}` : ""}${date ? `, ${date}` : ""}`;
  }
  const summary = configured.workflow_key === "content.plan"
    ? window.TinContentPlan.resultLabel(configured.last_result_summary)
    : configured.last_result_summary;
  if (summary) return `${summary}${date ? `, ${date}` : ""}`;
  const path = configured.last_artifact_path;
  if (path) return `${configured.last_artifact_title || path.split("/").at(-1)}${date ? `, ${date}` : ""}`;
  return "No earlier run";
}

function systemRunDuration(startedAt, finishedAt) {
  const started = Date.parse(startedAt || "");
  const finished = Date.parse(finishedAt || "");
  if (!Number.isFinite(started) || !Number.isFinite(finished) || finished < started) return "";
  const seconds = Math.round((finished - started) / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remaining = seconds % 60;
  if (minutes < 60) return `${minutes}m ${String(remaining).padStart(2, "0")}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${String(minutes % 60).padStart(2, "0")}m`;
}

function systemRunProgressLabel(run) {
  if (run.status === "pending") return "preparing";
  if (run.status === "needs_input") return "needs you";
  if (run.status === "succeeded") return "done";
  if (run.status === "failed") return "failed";
  if (run.status === "paused") return "paused";
  if (run.status === "stopped") return "stopped";
  const phase = {
    waiting: "waiting",
    sending: "sending",
    finishing: "finishing",
    review: "preparing",
  }[run.progress_step] || "running";
  if (Number.isInteger(run.progress_current) && Number.isInteger(run.progress_total)) {
    return `${phase} · ${run.progress_current} of ${run.progress_total}`;
  }
  if (Number.isInteger(run.progress_percent)) return `${phase} · ${run.progress_percent}%`;
  return phase;
}

function systemStartedBy(run) {
  if (run.trigger_source === "schedule") return "scheduled";
  if (run.trigger_source === "chat") return "from chat";
  if (run.trigger_client === "claude_code") return "from Claude Code";
  if (run.trigger_client === "codex") return "from Codex";
  if (["mcp", "api"].includes(run.trigger_source)) return "from the API";
  return "manual";
}

function systemRunningSentence(run, title) {
  const clause = run.progress_summary || `Working on ${title}.`;
  const startedAt = new Date(run.started_at || run.created_at);
  let started = ledgerTime(startedAt);
  try {
    started = startedAt.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
      timeZone: state.systemSummary?.timezone || state.project?.timezone,
    });
  } catch {
    // The Postgres projection is authoritative; local time is a safe display fallback.
  }
  const source = systemStartedBy(run);
  const startPhrase = ["from chat", "from Claude Code", "from Codex", "from the API"].includes(source)
    ? `Started ${source} at ${started}`
    : `Started ${started}, ${source}`;
  return `${clause.replace(/[.\s]+$/, "")}. ${startPhrase}.`;
}

function systemProgressBar(run) {
  const reportedPercent = Number.isInteger(run.progress_percent)
    ? run.progress_percent
    : Number.isInteger(run.progress_current) && Number.isInteger(run.progress_total)
      ? Math.round((run.progress_current / run.progress_total) * 100)
      : null;
  const determinate = reportedPercent !== null;
  const percent = determinate ? Math.max(0, Math.min(100, reportedPercent)) : null;
  return `<div class="system-progress ${determinate ? "is-determinate" : "is-indeterminate"}" aria-label="${determinate ? `${percent}% complete` : "In progress"}">
    <span ${determinate ? `style="width:${percent}%"` : ""}></span>
  </div>`;
}

function systemRunDetailTime(value, timeZone = null) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  try {
    const day = date.toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
      timeZone: timeZone || state.systemSummary?.timezone || state.project?.timezone,
    }).toLowerCase();
    const time = date.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
      timeZone: timeZone || state.systemSummary?.timezone || state.project?.timezone,
    });
    return `${day} ${time}`;
  } catch {
    return `${date.toLocaleDateString(undefined, { month: "short", day: "numeric" }).toLowerCase()} ${ledgerTime(date)}`;
  }
}

function campaignDeliveryState(delivery, campaign) {
  if (delivery.status === "sent") return `sent ${systemRunDetailTime(delivery.sent_at, campaign.send_timezone)}`;
  if (delivery.status === "started") return "sending";
  if (delivery.status === "skipped") return delivery.replied_at ? "skipped after reply" : "skipped";
  if (delivery.status === "unknown") return "needs checking";
  if (delivery.status === "failed") return "failed";
  if (delivery.scheduled_for) return `scheduled ${systemRunDetailTime(delivery.scheduled_for, campaign.send_timezone)}`;
  return "waiting";
}

function emailCampaignRunDetail(run, detail) {
  if (!detail || detail.loading) {
    return '<p class="system-run-loading">Loading campaign…</p>';
  }
  if (detail.error) {
    return `<p class="system-run-loading">Campaign details could not load. <button type="button" data-retry-run-detail="${escapeHtml(run.id)}">Try again</button></p>`;
  }
  const campaign = detail.campaign;
  const deliveries = detail.deliveries || [];
  if (!campaign) return '<p class="system-run-loading">Preparing campaign…</p>';
  const total = deliveries.length;
  const delivered = Number(campaign.delivered_count || 0);
  const unresolved = deliveries.filter((item) => ["pending", "started"].includes(item.status)).length;
  const problems = Number(campaign.unknown_delivery_count || 0) + Number(campaign.failed_delivery_count || 0);
  const next = campaign.next_delivery_at
    ? systemRunDetailTime(campaign.next_delivery_at, campaign.send_timezone)
    : unresolved ? "waiting" : "—";
  const start = String(campaign.send_window_start || "").slice(0, 5);
  const end = String(campaign.send_window_end || "").slice(0, 5);
  const deliveryRows = deliveries.map((delivery) => {
    const person = delivery.recipient_name || delivery.recipient_address;
    const address = delivery.recipient_name ? `<code>${escapeHtml(delivery.recipient_address)}</code>` : "";
    const touch = delivery.step === "follow_up" ? "Follow-up" : "Initial email";
    return `<div class="campaign-delivery-row is-${escapeHtml(delivery.status)}">
      <span><strong>${escapeHtml(person)}</strong>${address}</span>
      <span>${escapeHtml(touch)}</span>
      <code>${escapeHtml(campaignDeliveryState(delivery, campaign))}</code>
    </div>`;
  }).join("");
  return `<section class="campaign-run-detail">
    <div class="system-run-facts campaign-run-facts">
      <span><code>recipients</code><strong>${escapeHtml(campaign.recipient_count)} approved</strong></span>
      <span><code>delivery</code><strong>${escapeHtml(delivered)} sent of ${escapeHtml(total)} planned</strong></span>
      <span><code>next</code><strong>${escapeHtml(next)}</strong></span>
      <span><code>replies</code><strong>${escapeHtml(campaign.replied_count || 0)}</strong></span>
    </div>
    <p class="campaign-pace">${escapeHtml(`${start}–${end} ${campaign.send_timezone} · ${campaign.send_interval_seconds}s apart · up to ${campaign.daily_send_cap} a day${problems ? ` · ${problems} need checking` : ""}`)}</p>
    <div class="campaign-deliveries">
      <header><strong>Deliveries</strong><code>${escapeHtml(unresolved)} remaining</code></header>
      <div>${deliveryRows || '<p class="system-run-loading">No deliveries yet.</p>'}</div>
    </div>
    <footer class="system-run-detail-footer">
      <button type="button" data-campaign-plan="${escapeHtml(run.id)}">Review campaign →</button>
    </footer>
  </section>`;
}

function systemRunDetailHtml(run, includeClose = true) {
  const workflow = workflowForRun(run);
  const available = availableRunOutput(run);
  const output = available
    ? `<button type="button" data-run-detail-artifact="${escapeHtml(run.id)}">${available.source === "retained" ? retainedOutputLabel(run) : escapeHtml((available.source === "canonical" && run.artifact_title) || String(available.path).split("/").at(-1) || "Open result")} →</button>`
    : run.review_source_run_id
      ? `<button type="button" data-run-detail-artifact="${escapeHtml(run.id)}">${run.status === "failed" ? "Retry revision" : "Previous copy"} →</button>`
      : "—";
  const detail = state.runDetails.get(run.id);
  return `<div class="system-run-detail">
    <div class="system-run-facts">
      <span><code>started</code><strong>${escapeHtml(systemRunDetailTime(run.started_at || run.created_at))}</strong></span>
      <span><code>from</code><strong>${escapeHtml(humanize(systemStartedBy(run)))}</strong></span>
      <span><code>status</code><strong>${escapeHtml(systemRunProgressLabel(run))}</strong></span>
      <span><code>result</code><strong>${output}</strong></span>
    </div>
    ${run.error_message ? `<p class="system-run-error">${escapeHtml(run.error_message)}</p>` : ""}
    ${run.status === "failed" && run.progress_summary ? `<p class="system-run-error">Last update before it stopped: ${escapeHtml(run.progress_summary)}</p>` : ""}
    ${run.retained_output && (!run.error_message || run.retained_output.reason === "execution_interrupted") ? `<p class="system-run-error">${escapeHtml(retainedOutputMessage(run))}</p>` : ""}
    ${run.workflow_name === "outreach.email_campaign" ? emailCampaignRunDetail(run, detail) : ""}
    ${includeClose ? `<button class="system-run-close" type="button" data-observe-run="${escapeHtml(run.id)}" aria-label="Close ${escapeHtml(workflow?.title || "run")} details">Close</button>` : ""}
  </div>`;
}

function systemCardIndicator(kind) {
  if (kind === "running") return '<span class="system-card-dot is-running" aria-hidden="true"></span>';
  if (kind === "pending") return '<span class="system-card-dot is-pending" aria-hidden="true"></span>';
  if (kind === "failed") return '<span class="system-card-failed" aria-hidden="true">×</span>';
  return '<svg aria-hidden="true" viewBox="0 0 10 10"><path d="M2 3.5 5 6.5 8 3.5" /></svg>';
}

function systemRunCard(run, configured = null) {
  const workflow = workflowForRun(run);
  const title = configured?.name || workflow?.title || formatWorkflowKey(run.workflow_name);
  const key = configured?.workflow_key || workflow?.key || run.workflow_name;
  const schedule = configured ? systemScheduleLabel(configured) : "manual";
  const last = systemLastLabel(configured);
  if (
    configured
    && state.workflowEditor?.projectWorkflowId === configured.id
    && state.workflowEditor?.runId === run.id
  ) {
    return systemProjectWorkflowEditor(workflow, configured, run);
  }
  const configureAttributes = configured
    ? `data-configure-workflow="${escapeHtml(configured.workflow_id)}" data-project-workflow-id="${escapeHtml(configured.id)}" data-editor-run-id="${escapeHtml(run.id)}"`
    : "";
  const expanded = state.expandedRun?.runId === run.id && state.expandedRun?.eventId === null;
  return `<article class="system-workflow-card is-running ${expanded ? "is-open" : ""}">
    <div class="system-card-row ${configured ? "is-configurable" : ""}" ${configured ? `data-open-system-workflow ${configureAttributes}` : ""}>
      ${configured
        ? `<button class="system-card-mark is-toggle" type="button" ${configureAttributes} aria-label="Open ${escapeHtml(title)} settings">${systemCardIndicator(run.status)}</button>
           <button class="system-card-identity is-toggle" type="button" ${configureAttributes}><strong>${escapeHtml(title)}</strong><code>${escapeHtml(key)}</code></button>`
        : `<span class="system-card-mark">${systemCardIndicator(run.status)}</span>
           <span class="system-card-identity"><strong>${escapeHtml(title)}</strong><code>${escapeHtml(key)}</code></span>`}
      <code class="system-card-every">${escapeHtml(schedule)}</code>
      <code class="system-card-state">${escapeHtml(systemRunProgressLabel(run))}</code>
      <span class="system-card-last">${escapeHtml(last)}</span>
    </div>
    <div class="system-running-detail">
      <span>${escapeHtml(systemRunningSentence(run, title))}</span>
      <button type="button" data-observe-run="${escapeHtml(run.id)}">${expanded ? "Close" : "Observe →"}</button>
    </div>
    ${expanded ? systemRunDetailHtml(run, false) : ""}
    ${systemProgressBar(run)}
  </article>`;
}

function systemConfiguredCard(configured) {
  const workflow = workflowForProjectWorkflow(configured);
  if (state.workflowEditor?.projectWorkflowId === configured.id) {
    return systemProjectWorkflowEditor(workflow, configured);
  }
  const failed = configured.last_run_status === "failed";
  const scheduled = Boolean(configured.schedule);
  const actions = failed
    ? `<button class="system-action is-retry" type="button" data-retry-project-workflow="${escapeHtml(configured.id)}">Retry →</button>
       <button class="system-action is-strong" type="button" data-run-project-workflow="${escapeHtml(configured.id)}">Manual run</button>`
    : scheduled && configured.status === "paused"
      ? `<button class="system-action is-strong" type="button" data-toggle-project-workflow="${escapeHtml(configured.id)}" data-action="resume">Resume</button>
         <button class="system-action" type="button" data-run-project-workflow="${escapeHtml(configured.id)}">Manual run</button>`
      : scheduled
        ? `<button class="system-action is-strong" type="button" data-skip-project-workflow="${escapeHtml(configured.id)}">Skip once</button>
           <button class="system-action" type="button" data-toggle-project-workflow="${escapeHtml(configured.id)}" data-action="pause">Pause</button>`
        : `<button class="system-action is-strong" type="button" data-run-project-workflow="${escapeHtml(configured.id)}">Manual run</button>`;
  return `<article class="system-workflow-card ${failed ? "is-failed" : ""}">
    <div class="system-card-row is-configurable" data-open-system-workflow data-configure-workflow="${escapeHtml(configured.workflow_id)}" data-project-workflow-id="${escapeHtml(configured.id)}">
      <button class="system-card-mark is-toggle" type="button" data-configure-workflow="${escapeHtml(configured.workflow_id)}" data-project-workflow-id="${escapeHtml(configured.id)}" aria-label="Open ${escapeHtml(configured.name)} settings">${systemCardIndicator(failed ? "failed" : "idle")}</button>
      <button class="system-card-identity is-toggle" type="button" data-configure-workflow="${escapeHtml(configured.workflow_id)}" data-project-workflow-id="${escapeHtml(configured.id)}"><strong>${escapeHtml(configured.name)}</strong><code>${escapeHtml(configured.workflow_key)}</code></button>
      <code class="system-card-every">${escapeHtml(systemScheduleLabel(configured))}</code>
      <code class="system-card-state">${escapeHtml(systemNextLabel(configured))}</code>
      <span class="system-card-last">${escapeHtml(systemLastLabel(configured))}</span>
      <span class="system-card-actions">${actions}</span>
    </div>
  </article>`;
}

function systemGroupEntries(configuredItems, activeRuns) {
  const cards = [];
  const activeByConfiguration = new Map();
  for (const run of activeRuns) {
    const key = run.project_workflow_id || "";
    if (!activeByConfiguration.has(key)) activeByConfiguration.set(key, []);
    activeByConfiguration.get(key).push(run);
  }
  for (const configured of configuredItems) {
    const runs = activeByConfiguration.get(configured.id) || [];
    if (runs.length) cards.push(...runs.map((run) => systemRunCard(run, configured)));
    else cards.push(systemConfiguredCard(configured));
  }
  return cards;
}

function systemWorkflowMatches(configured, activeRuns, query) {
  if (!query) return true;
  const workflowText = `${configured.name} ${configured.workflow_key} ${configured.workflow_description}`.toLowerCase();
  if (workflowText.includes(query)) return true;
  return activeRuns.some((run) => run.project_workflow_id === configured.id && systemRunMatches(run, query));
}

function systemRunMatches(run, query) {
  if (!query) return true;
  const workflow = workflowForRun(run);
  return `${workflow?.title || ""} ${workflow?.key || run.workflow_name} ${shortRunId(run.id)} ${run.progress_summary || ""}`
    .toLowerCase()
    .includes(query);
}

function systemWorkflowGroup(title, cards) {
  if (!cards.length) return "";
  return `<section class="system-workflow-group">
    <header><strong>${escapeHtml(title)}</strong><code>${cards.length}</code></header>
    <div>${cards.join("")}</div>
  </section>`;
}

function systemMySystemHtml() {
  const activeRuns = state.runs.filter(
    (run) => RUNNING_STATES.has(run.status) && run.workflow_name !== "project.task",
  );
  const query = state.workflowSearch.trim().toLowerCase();
  const scheduled = state.projectWorkflows.filter(
    (item) => item.schedule && systemWorkflowMatches(item, activeRuns, query),
  );
  const available = state.projectWorkflows.filter(
    (item) => !item.schedule && systemWorkflowMatches(item, activeRuns, query),
  );
  const configuredIds = new Set(state.projectWorkflows.map((item) => item.id));
  const scheduledCards = systemGroupEntries(scheduled, activeRuns);
  const availableCards = systemGroupEntries(available, activeRuns);
  const runningCards = activeRuns
    .filter((run) =>
      (!run.project_workflow_id || !configuredIds.has(run.project_workflow_id)) &&
      systemRunMatches(run, query),
    )
    .map((run) => systemRunCard(run));
  const content = `${systemWorkflowGroup("Running", runningCards)}${systemWorkflowGroup("Scheduled", scheduledCards)}${systemWorkflowGroup("Available", availableCards)}`;
  const week = query ? "" : systemWeekAheadHtml();
  if (!content) {
    return query
      ? '<div class="empty-list workflow-empty"><strong>No workflows match this search.</strong><button class="button-quiet" type="button" data-clear-workflow-search>Clear search</button></div>'
      : '<div class="empty-list workflow-empty"><strong>Your system is ready for its first workflow.</strong><button class="button-secondary" type="button" data-browse-registry>Add a workflow</button></div>';
  }
  return `${week}${content}<button class="system-add-workflow" type="button" data-browse-registry>Add workflows →</button>`;
}

// The week ahead, as on Paper board SYS-V3: seven columns from today, what runs each day and
// where it lands, then the weekly rhythm in one sentence. Static; the cards below carry actions.
function systemWeekAheadHtml() {
  const timeZone = state.systemSummary?.timezone || state.project?.timezone || undefined;
  const scheduled = state.projectWorkflows.filter(
    (item) => item.schedule && item.status !== "paused" && item.status !== "archived",
  );
  const setUpAt = state.systemSummary?.set_up_at ? new Date(state.systemSummary.set_up_at) : null;
  const recentlySetUp = Boolean(setUpAt) && Date.now() - setUpAt.getTime() < 7 * 86400000;
  if (!scheduled.length && !recentlySetUp) return "";
  const format = (date, options) => {
    try {
      return date.toLocaleDateString("en-US", { ...options, timeZone });
    } catch (_error) {
      return date.toLocaleDateString("en-US", options);
    }
  };
  const clock = (date) => {
    try {
      return date.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone });
    } catch (_error) {
      return date.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false });
    }
  };
  const dayKey = (date) => format(date, { year: "numeric", month: "numeric", day: "numeric" });
  const now = new Date();
  const days = Array.from({ length: 7 }, (_, index) => new Date(now.getTime() + index * 86400000));
  const runsOn = (configured, date) => {
    const schedule = configured.schedule;
    if (schedule.end_at && new Date(schedule.end_at) <= date) return false;
    if (schedule.cadence === "daily") return true;
    const weekday = format(date, { weekday: "long" }).toLowerCase();
    return (schedule.weekdays || []).some((day) => String(day).toLowerCase() === weekday);
  };
  const lands = (configured) => {
    if (configured.last_run_status === "failed") return "last run failed; retry below";
    return String(configured.workflow_key || "").startsWith("content.") ? "in Decisions" : "in Files";
  };
  const columns = days.map((date, index) => {
    const label = `${format(date, { weekday: "short" }).toLowerCase()} ${format(date, { day: "numeric" })}`;
    const entries = [];
    if (index === 0 && setUpAt && dayKey(setUpAt) === dayKey(date)) {
      const firstRuns = state.runs.filter(
        (run) => run.workflow_name !== "project.task" && new Date(run.created_at) >= setUpAt,
      );
      const results = firstRuns.filter((run) => run.status === "succeeded").length;
      const running = firstRuns.filter((run) => RUNNING_STATES.has(run.status)).length;
      const waiting = firstRuns.filter((run) => run.status === "needs_input" || run.status === "failed").length;
      const parts = [];
      if (results) parts.push(`${results} ${results === 1 ? "result" : "results"}`);
      if (running) parts.push(`${running} running`);
      if (waiting) parts.push(`${waiting} waiting`);
      entries.push(
        `<span>${escapeHtml(clock(setUpAt))} · set up by your coding agent</span>` +
        `<small>${firstRuns.length} first ${firstRuns.length === 1 ? "run" : "runs"}${parts.length ? ` · ${escapeHtml(parts.join(", "))}` : ""}</small>`,
      );
    }
    const nowClock = clock(now);
    for (const configured of scheduled) {
      if (!runsOn(configured, date)) continue;
      // Today: what already ran is in the setup line or the cards below, not the week ahead.
      if (index === 0 && (entries.length || (configured.schedule.local_time || "09:00") <= nowClock)) continue;
      entries.push(
        `<span>${escapeHtml(configured.schedule.local_time || "09:00")} · ${escapeHtml(configured.name)}</span><small>${escapeHtml(lands(configured))}</small>`,
      );
    }
    const body = entries.length ? entries.join("") : '<span class="is-quiet">quiet</span>';
    return `<div class="system-week-day ${index === 0 ? "is-today" : ""}"><code>${escapeHtml(label)}${index === 0 ? " · today" : ""}</code>${body}</div>`;
  });
  const weekdayName = (day) => `${String(day).charAt(0).toUpperCase()}${String(day).slice(1)}s`;
  const WEEK = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"];
  const firstDay = (configured) => Math.min(...(configured.schedule.weekdays || []).map((day) => WEEK.indexOf(String(day).toLowerCase())), 7);
  const rhythm = [...scheduled].sort((a, b) => firstDay(a) - firstDay(b)).map((configured) => {
    const schedule = configured.schedule;
    if (schedule.cadence === "daily") return `${configured.name} every day`;
    return `${configured.name} ${(schedule.weekdays || []).map(weekdayName).join(" and ") || "weekly"}`;
  });
  const nextRunAt = scheduled.map((item) => item.next_run_at).filter(Boolean).sort()[0];
  const nextConfigured = scheduled.find((item) => item.next_run_at === nextRunAt);
  const footer = rhythm.length
    ? `Then every week: ${rhythm.join(", ")}.${nextConfigured ? ` Next: ${nextConfigured.name}, ${systemDateTime(nextRunAt)}.` : ""}`
    : "Nothing is on the calendar yet; the runs above were one-offs.";
  const city = timeZone ? String(timeZone).split("/").pop().replace(/_/g, " ") : "";
  const range = `${format(days[0], { month: "short" }).toLowerCase()} ${format(days[0], { day: "numeric" })} – ${format(days[6], { day: "numeric" })}`;
  return `<section class="system-week" aria-label="This week">
    <header><strong>This week</strong><code>${escapeHtml(range)}${city ? ` · ${escapeHtml(city)}` : ""}</code></header>
    <div class="system-week-days">${columns.join("")}</div>
    <p>${escapeHtml(footer)}</p>
  </section>`;
}

function systemProjectWorkflowEditor(workflow, configured, run = null) {
  if (workflow.key === "content.plan") return systemContentProgramEditor(workflow, configured, run);
  const schema = configured.input_schema || workflow.definition?.input_schema || {};
  const required = new Set(schema.required || []);
  const fields = workflow.key === "content.deliver" ? window.TinContentDelivery.fields(configured.inputs) : workflow.key === "content.generate" ? window.TinContentDraft.fields(configured.inputs, schema) : orderedWorkflowFields(schema).map(([name, definition]) => {
    const label = definition.title || humanize(name);
    const fieldId = `system-input-${String(name).replace(/[^a-z0-9_-]/gi, "-")}`;
    const value = configured.inputs[name] ?? definition.default ?? "";
    return `<label class="system-setting"><strong>${escapeHtml(label)}</strong>${workflowInputControl(`input:${name}`, definition, value, label, fieldId, required.has(name))}</label>`;
  }).join("");
  const mode = configured.schedule?.cadence || "manual";
  const isRunning = Boolean(run && RUNNING_STATES.has(run.status));
  const rowActions = !isRunning && configured.schedule
    ? `<span class="system-card-actions">
        ${configured.status === "paused"
          ? `<button class="system-action is-strong" type="button" data-toggle-project-workflow="${escapeHtml(configured.id)}" data-action="resume">Resume</button>`
          : `<button class="system-action is-strong" type="button" data-skip-project-workflow="${escapeHtml(configured.id)}">Skip once</button>
             <button class="system-action" type="button" data-toggle-project-workflow="${escapeHtml(configured.id)}" data-action="pause">Pause</button>`}
       </span>`
    : "";
  const runningDetail = isRunning
    ? `<div class="system-running-detail">
        <span>${escapeHtml(systemRunningSentence(run, configured.name))}</span>
        <button type="button" data-observe-run="${escapeHtml(run.id)}">${state.expandedRun?.runId === run.id && state.expandedRun?.eventId === null ? "Close" : "Observe →"}</button>
       </div>`
    : "";
  const runDetail = isRunning && state.expandedRun?.runId === run.id && state.expandedRun?.eventId === null
    ? systemRunDetailHtml(run, false)
    : "";
  return `<form class="system-workflow-card system-config-form ${isRunning ? "is-running" : ""} ${mode === "weekly" ? "is-weekly" : ""} ${mode === "manual" ? "is-manual" : ""}" data-workflow-id="${escapeHtml(workflow.id)}" data-project-workflow-id="${escapeHtml(configured.id)}">
    <div class="system-card-row is-configurable" data-close-system-workflow>
      <button class="system-card-mark is-toggle" type="button" data-cancel-workflow-editor aria-label="Close ${escapeHtml(configured.name)} settings">${systemCardIndicator("idle")}</button>
      <button class="system-card-identity is-toggle" type="button" data-cancel-workflow-editor><strong>${escapeHtml(configured.name)}</strong><code>${escapeHtml(configured.workflow_key)}</code></button>
      <code class="system-card-every">${escapeHtml(systemScheduleLabel(configured))}</code>
      <code class="system-card-state">${escapeHtml(isRunning ? systemRunProgressLabel(run) : systemNextLabel(configured))}</code>
      <span class="system-card-last">${escapeHtml(systemLastLabel(configured))}</span>
      ${rowActions}
    </div>
    ${runningDetail}
    ${runDetail}
    <div class="system-config-body">
      <section>
        <code class="system-config-kicker">what it works on</code>
        ${fields || '<span class="system-config-note">No additional choices.</span>'}
        <label class="system-setting"><strong>Name</strong><input class="workflow-inline-input" name="workflow_name" required maxlength="120" value="${escapeHtml(configured.name)}" /></label>
      </section>
      <section class="system-config-when">
        <code class="system-config-kicker">when</code>
        ${workflowScheduleControls(configured.id, configured.schedule, false, workflow.definition?.executor === "workflow.code" ? ["on_demand", "daily", "weekly"] : workflow.definition?.schedule_modes)}
        <p>${escapeHtml(systemConfigurationFact(configured))}</p>
      </section>
    </div>
    <footer class="system-config-footer">
      <button class="button" type="submit">Save changes</button>
      <button class="button-secondary" type="button" data-run-project-workflow="${escapeHtml(configured.id)}">Manual run</button>
      <span></span>
      ${configured.schedule ? `<button class="button-quiet" type="button" data-toggle-project-workflow="${escapeHtml(configured.id)}" data-action="${configured.status === "paused" ? "resume" : "pause"}">${configured.status === "paused" ? "Resume" : "Pause"}</button>` : ""}
      <button class="button-quiet is-muted" type="button" data-remove-project-workflow="${escapeHtml(configured.id)}">Remove</button>
    </footer>
    ${isRunning ? systemProgressBar(run) : ""}
  </form>`;
}

function systemContentProgramEditor(workflow, configured, run) {
  const isRunning = Boolean(run && RUNNING_STATES.has(run.status));
  const mode = configured.schedule?.cadence || "manual";
  return `<article class="system-workflow-card content-program-card ${isRunning ? "is-running" : ""}">
    <div class="system-card-row is-configurable" data-close-system-workflow>
      <button class="system-card-mark is-toggle" type="button" data-cancel-workflow-editor aria-label="Close ${escapeHtml(configured.name)} settings">${systemCardIndicator("idle")}</button>
      <button class="system-card-identity is-toggle" type="button" data-cancel-workflow-editor><strong>${escapeHtml(configured.name)}</strong><code>${escapeHtml(configured.workflow_key)}</code></button>
      <code class="system-card-every">${escapeHtml(systemScheduleLabel(configured))}</code>
      <code class="system-card-state">${escapeHtml(isRunning ? systemRunProgressLabel(run) : systemNextLabel(configured))}</code>
      <span class="system-card-last">${escapeHtml(systemLastLabel(configured))}</span>
    </div>
    ${isRunning ? `<div class="system-running-detail"><span>${escapeHtml(systemRunningSentence(run, configured.name))}</span><button type="button" data-observe-run="${escapeHtml(run.id)}">${state.expandedRun?.runId === run.id && state.expandedRun?.eventId === null ? "Close" : "Observe →"}</button></div>` : ""}
    ${isRunning && state.expandedRun?.runId === run.id && state.expandedRun?.eventId === null ? systemRunDetailHtml(run, false) : ""}
    <div class="system-config-body">
      <section class="content-program-work" aria-label="Upcoming content">
        <code class="system-config-kicker">what it works on</code>
        <div class="content-program-panel" data-content-program="${escapeHtml(configured.id)}" data-content-projection="${escapeHtml(JSON.stringify([configured.content_revision || null, configured.last_run_id, configured.last_run_status, configured.run_count, state.runs.filter(run => ["00000000-0000-4000-8000-000000000031", "00000000-0000-4000-8000-000000000036"].includes(run.workflow_id)).map(runFingerprint)]))}"></div>
      </section>
      <section class="system-config-when">
        <form class="system-config-form content-program-settings ${mode === "weekly" ? "is-weekly" : ""} ${mode === "manual" ? "is-manual" : ""}" data-workflow-id="${escapeHtml(workflow.id)}" data-project-workflow-id="${escapeHtml(configured.id)}">
          <code class="system-config-kicker">when</code>
          ${workflowScheduleControls(configured.id, configured.schedule, false, workflow.definition?.schedule_modes)}
          <p class="system-config-note">${escapeHtml(systemConfigurationFact(configured))}</p>
          <label class="system-setting"><strong>Name</strong><input class="workflow-inline-input" name="workflow_name" required maxlength="120" value="${escapeHtml(configured.name)}"></label>
          ${window.TinContentPlan.fields(configured.inputs, Boolean(configured.run_count))}
          <footer class="system-config-footer content-settings-footer"><button class="button-secondary" type="submit">Save settings</button></footer>
          <p class="system-config-note">Saves configuration only, not edits to upcoming content.</p>
        </form>
      </section>
    </div>
    <footer class="system-config-footer">
      <button class="button-secondary" type="button" data-run-project-workflow="${escapeHtml(configured.id)}" ${isRunning ? "disabled" : ""}>Manual run</button>
      <span class="system-config-note">Runs saved settings, not unsaved edits. No publishing.</span>
      ${isRunning ? `<button class="button-quiet" type="button" data-stop-content-run="${escapeHtml(run.id)}" data-program-id="${escapeHtml(configured.id)}">Stop planning</button>` : ""}
      ${configured.schedule ? `<button class="button-quiet" type="button" data-toggle-project-workflow="${escapeHtml(configured.id)}" data-action="${configured.status === "paused" ? "resume" : "pause"}">${configured.status === "paused" ? "Resume" : "Pause"}</button>` : ""}
      <button class="button-quiet is-muted" type="button" data-remove-project-workflow="${escapeHtml(configured.id)}">Remove</button>
    </footer>
    ${isRunning ? systemProgressBar(run) : ""}
  </article>`;
}

function systemConfigurationFact(configured) {
  const facts = [`${configured.run_count || 0} runs so far`, `${configured.done_count || 0} done`];
  if (configured.failed_count) facts.push(`${configured.failed_count} failed`);
  if (configured.typical_duration_seconds) facts.push(`about ${durationLabel(configured.typical_duration_seconds)} each`);
  const next = configured.schedule?.end_at && new Date(configured.schedule.end_at) <= new Date()
    ? "Program ended. No further batches will be prepared."
    : configured.next_run_at ? `Next run ${systemDateTime(configured.next_run_at)}.`
    : configured.schedule ? `${systemNextLabel(configured)}.` : "Runs on demand.";
  return `${next} ${facts.join(", ")}.`;
}

function durationLabel(seconds) {
  const value = Math.max(0, Number(seconds) || 0);
  if (value < 60) return `${Math.round(value)} seconds`;
  if (value < 3600) return `${Math.round(value / 60)} minutes`;
  return `${Math.round(value / 3600)} hours`;
}

function workflowRunRegions({ reviewQueue, activeTask, visibleLiveRuns }) {
  const visible = state.workflowSection === "yours";
  const liveRuns = visible && ["all", "running"].includes(state.workflowFilter) && visibleLiveRuns.length
    ? `<section class="live-workflow-runs" aria-label="Live workflow runs">
        <header><strong>Live runs</strong><code>${visibleLiveRuns.length} active</code></header>
        <div>${visibleLiveRuns.map(liveWorkflowRunCard).join("")}</div>
      </section>`
    : "";
  return `<div id="workflow-run-regions">
    ${visible ? reviewQueueBanner(reviewQueue) : ""}
    ${visible && activeTask ? projectTaskCard(activeTask) : ""}
    ${liveRuns}
  </div>`;
}

function bindWorkflowRunControls(root) {
  bindRunDetailControls(root);
  root.querySelectorAll("[data-review-run]").forEach((button) => {
    button.addEventListener("click", () => openReview(button.dataset.reviewRun));
  });
  root.querySelectorAll("[data-defer-review]").forEach((button) => {
    button.addEventListener("click", () => deferReview(button.dataset.deferReview));
  });
  root.querySelectorAll("[data-needs-you-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      state.workflowFilter = "needs_you";
      renderWorkflows();
    });
  });
  root.querySelectorAll("[data-open-task]").forEach((button) => {
    button.addEventListener("click", () => openTask(button.dataset.openTask));
  });
}

function renderRunDetailSurface() {
  if (state.view === "workflows") renderWorkflows();
  else if (state.view === "activity") renderActivity();
}

function bindRunDetailControls(root) {
  root.querySelectorAll("[data-observe-run]").forEach((button) => {
    button.addEventListener("click", () => {
      toggleRunDetails(
        button.dataset.observeRun,
        button.dataset.observeEvent || null,
      );
    });
  });
  root.querySelectorAll("[data-retry-run-detail]").forEach((button) => {
    button.addEventListener("click", () => loadRunDetails(button.dataset.retryRunDetail, true));
  });
  root.querySelectorAll("[data-run-detail-artifact]").forEach((button) => {
    button.addEventListener("click", () => openRunArtifact(button.dataset.runDetailArtifact, state.view));
  });
  root.querySelectorAll("[data-campaign-plan]").forEach((button) => {
    button.addEventListener("click", () => {
      const detail = state.runDetails.get(button.dataset.campaignPlan);
      const campaign = detail?.campaign;
      if (!campaign?.review_path || !campaign?.review_commit_sha) {
        showToast("The approved campaign is not ready to open.");
        return;
      }
      openProjectFile(campaign.review_path, campaign.review_commit_sha);
    });
  });
}

async function toggleRunDetails(runId, eventId = null) {
  const current = state.expandedRun;
  if (current?.runId === runId && current?.eventId === eventId) {
    state.expandedRun = null;
    renderRunDetailSurface();
    return;
  }
  state.workflowEditor = null;
  state.expandedRun = { runId, eventId };
  const run = state.runs.find((item) => item.id === runId);
  if (run?.workflow_name === "outreach.email_campaign" && !state.runDetails.has(runId)) {
    state.runDetails.set(runId, { loading: true, campaign: null, deliveries: [], error: null });
  }
  renderRunDetailSurface();
  if (run?.workflow_name === "outreach.email_campaign" && state.runDetails.get(runId)?.loading) {
    await loadRunDetails(runId);
  }
}

async function loadRunDetails(runId, force = false, renderWhenReady = true) {
  const run = state.runs.find((item) => item.id === runId);
  if (!run || run.workflow_name !== "outreach.email_campaign") return;
  const context = currentProjectContext();
  const existing = state.runDetails.get(runId);
  if (existing?.loading && !force && existing.campaign) return;
  state.runDetails.set(runId, {
    loading: true,
    campaign: existing?.campaign || null,
    deliveries: existing?.deliveries || [],
    error: null,
  });
  try {
    const [campaign, deliveries] = await Promise.all([
      api(`/api/outreach/campaigns/${encodeURIComponent(runId)}`),
      api(`/api/outreach/campaigns/${encodeURIComponent(runId)}/deliveries`),
    ]);
    if (!isCurrentProjectContext(context)) return;
    state.runDetails.set(runId, { loading: false, campaign, deliveries, error: null });
  } catch (error) {
    if (!isCurrentProjectContext(context)) return;
    state.runDetails.set(runId, {
      loading: false,
      campaign: existing?.campaign || null,
      deliveries: existing?.deliveries || [],
      error: error.status === 404 && RUNNING_STATES.has(run.status) ? null : error.message,
    });
  }
  if (renderWhenReady && state.expandedRun?.runId === runId) renderRunDetailSurface();
}

function updateWorkflowRunRegions() {
  const container = document.querySelector("#workflow-run-regions");
  if (!container) return;
  const reviewQueue = pendingReviewQueue();
  const queuedRunIds = new Set(reviewQueue.map((run) => run.id));
  const activeTask = state.runs.find(
    (run) => run.workflow_name === "project.task" && ACTIVE_TASK_STATES.has(run.status) && !queuedRunIds.has(run.id),
  );
  const query = state.workflowSearch.trim().toLowerCase();
  const visibleLiveRuns = activeUnsavedWorkflowRuns().filter((run) => {
    const workflow = workflowForRun(run);
    return `${workflow?.title || ""} ${workflow?.key || run.workflow_name} ${shortRunId(run.id)}`
      .toLowerCase()
      .includes(query);
  });
  const markup = workflowRunRegions({ reviewQueue, activeTask, visibleLiveRuns });
  const wrapper = document.createElement("div");
  wrapper.innerHTML = markup;
  container.replaceWith(wrapper.firstElementChild);
  bindWorkflowRunControls(document.querySelector("#workflow-run-regions"));
}

function projectTaskCard(run) {
  const label = run.task_phase === "review"
    ? "Changes ready for review"
    : run.task_phase === "applying"
      ? "Applying the approved changes to the latest project state"
      : run.task_phase === "needs_input"
        ? "Waiting for your answer"
        : run.status === "paused"
          ? "Paused at a saved checkpoint"
          : "Codex is working in an isolated project workspace";
  return `<article class="project-task-card is-${escapeHtml(run.status)}">
    <div>
      <span class="task-eyebrow"><span class="status-dot is-${escapeHtml(run.status)}"></span>one-off Codex task</span>
      <h2>${escapeHtml(run.task_title || "Project task")}</h2>
      <p>${escapeHtml(run.task_summary || label)}</p>
    </div>
    <code>${escapeHtml(shortRunId(run.id))} · ${escapeHtml(run.task_phase || run.status)}</code>
    <button class="button" type="button" data-open-task="${escapeHtml(run.id)}">Open task</button>
  </article>`;
}

function reviewQueueBanner(queue) {
  if (!queue.length) return "";
  const head = queue[0];
  const workflow = workflowForRun(head);
  const policy = isCampaignRevisionReview(head)
    ? {
        review_label: "Review revision",
        defer_label: "Not now",
        summary: "Your revised follow-up is ready. Remaining deliveries are paused until you approve or discard it.",
        queue_clause: "Follow-up revision ready to review",
      }
    : reviewPolicyFor(workflow);
  const isTask = head.workflow_name === "project.task";
  const primaryAction = isTask
    ? `<button class="needs-you-primary" type="button" data-open-task="${escapeHtml(head.id)}">Open task</button>`
    : `<button class="needs-you-primary" type="button" data-review-run="${escapeHtml(head.id)}">${escapeHtml(policy.review_label || "Review")}</button>`;
  const position = queue.length > 1 ? `1 of ${queue.length} · ` : "";
  const compact = queue.slice(1, 4).map(reviewQueueRow).join("");
  const remaining = Math.max(0, queue.length - 4);
  return `<section class="needs-you-queue" aria-label="Workflows needing your review">
    <div class="needs-you-head">
      <div class="needs-you-kicker">
        <span class="needs-you-dot" aria-hidden="true"></span>
        <code>needs you · ${escapeHtml(isTask ? head.task_title || "project task" : formatWorkflowKey(workflow?.key || head.workflow_name))} · ${escapeHtml(shortRunId(head.id))}</code>
        <span class="needs-you-spacer"></span>
        <time datetime="${escapeHtml(head.review_requested_at || "")}">${escapeHtml(position)}waiting ${escapeHtml(waitingLabel(head.review_requested_at))}</time>
      </div>
      <p>${escapeHtml(isTask ? (head.task_question || head.task_summary || "Your one-off task is waiting for you.") : (policy.summary || `${workflow?.title || "This workflow"} is ready for your review and will stay on hold until you act.`))}</p>
      <div class="needs-you-actions">
        ${primaryAction}
        <button class="needs-you-secondary" type="button" data-defer-review="${escapeHtml(head.id)}">${escapeHtml(policy.defer_label || "Not now")}</button>
      </div>
    </div>
    ${compact ? `<div class="needs-you-rows">${compact}</div>` : ""}
    ${remaining ? `<button class="needs-you-overflow" type="button" data-needs-you-filter>${escapeHtml(remaining)} more in the queue</button>` : ""}
  </section>`;
}

function reviewQueueRow(run) {
  const workflow = workflowForRun(run);
  const policy = isCampaignRevisionReview(run)
    ? { queue_clause: "Follow-up revision ready to review" }
    : reviewPolicyFor(workflow);
  return `<button class="needs-you-row" type="button" data-review-run="${escapeHtml(run.id)}">
    <span class="needs-you-row-marker" aria-hidden="true"></span>
    <code title="${escapeHtml(workflow?.key || run.workflow_name)}">${escapeHtml(formatWorkflowKey(workflow?.key || run.workflow_name))}</code>
    <span>${escapeHtml(policy.queue_clause || `${workflow?.title || "Workflow"} ready for review`)}</span>
    <time datetime="${escapeHtml(run.review_requested_at || "")}">waiting ${escapeHtml(waitingLabel(run.review_requested_at))}</time>
  </button>`;
}

function openReview(runId) {
  const run = state.runs.find((item) => item.id === runId);
  if (run?.workflow_name === "project.task") {
    openTask(runId);
    return;
  }
  if (!run?.artifact_path) {
    showToast("This review artifact is not ready yet.");
    return;
  }
  openRunArtifact(runId, "workflows");
}

function deferReview(runId) {
  state.deferredReviewRunIds = state.deferredReviewRunIds.filter((id) => id !== runId);
  state.deferredReviewRunIds.push(runId);
  renderWorkflows();
}

function renderTask() {
  const route = state.taskRoute;
  if (!route) {
    navigate("workflows");
    return;
  }
  if (!state.taskDetail || state.taskDetail.run.id !== route.runId) {
    main.innerHTML = '<div class="view-loading">Opening task…</div>';
    loadTask(route.runId);
    return;
  }
  const run = state.taskDetail.run;
  const terminal = ["succeeded", "failed", "stopped"].includes(run.status);
  const waitingForAnswer = run.status === "needs_input" && run.task_phase === "needs_input";
  const reviewing = run.status === "needs_input" && run.task_phase === "review";
  const applying = run.status === "running" && run.task_phase === "applying";
  const hasReviewFiles = Boolean(run.task_diff?.files?.length);
  const latestTaskEvent = [...state.taskDetail.entries]
    .reverse()
    .find((entry) => entry.kind === "event");
  const composerPlaceholder = waitingForAnswer
    ? "Answer Codex…"
    : reviewing
      ? "Request a change…"
      : run.status === "paused"
        ? "Add direction and resume…"
        : "Steer this task…";
  main.innerHTML = `<section class="task-view">
    <header class="task-header">
      <button class="task-back" type="button" data-task-back>← Workflows</button>
      <span class="task-header-rule"></span>
      <code>${escapeHtml(shortRunId(run.id))}</code>
      <span class="task-status is-${escapeHtml(run.status)}"><span class="status-dot is-${escapeHtml(run.status)}"></span>${escapeHtml(taskStateLabel(run))}</span>
    </header>
    <div class="task-layout">
      <div class="task-primary">
        <div class="task-title-row">
          <div>
            <span class="task-eyebrow">one-off Codex task</span>
            <h1>${escapeHtml(run.task_title || "Project task")}</h1>
            <p>${escapeHtml(run.task_summary || latestTaskEvent?.content || "Codex is preparing the isolated project workspace.")}</p>
          </div>
          <div class="task-controls">${taskControls(run)}</div>
        </div>
        ${hasReviewFiles ? taskReviewView(run) : ""}
        <div class="task-transcript" aria-live="polite">
          ${state.taskDetail.entries.map(taskEntryView).join("")}
          ${run.status === "running" && !applying ? '<div class="task-working"><span></span><span></span><span></span><em>Codex is working</em></div>' : ""}
        </div>
        ${terminal || applying ? "" : `<form class="task-composer" id="task-form">
          <textarea class="composer-field" id="task-message" rows="1" maxlength="8000" placeholder="${escapeHtml(composerPlaceholder)}" aria-label="${escapeHtml(composerPlaceholder)}"></textarea>
          <button class="send-button" type="submit" aria-label="Send to Codex">
            <svg aria-hidden="true" viewBox="0 0 18 18"><path d="M9 15V4M4.5 8L9 3.5 13.5 8" /></svg>
          </button>
        </form>`}
      </div>
      <aside class="task-meta">
        <section>
          <h2>Task state</h2>
          <dl>
            <div><dt>Status</dt><dd>${escapeHtml(taskStateLabel(run))}</dd></div>
            <div><dt>Started</dt><dd>${escapeHtml(timeLabel(run.started_at || run.created_at))}</dd></div>
            <div><dt>Turns finished</dt><dd>${escapeHtml(run.task_turn_number)}</dd></div>
            <div><dt>Last activity</dt><dd>${escapeHtml(timeLabel(latestTaskEvent?.created_at || run.started_at || run.created_at))}</dd></div>
            <div><dt>Changes</dt><dd>${run.task_has_changes ? "isolated" : "none yet"}</dd></div>
          </dl>
        </section>
        <section>
          <h2>Isolation</h2>
          <p>Codex works in a temporary project workspace. File changes stay isolated until you approve the exact diff.</p>
        </section>
      </aside>
    </div>
  </section>`;

  document.querySelector("[data-task-back]").addEventListener("click", () => navigate("workflows"));
  document.querySelectorAll("[data-task-action]").forEach((button) => {
    button.addEventListener("click", () => controlTask(run.id, button.dataset.taskAction, button));
  });
  document.querySelectorAll("[data-task-document]").forEach((button) => {
    button.addEventListener("click", () => openTaskDocument(run.id, button.dataset.taskDocument));
  });
  const form = document.querySelector("#task-form");
  const field = document.querySelector("#task-message");
  if (form && field) {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      sendTaskMessage(run.id, field.value);
    });
    field.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        form.requestSubmit();
      }
    });
    field.addEventListener("input", () => {
      field.style.height = "auto";
      field.style.height = `${Math.min(field.scrollHeight, 160)}px`;
    });
  }
}

function taskStateLabel(run) {
  if (run.status === "needs_input" && run.task_phase === "review") return "review changes";
  if (run.status === "running" && run.task_phase === "applying") return "applying changes";
  if (run.status === "needs_input") return "needs you";
  if (run.status === "succeeded") return "finished";
  return humanize(run.status);
}

function taskControls(run) {
  if (run.status === "running" && run.task_phase === "applying") {
    return '<span class="task-action-status">Applying approved changes…</span>';
  }
  if (run.status === "running" || run.status === "pending") {
    return `<button class="button-secondary" type="button" data-task-action="pause">Pause</button>
      <button class="button-quiet" type="button" data-task-action="stop">Stop</button>`;
  }
  if (run.status === "paused") {
    return `<button class="button" type="button" data-task-action="resume">Resume</button>
      <button class="button-quiet" type="button" data-task-action="stop">Stop</button>`;
  }
  if (run.status === "needs_input" && run.task_phase === "review") {
    return `<button class="button" type="button" data-task-action="approve">Approve changes</button>
      <button class="button-quiet" type="button" data-task-action="stop">Stop</button>`;
  }
  if (run.status === "needs_input") {
    return '<button class="button-quiet" type="button" data-task-action="stop">Stop</button>';
  }
  return "";
}

function taskEntryView(entry) {
  if (entry.kind === "event") {
    return `<div class="task-event"><span aria-hidden="true"></span><p>${escapeHtml(entry.content)}</p><time>${escapeHtml(clockTime(entry.created_at))}</time></div>`;
  }
  const founder = entry.source === "founder";
  const label = founder ? "You" : entry.source === "codex" ? "Codex" : "Tin";
  return `<article class="task-entry is-${escapeHtml(entry.source)}">
    <div class="task-entry-meta"><strong>${escapeHtml(label)}</strong><time>${escapeHtml(clockTime(entry.created_at))}</time></div>
    <p>${escapeHtml(entry.content)}</p>
  </article>`;
}

function taskReviewView(run) {
  const diff = run.task_diff;
  if (!diff?.files?.length) return "";
  const stats = diff.stats || {};
  const reviewState = run.status === "succeeded" && run.review_decision === "approved"
    ? { eyebrow: "changes applied", title: "Files added to the project" }
    : run.status === "running" && run.task_phase === "applying"
      ? { eyebrow: "approval accepted", title: "Applying the reviewed files" }
      : ["failed", "stopped"].includes(run.status)
        ? { eyebrow: "changes preserved", title: "Review the isolated files" }
        : { eyebrow: "changes ready", title: "Review the proposed files" };
  const reviewable = diff.files.filter((file) => {
    const path = String(file.path || "").toLowerCase();
    return file.state !== "deleted" && (path.endsWith(".md") || path.endsWith(".markdown"));
  });
  return `<section class="task-diff">
    <header><div><span class="task-eyebrow">${escapeHtml(reviewState.eyebrow)}</span><h2>${escapeHtml(reviewState.title)}</h2></div>
      <code>${escapeHtml(stats.files || diff.files.length)} files · +${escapeHtml(stats.additions || 0)} −${escapeHtml(stats.deletions || 0)}</code></header>
    ${reviewable.length ? `<div class="task-review-files">${reviewable.map((file) => `<div>
      <span><strong>${escapeHtml(file.path.split("/").at(-1) || file.path)}</strong><code>${escapeHtml(file.path)}</code></span>
      <button class="button-secondary" type="button" data-task-document="${escapeHtml(file.path)}">Open document</button>
    </div>`).join("")}</div>` : ""}
    <details class="task-exact-diff">
      <summary><span>View exact diff</span><code>${escapeHtml(diff.files.length)} file${diff.files.length === 1 ? "" : "s"}</code></summary>
      <div class="task-diff-files">${diff.files.map((file) => `<section>
        <header><span>${escapeHtml(file.path)}</span><code>${escapeHtml(file.state)}</code></header>
        <pre>${escapeHtml(file.patch || "Binary or empty change")}</pre>
      </section>`).join("")}</div>
    </details>
  </section>`;
}

async function loadTask(runId) {
  if (state.taskLoadingRunId === runId) return;
  const context = currentProjectContext();
  state.taskLoadingRunId = runId;
  try {
    const taskDetail = await api(`/api/tasks/${encodeURIComponent(runId)}`);
    if (!isCurrentProjectContext(context)) return;
    state.taskDetail = taskDetail;
    upsertRun(taskDetail.run);
    if (state.taskRoute?.runId === runId) render();
  } catch (error) {
    if (!isCurrentProjectContext(context)) return;
    if (state.taskRoute?.runId === runId) {
      main.innerHTML = `<div class="view-error"><div><strong>Tin could not open this task.</strong>${escapeHtml(error.message)}</div></div>`;
    }
  } finally {
    if (isCurrentProjectContext(context) && state.taskLoadingRunId === runId) state.taskLoadingRunId = null;
  }
}

async function sendTaskMessage(runId, rawMessage) {
  const message = String(rawMessage || "").trim();
  if (!message) return;
  const context = currentProjectContext();
  try {
    const taskDetail = await api(`/api/tasks/${encodeURIComponent(runId)}/messages`, {
      method: "POST",
      body: JSON.stringify({ request_id: window.crypto.randomUUID(), message }),
    });
    if (!isCurrentProjectContext(context)) return;
    state.taskDetail = taskDetail;
    upsertRun(taskDetail.run);
    render();
    schedulePolling();
  } catch (error) {
    if (!isCurrentProjectContext(context)) return;
    showToast(`Could not send task direction: ${error.message}`);
  }
}

async function controlTask(runId, action, button) {
  const context = currentProjectContext();
  button.disabled = true;
  if (action === "approve") button.textContent = "Applying";
  try {
    const run = await api(`/api/tasks/${encodeURIComponent(runId)}/${encodeURIComponent(action)}`, {
      method: "POST",
    });
    if (!isCurrentProjectContext(context)) return;
    upsertRun(run);
    if (state.taskDetail?.run.id === run.id) state.taskDetail.run = run;
    render();
    if (action === "approve") {
      showToast("Approval accepted. Applying the exact reviewed changes.");
    }
    schedulePolling();
  } catch (error) {
    if (!isCurrentProjectContext(context)) return;
    button.disabled = false;
    showToast(`Task ${action} failed: ${error.message}`);
  }
}

function renderDocument() {
  const route = state.documentRoute;
  if (!route) {
    navigate("workflows");
    return;
  }
  const reviewedRun = state.runs.find(item => item.id === route.runId);
  const previousReviewCopy = reviewedRun?.review_source_run_id && !reviewedRun.artifact_path;
  const cacheKey = route.taskPath ? `task:${route.runId}:${route.taskPath}` : `run:${route.runId}:${route.source || "canonical"}${previousReviewCopy ? ":previous" : ""}`;
  const cached = state.documentCache.get(cacheKey);
  if (cached) {
    const run = state.runs.find((item) => item.id === route.runId);
    state.documentCleanup = window.TinMarkdownViewer.mount(main, cached, {
      mode: "in-app",
      contextLabel: route.source === "retained" && run?.retained_output?.reason === "execution_interrupted"
        ? `Partial result · ${cached.filename}` : undefined,
      returnTo: {
        label: route.returnView,
        onActivate: () => route.taskPath ? openTask(route.runId) : navigate(route.returnView),
      },
      secondaryAction: route.source !== "retained" && isCampaignRevisionReview(run)
        ? {
            label: "Discard revision",
            onActivate: (button) => discardCampaignRevision(run.id, button),
          }
        : route.source !== "retained" && run?.status === "needs_input" && repositoryDeliveryAvailable(run) && !supportsArticleFeedback(run)
        ? {
            // Answer pages have no feedback controls, so the reader bar carries the PR option itself.
            label: "Open a pull request",
            onActivate: (button) => approveRun(run.id, button, { delivery: "github_pr" }),
          }
        : null,
      primaryAction:
        route.taskPath && run?.status === "needs_input" && run?.task_phase === "review"
          ? {
              label: "Approve changes",
              onActivate: (button) => controlTask(run.id, "approve", button),
            }
          : route.source !== "retained" && run?.status === "needs_input"
          ? {
              label: repositoryDeliveryAvailable(run) ? "Publish now" : run?.content_delivery?.approval_label || (workflowForRun(run)?.definition?.procedure?.output?.apply_on_approval ? "Use documents" : isCampaignRevisionReview(run) ? "Approve revision" : "Approve draft"),
              onActivate: (button) => approveRun(run.id, button, repositoryDeliveryAvailable(run) ? { delivery: "github_commit" } : {}),
            }
          : null,
    });
    if (route.source !== "retained" && supportsArticleFeedback(run)) {
      const cleanReader = state.documentCleanup;
      const cleanReview = mountArticleFeedback(main, run.id, true);
      state.documentCleanup = () => {cleanReview(); cleanReader();};
    }
    return;
  }

  main.innerHTML = '<div class="viewer-loading">Loading document…</div>';
  if (state.documentLoadingRunId === cacheKey) return;
  state.documentLoadingRunId = cacheKey;
  const context = currentProjectContext();
  const endpoint = route.taskPath
    ? `/api/tasks/${encodeURIComponent(route.runId)}/review/document?path=${encodeURIComponent(route.taskPath)}`
    : previousReviewCopy ? `/api/workflows/runs/${encodeURIComponent(route.runId)}/review/document`
    : `/api/workflows/runs/${encodeURIComponent(route.runId)}/artifact/document` + (route.source === "retained" ? "?source=retained" : "");
  api(endpoint)
    .then((documentData) => {
      if (!isCurrentProjectContext(context)) return;
      state.documentCache.set(cacheKey, documentData);
      if (state.documentRoute?.runId === route.runId && state.documentRoute?.taskPath === route.taskPath && state.documentRoute?.source === route.source) render();
    })
    .catch((error) => {
      if (!isCurrentProjectContext(context)) return;
      if (state.documentRoute?.runId !== route.runId || state.documentRoute?.taskPath !== route.taskPath || state.documentRoute?.source !== route.source) return;
      main.innerHTML = `<div class="viewer-error"><strong>Tin could not open this document.</strong><span>${escapeHtml(error.message)}</span></div>`;
    })
    .finally(() => {
      if (isCurrentProjectContext(context) && state.documentLoadingRunId === cacheKey) state.documentLoadingRunId = null;
    });
}

function disposeDocument() {
  if (!state.documentCleanup) return;
  state.documentCleanup();
  state.documentCleanup = null;
}

function runTriggerLabel(run) {
  const labels = {
    mcp: "via MCP",
    schedule: "by schedule",
    manual: "via dashboard",
  };
  return labels[run?.trigger_source] || "via Tin";
}

function liveWorkflowRunCard(run) {
  const workflow = workflowForRun(run);
  const title = workflow?.title || formatWorkflowKey(workflow?.key || run.workflow_name);
  const key = workflow?.key || run.workflow_name;
  const started = run.started_at || run.created_at;
  const stateLabel = run.status === "pending" ? "preparing" : "running";
  return `<article class="workflow-card live-workflow-run">
    <span class="status-dot is-${escapeHtml(run.status)}"></span>
    <div class="workflow-identity">
      <strong>${escapeHtml(title)}</strong>
      <code>${escapeHtml(key)} · ${escapeHtml(shortRunId(run.id))}</code>
    </div>
    <span class="workflow-version">started ${escapeHtml(waitingLabel(started))}</span>
    <span class="workflow-state workflow-description-slot">${escapeHtml(stateLabel)} · ${escapeHtml(runTriggerLabel(run))}</span>
    <button class="button-secondary" type="button" data-observe-run="${escapeHtml(run.id)}">Observe →</button>
  </article>`;
}

function registryWorkflowCard(workflow, query = "") {
  const canRunWithDefaults = workflowCanRunWithDefaults(workflow);
  const configureLabel = canRunWithDefaults ? "Configure" : "Set up";
  const runAction = canRunWithDefaults
    ? `<button class="button" type="button" data-start-workflow="${escapeHtml(workflow.id)}">Run now</button>`
    : "";
  const action = `<div class="workflow-actions">
      <button class="button-secondary workflow-configure" type="button" data-configure-workflow="${escapeHtml(workflow.id)}" ${canRunWithDefaults ? "" : "data-run-intent"}>${configureLabel}</button>
      ${runAction}
    </div>`;
  return `<article class="workflow-card registry-workflow-card">
    <span class="status-dot is-active"></span>
    <div class="workflow-identity">
      <strong>${workflowSearchMatch(workflow.title, query, "workflow-search-title-match")}</strong>
      <code>${workflowSearchMatch(workflow.key, query, "workflow-search-id-match")} · v${escapeHtml(workflow.version_label)}</code>
    </div>
    <span class="workflow-state workflow-description-slot">${escapeHtml(workflow.description)}</span>
    ${action}
  </article>`;
}

function latestArtifactLabel(workflow) {
  if (workflow?.key === "content.diagram") return "Latest diagram";
  if (["content.answer_page", "content.public_article"].includes(workflow?.key)) {
    return "Latest draft";
  }
  if (["research.deep_dive", "scan.report", "visibility.audit", "project.weekly_brief"].includes(workflow?.key)) {
    return "Latest report";
  }
  return "Latest output";
}

function workflowCanRunWithDefaults(workflow) {
  const schema = workflow.definition?.input_schema || {};
  const properties = schema.properties || {};
  return (schema.required || []).every(
    (name) => name === "project_id" || Object.hasOwn(properties[name] || {}, "default"),
  );
}

function workflowForProjectWorkflow(configured) {
  return state.workflows.find((workflow) => workflow.id === configured.workflow_id) || {
    id: configured.workflow_id,
    key: configured.workflow_key,
    title: configured.workflow_title,
    description: configured.workflow_description,
    version_label: configured.version_label,
    definition: { input_schema: configured.input_schema },
  };
}

function projectWorkflowCard(configured) {
  const run = state.runs.find((item) => item.id === configured.last_run_id);
  const runStatus = run?.status || configured.last_run_status || configured.status;
  const isRunning = RUNNING_STATES.has(runStatus);
  const schedule = configured.schedule ? scheduleLabel(configured.schedule, configured.next_run_at) : "manual";
  const open = configured.last_run_id && configured.last_artifact_path
    ? `<button class="button-quiet" type="button" data-artifact-run="${escapeHtml(configured.last_run_id)}">${escapeHtml(latestArtifactLabel({ key: configured.workflow_key }))}</button>`
    : "";
  const pause = configured.schedule && ["active", "paused"].includes(configured.status)
    ? `<button class="button-quiet" type="button" data-toggle-project-workflow="${escapeHtml(configured.id)}" data-action="${configured.status === "paused" ? "resume" : "pause"}">${configured.status === "paused" ? "Resume" : "Pause"}</button>`
    : "";
  const runState = configured.last_error || (
    runStatus === "needs_input"
      ? "needs you"
      : isRunning && run
        ? `${runStatus} · ${runTriggerLabel(run)}`
        : runStatus
  );
  return `<article class="workflow-card project-workflow-card">
    <span class="status-dot is-${escapeHtml(runStatus)}"></span>
    <div class="workflow-identity">
      <strong>${escapeHtml(configured.name)}</strong>
      <code>${escapeHtml(configured.workflow_key)} · v${escapeHtml(configured.version_label)}</code>
    </div>
    <span class="workflow-version">${escapeHtml(schedule)}</span>
    <span class="workflow-state workflow-description-slot">${escapeHtml(runState)}</span>
    <div class="workflow-actions">
      ${open}${pause}
      <button class="button-secondary workflow-configure" type="button" data-configure-workflow="${escapeHtml(configured.workflow_id)}" data-project-workflow-id="${escapeHtml(configured.id)}">Configure</button>
      <button class="button" type="button" data-run-project-workflow="${escapeHtml(configured.id)}" ${isRunning ? "disabled" : ""}>${isRunning ? "Running" : "Run now"}</button>
    </div>
  </article>`;
}

function scheduleLabel(schedule, nextRunAt) {
  if (!schedule) return "manual";
  const time = schedule.local_time || "";
  const base = schedule.cadence === "weekly"
    ? `${(schedule.weekdays || []).map((day) => day.slice(0, 3)).join(", ")} · ${time}`
    : `daily · ${time}`;
  if (!nextRunAt) return base;
  return `${base} · next ${timeLabel(nextRunAt)}`;
}

function workflowEditor(workflow, configured = null) {
  const editor = state.workflowEditor;
  if (!workflow || !editor || editor.workflowId !== workflow.id) return "";
  if ((editor.projectWorkflowId || null) !== (configured?.id || null)) return "";
  return configured
    ? projectWorkflowLedger(workflow, configured, editor.field)
    : workflowDraftForm(workflow);
}

function orderedWorkflowFields(schema) {
  return Object.entries(schema?.properties || {})
    .filter(([name]) => name !== "project_id")
    .sort(([leftName, left], [rightName, right]) => {
      const fallbackOrder = (field) => {
        if (Array.isArray(field.enum)) return 100;
        if (field.type === "boolean") return 200;
        if (["integer", "number"].includes(field.type)) return 300;
        if (field.type === "string") return 400;
        return 500;
      };
      const leftOrder = left["x-tin-ui"]?.order ?? fallbackOrder(left);
      const rightOrder = right["x-tin-ui"]?.order ?? fallbackOrder(right);
      return leftOrder - rightOrder || leftName.localeCompare(rightName);
    });
}

function workflowDraftForm(workflow) {
  const schema = workflow.definition?.input_schema || {};
  const required = new Set(schema.required || []);
  const fields = workflow.key === "content.deliver" ? window.TinContentDelivery.fields() : workflow.key === "content.generate" ? window.TinContentDraft.fields({}, schema) : workflow.key === "style.capture" ? window.TinStyleCapture.fields() : workflow.key === "content.plan" ? window.TinContentPlan.fields() : orderedWorkflowFields(schema)
    .map(([name, definition]) => workflowInputField(
      name,
      definition,
      undefined,
      required.has(name),
    ))
    .join("");
  return `<form class="workflow-config-form is-manual" data-workflow-id="${escapeHtml(workflow.id)}">
    <div class="workflow-config-heading">
      <span class="status-dot is-ready"></span>
      <div><strong>${escapeHtml(workflow.title)}</strong><code>${escapeHtml(workflow.key)} · v${escapeHtml(workflow.version_label)}</code></div>
      <button class="workflow-collapse" type="button" data-cancel-workflow-editor>collapse ↑</button>
    </div>
    <div class="workflow-config-rows">
      <div class="workflow-config-row">
        <label for="workflow-name-${escapeHtml(workflow.id)}">Name</label>
        <div class="workflow-row-control"><input class="workflow-inline-input" id="workflow-name-${escapeHtml(workflow.id)}" name="workflow_name" required maxlength="120" value="${escapeHtml(workflow.title)}" /></div>
      </div>
      ${fields || '<p class="workflow-no-inputs">This workflow has no additional inputs.</p>'}
      ${workflowHowItRuns(workflow)}
      <div class="workflow-config-divider"><span>Schedule</span></div>
      ${workflowScheduleControls(workflow.id, null, false, workflow.definition?.schedule_modes)}
    </div>
    <div class="workflow-config-actions">
      <code>Run now uses these inputs once · saving pins v${escapeHtml(workflow.version_label)}</code>
      <button class="button-quiet" type="button" data-cancel-workflow-editor>Cancel</button>
      <button class="button-secondary" type="submit" data-save-workflow>Save workflow</button>
      <button class="button" type="submit" ${workflow.key === "content.plan" ? "data-save-and-run" : "data-run-workflow-draft"}>${workflow.key === "content.plan" ? "Save and run" : "Run now"}</button>
    </div>
  </form>`;
}

function loadDiagramRenderer() {
  return window.TinDiagramLoader.load();
}

function workflowHowItRuns(workflow) {
  const flow = workflow.definition?.presentation?.flow;
  if (!flow) return "";
  const counts = flow.nodes.reduce((value, node) => {
    value[node.kind] = (value[node.kind] || 0) + 1;
    return value;
  }, {});
  const facts = [
    `${flow.nodes.length} ${flow.nodes.length === 1 ? "step" : "steps"}`,
    counts.gate ? `${counts.gate} gate` : null,
    counts.wait ? `${counts.wait} wait` : null,
  ].filter(Boolean).join(" · ");
  return `<section class="workflow-how-it-runs" aria-label="How it runs">
    <header><strong>How it runs</strong><code>derived from the pinned definition · ${escapeHtml(facts)}</code></header>
    <div class="tin-diagram workflow-diagram" data-workflow-diagram="${escapeHtml(workflow.id)}"><span>Drawing workflow…</span></div>
  </section>`;
}

async function hydrateWorkflowDiagrams(root) {
  const targets = [...root.querySelectorAll("[data-workflow-diagram]")];
  if (!targets.length) return;
  try {
    const renderer = await loadDiagramRenderer();
    for (const target of targets) {
      if (!target.isConnected) continue;
      const workflow = state.workflows.find((item) => item.id === target.dataset.workflowDiagram);
      const flow = workflow?.definition?.presentation?.flow;
      if (!flow) continue;
      const rendered = await renderer.renderFlow(flow);
      if (target.isConnected) target.innerHTML = rendered.svg;
    }
  } catch (_error) {
    for (const target of targets) {
      if (target.isConnected) target.innerHTML = "<span>Diagram unavailable.</span>";
    }
  }
}

function projectWorkflowLedger(workflow, configured, editingField) {
  const schema = configured?.input_schema || workflow.definition?.input_schema || {};
  const fields = orderedWorkflowFields(schema)
    .map(([name, definition]) => workflowLedgerInputRow(
      configured,
      name,
      definition,
      editingField === `input:${name}`,
    ))
    .join("");
  return `<article class="workflow-config-form workflow-config-ledger" data-workflow-id="${escapeHtml(workflow.id)}" data-project-workflow-id="${escapeHtml(configured.id)}">
    <div class="workflow-config-heading">
      <span class="status-dot is-${escapeHtml(configured.status)}"></span>
      <div><strong>${escapeHtml(configured.name)}</strong><code>${escapeHtml(workflow.key)} · v${escapeHtml(configured.version_label)}</code></div>
      <button class="workflow-collapse" type="button" data-cancel-workflow-editor>collapse ↑</button>
    </div>
    <div class="workflow-config-rows">
      ${workflowLedgerNameRow(configured, editingField === "name")}
      ${fields || '<p class="workflow-no-inputs">This workflow has no additional inputs.</p>'}
      <div class="workflow-config-divider"><span>Schedule</span></div>
      ${workflowLedgerSchedule(configured, editingField === "schedule")}
    </div>
    <div class="workflow-ledger-footer">
      <code>settings updated ${escapeHtml(timeLabel(configured.updated_at))} · edits logged</code>
      <span>${escapeHtml(workflowSettingsSummary(configured))}</span>
    </div>
  </article>`;
}

function workflowLedgerNameRow(configured, editing) {
  if (!editing) return workflowLedgerRestingRow("Name", configured.name, "name");
  return `<form class="workflow-ledger-form" data-project-workflow-id="${escapeHtml(configured.id)}" data-workflow-field="name">
    <div class="workflow-config-row workflow-ledger-row is-editing">
      <label for="workflow-ledger-name">Name</label>
      <div class="workflow-row-control"><input class="workflow-inline-input" id="workflow-ledger-name" name="value" required data-max-length="120" value="${escapeHtml(configured.name)}" /><small class="workflow-field-message" aria-live="polite">Shown in Your workflows.</small></div>
      ${workflowLedgerActions()}
    </div>
  </form>`;
}

function workflowLedgerInputRow(configured, name, definition, editing) {
  const label = definition.title || humanize(name);
  const value = configured.inputs[name] ?? definition.default ?? "";
  if (!editing) return workflowLedgerRestingRow(label, workflowDisplayValue(definition, value), `input:${name}`);
  const fieldId = `workflow-ledger-${String(name).replace(/[^a-z0-9_-]/gi, "-")}`;
  return `<form class="workflow-ledger-form" data-project-workflow-id="${escapeHtml(configured.id)}" data-workflow-field="input:${escapeHtml(name)}">
    <div class="workflow-config-row workflow-ledger-row is-editing">
      <span class="workflow-row-label">${escapeHtml(label)}</span>
      <div class="workflow-row-control">${workflowInputControl("value", definition, value, label, fieldId)}${workflowFieldHelp(definition, fieldId)}</div>
      ${workflowLedgerActions()}
    </div>
  </form>`;
}

function workflowLedgerRestingRow(label, value, field, nested = false) {
  return `<div class="workflow-config-row workflow-ledger-row ${nested ? "is-nested" : ""}">
    <span class="workflow-row-label">${escapeHtml(label)}</span>
    <code class="workflow-ledger-value">${escapeHtml(value)}</code>
    <button class="workflow-row-edit" type="button" data-edit-workflow-field="${escapeHtml(field)}">edit</button>
  </div>`;
}

function workflowLedgerActions() {
  return `<div class="workflow-ledger-actions"><button class="button-quiet" type="button" data-cancel-workflow-field>Cancel</button><button class="button" type="submit">Save</button></div>`;
}

function workflowLedgerSchedule(configured, editing) {
  const schedule = configured.schedule;
  const mode = schedule?.cadence || "manual";
  if (!editing) {
    const rows = [workflowLedgerRestingRow("Runs", humanize(mode === "manual" ? "on demand" : mode), "schedule")];
    if (mode === "weekly") {
      rows.push(workflowLedgerRestingRow("Day", (schedule.weekdays || []).map(humanize).join(", "), "schedule", true));
    }
    if (mode !== "manual") {
      rows.push(workflowLedgerRestingRow("Time", `${schedule.local_time} · ${schedule.timezone}`, "schedule", true));
    }
    return rows.join("");
  }
  const workflow = state.workflows.find((item) => item.id === configured.workflow_id);
  return `<form class="workflow-ledger-form workflow-ledger-schedule-form ${mode === "weekly" ? "is-weekly" : ""} ${mode === "manual" ? "is-manual" : ""}" data-project-workflow-id="${escapeHtml(configured.id)}" data-workflow-field="schedule">
    ${workflowScheduleControls(configured.workflow_id, schedule, true, workflow?.definition?.schedule_modes)}
  </form>`;
}

function workflowScheduleControls(id, schedule, ledger = false, scheduleModes = null) {
  const mode = schedule?.cadence || "manual";
  const timezone = schedule?.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const weekday = schedule?.weekdays?.[0] || "tuesday";
  const localTime = schedule?.local_time || "09:00";
  const actions = ledger ? workflowLedgerActions() : "";
  const allowed = new Set(scheduleModes || ["on_demand", "daily", "weekly"]);
  const modeOptions = [
    ["manual", "On demand", "on_demand"],
    ["daily", "Daily", "daily"],
    ["weekly", "Weekly", "weekly"],
  ].filter((item) => allowed.has(item[2])).map((item) => item.slice(0, 2));
  return `<div class="workflow-config-row ${ledger ? "workflow-ledger-row is-editing" : ""}">
      <span class="workflow-row-label">Runs</span>
      <div class="workflow-row-control">${tinSegmentedControl("schedule_mode", mode, modeOptions, "Runs")}</div>
      ${actions}
    </div>
    <div class="workflow-config-row schedule-weekday ${ledger ? "workflow-ledger-row is-nested" : ""}">
      <span class="workflow-row-label">Day</span>
      <div class="workflow-row-control">${tinSelectControl("schedule_weekday", weekday, ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"].map((day) => [day, humanize(day)]), "Day")}</div>
    </div>
    <div class="workflow-config-row schedule-timed ${ledger ? "workflow-ledger-row is-nested" : ""}">
      <label for="schedule-time-${escapeHtml(id)}">Time</label>
      <div class="workflow-row-control"><input class="workflow-inline-input is-short" id="schedule-time-${escapeHtml(id)}" name="schedule_time" inputmode="numeric" pattern="(?:[01]\\d|2[0-3]):[0-5]\\d" value="${escapeHtml(localTime)}" aria-describedby="schedule-time-help-${escapeHtml(id)}" /><small id="schedule-time-help-${escapeHtml(id)}">24-hour local time</small></div>
    </div>
    <div class="workflow-config-row schedule-timed ${ledger ? "workflow-ledger-row is-nested" : ""}">
      <label for="schedule-timezone-${escapeHtml(id)}">Timezone</label>
      <div class="workflow-row-control"><input class="workflow-inline-input" id="schedule-timezone-${escapeHtml(id)}" name="schedule_timezone" value="${escapeHtml(timezone)}" /></div>
    </div>`;
}

function workflowSettingsSummary(configured) {
  const values = orderedWorkflowFields(configured.input_schema)
    .slice(0, 2)
    .map(([name, definition]) => workflowDisplayValue(definition, configured.inputs[name]))
    .filter((value) => value && value !== "not set");
  values.push(configured.schedule ? scheduleLabel(configured.schedule, null) : "on demand");
  return values.join(" · ");
}

function workflowDisplayValue(definition, value) {
  if (definition.type === "boolean") return value ? "yes" : "no";
  if (definition.type === "array") return Array.isArray(value) && value.length ? value.join(", ") : "not set";
  if (value === undefined || value === null || value === "") return "not set";
  return Array.isArray(definition.enum) ? humanize(value) : String(value);
}

function workflowInputField(name, definition, currentValue, required = false) {
  const label = definition.title || humanize(name);
  const fieldId = `workflow-input-${String(name).replace(/[^a-z0-9_-]/gi, "-")}`;
  const value = currentValue ?? definition.default ?? "";
  return `<div class="workflow-config-row"><span class="workflow-row-label">${escapeHtml(label)}</span><div class="workflow-row-control">${workflowInputControl(`input:${name}`, definition, value, label, fieldId, required)}${workflowFieldHelp(definition, fieldId)}</div></div>`;
}

function workflowFieldHelp(definition, fieldId) {
  const helpId = `${fieldId}-help`;
  const description = definition.description || "Applies from the next run.";
  return `<small class="workflow-field-message" id="${helpId}" aria-live="polite">${escapeHtml(description)}</small>`;
}

function workflowInputControl(name, definition, value, label, fieldId, required = false) {
  const requestedControl = definition["x-tin-ui"]?.control;
  const textConstraints = `${required ? " required" : ""}${definition.minLength !== undefined ? ` minlength="${escapeHtml(definition.minLength)}"` : ""}${definition.maxLength !== undefined ? ` maxlength="${escapeHtml(definition.maxLength)}"` : ""}`;
  if (Array.isArray(definition.enum)) {
    const options = definition.enum.map((option) => [option, humanize(option)]);
    return requestedControl === "segmented"
      ? tinSegmentedControl(name, value, options, label)
      : tinSelectControl(name, value, options, label);
  }
  if (definition.type === "boolean") {
    return tinSegmentedControl(name, String(value), [["true", "Yes"], ["false", "No"]], label);
  }
  if (["integer", "number"].includes(definition.type) && requestedControl === "counter") {
    return tinCounterControl(name, value, definition, label);
  }
  if (definition.type === "array") {
    const list = Array.isArray(value) ? value.join(", ") : "";
    return `<input class="workflow-inline-input" id="${fieldId}" name="${escapeHtml(name)}" data-input-type="array" value="${escapeHtml(list)}" aria-describedby="${fieldId}-help" />`;
  }
  if (definition.type === "string" && requestedControl === "textarea") {
    return `<textarea class="workflow-inline-input workflow-inline-textarea" id="${fieldId}" name="${escapeHtml(name)}" data-max-length="${escapeHtml(definition.maxLength || "")}" aria-describedby="${fieldId}-help"${textConstraints}>${escapeHtml(value)}</textarea>`;
  }
  const type = ["integer", "number"].includes(definition.type) ? "number" : "text";
  const step = definition.type === "integer" ? "1" : "any";
  return `<input class="workflow-inline-input ${type === "number" ? "is-short" : ""}" id="${fieldId}" name="${escapeHtml(name)}" data-input-type="${escapeHtml(definition.type || "string")}" data-max-length="${escapeHtml(definition.maxLength || "")}" type="${type}" step="${step}" value="${escapeHtml(value)}" aria-describedby="${fieldId}-help"${definition.type === "string" ? textConstraints : ""} />`;
}

function tinCounterControl(name, value, definition, label) {
  const min = definition.minimum ?? "";
  const max = definition.maximum ?? "";
  const step = definition.type === "integer" ? 1 : (definition.multipleOf || 1);
  return `<div class="tin-counter" data-tin-counter>
    <button type="button" data-tin-counter-step="-${escapeHtml(step)}" aria-label="Decrease ${escapeHtml(label)}">−</button>
    <input name="${escapeHtml(name)}" type="number" value="${escapeHtml(value)}" step="${escapeHtml(step)}" ${min !== "" ? `min="${escapeHtml(min)}"` : ""} ${max !== "" ? `max="${escapeHtml(max)}"` : ""} aria-label="${escapeHtml(label)}" />
    <button type="button" data-tin-counter-step="${escapeHtml(step)}" aria-label="Increase ${escapeHtml(label)}">+</button>
  </div>`;
}

function tinSegmentedControl(name, value, options, label) {
  return `<div class="tin-segmented" role="group" aria-label="${escapeHtml(label)}">\
    <input type="hidden" name="${escapeHtml(name)}" value="${escapeHtml(value)}" />\
    ${options.map(([optionValue, label]) => `<button class="tin-segment ${String(value) === String(optionValue) ? "is-active" : ""}" type="button" data-tin-segment="${escapeHtml(optionValue)}" aria-pressed="${String(value) === String(optionValue)}">${escapeHtml(label)}</button>`).join("")}\
  </div>`;
}

function tinSelectControl(name, value, options, label, inputAttributes = {}) {
  const selected = options.find(([optionValue]) => String(optionValue) === String(value)) || options[0];
  const attributes = Object.entries(inputAttributes).map(([key, item]) => `${escapeHtml(key)}="${escapeHtml(item)}"`).join(" ");
  return `<div class="tin-select" data-tin-select>\
    <input type="hidden" name="${escapeHtml(name)}" value="${escapeHtml(selected?.[0] ?? "")}" ${attributes} />\
    <button class="tin-select-trigger" type="button" data-tin-select-trigger aria-label="${escapeHtml(label)}" aria-haspopup="listbox" aria-expanded="false">\
      <span data-tin-select-label>${escapeHtml(selected?.[1] ?? "Select")}</span>\
      <svg aria-hidden="true" viewBox="0 0 10 6"><path d="M1 1l4 4 4-4" /></svg>\
    </button>\
    <div class="tin-select-menu" role="listbox" aria-label="${escapeHtml(label)}" hidden>\
      ${options.map(([optionValue, label]) => `<button class="tin-select-option ${String(selected?.[0]) === String(optionValue) ? "is-selected" : ""}" type="button" role="option" aria-selected="${String(selected?.[0]) === String(optionValue)}" data-tin-select-value="${escapeHtml(optionValue)}"><span>${escapeHtml(label)}</span><span class="tin-select-marker" aria-hidden="true"></span></button>`).join("")}\
    </div>\
  </div>`;
}

function closeTinSelect(dropdown, returnFocus = false) {
  const trigger = dropdown.querySelector("[data-tin-select-trigger]");
  dropdown.classList.remove("is-open");
  trigger.setAttribute("aria-expanded", "false");
  dropdown.querySelector(".tin-select-menu").hidden = true;
  if (returnFocus) trigger.focus();
}

function openTinSelect(dropdown, focusOption = false) {
  document.querySelectorAll(".tin-select.is-open").forEach((other) => {
    if (other !== dropdown) closeTinSelect(other);
  });
  const trigger = dropdown.querySelector("[data-tin-select-trigger]");
  const menu = dropdown.querySelector(".tin-select-menu");
  dropdown.classList.add("is-open");
  trigger.setAttribute("aria-expanded", "true");
  menu.hidden = false;
  if (focusOption) (menu.querySelector(".is-selected") || menu.querySelector(".tin-select-option"))?.focus();
}

function bindTinControls(form) {
  form.querySelectorAll(".tin-segmented").forEach((control) => {
    if (control.dataset.tinBound) return;
    control.dataset.tinBound = "true";
    const input = control.querySelector("input[type=hidden]");
    control.querySelectorAll("[data-tin-segment]").forEach((button) => {
      button.addEventListener("click", () => {
        input.value = button.dataset.tinSegment;
        control.querySelectorAll("[data-tin-segment]").forEach((option) => {
          const active = option === button;
          option.classList.toggle("is-active", active);
          option.setAttribute("aria-pressed", String(active));
        });
        input.dispatchEvent(new Event("change", { bubbles: true }));
      });
    });
  });
  form.querySelectorAll("[data-tin-select]").forEach((dropdown) => {
    if (dropdown.dataset.tinBound) return;
    dropdown.dataset.tinBound = "true";
    const trigger = dropdown.querySelector("[data-tin-select-trigger]");
    const input = dropdown.querySelector("input[type=hidden]");
    const options = [...dropdown.querySelectorAll(".tin-select-option")];
    trigger.addEventListener("click", () => {
      if (dropdown.classList.contains("is-open")) closeTinSelect(dropdown);
      else openTinSelect(dropdown);
    });
    trigger.addEventListener("keydown", (event) => {
      if (["ArrowDown", "ArrowUp"].includes(event.key)) {
        event.preventDefault();
        openTinSelect(dropdown, true);
      }
    });
    options.forEach((option, index) => {
      option.addEventListener("click", () => {
        input.value = option.dataset.tinSelectValue;
        trigger.querySelector("[data-tin-select-label]").textContent = option.querySelector("span").textContent;
        options.forEach((candidate) => {
          const selected = candidate === option;
          candidate.classList.toggle("is-selected", selected);
          candidate.setAttribute("aria-selected", String(selected));
        });
        input.dispatchEvent(new Event("change", { bubbles: true }));
        closeTinSelect(dropdown, true);
      });
      option.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          closeTinSelect(dropdown, true);
        } else if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
          event.preventDefault();
          const nextIndex = event.key === "Home"
            ? 0
            : event.key === "End"
              ? options.length - 1
              : (index + (event.key === "ArrowDown" ? 1 : -1) + options.length) % options.length;
          options[nextIndex].focus();
        }
      });
    });
    dropdown.addEventListener("focusout", () => {
      window.setTimeout(() => {
        if (!dropdown.contains(document.activeElement)) closeTinSelect(dropdown);
      }, 0);
    });
  });
  form.querySelectorAll("[data-tin-counter]").forEach((counter) => {
    if (counter.dataset.tinBound) return;
    counter.dataset.tinBound = "true";
    const input = counter.querySelector("input[type=number]");
    counter.querySelectorAll("[data-tin-counter-step]").forEach((button) => {
      button.addEventListener("click", () => {
        const next = Number(input.value || 0) + Number(button.dataset.tinCounterStep);
        const min = input.min === "" ? -Infinity : Number(input.min);
        const max = input.max === "" ? Infinity : Number(input.max);
        input.value = String(Math.min(max, Math.max(min, next)));
        input.dispatchEvent(new Event("input", { bubbles: true }));
      });
    });
  });
}

function bindWorkflowFieldValidation(form) {
  const inputs = [...form.querySelectorAll("[data-max-length]")]
    .filter((input) => input.dataset.maxLength);
  if (!inputs.length) return;
  const submits = [...form.querySelectorAll("[type=submit]")];
  const messages = new Map(inputs.map((input) => {
    const helpId = input.getAttribute("aria-describedby");
    const message = helpId ? form.querySelector(`#${CSS.escape(helpId)}`) : null;
    return [input, { message, defaultText: message?.textContent || "" }];
  }));
  const validate = () => {
    let hasInvalidField = false;
    inputs.forEach((input) => {
      const max = Number(input.dataset.maxLength);
      const invalid = input.value.length > max;
      const { message, defaultText } = messages.get(input);
      input.setAttribute("aria-invalid", String(invalid));
      hasInvalidField ||= invalid;
      if (message) {
        message.textContent = invalid
          ? `${input.value.length} / ${max} characters — shorten this value.`
          : defaultText;
      }
    });
    form.classList.toggle("is-invalid", hasInvalidField);
    submits.forEach((submit) => {
      submit.disabled = hasInvalidField;
    });
  };
  inputs.forEach((input) => input.addEventListener("input", validate));
  validate();
}

async function startWorkflow(workflowId, button) {
  if (!state.project) return;
  const context = currentProjectContext();
  const idleLabel = button.textContent;
  button.disabled = true;
  button.textContent = "Starting";
  try {
    const run = await api(`/api/workflows/${encodeURIComponent(workflowId)}/runs`, {
      method: "POST",
      body: JSON.stringify({ project_id: context.projectId }),
    });
    if (!isCurrentProjectContext(context)) return;
    upsertRun(run);
    render();
    showToast(`${formatWorkflowKey(run.workflow_name)} started.`);
    schedulePolling();
  } catch (error) {
    if (!isCurrentProjectContext(context)) return;
    button.disabled = false;
    button.textContent = idleLabel;
    showToast(`Could not start workflow: ${error.message}`);
  }
}

async function setTemplateSaved(workflowId, saved, button) {
  if (!state.project) return;
  const context = currentProjectContext();
  const workflow = state.workflows.find((item) => item.id === workflowId);
  if (!workflow) return;
  button.disabled = true;
  try {
    await api(
      `/api/projects/${encodeURIComponent(context.projectId)}/workflow-templates/${encodeURIComponent(workflowId)}/saved`,
      { method: saved ? "PUT" : "DELETE" },
    );
    if (!isCurrentProjectContext(context)) return;
    workflow.saved = saved;
    renderWorkflows();
    showToast(saved ? `${workflow.title} saved.` : `${workflow.title} removed from Saved.`);
  } catch (error) {
    if (!isCurrentProjectContext(context)) return;
    button.disabled = false;
    showToast(`Could not update Saved: ${error.message}`);
  }
}

function upsertProjectWorkflow(configured) {
  const index = state.projectWorkflows.findIndex((item) => item.id === configured.id);
  if (index >= 0) state.projectWorkflows[index] = configured;
  else state.projectWorkflows.unshift(configured);
}

function styleCaptureServices() {
  const context = currentProjectContext();
  return {
    projectId: context.projectId, api, fetch: authorizedFetch,
    assertCurrent() { if (!isCurrentProjectContext(context)) throw new Error("Project changed. No samples were saved to the new project."); },
    openAgent() { if (agentRailBody.hidden) agentRailToggle.click(); },
  };
}

async function saveProjectWorkflow(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = event.submitter || form.querySelector("[type=submit]");
  const workflow = state.workflows.find((item) => item.id === form.dataset.workflowId);
  if (!workflow || !state.project) return;
  const context = currentProjectContext();
  const idleSubmitLabel = submit.textContent;
  submit.disabled = true;
  const runNow = submit.hasAttribute("data-run-workflow-draft");
  const saveAndRun = submit.hasAttribute("data-save-and-run");
  submit.textContent = runNow || saveAndRun ? "Starting" : "Saving";
  const schema = workflow.definition?.input_schema || {};
  if (["style.capture", "content.generate", "content.deliver"].includes(workflow.key)) {
    try {
      if (workflow.key === "style.capture") await window.TinStyleCapture.prepare(form, styleCaptureServices());
      else if (workflow.key === "content.deliver") window.TinContentDelivery.prepare(form);
      else window.TinContentDraft.prepare(form, {forRun: runNow || saveAndRun});
    } catch (error) {
      if (!isCurrentProjectContext(context)) return;
      submit.disabled = false;
      submit.textContent = idleSubmitLabel;
      showToast(error.message);
      return;
    }
  }
  const inputs = readWorkflowInputs(form, schema);
  if (runNow) {
    try {
      const run = await api(`/api/workflows/${encodeURIComponent(workflow.id)}/runs`, {
        method: "POST",
        headers: { "Idempotency-Key": workflow.key === "content.generate" ? window.TinContentDraft.requestId(form, inputs) : `ui:${window.crypto.randomUUID()}` },
        body: JSON.stringify({ project_id: context.projectId, inputs }),
      });
      if (!isCurrentProjectContext(context)) return;
      upsertRun(run);
      state.workflowEditor = null;
      render();
      showToast(`${workflow.title} started.`);
      schedulePolling();
    } catch (error) {
      if (!isCurrentProjectContext(context)) return;
      submit.disabled = false;
      submit.textContent = idleSubmitLabel;
      showToast(`Could not start workflow: ${error.message}`);
    }
    return;
  }
  const schedule = workflowScheduleFromForm(form);
  const payload = {
    name: form.elements.workflow_name.value.trim(),
    inputs,
    schedule,
    workflow_id: workflow.id,
    request_id: window.crypto.randomUUID(),
  };
  try {
    const projectId = encodeURIComponent(context.projectId);
    const saved = await api(
      `/api/projects/${projectId}/workflows`,
      { method: "POST", body: JSON.stringify(payload) },
    );
    if (!isCurrentProjectContext(context)) return;
    upsertProjectWorkflow(saved);
    workflow.project_workflow_count = Number(workflow.project_workflow_count || 0) + 1;
    if (saveAndRun) {
      const run = await api(
        `/api/projects/${projectId}/workflows/${encodeURIComponent(saved.id)}/runs`,
        {
          method: "POST",
          headers: { "Idempotency-Key": `ui:${window.crypto.randomUUID()}` },
        },
      );
      if (!isCurrentProjectContext(context)) return;
      upsertRun(run);
    }
    state.workflowEditor = null;
    state.workflowSection = "yours";
    state.workflowFilter = "all";
    renderWorkflows();
    showToast(saveAndRun
      ? "Workflow added and started."
      : schedule ? "Workflow saved and scheduled." : "Workflow saved.");
  } catch (error) {
    if (!isCurrentProjectContext(context)) return;
    submit.disabled = false;
    submit.textContent = idleSubmitLabel;
    showToast(`Could not save workflow: ${error.message}`);
  }
}

function readWorkflowInputs(form, schema) {
  const inputs = {};
  for (const [name, definition] of Object.entries(schema.properties || {})) {
    if (name === "project_id") continue;
    const field = form.elements[`input:${name}`];
    if (!field) continue;
    inputs[name] = readWorkflowInputValue(field, definition);
  }
  return inputs;
}

function readWorkflowInputValue(field, definition) {
  if (field.hasAttribute("data-content-files")) return JSON.parse(field.value || "[]");
  if (definition.type === "boolean") return field.value === "true";
  if (definition.type === "integer") return Number.parseInt(field.value, 10);
  if (definition.type === "number") return Number(field.value);
  if (definition.type === "array") {
    return field.value.split(",").map((item) => item.trim()).filter(Boolean);
  }
  return field.value;
}

function workflowScheduleFromForm(form) {
  const mode = form.elements.schedule_mode.value;
  if (mode === "manual") return null;
  return {
    ...(form.tinCodeSchedule || {}),
    cadence: mode,
    weekdays: mode === "weekly" ? (form.querySelector("[data-code-weekdays]")
      ? [...form.querySelectorAll("[name=schedule_days]:checked")].map((field) => field.value)
      : [form.elements.schedule_weekday.value]) : [],
    local_time: form.elements.schedule_time.value,
    timezone: form.elements.schedule_timezone.value.trim(),
  };
}

async function saveProjectWorkflowField(event) {
  event.preventDefault();
  if (!state.project) return;
  const context = currentProjectContext();
  const form = event.currentTarget;
  const configured = state.projectWorkflows.find(
    (item) => item.id === form.dataset.projectWorkflowId,
  );
  if (!configured) return;
  const fieldName = form.dataset.workflowField;
  const submit = form.querySelector("[type=submit]");
  const idleLabel = submit.textContent;
  submit.disabled = true;
  submit.textContent = "Saving";
  let name = configured.name;
  const inputs = { ...configured.inputs };
  let schedule = configured.schedule ? { ...configured.schedule } : null;
  if (fieldName === "name") {
    name = form.elements.value.value.trim();
  } else if (fieldName === "schedule") {
    schedule = workflowScheduleFromForm(form);
  } else if (fieldName.startsWith("input:")) {
    const inputName = fieldName.slice("input:".length);
    const definition = configured.input_schema.properties?.[inputName];
    if (!definition) return;
    inputs[inputName] = readWorkflowInputValue(form.elements.value, definition);
  }
  try {
    const saved = await api(
      `/api/projects/${encodeURIComponent(context.projectId)}/workflows/${encodeURIComponent(configured.id)}`,
      {
        method: "PUT",
        body: JSON.stringify({
          name,
          inputs,
          schedule,
          expected_settings_revision: configured.settings_revision,
        }),
      },
    );
    if (!isCurrentProjectContext(context)) return;
    upsertProjectWorkflow(saved);
    state.workflowEditor = {
      workflowId: saved.workflow_id,
      projectWorkflowId: saved.id,
      field: null,
    };
    renderWorkflows();
    showToast(`${humanize(fieldName.replace("input:", ""))} saved.`);
  } catch (error) {
    if (!isCurrentProjectContext(context)) return;
    if (error.status === 409) {
      state.projectWorkflows = await api(
        `/api/projects/${encodeURIComponent(context.projectId)}/workflows`,
      );
      if (!isCurrentProjectContext(context)) return;
      state.workflowEditor = null;
      renderWorkflows();
      showToast("These settings changed elsewhere. The latest version is now shown.");
      return;
    }
    submit.disabled = false;
    submit.textContent = idleLabel;
    showToast(`Could not save ${humanize(fieldName)}: ${error.message}`);
  }
}

async function saveSystemWorkflowSettings(event) {
  event.preventDefault();
  if (!state.project) return;
  const form = event.currentTarget;
  const configured = state.projectWorkflows.find(
    (item) => item.id === form.dataset.projectWorkflowId,
  );
  if (!configured) return;
  const submit = event.submitter || form.querySelector("[type=submit]");
  const idleLabel = submit.textContent;
  submit.disabled = true;
  submit.textContent = "Saving";
  const context = currentProjectContext();
  try {
    if (configured.workflow_key === "content.generate") window.TinContentDraft.prepare(form, {forRun: false});
    const saved = await api(
      `/api/projects/${encodeURIComponent(context.projectId)}/workflows/${encodeURIComponent(configured.id)}`,
      {
        method: "PUT",
        body: JSON.stringify({
          name: form.elements.workflow_name.value.trim(),
          inputs: readWorkflowInputs(form, configured.input_schema),
          schedule: workflowScheduleFromForm(form),
          expected_settings_revision: configured.settings_revision,
        }),
      },
    );
    if (!isCurrentProjectContext(context)) return;
    upsertProjectWorkflow(saved);
    state.workflowEditor = null;
    state.systemSummary = await api(`/api/projects/${encodeURIComponent(context.projectId)}/system`);
    if (!isCurrentProjectContext(context)) return;
    renderWorkflows();
    showToast("Changes saved.");
  } catch (error) {
    if (!isCurrentProjectContext(context)) return;
    if (error.status === 409) {
      state.projectWorkflows = await api(
        `/api/projects/${encodeURIComponent(context.projectId)}/workflows`,
      );
      if (!isCurrentProjectContext(context)) return;
      state.workflowEditor = null;
      renderWorkflows();
      showToast("These settings changed elsewhere. The latest version is now shown.");
      return;
    }
    submit.disabled = false;
    submit.textContent = idleLabel;
    showToast(`Could not save changes: ${error.message}`);
  }
}

async function runProjectWorkflow(projectWorkflowId, button) {
  if (!state.project) return;
  const context = currentProjectContext();
  const idleLabel = button.textContent;
  button.disabled = true;
  button.textContent = "Starting";
  try {
    const run = await api(
      `/api/projects/${encodeURIComponent(context.projectId)}/workflows/${encodeURIComponent(projectWorkflowId)}/runs`,
      {
        method: "POST",
        headers: { "Idempotency-Key": `ui:${window.crypto.randomUUID()}` },
      },
    );
    if (!isCurrentProjectContext(context)) return;
    upsertRun(run);
    const configured = state.projectWorkflows.find((item) => item.id === projectWorkflowId);
    if (configured) {
      configured.last_run_id = run.id;
      configured.last_run_status = run.status;
      configured.last_artifact_path = run.artifact_path;
      configured.last_artifact_title = run.artifact_title;
    }
    render();
    showToast(`${formatWorkflowKey(run.workflow_name)} started.`);
    schedulePolling();
  } catch (error) {
    if (!isCurrentProjectContext(context)) return;
    button.disabled = false;
    button.textContent = idleLabel;
    showToast(`Could not start workflow: ${error.message}`);
  }
}

async function retryProjectWorkflow(projectWorkflowId, button) {
  if (!state.project) return;
  const context = currentProjectContext();
  const idleLabel = button.textContent;
  button.disabled = true;
  button.textContent = "Retrying";
  try {
    const configured = state.projectWorkflows.find(item => item.id === projectWorkflowId);
    if (configured?.last_run_id) {
      const last = await api(`/api/workflows/runs/${configured.last_run_id}`);
      if (!isCurrentProjectContext(context)) return;
      if (last.review_source_run_id) {
        upsertRun(last);
        openDocument(last.id, "workflows");
        return;
      }
    }
    const run = await api(
      `/api/projects/${encodeURIComponent(context.projectId)}/workflows/${encodeURIComponent(projectWorkflowId)}/retry`,
      {
        method: "POST",
        headers: { "Idempotency-Key": `ui:${window.crypto.randomUUID()}` },
      },
    );
    if (!isCurrentProjectContext(context)) return;
    upsertRun(run);
    renderWorkflows();
    showToast(`${formatWorkflowKey(run.workflow_name)} is retrying.`);
    schedulePolling();
  } catch (error) {
    if (!isCurrentProjectContext(context)) return;
    button.disabled = false;
    button.textContent = idleLabel;
    showToast(`Could not retry this workflow: ${error.message}`);
  }
}

async function skipProjectWorkflow(projectWorkflowId, button) {
  if (!state.project) return;
  const context = currentProjectContext();
  button.disabled = true;
  button.textContent = "Skipping";
  try {
    const configured = await api(
      `/api/projects/${encodeURIComponent(context.projectId)}/workflows/${encodeURIComponent(projectWorkflowId)}/skip-once`,
      { method: "POST" },
    );
    if (!isCurrentProjectContext(context)) return;
    upsertProjectWorkflow(configured);
    state.systemSummary = await api(`/api/projects/${encodeURIComponent(context.projectId)}/system`);
    if (!isCurrentProjectContext(context)) return;
    renderWorkflows();
    showToast("The next run will be skipped.");
  } catch (error) {
    if (!isCurrentProjectContext(context)) return;
    button.disabled = false;
    button.textContent = "Skip once";
    showToast(`Could not skip the next run: ${error.message}`);
  }
}

async function removeProjectWorkflow(projectWorkflowId, button) {
  if (!state.project) return;
  const configured = state.projectWorkflows.find((item) => item.id === projectWorkflowId);
  if (!configured || !window.confirm(`Remove ${configured.name} from My system? Earlier runs and outputs will remain.`)) return;
  const context = currentProjectContext();
  button.disabled = true;
  button.textContent = "Removing";
  try {
    await api(
      `/api/projects/${encodeURIComponent(context.projectId)}/workflows/${encodeURIComponent(projectWorkflowId)}?expected_settings_revision=${configured.settings_revision}`,
      { method: "DELETE" },
    );
    if (!isCurrentProjectContext(context)) return;
    state.projectWorkflows = state.projectWorkflows.filter((item) => item.id !== projectWorkflowId);
    const workflow = state.workflows.find((item) => item.id === configured.workflow_id);
    if (workflow) {
      workflow.project_workflow_count = Math.max(
        0,
        Number(workflow.project_workflow_count || 0) - 1,
      );
    }
    state.workflowEditor = null;
    state.systemSummary = await api(`/api/projects/${encodeURIComponent(context.projectId)}/system`);
    if (!isCurrentProjectContext(context)) return;
    renderWorkflows();
    showToast(`${configured.name} removed.`);
  } catch (error) {
    if (!isCurrentProjectContext(context)) return;
    button.disabled = false;
    button.textContent = "Remove";
    showToast(`Could not remove this workflow: ${error.message}`);
  }
}

async function toggleProjectWorkflow(projectWorkflowId, action, button) {
  if (!state.project) return;
  const context = currentProjectContext();
  button.disabled = true;
  button.textContent = action === "pause" ? "Pausing" : "Resuming";
  try {
    const configured = await api(
      `/api/projects/${encodeURIComponent(context.projectId)}/workflows/${encodeURIComponent(projectWorkflowId)}/${action}`,
      { method: "POST" },
    );
    if (!isCurrentProjectContext(context)) return;
    upsertProjectWorkflow(configured);
    renderWorkflows();
    showToast(action === "pause" ? "Schedule paused." : "Schedule resumed.");
  } catch (error) {
    if (!isCurrentProjectContext(context)) return;
    button.disabled = false;
    button.textContent = action === "pause" ? "Pause" : "Resume";
    showToast(`Could not ${action} schedule: ${error.message}`);
  }
}

async function approveRun(runId, button, { delivery = null, remember = false } = {}) {
  const context = currentProjectContext();
  const run = state.runs.find((item) => item.id === runId);
  const revisionReview = isCampaignRevisionReview(run);
  const diagramReview = run?.workflow_name === "content.diagram";
  const originalLabel = button.textContent;
  button.disabled = true;
  button.textContent = "Approving";
  try {
    await api(`/api/workflows/runs/${encodeURIComponent(runId)}/approve`, {
      method: "POST",
      body: JSON.stringify({
        review_token: window.TinWorkflowReview?.token(context.projectId, runId) || null,
        ...(delivery ? { delivery, remember } : {}),
      }),
    });
    if (!isCurrentProjectContext(context)) return;
    state.pendingReviewRunIds.add(runId);
    state.deferredReviewRunIds = state.deferredReviewRunIds.filter((id) => id !== runId);
    if (run) upsertRun({ ...run, status: "running" });
    render();
    showToast(revisionReview
      ? "Revision approved. Remaining deliveries resumed."
      : diagramReview ? "Diagram approved." : deliveryToast(delivery || (run?.content_delivery ? "github_pr" : null), "Draft approved."));
    schedulePolling();
  } catch (error) {
    if (!isCurrentProjectContext(context)) return;
    button.disabled = false;
    button.textContent = originalLabel;
    showToast(`Could not approve ${revisionReview ? "revision" : diagramReview ? "diagram" : "draft"}: ${error.message}`);
  }
}

async function discardCampaignRevision(runId, button) {
  button.disabled = true;
  button.textContent = "Discarding";
  try {
    const campaign = await api(`/api/outreach/campaigns/${encodeURIComponent(runId)}`);
    if (!campaign.pending_revision_id) throw new Error("No revision is awaiting review");
    const run = await api(
      `/api/outreach/campaigns/${encodeURIComponent(runId)}/revisions/${encodeURIComponent(campaign.pending_revision_id)}/discard`,
      { method: "POST" },
    );
    upsertRun(run);
    state.deferredReviewRunIds = state.deferredReviewRunIds.filter((id) => id !== runId);
    navigate("workflows");
    showToast("Revision discarded. The approved campaign resumed unchanged.");
    schedulePolling();
  } catch (error) {
    button.disabled = false;
    button.textContent = "Discard revision";
    showToast(`Could not discard revision: ${error.message}`);
  }
}

function supportsArticleFeedback(run) {
  if (!run) return false;
  return Boolean(workflowForRun(run)?.definition?.procedure?.output?.apply_on_approval) ||
    ["content.generate", "content.public_article"].includes(workflowForRun(run)?.key || run?.workflow_name);
}

function mountArticleFeedback(host, runId, reader = false) {
  const context = currentProjectContext();
  const run = state.runs.find(item => item.id === runId);
  const repositoryDelivery = repositoryDeliveryAvailable(run);
  return window.TinWorkflowReview.mount(host, {
    approvalLabel: repositoryDelivery ? "Publish now" : run?.content_delivery?.approval_label,
    deliveryOptions: reader && repositoryDelivery ? [{ label: "Open a pull request", delivery: "github_pr" }] : [],
    onApprove: (button, delivery = repositoryDelivery ? "github_commit" : null) => approveRun(runId, button, delivery ? { delivery } : {}),
    api, projectId: context.projectId, runId, reader, toast: showToast,
    loadRenderer: loadComparisonRenderer,
    openRun: id => openDocument(id, "decisions"),
    onRevised: successor => {
      if (!isCurrentProjectContext(context)) return;
      const source = state.runs.find(r => r.id === runId);
      if (source) upsertRun({...source, status: "superseded"});
      upsertRun(successor);
      state.decisions = state.decisions.filter(d => d.run_id !== runId);
      state.workflowSection = "yours";
      navigate("workflows");
      showToast("Revising from your feedback. The previous copy remains readable.");
      schedulePolling({immediate: true});
    },
  });
}

function selectedDecision() {
  if (!state.decisions.length) return null;
  return state.decisions.find((item) => item.id === state.decisionId) || state.decisions[0];
}

function decisionItemHtml(decision, item, index) {
  const title = item.title || item.file || `Output ${index + 1}`;
  const facts = [item.file, item.words ? `${item.words} words` : null, item.sources ? `${item.sources} sources` : null]
    .filter(Boolean)
    .join(" · ");
  return `<article class="decision-output">
    <span><strong>${escapeHtml(title)}</strong><code>${escapeHtml(facts)}</code></span>
    <button type="button" data-decision-output="${escapeHtml(decision.id)}" data-decision-output-index="${index}">Read →</button>
  </article>`;
}

const REPOSITORY_DELIVERY_WORKFLOWS = new Set(["content.generate", "content.public_article", "content.answer_page"]);

function connectedRepository() {
  const github = state.integrations.find((item) => item.key === "infra.github");
  if (!github?.connection_id || github.status !== "connected") return null;
  return github.configuration?.selected_repository || null;
}

function isContentDraftReview(run) {
  if (!run) return false;
  return REPOSITORY_DELIVERY_WORKFLOWS.has(workflowForRun(run)?.key || run.workflow_name);
}

function repositoryDeliveryAvailable(run) {
  return !run?.content_delivery?.system_run_id && isContentDraftReview(run) && Boolean(connectedRepository());
}

function decisionApprovalHtml(decision, run) {
  const id = escapeHtml(decision.id);
  if (repositoryDeliveryAvailable(run)) {
    return `<label class="decision-remember"><input type="checkbox" data-decision-remember> Do this for future drafts</label>
      <button class="button-quiet" type="button" data-decision-not-now>Not now</button>
      <button class="button-secondary" type="button" data-apply-decision="${id}" data-delivery="github_pr">Open a pull request</button>
      <button class="decision-approval" type="button" data-apply-decision="${id}" data-delivery="github_commit">Publish now</button>`;
  }
  const label = run?.content_delivery?.approval_label || (workflowForRun(run)?.definition?.procedure?.output?.apply_on_approval ? "Use documents" : "Approve");
  return `<button class="button-quiet" type="button" data-decision-not-now>Not now</button>
    <button class="decision-approval" type="button" data-apply-decision="${id}">${escapeHtml(label)}</button>`;
}

function decisionDetailHtml(decision) {
  if (!decision) return "";
  const outputs = decision.items || [];
  // Output rows already open the review. Keep a run action only when it is
  // distinct (conflicts), or when there is no output row to open.
  const showRunAction = decision.kind === "output_conflict" || !outputs.length;
  const run = state.runs.find((item) => item.id === decision.run_id);
  const deliveryNote = decision.kind !== "output_conflict" && isContentDraftReview(run) && !connectedRepository()
    ? 'Approved drafts stay in Tin until GitHub is connected. <a href="/integrations" data-decision-connect-github>Connect GitHub</a>'
    : "";
  const consequence = String(decision.consequence || "").trim();
  return `<article class="decision-detail-card">
    <header>
      <span class="decision-workflow-mark">${escapeHtml((decision.workflow_title || "W").slice(0, 1))}</span>
      <span><strong>${escapeHtml(decision.workflow_title)}</strong><code>${escapeHtml(decision.workflow_key)} · ${escapeHtml(shortRunId(decision.run_id))} · ${escapeHtml(waitingLabel(decision.created_at))}${decision.kind === "output_conflict" ? " · result saved" : ""}</code></span>
      ${showRunAction ? `<button type="button" data-decision-read="${escapeHtml(decision.id)}">Observe →</button>` : ""}
    </header>
    <div class="decision-detail-body">
      <p>${escapeHtml(decision.explanation)}</p>
      <div class="decision-outputs">
        ${outputs.length ? outputs.map((item, index) => decisionItemHtml(decision, item, index)).join("") : '<span class="decision-no-output">Open the run to review its proposed changes.</span>'}
      </div>
    </div>
    <footer${consequence || deliveryNote ? "" : ' class="is-actions-only"'}>
      ${consequence || deliveryNote ? `<span>${escapeHtml(consequence)}${consequence && deliveryNote ? " " : ""}${deliveryNote}</span>` : ""}
      ${decision.kind === "output_conflict" ? `<button class="button-quiet" type="button" data-decision-not-now>Not now</button>
      <button class="compare-confirm" type="button" data-output-compare="${escapeHtml(decision.run_id)}">${decision.output_resolution?.state === "applying" ? "Check outcome" : "Compare"}</button>` : decisionApprovalHtml(decision, run)}
    </footer>
  </article>`;
}

function decisionsPace() {
  if (!state.decisions.length) return "nothing waiting";
  const nearestDeadline = state.decisions
    .map((item) => item.deadline_at)
    .filter(Boolean)
    .sort()[0];
  return `${state.decisions.length} waiting${nearestDeadline ? ` · nearest deadline ${systemDateTime(nearestDeadline)}` : ""}`;
}

function renderDecisions() {
  disposeDocument();
  const decision = selectedDecision();
  if (decision && state.decisionId !== decision.id) state.decisionId = decision.id;
  main.innerHTML = `<section class="product-view decisions-view">
    <header class="decisions-header"><h1>Decisions</h1><code>${escapeHtml(decisionsPace())}</code></header>
    ${state.decisions.length ? `<div class="decisions-layout">
      <div class="decision-list">${state.decisions.map((item) => `<button class="decision-list-item ${item.id === decision.id ? "is-active" : ""}" type="button" data-decision-id="${escapeHtml(item.id)}">
        <span class="activity-marker is-needs-you" aria-hidden="true"></span>
        <span><strong>${escapeHtml(item.title)}</strong></span>
        <code>${escapeHtml(waitingLabel(item.created_at))}</code>
      </button>`).join("")}</div>
      ${decisionDetailHtml(decision)}
    </div>` : '<div class="decisions-empty"><strong>Nothing needs you.</strong><span>When a workflow needs review or an answer, it will appear here.</span><button class="button-secondary" type="button" data-open-system>Open System</button></div>'}
  </section>`;
  main.querySelectorAll("[data-decision-id]").forEach((button) => {
    button.addEventListener("click", () => {
      state.decisionId = button.dataset.decisionId;
      renderDecisions();
    });
  });
  main.querySelector("[data-open-system]")?.addEventListener("click", () => navigate("workflows"));
  main.querySelector("[data-decision-not-now]")?.addEventListener("click", () => {
    state.decisionId = state.decisions.find((item) => item.id !== decision.id)?.id || decision.id;
    renderDecisions();
  });
  main.querySelectorAll("[data-decision-read], [data-decision-output]").forEach((button) => {
    button.addEventListener("click", () => openDecisionReview(decision, button));
  });
  main.querySelectorAll("[data-apply-decision]").forEach((button) => {
    button.addEventListener("click", (event) => applyDecision(decision, event.currentTarget));
  });
  main.querySelector("[data-decision-connect-github]")?.addEventListener("click", (event) => {
    event.preventDefault();
    navigate("integrations");
  });
  main.querySelector("[data-output-compare]")?.addEventListener("click", () => openOutputComparison(decision.run_id, "decisions"));
  if (decision && supportsArticleFeedback(state.runs.find(run => run.id === decision.run_id) || {workflow_name: decision.workflow_key})) {
    state.documentCleanup = mountArticleFeedback(main.querySelector(".decision-detail-card"), decision.run_id);
  }
}

function openDecisionReview(decision, button) {
  if (decision.kind === "output_conflict") {
    if (button.hasAttribute("data-decision-read")) {
      const event = state.activity.find((item) => item.run_id === decision.run_id);
      state.activityFilter = "all";
      if (event) state.expandedRun = { runId: decision.run_id, eventId: event.id };
      navigate("activity");
      return;
    }
    const item = decision.items[Number(button.dataset.decisionOutputIndex || 0)];
    goToRoute(`file?${new URLSearchParams({ path: item.file, revision: item.revision, compareRun: decision.run_id, return: "decisions", back: "decisions", source: "retained" })}`);
    return;
  }
  const run = state.runs.find((item) => item.id === decision.run_id);
  if (run?.workflow_name === "project.task") {
    openTask(run.id);
    return;
  }
  const index = Number(button.dataset.decisionOutputIndex || 0);
  const item = decision.items?.[index];
  if (item?.file && item?.revision && item.file !== run?.artifact_path) {
    openProjectFile(item.file, item.revision, decision.run_id);
    return;
  }
  openRunArtifact(decision.run_id, "decisions");
}

async function applyDecision(decision, button) {
  const context = currentProjectContext();
  const originalLabel = button.textContent;
  const delivery = button.dataset.delivery || null;
  const remember = Boolean(delivery && main.querySelector("[data-decision-remember]")?.checked);
  const siblings = [...main.querySelectorAll("[data-apply-decision]")].filter((item) => item !== button);
  siblings.forEach((item) => { item.disabled = true; });
  button.disabled = true;
  button.textContent = "Applying";
  try {
    const run = await api(`/api/decisions/${encodeURIComponent(decision.id)}/apply`, {
      method: "POST",
      body: JSON.stringify({
        action: "approve",
        review_token: window.TinWorkflowReview?.token(context.projectId, decision.run_id) || null,
        ...(delivery ? { delivery, remember } : {}),
      }),
    });
    if (!isCurrentProjectContext(context)) return;
    upsertRun(run);
    state.decisions = state.decisions.filter((item) => item.id !== decision.id);
    state.decisionId = state.decisions[0]?.id || null;
    render();
    showToast(deliveryToast(delivery, "Decision applied."));
    schedulePolling({ immediate: true });
  } catch (error) {
    if (!isCurrentProjectContext(context)) return;
    siblings.forEach((item) => { item.disabled = false; });
    button.disabled = false;
    button.textContent = originalLabel;
    showToast(`Could not apply decision: ${error.message}`);
  }
}

function deliveryToast(delivery, fallback) {
  if (delivery === "github_commit") return "Draft approved. Publishing it to the repository now.";
  if (delivery === "github_pr") return "Draft approved. GitHub PR delivery will follow; nothing is merged.";
  return fallback;
}

function renderActivity() {
  const events = state.activity.filter((event) => {
    const kind = activityKind(event);
    return state.activityFilter === "all" || state.activityFilter === kind;
  });
  const groups = groupActivityByDay(events);
  main.innerHTML = `<section class="activity-view">
    <header class="activity-header">
      <h1>Activity</h1>
      <div class="activity-filters" aria-label="Activity filters">
        ${activityFilterButton("all", "All")}
        ${activityFilterButton("runs", "Runs")}
        ${activityFilterButton("needs_you", "Needs you")}
        ${activityFilterButton("your_edits", "Your edits")}
      </div>
      <span class="activity-header-spacer"></span>
      <span class="activity-live"><span aria-hidden="true"></span>live · tailing runs</span>
    </header>
    ${groups.length ? `<div class="activity-ledger">${groups.map(activityGroup).join("")}</div>` : '<p class="activity-empty">Nothing recorded yet — the first runs will land here.</p>'}
    ${state.activity.length ? `<p class="activity-boundary">${state.activityLoading ? "loading earlier activity…" : state.activityHasMore ? "scroll for earlier activity" : "beginning of recorded activity"}</p>` : ""}
  </section>`;

  document.querySelectorAll("[data-activity-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      state.activityFilter = button.dataset.activityFilter;
      renderActivity();
    });
  });
  bindRunDetailControls(main);
  bindActivityControls(main, "activity");
}

function bindActivityControls(root, returnView) {
  root.querySelectorAll("[data-current-output]").forEach((button) => {
    button.addEventListener("click", () => openCurrentComparisonFile(button.dataset.currentOutput));
  });
  root.querySelectorAll("[data-activity-artifact]").forEach((button) => {
    button.addEventListener("click", () => {
      openRunArtifact(button.dataset.activityArtifact, returnView);
    });
  });
  root.querySelectorAll("[data-activity-task]").forEach((button) => {
    button.addEventListener("click", () => openTask(button.dataset.activityTask));
  });
}

function activityFilterButton(filter, label) {
  const active = state.activityFilter === filter;
  return `<button class="activity-filter ${active ? "is-active" : ""}" type="button" data-activity-filter="${filter}" aria-pressed="${active}">${escapeHtml(label)}</button>`;
}

function groupActivityByDay(events) {
  const groups = [];
  for (const event of events) {
    const key = dayKey(event.created_at);
    let group = groups.at(-1);
    if (!group || group.key !== key) {
      group = { key, label: dayLabel(event.created_at), events: [] };
      groups.push(group);
    }
    group.events.push(event);
  }
  return groups;
}

function activityGroup(group) {
  return `<section class="activity-day">
    <h2>${escapeHtml(group.label)}</h2>
    <div class="activity-day-rows">${group.events.map(activityItem).join("")}</div>
  </section>`;
}

function activityKind(event) {
  const declared = String(event.details?.kind || "");
  if (["runs", "needs_you", "your_edits"].includes(declared)) return declared;
  const type = String(event.event_type || "").toLowerCase();
  if (/(needs_you|review|approval|decision)/.test(type)) return "needs_you";
  if (/(founder|user|manual|edited|config|chat_steer)/.test(type)) return "your_edits";
  return "runs";
}

function activityStatus(event) {
  const type = String(event.event_type || "").toLowerCase();
  const status = String(event.details?.status || "").toLowerCase();
  if (status === "failed" || /(failed|failure|error)/.test(type)) return "failed";
  if (activityKind(event) === "needs_you") return "needs-you";
  if (activityKind(event) === "your_edits") return "founder";
  return "completed";
}

function activityAction(event) {
  if (["procedure_output_applied", "procedure_output_kept"].includes(event.event_type) && event.details?.path) {
    return `<button type="button" data-current-output="${escapeHtml(event.details.path)}">Open file →</button>`;
  }
  const externalUrl = safeHttpsUrl(event.details?.external_url);
  if (externalUrl) {
    const label = String(event.details?.external_label || "Open");
    return `<a href="${escapeHtml(externalUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)} →</a>`;
  }
  const run = state.runs.find((item) => item.id === event.run_id);
  if (!run) return "";
  if (run.workflow_name === "project.task" && run.task_diff?.files?.length) {
    return `<button type="button" data-activity-task="${escapeHtml(run.id)}">Open task →</button>`;
  }
  if (run.retained_output && !run.canonical_commit_sha) {
    return `<button type="button" data-activity-artifact="${escapeHtml(run.id)}">${retainedOutputLabel(run)} →</button>`;
  }
  if (!availableRunOutput(run)) {
    return `<button type="button" data-observe-run="${escapeHtml(run.id)}" data-observe-event="${escapeHtml(event.id)}">Observe →</button>`;
  }
  const filename = String(run.artifact_path).split("/").at(-1) || "receipt";
  const label = run.status === "needs_input" && isMarkdownArtifact(run) ? "Review draft" : filename;
  return `<button type="button" data-activity-artifact="${escapeHtml(run.id)}">${escapeHtml(label)} →</button>`;
}

function safeHttpsUrl(value) {
  if (typeof value !== "string" || !value) return "";
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:" ? parsed.href : "";
  } catch (_error) {
    return "";
  }
}

function activityItem(event) {
  const status = activityStatus(event);
  const workflow = event.workflow_key || "project";
  const runRef = event.run_id ? shortRunId(event.run_id) : String(event.details?.ref || "record");
  const plainSummary = event.summary || humanize(event.event_type);
  const startedBy = event.run_id ? systemStartedBy({
    trigger_source: event.details?.trigger_source,
    trigger_client: event.details?.trigger_client,
  }) : null;
  const summary = startedBy
    ? `${plainSummary.replace(/[.\s]+$/, "")}. ${humanize(startedBy)}.`
    : plainSummary;
  const run = event.run_id ? state.runs.find((item) => item.id === event.run_id) : null;
  const expanded = run && state.expandedRun?.runId === run.id && state.expandedRun?.eventId === event.id;
  return `<div class="activity-ledger-entry ${expanded ? "is-open" : ""}">
    <article class="activity-ledger-row is-${status}">
      <time datetime="${escapeHtml(event.created_at)}">${escapeHtml(ledgerTime(event.created_at))}</time>
      <span class="activity-marker is-${status}" aria-label="${escapeHtml(status.replaceAll("-", " "))}">${status === "failed" ? "×" : ""}</span>
      <code class="activity-workflow" title="${escapeHtml(workflow)}">${escapeHtml(workflow)}</code>
      <p>${escapeHtml(summary)}</p>
      <span class="activity-action">${activityAction(event)}</span>
      ${run
        ? `<button class="activity-ref" type="button" data-observe-run="${escapeHtml(run.id)}" data-observe-event="${escapeHtml(event.id)}" aria-label="${expanded ? "Close" : "Open"} run details">${escapeHtml(runRef)}</button>`
        : `<code class="activity-ref">${escapeHtml(runRef)}</code>`}
    </article>
    ${expanded ? systemRunDetailHtml(run) : ""}
  </div>`;
}

function shortRevision(revision) {
  return String(revision || "").slice(0, 7);
}

function projectFileType(path) {
  const filename = String(path).split("/").at(-1) || path;
  if (filename === "SKILL.md") return "skill";
  const extension = filename.includes(".") ? filename.split(".").at(-1).toLowerCase() : "";
  const labels = {
    md: "markdown",
    markdown: "markdown",
    mmd: "mermaid",
    json: "json",
    js: "javascript",
    ts: "typescript",
    py: "python",
    css: "css",
    html: "html",
    txt: "text",
    yaml: "yaml",
    yml: "yaml",
    xml: "xml",
    toml: "toml",
    csv: "csv",
    tsv: "tsv",
    pdf: "pdf",
  };
  if (labels[extension]) return labels[extension];
  const mediaType = MEDIA_FILE_TYPES[extension];
  if (mediaType?.startsWith("image/")) return `${extension} image`;
  if (mediaType?.startsWith("video/")) return `${extension} video`;
  if (mediaType?.startsWith("audio/")) return `${extension} audio`;
  return extension || "file";
}

function projectFileIcon(path, folder = false, open = false) {
  let symbol = folder ? (open ? "tin-folder-open" : "tin-folder") : "tin-file";
  if (!folder && String(path).split("/").at(-1) === "SKILL.md") symbol = "tin-skill";
  else if (!folder && ["md", "markdown", "mmd"].includes(String(path).split(".").at(-1)?.toLowerCase())) symbol = "tin-markdown";
  else if (!folder && String(path).split(".").at(-1)?.toLowerCase() === "json") symbol = "tin-json";
  return `<svg class="project-file-icon" aria-hidden="true" viewBox="0 0 16 16"><use href="#${symbol}"></use></svg>`;
}

function projectDirectoryEntries(paths, directory = "") {
  const prefix = directory ? `${directory.replace(/\/$/, "")}/` : "";
  const folders = new Map();
  const files = [];
  for (const path of paths) {
    if (!path.startsWith(prefix)) continue;
    const remainder = path.slice(prefix.length);
    if (!remainder) continue;
    const slash = remainder.indexOf("/");
    if (slash < 0) {
      files.push({ kind: "file", name: remainder, path });
      continue;
    }
    const name = remainder.slice(0, slash);
    const folderPath = `${prefix}${name}`;
    if (!folders.has(folderPath)) folders.set(folderPath, { kind: "directory", name, path: folderPath });
  }
  return [
    ...[...folders.values()].sort((left, right) => left.name.localeCompare(right.name)),
    ...files.sort((left, right) => left.name.localeCompare(right.name)),
  ];
}

function projectDirectoryCount(path, paths = state.filesSnapshot?.files.map((item) => item.path) || []) {
  return projectDirectoryEntries(paths, String(path).replace(/\/$/, "")).length;
}

function projectDirectoryCountLabel(path, paths) {
  const count = projectDirectoryCount(path, paths);
  return `${count} item${count === 1 ? "" : "s"}`;
}

function projectFileTreeUnsafeCss() {
  const colorScheme = document.documentElement.dataset.theme === "dark" ? "dark" : "light";
  return `
    :host { color-scheme: ${colorScheme}; }
    [data-file-tree-virtualized-scroll="true"] { padding-inline: 0; }
    [data-type="item"] {
      box-sizing: border-box;
      margin: 0;
      padding-inline: 0 8px;
      border-radius: 0;
      border-top: 1px solid rgb(var(--ink-rgb) / 6%);
    }
    [data-type="item"][aria-level="1"] { border-top-color: rgb(var(--ink-rgb) / 8%); }
    [data-type="item"]:focus-visible:before,
    [data-type="item"][data-item-focused="true"]:before { display: none; }
    [data-item-section="spacing-item"] { opacity: 0 !important; }
    [data-item-section="icon"] { width: 38px; min-width: 38px; }
    [data-item-type="file"] > [data-item-section="icon"] { justify-content: flex-end; }
    [data-item-type="folder"] > [data-item-section="icon"] {
      justify-content: flex-start;
      gap: 8px;
    }
    .tin-tree-disclosure {
      width: 14px;
      color: var(--ink-muted);
      font-family: "Geist Mono", "SFMono-Regular", Consolas, monospace;
      font-size: 11px;
      line-height: 1;
      text-align: center;
    }
    .tin-tree-folder { width: 16px; height: 16px; flex: 0 0 16px; }
    [data-item-type="folder"] > [data-item-section="content"] {
      display: flex;
      align-items: center;
    }
    [data-item-type="folder"] > [data-item-section="content"]:after {
      content: "/";
      flex: 0 0 auto;
    }
    [data-item-section="decoration"] {
      flex: 0 0 76px;
      width: 76px;
      margin-left: auto;
      font-size: 11px;
    }
  `;
}

function decorateProjectFileTree() {
  const container = state.filesTree?.getFileTreeContainer();
  const root = container?.shadowRoot;
  if (!root) return;
  let scheduled = false;
  const apply = () => {
    scheduled = false;
    root.querySelectorAll('[data-type="item"][data-item-type="folder"]').forEach((row) => {
      const icon = row.querySelector('[data-item-section="icon"]');
      if (icon && !icon.querySelector(".tin-tree-disclosure")) {
        const open = row.getAttribute("aria-expanded") === "true";
        icon.innerHTML = `<span class="tin-tree-disclosure">${open ? "⌄" : "›"}</span><svg class="tin-tree-folder" aria-hidden="true" viewBox="0 0 16 16"><use href="#${open ? "tin-folder-open" : "tin-folder"}"></use></svg>`;
      } else if (icon) {
        const open = row.getAttribute("aria-expanded") === "true";
        const disclosure = icon.querySelector(".tin-tree-disclosure");
        const glyph = open ? "⌄" : "›";
        const href = `#${open ? "tin-folder-open" : "tin-folder"}`;
        if (disclosure.textContent !== glyph) disclosure.textContent = glyph;
        const use = icon.querySelector("use");
        if (use?.getAttribute("href") !== href) use?.setAttribute("href", href);
      }
    });
  };
  const observer = new MutationObserver(() => {
    if (scheduled) return;
    scheduled = true;
    window.requestAnimationFrame(apply);
  });
  observer.observe(root, { attributes: true, childList: true, subtree: true });
  state.filesTreeObserver = observer;
  apply();
}

function mountProjectFileTree() {
  const mount = document.querySelector("#project-file-tree");
  if (!mount || !state.filesSnapshot) return;
  if (!window.TinFilesTree) {
    mount.innerHTML = '<p class="files-empty">Tin could not load the project tree.</p>';
    return;
  }
  const paths = state.filesSnapshot.files.map((item) => item.path);
  const preparedInput = window.TinFilesTree.prepareFileTreeInput(paths, {
    flattenEmptyDirectories: true,
  });
  const tree = new window.TinFilesTree.FileTree({
    preparedInput,
    flattenEmptyDirectories: true,
    initialExpansion: 1,
    itemHeight: 40,
    dragAndDrop: false,
    renaming: false,
    fileTreeSearchMode: "hide-non-matches",
    icons: {
      set: "none",
      colored: false,
      spriteSheet: TIN_FILE_ICON_SPRITE,
      remap: { "file-tree-icon-file": "tin-file" },
      byFileExtension: { md: "tin-markdown", markdown: "tin-markdown", json: "tin-json" },
      byFileName: { "SKILL.md": "tin-skill" },
    },
    onSelectionChange: (selectedPaths) => {
      const path = selectedPaths.at(-1);
      if (!path || tree.getItem(path)?.isDirectory()) return;
      openProjectFile(path, state.filesSnapshot.revision);
    },
    renderRowDecoration: ({ item }) => {
      if (item.kind !== "directory") return { text: projectFileType(item.path) };
      return { text: projectDirectoryCountLabel(item.path, paths) };
    },
    unsafeCSS: projectFileTreeUnsafeCss(),
  });
  tree.render({ containerWrapper: mount });
  let resizeActive = true;
  const resize = () => {
    if (!resizeActive) return;
    mount.style.height = `${Math.min(640, Math.max(80, tree.getVisibleCount() * 40 + 2))}px`;
  };
  resize();
  const unsubscribeResize = tree.subscribe(() => window.requestAnimationFrame(resize));
  state.filesTreeSubscription = () => {
    resizeActive = false;
    unsubscribeResize();
  };
  const container = tree.getFileTreeContainer();
  if (container) {
    const styles = {
      ...window.TinFilesTree.themeToTreeStyles(tinFilesTheme()),
      width: "100%",
      height: "100%",
      background: "var(--paper-bg)",
      border: "0",
      fontFamily: '"Geist Mono", "SFMono-Regular", Consolas, monospace',
      "--trees-bg-override": "var(--paper-bg)",
      "--trees-bg-muted-override": "var(--tree-hover)",
      "--trees-selected-bg-override": "var(--tree-active)",
      "--trees-selected-fg-override": "var(--ink)",
      "--trees-fg-override": "var(--ink)",
      "--trees-fg-muted-override": "var(--ink-muted)",
      "--trees-border-color-override": "var(--tree-border)",
      "--trees-focus-ring-width-override": "0px",
      "--trees-font-family-override": '"Geist Mono", "SFMono-Regular", Consolas, monospace',
      "--trees-font-size-override": "13px",
      "--trees-item-height": "40px",
      "--trees-item-padding-x-override": "0px",
      "--trees-item-margin-x-override": "0px",
      "--trees-item-row-gap-override": "8px",
      "--trees-level-gap-override": "12px",
      "--trees-padding-inline-override": "0px",
    };
    Object.entries(styles).forEach(([name, value]) => container.style.setProperty(name, value));
  }
  state.filesTree = tree;
  decorateProjectFileTree();
}

function disposeFilesTree() {
  state.filesTreeObserver?.disconnect();
  state.filesTreeObserver = null;
  state.filesTreeSubscription?.();
  state.filesTreeSubscription = null;
  state.filesTree?.cleanUp();
  state.filesTree = null;
}

function highlightFileMatch(filename, query) {
  const source = String(filename);
  const index = source.toLowerCase().indexOf(String(query).toLowerCase());
  if (index < 0) return escapeHtml(source);
  return `${escapeHtml(source.slice(0, index))}<mark>${escapeHtml(source.slice(index, index + query.length))}</mark>${escapeHtml(source.slice(index + query.length))}`;
}

function filesSearchResults(paths, query) {
  let matches = paths.filter((path) => path.toLowerCase().includes(query.toLowerCase()));
  if (state.filesTree) {
    state.filesTree.setSearch(query || null);
    const libraryMatches = new Set(state.filesTree.getSearchMatchingPaths());
    matches = matches.filter((path) => libraryMatches.has(path));
  }
  matches.sort((left, right) => left.localeCompare(right));
  if (!matches.length) return '<p class="files-empty">No project files match that search.</p>';
  return `<p class="files-match-count">${matches.length} match${matches.length === 1 ? "" : "es"} · paths only</p>
    <div class="files-search-ledger">${matches.map((path) => {
      const parts = path.split("/");
      const filename = parts.pop();
      return `<button type="button" data-project-file="${escapeHtml(path)}">
        <span>${projectFileIcon(path)}<strong>${highlightFileMatch(filename, query)}</strong></span>
        <code>${escapeHtml(parts.length ? `${parts.join("/")}/` : "Project /")}</code>
        <small>${escapeHtml(projectFileType(path))}</small>
      </button>`;
    }).join("")}</div>`;
}

function filesBreadcrumb(directory) {
  const segments = directory ? directory.split("/").filter(Boolean) : [];
  const crumbs = ['<button type="button" data-files-directory="">Project</button>'];
  segments.forEach((segment, index) => {
    const path = segments.slice(0, index + 1).join("/");
    crumbs.push(`<span>/</span><button type="button" data-files-directory="${escapeHtml(path)}">${escapeHtml(segment)}</button>`);
  });
  return crumbs.join("");
}

function filesDrillName(entry) {
  const name = String(entry.name);
  if (entry.kind === "directory") {
    return `<span class="files-drill-name" title="${escapeHtml(name)}/"><span>${escapeHtml(name)}</span><b>/</b></span>`;
  }
  const dot = name.lastIndexOf(".");
  const hasExtension = dot > 0;
  const tailLength = hasExtension ? name.length - dot : Math.min(8, Math.floor(name.length / 2));
  const splitAt = Math.max(1, name.length - tailLength);
  return `<span class="files-drill-name" title="${escapeHtml(name)}"><span>${escapeHtml(name.slice(0, splitAt))}</span><b>${escapeHtml(name.slice(splitAt))}</b></span>`;
}

function renderFilesDrilldown() {
  const container = document.querySelector("#files-mobile-drill");
  if (!container || !state.filesSnapshot) return;
  const paths = state.filesSnapshot.files.map((item) => item.path);
  const entries = projectDirectoryEntries(paths, state.filesDirectory);
  container.innerHTML = `<nav class="files-breadcrumb" aria-label="Project path">${filesBreadcrumb(state.filesDirectory)}</nav>
    <div class="files-drill-rows">${entries.map((entry) => `<button type="button" ${entry.kind === "directory" ? `data-files-directory="${escapeHtml(entry.path)}"` : `data-project-file="${escapeHtml(entry.path)}"`}>
      ${projectFileIcon(entry.path, entry.kind === "directory")}
      ${filesDrillName(entry)}
      <small>${entry.kind === "directory" ? projectDirectoryCountLabel(entry.path, paths) : escapeHtml(projectFileType(entry.path))}</small>
    </button>`).join("")}</div>`;
  bindProjectFileLinks(container);
  container.querySelectorAll("[data-files-directory]").forEach((button) => {
    button.addEventListener("click", () => {
      state.filesDirectory = button.dataset.filesDirectory;
      renderFilesDrilldown();
    });
  });
}

function bindProjectFileLinks(root = document) {
  root.querySelectorAll("[data-project-file]").forEach((button) => {
    button.addEventListener("click", () => {
      if (!state.filesSnapshot) return;
      openProjectFile(button.dataset.projectFile, state.filesSnapshot.revision);
    });
  });
}

function updateFilesSearch() {
  if (!state.filesSnapshot) return;
  const query = state.filesSearch.trim();
  const treeRegion = document.querySelector("#files-tree-region");
  const drill = document.querySelector("#files-mobile-drill");
  const results = document.querySelector("#files-search-results");
  if (!treeRegion || !drill || !results) return;
  treeRegion.hidden = Boolean(query);
  drill.hidden = Boolean(query);
  results.hidden = !query;
  if (!query) {
    state.filesTree?.setSearch(null);
    renderFilesDrilldown();
    return;
  }
  results.innerHTML = filesSearchResults(
    state.filesSnapshot.files.map((item) => item.path),
    query,
  );
  bindProjectFileLinks(results);
}

function filesProposedTask() {
  return state.runs.find(
    (run) =>
      run.workflow_name === "project.task" &&
      run.status === "needs_input" &&
      run.task_phase === "review" &&
      run.task_has_changes === true,
  );
}

function renderFiles() {
  if (!state.filesSnapshot) {
    if (state.filesError) {
      main.innerHTML = `<div class="view-error"><div><strong>Tin could not load project files.</strong>${escapeHtml(state.filesError.message)}<button class="button-secondary" type="button" data-retry-files>Retry</button></div></div>`;
      document.querySelector("[data-retry-files]")?.addEventListener("click", () => loadProjectFiles());
      return;
    }
    main.innerHTML = '<div class="view-loading">Loading project files…</div>';
    if (!state.filesLoading) loadProjectFiles();
    return;
  }
  const paths = state.filesSnapshot.files.map((item) => item.path);
  const proposedTask = filesProposedTask();
  main.innerHTML = `<section class="product-view files-view">
    ${TIN_FILE_ICON_SPRITE}
    <div class="files-grid">
      <div class="files-primary">
        <header class="files-header">
          <h1>Files</h1>
          <label class="search-field files-search-field">
            <svg aria-hidden="true" viewBox="0 0 13 13"><circle cx="5.5" cy="5.5" r="4"/><path d="m8.5 8.5 3.5 3.5"/></svg>
            <input id="files-search" type="search" placeholder="Search project files…" value="${escapeHtml(state.filesSearch)}" />
          </label>
          <code>canonical · ${escapeHtml(shortRevision(state.filesSnapshot.revision))}</code>
        </header>
        ${state.filesUpdated ? '<p class="files-updated">Project files updated · <button type="button" data-files-activity>View in Activity</button></p>' : ""}
        ${paths.length ? `<div id="files-tree-region"><div id="project-file-tree" class="project-file-tree"></div></div>
          <div id="files-mobile-drill" class="files-mobile-drill"></div>
          <div id="files-search-results" class="files-search-results" hidden></div>` : '<p class="files-empty files-empty-project">No project files yet. Completed workflows and approved task changes will appear here.</p>'}
      </div>
      <aside class="files-state-panel">
        <h2>Project state</h2>
        <code>canonical · ${escapeHtml(shortRevision(state.filesSnapshot.revision))}</code>
        <code>${paths.length} file${paths.length === 1 ? "" : "s"}</code>
        <p>Workflows and Codex runs begin from this shared state.</p>
        <button type="button" data-files-activity>View changes in Activity →</button>
        ${proposedTask ? `<div class="files-proposed-task">
          <code>1 task has proposed changes</code>
          <button type="button" data-files-task="${escapeHtml(proposedTask.id)}">Review task →</button>
          <p>Proposed files stay on the task page until you approve them.</p>
        </div>` : ""}
      </aside>
    </div>
  </section>`;
  if (paths.length) {
    mountProjectFileTree();
    renderFilesDrilldown();
    updateFilesSearch();
    const search = document.querySelector("#files-search");
    search.addEventListener("input", () => {
      state.filesSearch = search.value;
      updateFilesSearch();
    });
  }
  document.querySelectorAll("[data-files-activity]").forEach((button) => {
    button.addEventListener("click", () => {
      state.filesUpdated = false;
      state.activityFilter = "all";
      navigate("activity");
    });
  });
  document.querySelector("[data-files-task]")?.addEventListener("click", (event) => {
    openTask(event.currentTarget.dataset.filesTask);
  });
}

async function loadProjectFiles({ silent = false, renderWhenReady = true } = {}) {
  if (!state.project || state.filesLoading) return;
  const context = currentProjectContext();
  state.filesLoading = true;
  if (!silent) state.filesError = null;
  const previousRevision = state.filesSnapshot?.revision || null;
  try {
    const snapshot = await api(`/api/projects/${encodeURIComponent(context.projectId)}/files`);
    if (!isCurrentProjectContext(context)) return;
    state.filesSnapshot = snapshot;
    state.filesError = null;
    if (previousRevision && previousRevision !== snapshot.revision) state.filesUpdated = true;
  } catch (error) {
    if (!isCurrentProjectContext(context)) return;
    if (!silent) state.filesError = error;
  } finally {
    if (!isCurrentProjectContext(context)) return;
    state.filesLoading = false;
    if (renderWhenReady && state.view === "files") render();
  }
}

function projectFileRawUrl(route, download = false, projectId = state.project.id) {
  if (route.source === "retained" && route.compareRun) return outputReadUrl(route.compareRun, "retained");
  const query = new URLSearchParams({ path: route.path, revision: route.revision });
  if (download) query.set("download", "true");
  return `/api/projects/${encodeURIComponent(projectId)}/files/raw?${query.toString()}`;
}

function formatFileSize(bytes) {
  if (bytes < 1024) return `${bytes} b`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} kb`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} mb`;
}

async function downloadProjectFile(route, open = false) {
  try {
    const response = await authorizedFetch(projectFileRawUrl(route, !open), {
      headers: { Accept: "*/*" },
    });
    // HTTP sandbox/disposition headers do not survive a Blob URL. Raw views
    // must be plain text, and download URLs must never become active documents.
    const blob = new Blob([await response.arrayBuffer()], {
      type: open ? "text/plain;charset=utf-8" : "application/octet-stream",
    });
    const blobUrl = URL.createObjectURL(blob);
    if (open) {
      window.open(blobUrl, "_blank", "noopener,noreferrer");
      window.setTimeout(() => URL.revokeObjectURL(blobUrl), 60000);
      return;
    }
    const link = document.createElement("a");
    link.href = blobUrl;
    link.download = route.path.split("/").at(-1) || "project-file";
    link.click();
    URL.revokeObjectURL(blobUrl);
  } catch (error) {
    showToast(`Could not open raw file: ${error.message}`);
  }
}

function bindFileContextActions(route, returnView = "files") {
  const back = document.querySelector("[data-back-files]");
  if (route.compareRun && back) back.textContent = `← ${comparisonFileReturnLabel(route)}`;
  back?.addEventListener("click", () => route.compareRun || route.backTo ? returnFromComparisonFile(route) : navigate(returnView));
  document.querySelector("[data-copy-file-path]")?.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(route.path);
      showToast("Path copied.");
    } catch (_error) {
      showToast("Tin could not copy that path.");
    }
  });
  document.querySelector("[data-download-file]")?.addEventListener("click", () => downloadProjectFile(route));
}

async function projectFileRawText(route) {
  const response = await authorizedFetch(projectFileRawUrl(route), {
    headers: { Accept: "*/*" },
  });
  const bytes = await response.arrayBuffer();
  return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
}

function renderDelimitedProjectFile(route, file) {
  const table = file.table;
  const extension = route.path.split(".").at(-1)?.toLowerCase();
  const type = table.delimiterLabel || (extension === "tsv" ? "tsv" : "csv");
  const facts = table.error
    ? `${type} · ${formatFileSize(file.bytes)} · ${comparisonFileLabel(route)} · ${shortRevision(route.revision)}`
    : `${type} · ${table.totalRows} rows · ${table.headers.length} columns · ${formatFileSize(file.bytes)} · ${comparisonFileLabel(route)} · ${shortRevision(route.revision)}`;
  state.documentCleanup = window.TinDelimitedViewer.mount(main, file, {
    contextLabel: route.path,
    factsText: facts,
    returnLabel: comparisonFileReturnLabel(route),
    onReturn: () => returnFromComparisonFile(route),
    onDownload: () => downloadProjectFile(route),
    onCopy: async (button) => {
      button.disabled = true;
      try {
        const text = file.rawText === null ? await projectFileRawText(route) : file.rawText;
        await navigator.clipboard.writeText(text);
        showToast("Raw file copied.");
      } catch (_error) {
        showToast("Tin could not copy this file.");
      } finally {
        button.disabled = false;
      }
    },
    onToggleMode: async (button) => {
      if (table.error) {
        showToast("Tin couldn't read this as a table.");
        return;
      }
      button.disabled = true;
      try {
        if (file.mode !== "raw" && file.rawText === null) {
          file.rawText = await projectFileRawText(route);
        }
        file.mode = file.mode === "raw" ? "table" : "raw";
        render();
      } catch (_error) {
        button.disabled = false;
        showToast("Tin could not open the raw file.");
      }
    },
  });
}

function renderJsonProjectFile(route, file) {
  // Keep controls on this opening of the file, outside the durable content cache.
  route.jsonView ??= window.TinJsonViewer.createViewState(file);
  state.documentCleanup = window.TinJsonViewer.mount(main, file, {
    view: route.jsonView,
    contextLabel: route.path,
    sizeLabel: `${file.tooLarge && !file.exactSize ? "over " : ""}${formatFileSize(file.bytes)}`,
    revisionLabel: route.compareRun
      ? `${comparisonFileLabel(route)} · ${shortRevision(route.revision)}`
      : shortRevision(route.revision),
    returnLabel: comparisonFileReturnLabel(route),
    onReturn: () => returnFromComparisonFile(route),
    onDownload: () => downloadProjectFile(route),
    onCopy: async (button) => {
      button.disabled = true;
      try {
        await navigator.clipboard.writeText(file.text);
        showToast("Raw file copied.");
      } catch (_error) {
        showToast("Tin could not copy this file.");
      } finally {
        button.disabled = false;
      }
    },
  });
}

function renderTextProjectFile(route, file) {
  const type = projectFileType(route.path);
  const facts = `${type} · ${formatFileSize(file.bytes)} · ${comparisonFileLabel(route)} · ${shortRevision(route.revision)}`;
  const returnLabel = comparisonFileReturnLabel(route);
  main.innerHTML = `<section class="project-file-view">
    ${TIN_FILE_ICON_SPRITE}
    <header class="project-file-context">
      <button type="button" data-back-files>← ${escapeHtml(returnLabel)}</button><span></span>
      <code class="project-file-path">${escapeHtml(route.path)}</code>
      <code class="project-file-facts">${escapeHtml(facts)}</code>
      <button type="button" data-copy-file-path>Copy</button>
      <button type="button" data-download-file>Download raw</button>
    </header>
    <div class="project-file-body">
      ${file.text === null ? `<article class="project-file-information">
        ${projectFileIcon(route.path)}
        <div><strong>${escapeHtml(route.path.split("/").at(-1))}</strong><code>${escapeHtml(route.path)}</code><span>${escapeHtml(type)} · ${escapeHtml(formatFileSize(file.bytes))}</span></div>
      </article>` : `<article class="project-file-text"><pre>${escapeHtml(file.text)}</pre></article>`}
    </div>
  </section>`;
  bindFileContextActions(route, returnLabel);
}

const TEXT_FILE_EXTENSIONS = new Set([
  "json", "jsonl", "txt", "text", "log", "mmd", "md", "markdown", "rst", "tex", "xml", "svg",
  "js", "mjs", "cjs", "jsx", "ts", "tsx", "py", "rb", "go", "rs", "java", "kt", "swift", "c", "h",
  "cpp", "hpp", "cs", "php", "sh", "bash", "zsh", "ps1", "sql", "graphql", "proto",
  "css", "scss", "less", "html", "htm", "vue", "svelte", "yaml", "yml", "toml", "ini", "cfg",
  "conf", "env", "properties", "csv", "tsv", "diff", "patch",
]);

// Files the studio workflows produce: fetched with the session header before preview.
const MEDIA_FILE_TYPES = {
  svg: "image/svg+xml", png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif",
  webp: "image/webp", avif: "image/avif", bmp: "image/bmp", ico: "image/x-icon",
  mp4: "video/mp4", m4v: "video/mp4", webm: "video/webm", mov: "video/quicktime", ogv: "video/ogg",
  mp3: "audio/mpeg", wav: "audio/wav", ogg: "audio/ogg", oga: "audio/ogg", opus: "audio/ogg",
  m4a: "audio/mp4", aac: "audio/aac", flac: "audio/flac", weba: "audio/webm",
  pdf: "application/pdf",
};

async function projectMediaUrl(blob) {
  if (blob.type !== "image/svg+xml") return URL.createObjectURL(blob);
  // SVG is inert in <img>, but opening a same-origin SVG Blob as a document
  // enables its scripts. Data URLs preserve vector previews with an opaque
  // origin, including when the user opens the image outside the viewer.
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error("Tin could not load this image."));
    reader.readAsDataURL(blob);
  });
}

function mediaReviewLabel(reviewRun, mediaType) {
  if (reviewRun?.workflow_name === "creative.character") return "Approve character";
  if (mediaType.startsWith("video/")) return "Approve video";
  if (mediaType.startsWith("audio/")) return "Approve audio";
  if (mediaType === "application/pdf") return "Approve document";
  return "Approve image";
}

function renderMediaProjectFile(route, file) {
  const type = projectFileType(route.path);
  const facts = `${type} · ${formatFileSize(file.bytes)} · ${comparisonFileLabel(route)} · ${shortRevision(route.revision)}`;
  const reviewRun = route.reviewRunId
    ? state.runs.find((item) => item.id === route.reviewRunId && item.status === "needs_input")
    : null;
  const isVideo = file.mediaType.startsWith("video/");
  const isAudio = file.mediaType.startsWith("audio/");
  const isPdf = file.mediaType === "application/pdf";
  const reviewLabel = mediaReviewLabel(reviewRun, file.mediaType);
  const filename = route.path.split("/").at(-1);
  const element = isVideo
    ? `<video class="project-media-video" controls playsinline preload="metadata" src="${file.url}"></video>`
    : isAudio
      ? `<audio class="project-media-audio" controls src="${file.url}"></audio>`
      : isPdf
        ? `<iframe class="project-media-pdf" title="${escapeHtml(filename)}" src="${file.url}"></iframe>`
        : `<img class="project-media-image" alt="${escapeHtml(filename)}" src="${file.url}">`;
  const returnLabel = comparisonFileReturnLabel(route, reviewRun);
  main.innerHTML = `<section class="project-file-view">
    ${TIN_FILE_ICON_SPRITE}
    <header class="project-file-context">
      <button type="button" data-back-files>← ${escapeHtml(returnLabel)}</button><span></span>
      <code class="project-file-path">${escapeHtml(route.path)}</code>
      <code class="project-file-facts">${escapeHtml(facts)}</code>
      <button type="button" data-copy-file-path>Copy path</button>
      <button type="button" data-download-file>Download</button>
      ${reviewRun ? `<button class="project-diagram-approve" type="button" data-approve-diagram-run="${escapeHtml(reviewRun.id)}">${reviewLabel}</button>` : ""}
    </header>
    <div class="project-file-body project-media-body">
      <article class="project-media-canvas ${isVideo ? "is-video" : ""} ${isPdf ? "is-document" : ""}">${element}</article>
    </div>
  </section>`;
  bindFileContextActions(route, returnLabel);
  document.querySelector("[data-approve-diagram-run]")?.addEventListener("click", (event) => {
    approveRun(event.currentTarget.dataset.approveDiagramRun, event.currentTarget);
  });
}

function renderDiagramProjectFile(route, file) {
  const facts = `mermaid · ${formatFileSize(file.bytes)} · ${comparisonFileLabel(route)} · ${shortRevision(route.revision)}`;
  const mode = route.diagramMode || "diagram";
  const reviewRun = route.reviewRunId
    ? state.runs.find((item) => item.id === route.reviewRunId && item.status === "needs_input")
    : null;
  const returnLabel = comparisonFileReturnLabel(route, reviewRun);
  main.innerHTML = `<section class="project-file-view project-diagram-view">
    ${TIN_FILE_ICON_SPRITE}
    <header class="project-file-context">
      <button type="button" data-back-files>← ${escapeHtml(returnLabel)}</button><span></span>
      <code class="project-file-path">${escapeHtml(route.path)}</code>
      <code class="project-file-facts">${escapeHtml(facts)}</code>
      <button type="button" data-copy-file-path>Copy path</button>
      <button type="button" data-download-file>Download source</button>
      ${reviewRun ? `<button class="project-diagram-approve" type="button" data-approve-diagram-run="${escapeHtml(reviewRun.id)}">Approve diagram</button>` : ""}
    </header>
    <div class="project-file-body project-diagram-body">
      <div class="project-diagram-controls">
        <div class="project-diagram-toolbar" role="group" aria-label="Diagram view">
          ${["diagram", "source", "ascii"].map((value) => `<button class="${mode === value ? "is-active" : ""}" aria-pressed="${mode === value}" type="button" data-diagram-mode="${value}">${value === "ascii" ? "ASCII" : humanize(value)}</button>`).join("")}
        </div>
        <div class="project-diagram-zoom" role="group" aria-label="Diagram zoom" ${mode !== "diagram" ? "hidden" : ""}>
          <button type="button" data-diagram-zoom="out" aria-label="Zoom out" title="Zoom out (−)" disabled>−</button>
          <output aria-label="Zoom level">—</output>
          <button type="button" data-diagram-zoom="in" aria-label="Zoom in" title="Zoom in (+)" disabled>+</button>
          <button type="button" data-diagram-zoom="actual" aria-label="Actual size" title="Actual size (1)" disabled>100%</button>
          <button type="button" data-diagram-zoom="fit" aria-label="Fit diagram" title="Fit diagram (0)" aria-pressed="true" disabled>Fit</button>
        </div>
      </div>
      <article class="project-diagram-canvas tin-diagram" data-project-diagram><span>Drawing diagram…</span></article>
      <p class="project-diagram-help" id="diagram-navigation-help" ${mode !== "diagram" ? "hidden" : ""}>Drag to pan · Pinch or Ctrl/⌘ + scroll to zoom<span> · Arrow keys to pan, +/− to zoom, 0 to fit</span></p>
    </div>
  </section>`;
  bindFileContextActions(route, returnLabel);
  document.querySelector("[data-approve-diagram-run]")?.addEventListener("click", (event) => {
    approveRun(event.currentTarget.dataset.approveDiagramRun, event.currentTarget);
  });
  document.querySelectorAll("[data-diagram-mode]").forEach((button) => {
    button.addEventListener("click", () => {
      route.diagramMode = button.dataset.diagramMode;
      render();
    });
  });
  const canvas = document.querySelector("[data-project-diagram]");
  if (mode === "source") {
    canvas.classList.add("is-source");
    canvas.innerHTML = `<pre>${escapeHtml(file.source)}</pre>`;
    return;
  }
  loadDiagramRenderer()
    .then(async (renderer) => {
      if (!canvas.isConnected) return;
      if (mode === "ascii") {
        canvas.classList.add("is-ascii");
        canvas.innerHTML = `<pre>${escapeHtml(renderer.renderASCII(file.source))}</pre>`;
      } else {
        const svg = await renderer.renderSource(file.source);
        if (!canvas.isConnected) return;
        canvas.innerHTML = svg;
        route.diagramView ??= { mode: "fit" };
        state.documentCleanup = window.TinDiagramViewport.mount(canvas, {
          controls: document.querySelector(".project-diagram-zoom"), view: route.diagramView,
        });
      }
    })
    .catch((error) => {
      if (!canvas.isConnected) return;
      canvas.innerHTML = `<div class="project-diagram-error"><strong>Tin could not draw this diagram.</strong><span>${escapeHtml(error.message)}</span></div>`;
    });
}

function renderProjectFileFailure(route, error) {
  const changed = error?.status === 404;
  main.innerHTML = `<div class="viewer-error project-file-error">
    <strong>${changed ? "This file changed with the project. Open the latest version." : "Tin could not open this file."}</strong>
    <button class="button-secondary" type="button" data-file-retry>${changed ? "Open latest version" : "Retry"}</button>
  </div>`;
  document.querySelector("[data-file-retry]")?.addEventListener("click", async () => {
    if (!changed) {
      state.fileError = null;
      state.fileCache.delete(`${route.revision}:${route.path}`);
      renderFile();
      return;
    }
    await loadProjectFiles({ silent: true });
    const latest = state.filesSnapshot;
    if (latest?.files.some((item) => item.path === route.path)) openProjectFile(route.path, latest.revision);
    else {
      state.filesSearch = route.path.split("/").at(-1) || "";
      navigate("files");
    }
  });
}

function renderFile() {
  const route = state.fileRoute;
  if (!route || !state.project) {
    navigate("files");
    return;
  }
  const key = `${route.revision}:${route.path}`;
  const cached = state.fileCache.get(key);
  if (cached?.kind === "markdown") {
    state.documentCleanup = window.TinMarkdownViewer.mount(main, cached.document, {
      mode: "in-app",
      contextLabel: route.path,
      factsText: `markdown · ${comparisonFileLabel(route)} · ${shortRevision(route.revision)}`,
      returnTo: { label: comparisonFileReturnLabel(route), onActivate: () => returnFromComparisonFile(route) },
      rawAction: { label: "View raw", onActivate: () => downloadProjectFile(route, true) },
    });
    if (route.compareRun) main.querySelector(".markdown-viewer")?.classList.add("is-comparison-reader");
    return;
  }
  if (cached?.kind === "file") {
    renderTextProjectFile(route, cached);
    return;
  }
  if (cached?.kind === "delimited") {
    renderDelimitedProjectFile(route, cached);
    return;
  }
  if (cached?.kind === "json") {
    renderJsonProjectFile(route, cached);
    return;
  }
  if (cached?.kind === "diagram") {
    renderDiagramProjectFile(route, cached);
    return;
  }
  if (cached?.kind === "media") {
    renderMediaProjectFile(route, cached);
    return;
  }
  if (state.fileError?.key === key) {
    renderProjectFileFailure(route, state.fileError.error);
    return;
  }
  main.innerHTML = '<div class="viewer-loading">Loading project files…</div>';
  if (state.fileLoadingKey === key) return;
  const context = currentProjectContext();
  state.fileLoadingKey = key;
  const extension = route.path.split(".").at(-1)?.toLowerCase();
  const endpointQuery = new URLSearchParams({ path: route.path, revision: route.revision });
  const load = ["md", "markdown"].includes(extension)
    ? api(route.source === "retained" && route.compareRun
        ? `/api/workflows/runs/${route.compareRun}/artifact/document?source=retained`
        : `/api/projects/${encodeURIComponent(context.projectId)}/files/document?${endpointQuery.toString()}`)
        .then((document) => ({ kind: "markdown", document }))
    : authorizedFetch(projectFileRawUrl(route, false, context.projectId), { headers: { Accept: "*/*" } }).then(async (response) => {
        if (["csv", "tsv"].includes(extension)) {
          return window.TinDelimitedViewer.readResponse(response);
        }
        if (extension === "json") {
          return window.TinJsonViewer.readResponse(response);
        }
        const bytes = await response.arrayBuffer();
        if (MEDIA_FILE_TYPES[extension]) {
          const blob = new Blob([bytes], { type: MEDIA_FILE_TYPES[extension] });
          return { kind: "media", url: await projectMediaUrl(blob), bytes: bytes.byteLength, mediaType: MEDIA_FILE_TYPES[extension] };
        }
        if (extension === "mmd") {
          return {
            kind: "diagram",
            source: new TextDecoder("utf-8", { fatal: true }).decode(bytes),
            bytes: bytes.byteLength,
            mode: "diagram",
          };
        }
        const contentType = response.headers.get("content-type") || "";
        let text = null;
        const textLike = contentType.startsWith("text/") || /(json|javascript|xml|yaml)/i.test(contentType) ||
          TEXT_FILE_EXTENSIONS.has(extension);
        if (textLike) {
          try {
            text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
          } catch (_error) {
            text = null;
          }
        }
        return { kind: "file", text, bytes: bytes.byteLength, contentType };
      });
  load
    .then((file) => {
      if (!isCurrentProjectContext(context)) return;
      state.fileCache.set(key, file);
      state.fileError = null;
      if (state.fileRoute?.path === route.path && state.fileRoute?.revision === route.revision) render();
    })
    .catch((error) => {
      if (!isCurrentProjectContext(context)) return;
      state.fileError = { key, error };
      if (state.fileRoute?.path === route.path && state.fileRoute?.revision === route.revision) render();
    })
    .finally(() => {
      if (isCurrentProjectContext(context) && state.fileLoadingKey === key) state.fileLoadingKey = null;
    });
}

function renderConnectRequest(requested) {
  const items = requested
    .map((key) => state.integrations.find((integration) => integration.key === key))
    .filter(Boolean);
  const done = items.filter((integration) => integration.connection_id && integration.status === "connected" &&
    (!RESOURCE_SCOPED_INTEGRATIONS.has(integration.key) || integrationSelection(integration)));
  const projectName = escapeHtml(state.project?.name || "your project");
  main.innerHTML = `<section class="product-view integrations-view connect-view">
    <header class="workspace-header">
      <div>
        <h1>Connect for ${projectName}</h1>
        <p class="connect-intro">Your agent asked for ${items.length === 1 ? "this connection" : `these ${items.length} connections`}. Each takes about a minute in the browser${items.some((i) => i.key === "infra.github") ? "; GitHub also asks which repository" : ""}.</p>
      </div>
      <span class="header-spacer"></span>
      <button class="button-quiet" type="button" data-connect-show-all>${state.projectAccess === "locked" ? "Back to setup" : "All integrations"}</button>
    </header>
    <div class="integration-list">
      ${items.length ? items.map(renderIntegrationCard).join("") : `<div class="integration-no-results"><strong>Nothing to connect.</strong><span>The link named no integration Tin offers.</span></div>`}
    </div>
    <footer class="connect-footer">
      ${done.length === items.length && items.length
        ? `<strong>All connected.</strong> Go back to your agent and say: connected.`
        : `${done.length} of ${items.length} connected. When you are done, go back to your agent and say: connected.`}
    </footer>
  </section>`;
  document.querySelector("[data-connect-show-all]").addEventListener("click", () => {
    clearConnectRequest();
    render();
  });
  bindIntegrationCardControls();
}

function bindIntegrationCardControls() {
  document.querySelectorAll("[data-integration-expand]").forEach((button) => {
    button.addEventListener("click", () => toggleIntegration(button.dataset.integrationExpand));
  });
  document.querySelectorAll("[data-integration-connect]").forEach((button) => {
    button.addEventListener("click", () => connectIntegration(button.dataset.integrationConnect));
  });
  document.querySelectorAll("[data-integration-upgrade]").forEach((button) => {
    button.addEventListener("click", () => connectIntegration(
      button.dataset.integrationUpgrade,
      ["gmail.messages.send"],
    ));
  });
  document.querySelectorAll("[data-integration-disconnect]").forEach((button) => {
    button.addEventListener("click", () => disconnectIntegration(button.dataset.integrationDisconnect));
  });
  document.querySelectorAll("[data-integration-refresh]").forEach((button) => {
    button.addEventListener("click", () => refreshGoogleAds(button));
  });
  document.querySelectorAll("[data-stripe-refresh]").forEach((button) => {
    button.addEventListener("click", () => refreshStripe(button));
  });
  document.querySelectorAll("[data-integration-form]").forEach((form) => {
    bindTinControls(form);
    const selection = form.querySelector('input[name="option_id"]');
    selection?.addEventListener("change", () => {
      if (selection.value) {
        saveIntegrationSelection(form.dataset.integrationForm, selection.value);
      }
    });
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      saveIntegrationSelection(form.dataset.integrationForm, new FormData(form).get("option_id"));
    });
  });
}

function renderIntegrations() {
  const requested = connectRequest();
  if (requested.length) return renderConnectRequest(requested);
  if (state.projectAccess === "locked") return renderLockPage();
  const search = state.integrationSearch.trim().toLowerCase();
  const catalog = [...state.integrations, CUSTOM_API_TEMPLATE];
  const visible = catalog.filter((integration) => {
    const matchesSearch = !search || [
      integration.name,
      integration.key,
      integration.description,
      ...(integration.unlocks || []),
    ].join(" ").toLowerCase().includes(search);
    const connected = Boolean(integration.connection_id);
    const matchesFilter = state.integrationFilter === "all" ||
      (state.integrationFilter === "connected" && connected && integration.status === "connected") ||
      (state.integrationFilter === "needs_attention" && integration.status === "needs_attention") ||
      (state.integrationFilter === "available" && !connected);
    return matchesSearch && matchesFilter;
  });
  const counts = {
    all: catalog.length,
    connected: catalog.filter((item) => item.connection_id && item.status === "connected").length,
    needs_attention: catalog.filter((item) => item.status === "needs_attention").length,
    available: catalog.filter((item) => !item.connection_id).length,
  };
  main.innerHTML = `<section class="product-view integrations-view">
    <header class="workspace-header">
      <h1>Integrations</h1>
      <span class="header-spacer"></span>
      <label class="search-field">
        <svg aria-hidden="true" viewBox="0 0 13 13"><circle cx="5.5" cy="5.5" r="4" /><path d="M8.5 8.5L12 12" /></svg>
        <input id="integration-search" type="search" placeholder="Search integrations…" value="${escapeHtml(state.integrationSearch)}" />
      </label>
    </header>
    <div class="filters integration-filters">
      ${[
        ["all", "All"],
        ["connected", "Connected"],
        ["needs_attention", "Needs you"],
        ["available", "Available"],
      ].map(([key, label]) => `<button class="filter ${state.integrationFilter === key ? "is-active" : ""}" type="button" data-integration-filter="${key}">${label} ${counts[key]}</button>`).join("")}
    </div>
    <div class="integration-list">
      ${visible.length ? visible.map(renderIntegrationCard).join("") : `<div class="integration-no-results"><strong>No integrations match.</strong><span>Try another search or filter.</span></div>`}
    </div>
  </section>`;

  document.querySelector("#integration-search").addEventListener("input", (event) => {
    state.integrationSearch = event.target.value;
    renderIntegrations();
    const field = document.querySelector("#integration-search");
    field.focus();
    field.setSelectionRange(field.value.length, field.value.length);
  });
  document.querySelectorAll("[data-integration-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      state.integrationFilter = button.dataset.integrationFilter;
      renderIntegrations();
    });
  });
  bindIntegrationCardControls();
}

function renderIntegrationCard(integration) {
  const expanded = state.expandedIntegration === integration.key;
  const connected = Boolean(integration.connection_id);
  const selected = integrationSelection(integration);
  const needsResource = connected && RESOURCE_SCOPED_INTEGRATIONS.has(integration.key) && !selected;
  const logoPaths = {
    "analytics.gsc": "/assets/integrations/google-search-console.svg",
    "infra.github": "/assets/integrations/github.svg",
    "workspace.google": "/assets/integrations/google-workspace.svg",
    "ads.google": "/assets/integrations/google-ads.svg",
    "payments.stripe": "/assets/integrations/stripe.svg",
    "analytics.posthog": "/assets/integrations/posthog.svg",
  };
  const logo = logoPaths[integration.key]
    ? `<img src="${logoPaths[integration.key]}" alt="" />`
    : escapeHtml(integration.badge);
  const primary = needsResource
    ? integration.key === "infra.github" ? "Choose a repository"
      : integration.key === "analytics.posthog" ? "Choose a PostHog project" : "Choose a Search property"
    : integration.key === "analytics.posthog"
      ? integration.external_account_label || "PostHog"
      : selected || integration.external_account_label || "Choose an account";
  const health = integration.key.startsWith("custom.api.")
    ? integration.configuration?.access_verified ? "access verified" : "saved · not verified"
    : integration.key === "ads.google" && connected
    ? googleAdsHealth(integration)
    : integration.key === "payments.stripe" && connected
    ? stripeHealth(integration)
    : needsResource
    ? "setup required"
    : integration.status === "needs_attention"
    ? "needs you"
    : integration.last_checked_at
      ? `checked ${timeLabel(integration.last_checked_at)}`
      : "ready for workflows";
  const unlocks = (integration.unlocks || []).join(" · ");
  return `<article class="integration-card ${connected ? "is-connected" : "is-available"} ${needsResource ? "is-needs-setup" : ""} ${expanded ? "is-expanded" : ""}">
    <div class="integration-card-row">
      <span class="integration-badge ${["infra.github", "payments.stripe", "analytics.posthog"].includes(integration.key) ? "is-monochrome" : ""}" aria-hidden="true">${logo}</span>
      <span class="integration-identity">
        <strong class="integration-name">${escapeHtml(integration.name)}</strong>
        <code class="integration-key">${escapeHtml(integration.key)}</code>
      </span>
      <span class="integration-state-mark is-${escapeHtml(integration.status)}" aria-hidden="true"></span>
      ${connected ? `<span class="integration-primary">${escapeHtml(primary)}</span>
        <span class="integration-health">${escapeHtml(health)}</span>
        <button class="integration-row-action" type="button" data-integration-expand="${escapeHtml(integration.key)}" aria-expanded="${expanded}">${needsResource ? "Finish setup" : "Configure"}</button>`
        : `<span class="integration-unlocks">would unlock ${escapeHtml(unlocks)}</span>
        <button class="integration-connect" type="button" data-integration-connect="${escapeHtml(integration.key)}">Connect</button>`}
    </div>
    ${expanded && connected ? renderIntegrationExpanded(integration) : ""}
  </article>`;
}

function googleAdsHealth(integration) {
  const link = integration.configuration?.link_status;
  const health = integration.configuration?.health || {};
  if (link === "pending") return "accept Tin's request in Google Ads";
  if (link && link !== "active") return "invitation " + link + " · send it again";
  if (!health.checked_at) return "linked · check the account";
  if (health.account_status && health.account_status !== "ENABLED") return "account not enabled";
  if (!health.billing_approved) return "billing missing in Google Ads";
  if (!health.conversion_actions_with_data) return "no conversions recorded yet";
  return "ready for workflows";
}

function stripeHealth(integration) {
  const config = integration.configuration || {};
  if (integration.status === "needs_attention") return "Stripe rejected the key · enter a new one";
  const mode = config.livemode ? "" : "test mode · ";
  if ((config.missing_permissions || []).length) return `${mode}some reads not allowed`;
  return `${mode}ready for workflows`;
}

const STRIPE_READS = {
  "subscriptions.read": "subscriptions",
  "customers.read": "customers",
  "invoices.read": "invoices",
  "prices.read": "prices",
  "charges.read": "charges",
};

function renderStripeExpanded(integration) {
  const config = integration.configuration || {};
  const granted = (config.granted_capabilities || []).map((item) => STRIPE_READS[item] || item);
  const missing = config.missing_permissions || [];
  const checkedLabel = integration.last_checked_at ? `checked ${timeLabel(integration.last_checked_at)}` : "not checked yet";
  const prompt = integration.status === "needs_attention"
    ? `<p class="integration-setup-prompt" role="status"><strong>Stripe rejected the stored key.</strong> It may have been deleted or expired. Enter a new restricted key.</p>`
    : missing.length
      ? `<p class="integration-setup-prompt" role="status">The key cannot read ${escapeHtml(missing.join(", "))}. Give it Read access to them in Stripe (Developers, API keys, edit the Tin key), then check again.</p>`
      : "";
  return `<div class="integration-expanded">
    ${prompt}
    <div class="integration-detail-row"><span class="integration-detail-label">Account</span><span class="integration-detail-value">${escapeHtml(integration.external_account_label || "Stripe")}</span></div>
    <div class="integration-detail-row"><span class="integration-detail-label">Mode</span><span class="integration-detail-value">${config.livemode ? "Live data" : "Test mode · sandbox data only"}</span></div>
    <div class="integration-detail-row">
      <span class="integration-detail-label">Access</span>
      <span class="integration-detail-value" title="Tin stores the restricted key encrypted and only ever reads with it. Workflows receive projected records, never the key. Full customer email addresses and names are included in customer records.">read only · ${escapeHtml(granted.join(", ") || "nothing yet")}</span>
    </div>
    <div class="integration-detail-row">
      <span class="integration-detail-label">Unlocks</span>
      <span class="integration-detail-value is-muted">${escapeHtml((integration.unlocks || []).join(" · "))}</span>
    </div>
    <div class="integration-control-footer">
      <span>connected ${escapeHtml(integration.connected_at ? timeLabel(integration.connected_at) : "recently")} · via restricted key · ${escapeHtml(checkedLabel)}</span>
      <button class="integration-reconnect" type="button" data-stripe-refresh>Check again</button>
      <button class="integration-reconnect" type="button" data-integration-connect="payments.stripe">Replace key</button>
      <button class="integration-disconnect" type="button" data-integration-disconnect="payments.stripe">Disconnect</button>
      <button class="integration-done" type="button" data-integration-expand="payments.stripe">Done</button>
    </div>
  </div>`;
}

function renderIntegrationExpanded(integration) {
  if (integration.key === "payments.stripe") return renderStripeExpanded(integration);
  const options = state.integrationOptions.get(integration.key);
  const selected = integrationSelection(integration) || "";
  const isPostHog = integration.key === "analytics.posthog";
  const optionLabel = integration.key === "infra.github" ? "Repository" : isPostHog ? "PostHog project" : "Search property";
  const isWorkspace = integration.key === "workspace.google";
  const isAds = integration.key === "ads.google";
  const setupPrompt = RESOURCE_SCOPED_INTEGRATIONS.has(integration.key) && !selected
    ? `<p class="integration-setup-prompt" role="status"><strong>Finish setup.</strong> OAuth is connected, but workflows cannot use ${escapeHtml(integration.name)} until you choose a ${escapeHtml(integrationResourceNoun(integration.key))} for ${escapeHtml(state.project.name)}.</p>`
    : integration.status === "needs_attention" && isPostHog
      ? `<p class="integration-setup-prompt" role="status"><strong>PostHog needs reconnecting.</strong> It no longer accepts Tin's access. Reconnect to keep reading ${escapeHtml(integration.external_account_label || "the project")}.</p>`
      : "";
  const workspaceCanSend = (integration.configuration?.granted_capabilities || []).includes("gmail.messages.send");
  const accessValue = integration.key === "infra.github"
    ? "selected repositories · Contents + Pull requests write"
    : isPostHog
      ? `read only · ${(integration.configuration?.granted_capabilities || []).map((item) => item.replace(".read", "")).join(", ") || "nothing yet"} · ${String(integration.configuration?.region || "").toUpperCase()} Cloud`
    : isAds
      ? "one linked account · campaign read + write via Tin's manager account"
    : isWorkspace
      ? workspaceCanSend
        ? "Gmail read + send · Calendar read"
        : "Gmail + Calendar read"
      : "read only · search performance + indexing signals";
  const accessCopy = integration.key === "infra.github"
    ? "By installing the Tin GitHub App, you opt in to Contents and Pull requests write access for only the repositories granted in GitHub. Tin uses short-lived installation tokens; it never stores a personal access token."
    : isPostHog
      ? "Tin stores encrypted PostHog OAuth tokens with read scopes only and reads the one project chosen here. Workflows receive small projected records and bounded query results, never the tokens. HogQL reads must be one SELECT with a LIMIT of at most 1000."
    : isAds
      ? "Tin's manager account is linked to your Google Ads account by an invitation you accept inside Google Ads. Tin stores no Google credential of yours. Every campaign change waits for your approval; removing the manager in Google Ads ends Tin's access at once."
    : isWorkspace
      ? workspaceCanSend
        ? "Tin stores an encrypted Google refresh token. Workflow sandboxes receive only short-lived, run-bound Tin tools and never receive Google credentials. Campaigns still require explicit review before Tin sends anything."
        : "Tin stores an encrypted Google refresh token. Workflow sandboxes receive only short-lived, run-bound Tin tools and never receive Google credentials. Enable sending to run approved email campaigns."
      : "Tin requests Search Console read-only access. It cannot edit your site, indexing settings, or Google account.";
  let selectionControl;
  if (isWorkspace) {
    selectionControl = `<div class="integration-detail-row"><span class="integration-detail-label">Account</span><span class="integration-detail-value">${escapeHtml(integration.external_account_label || "Google Workspace")}</span></div>`;
  } else if (isAds) {
    const link = integration.configuration?.link_status || "pending";
    const linkCopy = link === "active"
      ? "Linked to Tin's manager account."
      : link === "pending"
        ? "Invitation sent. In Google Ads open Admin, then Access and security, then Managers, and accept the request from Tin Computer."
        : `The invitation is ${escapeHtml(link)}. Send it again to link the account.`;
    selectionControl = `<div class="integration-detail-row"><span class="integration-detail-label">Account</span><span class="integration-detail-value">${escapeHtml(integration.external_account_label || "Google Ads")}</span></div>
      <p class="integration-setup-prompt" role="status">${linkCopy} <button class="integration-row-action" type="button" data-integration-refresh="${escapeHtml(integration.key)}">Check again</button>${link !== "active" && link !== "pending" ? ` <button class="integration-row-action" type="button" data-integration-connect="${escapeHtml(integration.key)}">Send again</button>` : ""}</p>`;
  } else if (state.integrationLoading === integration.key) {
    selectionControl = `<span class="integration-detail-value is-muted">Checking the connected account…</span>`;
  } else if (options) {
    const availableOptions = options.map((option) => [
      option.id,
      option.detail ? `${option.label} · ${option.detail}` : option.label,
    ]);
    const controlOptions = options.length
      ? [
          ...(!selected ? [["", `Choose a ${optionLabel.toLowerCase()}`]] : []),
          ...availableOptions,
        ]
      : [["", `No ${optionLabel.toLowerCase()} available`]];
    selectionControl = `<form class="integration-config-form" data-integration-form="${escapeHtml(integration.key)}">
      <label>${escapeHtml(optionLabel)}</label>
      ${tinSelectControl("option_id", selected, controlOptions, optionLabel)}
    </form>`;
  } else {
    selectionControl = `<span class="integration-detail-value is-muted">Choices unavailable. Close and reopen to retry.</span>`;
  }
  const connectedLabel = integration.connected_at ? timeLabel(integration.connected_at) : "recently";
  const checkedLabel = integration.last_checked_at
    ? `checked ${timeLabel(integration.last_checked_at)}`
    : "not checked yet";
  return `<div class="integration-expanded">
    ${setupPrompt}
    <div class="integration-selection-row">${selectionControl}</div>
    <div class="integration-detail-row">
      <span class="integration-detail-label">Access</span>
      <span class="integration-detail-value" title="${escapeHtml(accessCopy)}">${escapeHtml(accessValue)}</span>
    </div>
    <div class="integration-detail-row">
      <span class="integration-detail-label">Unlocks</span>
      <span class="integration-detail-value is-muted">${escapeHtml((integration.unlocks || []).join(" · "))}</span>
    </div>
    <div class="integration-control-footer">
      <span>connected ${escapeHtml(connectedLabel)} · via ${isAds ? "manager invitation" : "OAuth"} · ${escapeHtml(checkedLabel)}</span>
      ${isWorkspace && !workspaceCanSend ? `<button class="integration-reconnect" type="button" data-integration-upgrade="${escapeHtml(integration.key)}">Enable sending</button>` : ""}
      ${integration.status === "needs_attention" ? `<button class="integration-reconnect" type="button" data-integration-connect="${escapeHtml(integration.key)}">Reconnect</button>` : ""}
      <button class="integration-disconnect" type="button" data-integration-disconnect="${escapeHtml(integration.key)}">Disconnect</button>
      <button class="integration-done" type="button" data-integration-expand="${escapeHtml(integration.key)}">Done</button>
    </div>
  </div>`;
}

async function toggleIntegration(providerKey) {
  if (providerKey.startsWith("custom.api.")) {
    await openCustomApi(state.integrations.find(item => item.key === providerKey));
    return;
  }
  if (state.expandedIntegration === providerKey) {
    state.expandedIntegration = null;
    renderIntegrations();
    return;
  }
  state.expandedIntegration = providerKey;
  const integration = state.integrations.find((item) => item.key === providerKey);
  const context = currentProjectContext();
  if (integration?.connection_id && !["workspace.google", "ads.google", "payments.stripe"].includes(providerKey) && !state.integrationOptions.has(providerKey)) {
    state.integrationLoading = providerKey;
    renderIntegrations();
    try {
      const options = await api(`/api/projects/${encodeURIComponent(context.projectId)}/integrations/${encodeURIComponent(providerKey)}/options`);
      if (!isCurrentProjectContext(context)) return;
      state.integrationOptions.set(providerKey, options);
    } catch (error) {
      if (!isCurrentProjectContext(context)) return;
      showToast(`Could not load ${integration.name}: ${error.message}`);
    } finally {
      if (isCurrentProjectContext(context)) state.integrationLoading = null;
    }
  }
  if (!isCurrentProjectContext(context)) return;
  renderIntegrations();
}

async function openCustomApi(connection = null) {
  const context = currentProjectContext();
  if (!context.projectId) return;
  try {
    await window.TinProjectConnections.open({ api, projectId: context.projectId, connection, done: async () => {
      const integrations = await api(`/api/projects/${encodeURIComponent(context.projectId)}/integrations`);
      if (!isCurrentProjectContext(context)) return;
      state.integrations = integrations;
      renderIntegrations();
      showToast("API connection saved. Access is verified by a successful workflow request.");
    } });
  } catch (error) { showToast(error.message); }
}

async function promptForIntegrationResource(providerKey) {
  if (!RESOURCE_SCOPED_INTEGRATIONS.has(providerKey)) return;
  const integration = state.integrations.find((item) => item.key === providerKey);
  const selected = integrationSelection(integration);
  if (!integration?.connection_id || selected) return;
  if (providerKey === "infra.github") {
    await chooseGitHubRepository();
    return;
  }
  if (state.expandedIntegration !== providerKey) await toggleIntegration(providerKey);
  const form = document.querySelector(`[data-integration-form="${providerKey}"]`);
  if (!form) return;
  form.scrollIntoView({ block: "center", behavior: "smooth" });
  form.querySelector("[data-tin-select-trigger]")?.focus({ preventScroll: true });
  showToast(`Choose a ${integrationResourceNoun(providerKey)} to finish connecting ${integration.name}.`);
}

function renderIntegrationProjectOptions({ focusProjectId = null } = {}) {
  integrationProjectOptions.replaceChildren();
  for (const project of state.projects) {
    const selected = project.id === state.integrationConnectIntent?.projectId;
    const option = document.createElement("button");
    option.type = "button";
    option.className = `integration-project-option${selected ? " is-selected" : ""}`;
    option.dataset.integrationProjectId = project.id;
    option.setAttribute("role", "radio");
    option.setAttribute("aria-checked", String(selected));
    option.innerHTML = `<span><strong>${escapeHtml(project.name)}</strong>${project.id === state.project?.id ? "<small>Current project</small>" : ""}</span><i aria-hidden="true"></i>`;
    option.addEventListener("click", () => {
      state.integrationConnectIntent.projectId = project.id;
      renderIntegrationProjectOptions({ focusProjectId: project.id });
    });
    integrationProjectOptions.append(option);
  }
  if (focusProjectId) {
    integrationProjectOptions.querySelector(`[data-integration-project-id="${focusProjectId}"]`)?.focus();
  }
}

function chooseIntegrationProject(providerKey, capabilities) {
  const integration = state.integrations.find((item) => item.key === providerKey);
  if (!integration || !state.project) return;
  state.integrationConnectIntent = {
    providerKey,
    capabilities,
    projectId: state.project.id,
  };
  const nextStep = providerKey === "infra.github"
    ? "After GitHub authorizes Tin, you’ll choose the repository this project can use."
    : providerKey === "analytics.posthog"
      ? "PostHog then asks which one of your PostHog projects Tin may read."
      : "After Google authorizes Tin, you’ll choose the Search Console property this project can use.";
  integrationProjectTitle.textContent = `Connect ${integration.name} to a project`;
  integrationProjectCopy.textContent = `Connections are project-owned. Choose the Tin project for this connection. ${nextStep}`;
  integrationProjectForm.querySelector("[data-confirm-integration-project]").textContent = `Continue to ${integration.name}`;
  renderIntegrationProjectOptions();
  integrationProjectDialog.showModal();
  integrationProjectOptions.querySelector('[aria-checked="true"]')?.focus();
}

async function connectIntegration(providerKey, capabilities = null, targetProjectId = null) {
  if (providerKey === CUSTOM_API_TEMPLATE.key) {
    await openCustomApi();
    return;
  }
  const integration = state.integrations.find((item) => item.key === providerKey);
  if (!integration?.configured) {
    showToast(`${integration?.name || "This integration"} is not configured on this Tin deployment.`);
    return;
  }
  if (providerKey === "ads.google") {
    chooseGoogleAdsAccount(targetProjectId || currentProjectContext().projectId);
    return;
  }
  if (providerKey === "payments.stripe") {
    chooseStripeKey(targetProjectId || currentProjectContext().projectId);
    return;
  }
  if (
    !targetProjectId &&
    !integration.connection_id &&
    RESOURCE_SCOPED_INTEGRATIONS.has(providerKey)
  ) {
    chooseIntegrationProject(providerKey, capabilities);
    return;
  }
  const context = currentProjectContext();
  const projectId = targetProjectId || context.projectId;
  try {
    const result = await api(`/api/projects/${encodeURIComponent(projectId)}/integrations/${encodeURIComponent(providerKey)}/connect`, {
      method: "POST",
      ...(capabilities ? { body: JSON.stringify({ capabilities }) } : {}),
    });
    if (!isCurrentProjectContext(context)) return;
    if (
      targetProjectId &&
      (!state.integrationConnectIntent ||
        state.integrationConnectIntent.providerKey !== providerKey ||
        state.integrationConnectIntent.projectId !== targetProjectId)
    ) {
      return;
    }
    persistProjectSelection(projectId);
    if (state.projectAccess === "locked" || connectRequest().length) {
      rememberConnectRequest(projectId, [...connectRequest(projectId), providerKey]);
    }
    if (integrationProjectDialog.open) integrationProjectDialog.close();
    window.location.assign(result.authorization_url);
  } catch (error) {
    if (!isCurrentProjectContext(context)) return;
    showToast(`Could not connect ${integration.name}: ${error.message}`);
  }
}

async function saveIntegrationSelection(providerKey, optionId) {
  if (!optionId) return;
  const context = currentProjectContext();
  try {
    const updated = await api(`/api/projects/${encodeURIComponent(context.projectId)}/integrations/${encodeURIComponent(providerKey)}`, {
      method: "PUT",
      body: JSON.stringify({ option_id: optionId }),
    });
    if (!isCurrentProjectContext(context)) return;
    const index = state.integrations.findIndex((item) => item.key === providerKey);
    if (index >= 0) state.integrations[index] = updated;
    showToast(`${updated.name} configuration saved.`);
    renderIntegrations();
  } catch (error) {
    if (!isCurrentProjectContext(context)) return;
    showToast(`Could not save integration: ${error.message}`);
  }
}

async function disconnectIntegration(providerKey) {
  const integration = state.integrations.find((item) => item.key === providerKey);
  if (!window.confirm(`Disconnect ${integration?.name || "this integration"} from this project?`)) return;
  const context = currentProjectContext();
  try {
    await api(`/api/projects/${encodeURIComponent(context.projectId)}/integrations/${encodeURIComponent(providerKey)}`, { method: "DELETE" });
    if (!isCurrentProjectContext(context)) return;
    state.integrationOptions.delete(providerKey);
    state.expandedIntegration = null;
    const integrations = await api(`/api/projects/${encodeURIComponent(context.projectId)}/integrations`);
    if (!isCurrentProjectContext(context)) return;
    state.integrations = integrations;
    showToast(providerKey === "payments.stripe"
      ? "Stripe disconnected. Also delete the Tin key in Stripe under Developers, API keys."
      : `${integration.name} disconnected.`);
    renderIntegrations();
  } catch (error) {
    if (!isCurrentProjectContext(context)) return;
    showToast(`Could not disconnect ${integration.name}: ${error.message}`);
  }
}

function upsertRun(run) {
  const index = state.runs.findIndex((item) => item.id === run.id);
  if (index >= 0) state.runs[index] = run;
  else state.runs.unshift(run);
}

function updateMessageRuns() {
  const runs = new Map(state.runs.map((run) => [run.id, run]));
  for (const message of state.messages) {
    if (message.run && runs.has(message.run.id)) message.run = runs.get(message.run.id);
  }
}

function schedulePolling({ immediate = false } = {}) {
  window.clearTimeout(state.pollTimer);
  state.pollTimer = null;
  if (document.hidden || state.projectAccess !== "ready" || state.pollInFlight) return;
  const active =
    state.runs.some((run) => RUNNING_STATES.has(run.status) || (run.status === "succeeded" && ["pending", "started"].includes(run.content_delivery?.status))) ||
    state.pendingReviewRunIds.size > 0;
  state.pollTimer = window.setTimeout(pollRuns, immediate ? 0 : active ? 1800 : 5000);
}

function runFingerprint(run) {
  return [
    run.id,
    run.status,
    run.task_phase,
    run.task_summary,
    run.review_requested_at,
    run.canonical_commit_sha,
    run.retained_output?.revision,
    run.retained_output?.reason,
    JSON.stringify(run.output_resolution),
    JSON.stringify(run.content_delivery),
    run.error_message,
    run.progress_step,
    run.progress_current,
    run.progress_total,
    run.progress_percent,
    run.progress_summary,
    run.heartbeat_at,
  ].join(":");
}

function runsHaveChanged(previous, next) {
  if (previous.length !== next.length) return true;
  const before = new Map(previous.map((run) => [run.id, runFingerprint(run)]));
  return next.some((run) => before.get(run.id) !== runFingerprint(run));
}

function renderPolledRuns({ collectionsChanged }) {
  updateRail();
  if (state.view === "chat") renderChat();
  if (state.view === "workflows") renderWorkflows();
  if (state.view === "activity" && collectionsChanged) renderActivity();
  if (state.view === "decisions" && collectionsChanged) renderDecisions();
  if (state.view === "task" && document.activeElement?.id !== "task-message") renderTask();
}

async function pollRuns() {
  if (state.pollInFlight || document.hidden || state.projectAccess !== "ready" || !state.project) return;
  const generation = state.projectGeneration;
  const projectId = state.project.id;
  state.pollInFlight = true;
  try {
    const [results, systemSummary, decisions] = await Promise.all([
      api(`/api/projects/${encodeURIComponent(projectId)}/runs?limit=100`),
      api(`/api/projects/${encodeURIComponent(projectId)}/system`),
      api(`/api/projects/${encodeURIComponent(projectId)}/decisions`),
    ]);
    if (generation !== state.projectGeneration || state.project?.id !== projectId) return;
    const workflowsChanged = await loadRunWorkflows(results);
    if (generation !== state.projectGeneration || state.project?.id !== projectId) return;
    const collectionsChanged = workflowsChanged || runsHaveChanged(state.runs, results)
      || JSON.stringify(state.decisions) !== JSON.stringify(decisions);
    state.decisions = decisions;
    state.runs = results;
    state.systemSummary = systemSummary;
    for (const run of results) {
      if (["succeeded", "failed", "stopped"].includes(run.status)) state.pendingReviewRunIds.delete(run.id);
    }
    if (state.taskRoute?.runId) {
      const taskDetail = await api(`/api/tasks/${encodeURIComponent(state.taskRoute.runId)}`);
      if (generation !== state.projectGeneration || state.project?.id !== projectId) return;
      state.taskDetail = taskDetail;
      upsertRun(taskDetail.run);
    }
    updateMessageRuns();
    const expandedRun = state.expandedRun
      ? results.find((run) => run.id === state.expandedRun.runId)
      : null;
    if (expandedRun?.workflow_name === "outreach.email_campaign") {
      await loadRunDetails(expandedRun.id, true, false);
      if (generation !== state.projectGeneration || state.project?.id !== projectId) return;
    }
    if (collectionsChanged) {
      const encodedProjectId = encodeURIComponent(projectId);
      const refreshes = [
        api(`/api/projects/${encodedProjectId}/activity?limit=100`),
        api(`/api/projects/${encodedProjectId}/workflows`),
      ];
      if (state.filesSnapshot) refreshes.push(loadProjectFiles({ silent: true, renderWhenReady: false }));
      const [activity, projectWorkflows] = await Promise.all(refreshes);
      if (generation !== state.projectGeneration || state.project?.id !== projectId) return;
      state.activity = activity;
      state.projectWorkflows = projectWorkflows;
      state.activityHasMore = state.activity.length === 100;
    }
    renderPolledRuns({ collectionsChanged });
  } catch (error) {
    if (generation === state.projectGeneration && state.project?.id === projectId) {
      showToast(`Live status refresh failed: ${error.message}`);
    }
  } finally {
    if (generation === state.projectGeneration) {
      state.pollInFlight = false;
      schedulePolling();
    }
  }
}

async function loadMoreActivity() {
  const activityVisible = state.view === "activity" ||
    (state.view === "workflows" && state.workflowSection === "activity");
  if (
    !activityVisible ||
    state.activityLoading ||
    !state.activityHasMore ||
    !state.project
  ) {
    return;
  }
  const context = currentProjectContext();
  state.activityLoading = true;
  if (state.view === "workflows") renderWorkflows();
  else renderActivity();
  try {
    const page = await api(
      `/api/projects/${encodeURIComponent(context.projectId)}/activity?limit=100&offset=${state.activity.length}`,
    );
    if (!isCurrentProjectContext(context)) return;
    const known = new Set(state.activity.map((event) => event.id));
    state.activity.push(...page.filter((event) => !known.has(event.id)));
    state.activityHasMore = page.length === 100;
  } catch (error) {
    if (!isCurrentProjectContext(context)) return;
    showToast(`Earlier activity could not load: ${error.message}`);
  } finally {
    if (!isCurrentProjectContext(context)) return;
    state.activityLoading = false;
    if (state.view === "workflows") renderWorkflows();
    else renderActivity();
  }
}

function routeAfterProjectSwitch() {
  if (state.view === "compare") return "decisions";
  if (state.view === "task" || state.view === "document") return "workflows";
  if (state.view === "file") return "files";
  return ALLOWED_VIEWS.has(state.view) ? state.view : "workflows";
}

function resetProjectState(project) {
  const initialProjectLoad = !state.project;
  state.comparePage?.destroy();
  state.comparePage = null;
  if (state.project) state.chatDrafts.set(state.project.id, state.chatDraft);
  window.clearTimeout(state.pollTimer);
  state.pollTimer = null;
  disposeDocument();
  disposeFilesTree();
  state.projectGeneration += 1;
  state.project = project;
  state.projectAccess = "loading";
  state.workflows = [];
  state.runWorkflows = new Map();
  state.projectWorkflows = [];
  state.systemSummary = null;
  state.runs = [];
  state.activity = [];
  state.decisions = [];
  state.messages = [];
  state.integrations = [];
  state.billing = null;
  state.taskDetail = null;
  state.taskLoadingRunId = null;
  state.documentCache = new Map();
  state.documentLoadingRunId = null;
  state.filesSnapshot = null;
  state.filesLoading = false;
  state.filesError = null;
  state.filesSearch = "";
  state.filesDirectory = "";
  state.filesUpdated = false;
  state.fileCache = new Map();
  state.fileLoadingKey = null;
  state.fileError = null;
  state.workflowFilter = "all";
  state.workflowSection = "yours";
  state.templateView = "all";
  state.workflowSearch = "";
  state.workflowEditor = null;
  state.expandedRun = null;
  state.runDetails = new Map();
  state.activityFilter = "all";
  state.activityHasMore = false;
  state.activityLoading = false;
  state.decisionId = null;
  state.integrationFilter = "all";
  state.integrationSearch = "";
  state.expandedIntegration = null;
  state.integrationOptions = new Map();
  state.integrationLoading = null;
  state.integrationConnectIntent = null;
  if (integrationProjectDialog.open) integrationProjectDialog.close();
  if (projectCreateDialog.open) projectCreateDialog.close();
  if (projectInviteDialog.open) projectInviteDialog.close();
  state.chatDraft = state.chatDrafts.get(project.id) || "";
  state.sending = false;
  state.pendingReviewRunIds = new Set();
  state.deferredReviewRunIds = [];
  state.pollInFlight = false;

  // Cold links and reloads keep their parsed route, including reader revisions,
  // retained output and unconfirmed comparisons. Initial load is not a switch.
  // An explicit project switch still leaves every project-bound detail route.
  if (initialProjectLoad) return;
  const nextView = routeAfterProjectSwitch();
  state.view = nextView;
  state.documentRoute = null;
  state.taskRoute = null;
  state.fileRoute = null;
  state.compareRoute = null;
  window.history.replaceState(null, "", routeUrl(nextView));
}

async function loadProject(project, { announce = false, integrationReturn = null } = {}) {
  closeProjectMenu();
  resetProjectState(project);
  const generation = state.projectGeneration;
  const projectId = encodeURIComponent(project.id);
  persistProjectSelection(project.id);
  renderProjectMenu();
  projectName.textContent = project.name;
  projectSwitcher.setAttribute("aria-label", `Current project: ${project.name}`);
  projectSummary.textContent = "Connecting to live state";
  projectSwitcher.disabled = true;
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.disabled = true;
  });
  main.innerHTML = '<div class="view-loading">Connecting to Tin…</div>';
  let decisionLoadError = null;
  const decisionsRequest = api(`/api/projects/${projectId}/decisions`).catch((error) => {
    decisionLoadError = error;
    return [];
  });
  try {
    const [workflows, projectWorkflows, systemSummary, runs, activity, decisions, messages, integrations] = await Promise.all([
      api(`/api/workflows?project_id=${projectId}`),
      api(`/api/projects/${projectId}/workflows`),
      api(`/api/projects/${projectId}/system`),
      api(`/api/projects/${projectId}/runs?limit=100`),
      api(`/api/projects/${projectId}/activity?limit=100`),
      decisionsRequest,
      api(`/api/projects/${projectId}/chat/messages?limit=100`),
      api(`/api/projects/${projectId}/integrations`),
    ]);
    if (generation !== state.projectGeneration || state.project?.id !== project.id) return false;
    state.workflows = workflows;
    state.projectWorkflows = projectWorkflows;
    state.systemSummary = systemSummary;
    state.runs = runs;
    state.activity = activity;
    state.decisions = decisions;
    state.messages = messages.map(chatTurnFromMessage);
    state.integrations = integrations;
    await loadRunWorkflows(runs);
    if (generation !== state.projectGeneration || state.project?.id !== project.id) return false;
    state.activityHasMore = activity.length === 100;
    state.projectAccess = BROWSER_LOCK_ENABLED && !projectWorkflows.length && !runs.length ? "locked" : "ready";
    // A callback may arrive in a new tab or from the legacy origin. Let this project
    // finish that connection while keeping its dashboard locked.
    if (state.projectAccess === "locked" && integrationReturn?.projectId === project.id) {
      rememberConnectRequest(project.id, [...connectRequest(project.id), integrationReturn.provider]);
    }
    state.billing = BILLING_ENABLED ? await api(`/api/projects/${projectId}/billing`).catch(() => null) : null;
    if (generation !== state.projectGeneration || state.project?.id !== project.id) return false;
    renderProjectMenu();
    render();
    if (state.projectAccess === "locked") {
      if (state.view !== "integrations" || !connectRequest().length) recordLockPageEvent("viewed");
      return true;
    }
    schedulePolling();
    if (decisionLoadError) showToast("Decisions could not load. Refresh to try again.");
    if (announce) showToast(`Switched to ${project.name}.`);
    return true;
  } catch (error) {
    if (generation !== state.projectGeneration || state.project?.id !== project.id) return false;
    state.projectAccess = "error";
    projectSwitcher.disabled = !state.projects.length;
    main.innerHTML = '<div class="view-error"><div><strong>Tin could not load this project.</strong>Please refresh and try again.</div></div>';
    projectSummary.textContent = "Could not connect";
    return false;
  }
}

async function switchProject(projectId) {
  const project = state.projects.find((item) => item.id === projectId);
  if (!project) return;
  if (project.id === state.project?.id && state.projectAccess === "ready") {
    persistProjectSelection(project.id);
    closeProjectMenu({ restoreFocus: true });
    return;
  }
  await loadProject(project, { announce: true });
}

async function bootstrap(invitedProjectId = null, integrationReturn = null) {
  main.innerHTML = '<div class="view-loading">Connecting to Tin…</div>';
  const paymentId = BILLING_ENABLED ? new URL(window.location.href).searchParams.get("billing_payment") : null;
  try {
    let projects = await api("/api/projects");
    if (!projects.length) {
      projectName.textContent = "Preparing your project";
      projectSummary.textContent = "Creating durable state";
      main.innerHTML = '<div class="view-loading">Preparing your first project…</div>';
      const bootstrapResult = await api("/api/workspaces/bootstrap", {
        method: "POST",
        body: JSON.stringify({ name: personalProjectName() }),
      });
      projects = [bootstrapResult.project];
    }
    state.projects = projects;
    renderProjectMenu();
    let project = selectedProject(projects, invitedProjectId);
    if (paymentId !== null) {
      const payment = await api(`/api/billing/payments/${encodeURIComponent(paymentId)}/return`);
      const candidates = projects.filter((item) => item.workspace_id === payment.workspace_id);
      project = candidates.find((item) => item.id === project?.id) || candidates[0];
      if (!project) throw new Error("No accessible project in this payment’s workspace.");
      state.view = "billing";
    }
    const loaded = await loadProject(project, { integrationReturn });
    if (loaded && paymentId !== null) {
      // Persisted project + hash make reloads durable. Consume the return once so a
      // later explicit project switch is not redirected back to this payment.
      const url = new URL(window.location.href);
      url.searchParams.delete("billing_payment");
      url.pathname = "/billing";
      url.hash = "";
      window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
    }
  } catch (error) {
    if (paymentId !== null) {
      main.innerHTML = '<div class="view-error"><div><strong>Couldn’t open the payment’s workspace.</strong><p>This does not mean your payment failed. Retry, or open Tin and check Billing in the workspace you funded.</p><button data-payment-retry>Retry</button> <button data-payment-leave>Open Tin</button></div></div>';
      main.querySelector("[data-payment-retry]").addEventListener("click", () => bootstrap(invitedProjectId));
      main.querySelector("[data-payment-leave]").addEventListener("click", () => {
        const url = new URL(window.location.href);
        url.searchParams.delete("billing_payment");
        window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
        bootstrap(invitedProjectId);
      });
      projectSummary.textContent = "Payment workspace unavailable";
      return;
    }
    main.innerHTML = `<div class="view-error"><div><strong>Tin could not load.</strong>${escapeHtml(error.message)}</div></div>`;
    projectName.textContent = "Unavailable";
    projectSummary.textContent = "Live state could not be reached";
  }
}

async function completeIntegrationCallback() {
  const path = window.location.pathname.replace(/\/$/, "");
  if (!["/integrations/callback/google", "/integrations/callback/github", "/integrations/callback/posthog"].includes(path)) return null;
  const values = new URL(window.location.href).searchParams;
  const provider = path.split("/").pop();
  const stateToken = values.get("state");
  let connected;
  if (provider === "google" || provider === "posthog") {
    if (!stateToken) throw new Error("The integration connection did not return a valid state.");
    const code = values.get("code");
    const label = provider === "google" ? "Google" : "PostHog";
    if (!code) throw new Error(values.get("error_description") || `${label} connection was cancelled.`);
    connected = await api(`/api/integrations/${provider}/complete`, {
      method: "POST",
      body: JSON.stringify({ state: stateToken, code }),
    });
    showToast(`${connected.name} connected.`);
  } else {
    const code = values.get("code");
    const installationId = Number(values.get("installation_id"));
    const hasInstallation = Number.isInteger(installationId) && installationId > 0;
    if (!code) {
      if (!hasInstallation) throw new Error("GitHub authorization was not completed.");
      // The app was already installed, so GitHub returned without an OAuth code. Ask the
      // signed-in GitHub user to authorize; Tin remembers the installation on the attempt.
      const started = await api("/api/integrations/github/authorize", {
        method: "POST",
        body: JSON.stringify({
          installation_id: installationId,
          setup_action: values.get("setup_action"),
          state: stateToken || null,
          project_id: stateToken ? null : callbackProjectId(),
        }),
      });
      window.location.assign(started.authorization_url);
      return new Promise(() => {});
    }
    if (!stateToken) throw new Error("The integration connection did not return a valid state.");
    try {
      connected = await api("/api/integrations/github/complete", {
        method: "POST",
        body: JSON.stringify({
          state: stateToken,
          code,
          installation_id: hasInstallation ? installationId : null,
          setup_action: values.get("setup_action"),
        }),
      });
    } catch (error) {
      if (error.code === "github_install_required" && error.detail?.install_url) {
        // The app is not installed for this GitHub user yet; the install page returns
        // with a code and the new installation id.
        window.location.assign(error.detail.install_url);
        return new Promise(() => {});
      }
      if (error.code === "github_installation_choice" && error.detail?.choices?.length) {
        clearIntegrationCallbackUrl(error.detail.project_id);
        state.githubInstallationChoice = {
          projectId: error.detail.project_id,
          choices: error.detail.choices,
          selected: error.detail.choices[0].installation_id,
        };
        return null;
      }
      throw error;
    }
    showToast("GitHub connected with repository write access.");
  }
  clearIntegrationCallbackUrl(connected.project_id);
  if (APP_URL !== window.location.origin && connected.project_id) {
    // Exchange/check the callback on its registered origin first. Only public
    // project/provider identifiers cross back; never the OAuth code or state.
    const target = new URL("/", APP_URL);
    target.searchParams.set("project", connected.project_id);
    target.searchParams.set("connected_provider", connected.key);
    target.pathname = "/integrations";
    window.location.replace(target.href);
    return new Promise(() => {});
  }
  return { provider: connected.key, projectId: connected.project_id || null };
}

function callbackProjectId() {
  // GitHub returned without the connection state: the project in the URL is the one the
  // connect link was opened from. Never guess from a remembered project.
  const projectId = new URL(location.href).searchParams.get("project") || storedProjectId();
  if (!projectId) {
    throw new Error("GitHub returned without the connection state. Start the connection again from the project.");
  }
  return projectId;
}

function clearIntegrationCallbackUrl(projectId = null) {
  const callbackUrl = new URL(window.location.href);
  for (const key of ["code", "state", "installation_id", "setup_action", "error", "error_description"]) {
    callbackUrl.searchParams.delete(key);
  }
  if (projectId) callbackUrl.searchParams.set("project", projectId);
  callbackUrl.pathname = "/integrations";
  callbackUrl.hash = "";
  window.history.replaceState(null, "", `${callbackUrl.pathname}${callbackUrl.search}${callbackUrl.hash}`);
  state.view = "integrations";
}

const GITHUB_INSTALLATIONS_URL = "https://github.com/settings/installations";

async function chooseGitHubRepository() {
  // Same shape as the account chooser: the connect just finished, so the pick happens here
  // instead of hunting for a select on the Integrations page.
  const context = currentProjectContext();
  if (!context.projectId) return;
  state.repositoryChoice = { projectId: context.projectId, options: null, selected: null };
  integrationProjectTitle.textContent = `Choose the repository for ${state.project?.name || "this project"}`;
  integrationProjectCopy.textContent = "Tin writes approved drafts to one repository per project.";
  integrationProjectForm.querySelector("[data-confirm-integration-project]").textContent = "Save";
  renderGitHubRepositoryOptions();
  integrationProjectDialog.showModal();
  try {
    const options = await api(`/api/projects/${encodeURIComponent(context.projectId)}/integrations/infra.github/options`);
    if (!isCurrentProjectContext(context) || state.repositoryChoice?.projectId !== context.projectId) return;
    state.integrationOptions.set("infra.github", options);
    state.repositoryChoice.options = options;
    state.repositoryChoice.selected = options[0]?.id || null;
  } catch (error) {
    if (!isCurrentProjectContext(context) || !state.repositoryChoice) return;
    state.repositoryChoice.options = [];
    showToast(`Could not load repositories: ${error.message}`);
  }
  renderGitHubRepositoryOptions();
  integrationProjectOptions.querySelector('[aria-checked="true"]')?.focus();
}

function renderGitHubRepositoryOptions() {
  const choice = state.repositoryChoice;
  if (!choice) return;
  const confirm = integrationProjectForm.querySelector("[data-confirm-integration-project]");
  integrationProjectOptions.replaceChildren();
  if (choice.options === null) {
    const loading = document.createElement("p");
    loading.className = "integration-project-empty";
    loading.textContent = "Loading repositories…";
    integrationProjectOptions.append(loading);
    confirm.disabled = true;
    return;
  }
  for (const item of choice.options) {
    const selected = item.id === choice.selected;
    const option = document.createElement("button");
    option.type = "button";
    option.className = `integration-project-option${selected ? " is-selected" : ""}`;
    option.setAttribute("role", "radio");
    option.setAttribute("aria-checked", String(selected));
    option.innerHTML = `<span><strong>${escapeHtml(item.label)}</strong>${item.detail ? `<small>${escapeHtml(item.detail)}</small>` : ""}</span><i aria-hidden="true"></i>`;
    option.addEventListener("click", () => {
      choice.selected = item.id;
      renderGitHubRepositoryOptions();
      integrationProjectOptions.querySelector('[aria-checked="true"]')?.focus();
    });
    integrationProjectOptions.append(option);
  }
  const note = document.createElement("p");
  note.className = "integration-project-empty";
  note.innerHTML = `Only repositories the Tin app is installed on appear here. Add the repository under <a href="${GITHUB_INSTALLATIONS_URL}" target="_blank" rel="noreferrer">the Tin app’s access on GitHub</a>, then reopen the connect link.`;
  integrationProjectOptions.append(note);
  confirm.disabled = !choice.selected;
}

async function confirmGitHubRepository() {
  const choice = state.repositoryChoice;
  if (!choice?.selected) return;
  const confirm = integrationProjectForm.querySelector("[data-confirm-integration-project]");
  confirm.disabled = true;
  confirm.textContent = "Saving…";
  try {
    const updated = await api(`/api/projects/${encodeURIComponent(choice.projectId)}/integrations/infra.github`, {
      method: "PUT",
      body: JSON.stringify({ option_id: choice.selected }),
    });
    if (state.project?.id === choice.projectId) {
      const index = state.integrations.findIndex((item) => item.key === "infra.github");
      if (index >= 0) state.integrations[index] = updated;
      else state.integrations.push(updated);
    }
    if (integrationProjectDialog.open) integrationProjectDialog.close();
    showToast(`${updated.name} will write to ${updated.configuration?.selected_repository || "the chosen repository"}.`);
    if (state.view === "integrations") renderIntegrations();
  } catch (error) {
    showToast(`Could not save the repository: ${error.message}`);
    confirm.disabled = false;
    confirm.textContent = "Save";
  }
}

function chooseGoogleAdsAccount(projectId) {
  // Google Ads links by invitation, not OAuth: the founder types the customer id here, Tin's
  // manager account sends the request, and the founder accepts it inside Google Ads.
  if (!projectId) return;
  const existing = state.integrations.find((item) => item.key === "ads.google");
  state.googleAdsChoice = { projectId, customerId: existing?.configuration?.customer_id || "" };
  integrationProjectTitle.textContent = `Link Google Ads to ${state.project?.name || "this project"}`;
  integrationProjectCopy.textContent =
    "Enter the ten-digit customer id shown at the top right of Google Ads. Tin sends a manager request from Tin Computer; you accept it under Admin, Access and security, Managers. Tin never sees your Google password.";
  integrationProjectForm.querySelector("[data-confirm-integration-project]").textContent = "Send invitation";
  integrationProjectOptions.replaceChildren();
  const field = document.createElement("label");
  field.className = "project-create-field";
  field.innerHTML = `<span>Google Ads customer id</span><input type="text" name="customer_id" inputmode="numeric" autocomplete="off" placeholder="123-456-7890" maxlength="14" required />`;
  const input = field.querySelector("input");
  input.value = state.googleAdsChoice.customerId;
  input.addEventListener("input", () => { state.googleAdsChoice.customerId = input.value; });
  integrationProjectOptions.append(field);
  integrationProjectDialog.showModal();
  input.focus();
}

async function confirmGoogleAdsAccount() {
  const choice = state.googleAdsChoice;
  if (!choice) return;
  const digits = (choice.customerId || "").replace(/[^0-9]/g, "");
  if (digits.length !== 10) {
    showToast("A Google Ads customer id has ten digits, like 123-456-7890.");
    return;
  }
  const confirm = integrationProjectForm.querySelector("[data-confirm-integration-project]");
  confirm.disabled = true;
  confirm.textContent = "Sending…";
  try {
    const updated = await api(`/api/projects/${encodeURIComponent(choice.projectId)}/integrations/ads.google/link`, {
      method: "POST",
      body: JSON.stringify({ customer_id: digits }),
    });
    if (state.project?.id === choice.projectId) {
      const index = state.integrations.findIndex((item) => item.key === "ads.google");
      if (index >= 0) state.integrations[index] = updated;
      else state.integrations.push(updated);
    }
    if (integrationProjectDialog.open) integrationProjectDialog.close();
    showToast(updated.configuration?.link_status === "active"
      ? "Google Ads is linked to Tin's manager account."
      : "Invitation sent. Accept it in Google Ads, then press Check again.");
    if (state.view === "integrations") renderIntegrations();
  } catch (error) {
    showToast(`Could not link Google Ads: ${error.message}`);
    confirm.disabled = false;
    confirm.textContent = "Send invitation";
  }
}

function chooseStripeKey(projectId) {
  // Stripe connects by a restricted key the founder creates from Tin's link and pastes here.
  // The value stays in this password field only; it is never kept in page state.
  if (!projectId) return;
  const existing = state.integrations.find((item) => item.key === "payments.stripe");
  state.stripeKeyChoice = { projectId, expectedRevision: existing?.configuration?.revision || null };
  integrationProjectTitle.textContent = `${existing?.connection_id ? "Replace the Stripe key for" : "Connect Stripe to"} ${state.project?.name || "this project"}`;
  integrationProjectCopy.textContent =
    "Create a restricted key in Stripe with read permissions only, then paste it here. Tin stores it encrypted and only reads with it; workflows never see it. Keys starting sk_ are refused.";
  integrationProjectForm.querySelector("[data-confirm-integration-project]").textContent = "Save key";
  integrationProjectOptions.replaceChildren();
  const link = document.createElement("p");
  link.className = "integration-setup-prompt";
  const anchor = document.createElement("a");
  anchor.href = existing?.setup_url || "https://dashboard.stripe.com/apikeys";
  anchor.target = "_blank";
  anchor.rel = "noopener noreferrer";
  anchor.textContent = "Create the key in Stripe";
  link.append(anchor, document.createTextNode(" with the read permissions already selected. Name it Tin, create it, and copy the rk_ value."));
  const field = document.createElement("label");
  field.className = "project-create-field";
  field.innerHTML = `<span>Stripe restricted key</span><input type="password" name="restricted_key" autocomplete="off" spellcheck="false" placeholder="rk_live_…" maxlength="300" required />`;
  integrationProjectOptions.append(link, field);
  integrationProjectDialog.showModal();
  field.querySelector("input").focus();
}

async function confirmStripeKey() {
  const choice = state.stripeKeyChoice;
  if (!choice) return;
  const input = integrationProjectOptions.querySelector('input[name="restricted_key"]');
  const key = (input?.value || "").trim();
  if (!/^rk_(live|test)_/.test(key)) {
    showToast(/^(sk|pk)_/.test(key)
      ? "That is a secret or publishable key. Paste a restricted key starting rk_live_ or rk_test_."
      : "Paste a Stripe restricted key; it starts with rk_live_ or rk_test_.");
    return;
  }
  const confirm = integrationProjectForm.querySelector("[data-confirm-integration-project]");
  confirm.disabled = true;
  confirm.textContent = "Checking…";
  try {
    const updated = await api(`/api/projects/${encodeURIComponent(choice.projectId)}/integrations/payments.stripe/key`, {
      method: "POST",
      body: JSON.stringify({ restricted_key: key, expected_revision: choice.expectedRevision }),
    });
    if (input) input.value = "";
    if (state.project?.id === choice.projectId) {
      const index = state.integrations.findIndex((item) => item.key === "payments.stripe");
      if (index >= 0) state.integrations[index] = updated;
      else state.integrations.push(updated);
    }
    if (integrationProjectDialog.open) integrationProjectDialog.close();
    showToast(`Stripe connected: ${stripeHealth(updated)}.`);
    if (state.view === "integrations") renderIntegrations();
  } catch (error) {
    showToast(`Could not connect Stripe: ${error.message}`);
    confirm.disabled = false;
    confirm.textContent = "Save key";
  }
}

async function refreshStripe(button) {
  const context = currentProjectContext();
  if (!context.projectId) return;
  button.disabled = true;
  try {
    const updated = await api(`/api/projects/${encodeURIComponent(context.projectId)}/integrations/payments.stripe/refresh`, { method: "POST" });
    if (!isCurrentProjectContext(context)) return;
    const index = state.integrations.findIndex((item) => item.key === "payments.stripe");
    if (index >= 0) state.integrations[index] = updated;
    showToast(`Stripe: ${stripeHealth(updated)}.`);
    renderIntegrations();
  } catch (error) {
    if (!isCurrentProjectContext(context)) return;
    button.disabled = false;
    showToast(`Could not check Stripe: ${error.message}`);
  }
}

async function refreshGoogleAds(button) {
  const context = currentProjectContext();
  if (!context.projectId) return;
  button.disabled = true;
  try {
    const updated = await api(`/api/projects/${encodeURIComponent(context.projectId)}/integrations/ads.google/refresh`, { method: "POST" });
    if (!isCurrentProjectContext(context)) return;
    const index = state.integrations.findIndex((item) => item.key === "ads.google");
    if (index >= 0) state.integrations[index] = updated;
    showToast(`Google Ads: ${googleAdsHealth(updated)}.`);
    renderIntegrations();
  } catch (error) {
    if (!isCurrentProjectContext(context)) return;
    button.disabled = false;
    showToast(`Could not check Google Ads: ${error.message}`);
  }
}

function chooseGitHubInstallation() {
  const choice = state.githubInstallationChoice;
  if (!choice) return;
  integrationProjectTitle.textContent = "Choose the GitHub account";
  integrationProjectCopy.textContent =
    "The Tin GitHub App is installed on more than one account you can access. Choose the one that owns this project’s repository.";
  integrationProjectForm.querySelector("[data-confirm-integration-project]").textContent = "Continue to GitHub";
  renderGitHubInstallationOptions();
  integrationProjectDialog.showModal();
  integrationProjectOptions.querySelector('[aria-checked="true"]')?.focus();
}

function renderGitHubInstallationOptions() {
  const choice = state.githubInstallationChoice;
  integrationProjectOptions.replaceChildren();
  for (const item of choice.choices) {
    const selected = item.installation_id === choice.selected;
    const option = document.createElement("button");
    option.type = "button";
    option.className = `integration-project-option${selected ? " is-selected" : ""}`;
    option.setAttribute("role", "radio");
    option.setAttribute("aria-checked", String(selected));
    option.innerHTML = `<span><strong>${escapeHtml(item.account)}</strong></span><i aria-hidden="true"></i>`;
    option.addEventListener("click", () => {
      choice.selected = item.installation_id;
      renderGitHubInstallationOptions();
      integrationProjectOptions.querySelector('[aria-checked="true"]')?.focus();
    });
    integrationProjectOptions.append(option);
  }
}

async function confirmGitHubInstallation() {
  const choice = state.githubInstallationChoice;
  const confirm = integrationProjectForm.querySelector("[data-confirm-integration-project]");
  confirm.disabled = true;
  confirm.textContent = "Continuing…";
  try {
    const started = await api("/api/integrations/github/authorize", {
      method: "POST",
      body: JSON.stringify({ installation_id: choice.selected, project_id: choice.projectId }),
    });
    window.location.assign(started.authorization_url);
  } catch (error) {
    showToast(error.message);
    confirm.disabled = false;
    confirm.textContent = "Continue to GitHub";
  }
}

async function acceptPendingInvitation() {
  const url = new URL(window.location.href);
  const token = url.searchParams.get("invite");
  if (!token) return null;
  const project = await api(`/api/invitations/${encodeURIComponent(token)}/accept`, {
    method: "POST",
  });
  url.searchParams.delete("invite");
  window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
  showToast(`You joined ${project.name}.`);
  return project;
}

function showAuthError(message) {
  authRoot.hidden = false;
  appShell.hidden = true;
  authView.innerHTML = `<section class="auth-card auth-card-error">
    <strong>Tin could not start sign-in.</strong>
    <span>${escapeHtml(message)}</span>
  </section>`;
}

function authMode() {
  return /^\/sign-up(?:\/|$)/.test(window.location.pathname)
    ? "sign-up"
    : "sign-in";
}

function authUrl(mode) {
  const url = new URL(window.location.href);
  url.pathname = mode === "sign-up" ? "/sign-up" : "/sign-in";
  url.searchParams.delete("auth");
  const view = authViewFragment();
  if (view && !url.searchParams.has("redirect_url")) url.searchParams.set("redirect_url", new URL(postAuthUrl(), window.location.origin).href);
  url.searchParams.delete("auth_view");
  url.hash = "";
  return `${url.pathname}${url.search}`;
}

function authViewFragment() {
  const current = new URL(window.location.href);
  // Bounded local dashboard paths only. Accept old auth_view bookmarks as input.
  const candidate = current.searchParams.get("auth_view");
  if (candidate && candidate.length <= 16384 && DASHBOARD_ROUTE.test(candidate.replace(/^#?\/?/, ""))) return routeUrl(candidate, current);
  if (DASHBOARD_ROUTE.test(current.pathname.slice(1))) return `${current.pathname}${current.search}`;
  return "";
}

function postAuthUrl() {
  const continuation = authRoot.dataset.authReturn;
  if (continuation && !continuation.startsWith("{{")) return continuation;
  const url = new URL(window.location.href);
  url.searchParams.delete("auth");
  if (url.pathname.startsWith("/integrations/callback/")) {
    url.hash = "";
    return `${url.pathname}${url.search}`;
  }
  const view = authViewFragment();
  url.searchParams.delete("auth_view");
  url.hash = "";
  url.pathname = "/system";
  const target = new URL(view || `${url.pathname}${url.search}`, url);
  target.searchParams.delete("auth_view");
  const path = `${target.pathname}${target.search}`;
  return APP_URL === window.location.origin ? path : `${APP_URL}${path}`;
}

async function initializeAuth() {
  if (!window.Clerk) {
    showAuthError("The identity service did not load.");
    return;
  }
  try {
    const localization = structuredClone(TIN_AUTH_LOCALIZATION);
    if (authRoot.dataset.authFlow === "mcp") {
      for (const mode of ["signIn", "signUp"]) {
        localization[mode].start.subtitle = "Connect your coding agent to your Tin account.";
        localization[mode].start.subtitleCombined = localization[mode].start.subtitle;
      }
    }
    await window.Clerk.load({
      ui: { ClerkUI: window.__internal_ClerkUICtor },
      appearance: TIN_AUTH_APPEARANCE,
      localization,
      // The server has validated the entire return URL, including its path.
      allowedRedirectOrigins: [...new Set([
        ...(authRoot.dataset.authReturn && !authRoot.dataset.authReturn.startsWith("{{")
          ? [new URL(authRoot.dataset.authReturn).origin] : []),
        ...(APP_URL !== window.location.origin ? [APP_URL] : []),
      ])],
    });
  } catch (error) {
    showAuthError(error.message || "The identity service is unavailable.");
    return;
  }
  if (!window.Clerk.isSignedIn) {
    // Clerk owns its nested verification routes. Enter its real path before
    // mounting, preserving validated MCP and integration continuations intact.
    if (!/^\/sign-(?:in|up)(?:\/|$)/.test(window.location.pathname)) {
      const target = new URL(authUrl("sign-in"), window.location.origin);
      target.searchParams.set("redirect_url", new URL(postAuthUrl(), window.location.origin).href);
      window.location.replace(`${target.pathname}${target.search}`);
      return;
    }
    authRoot.hidden = false;
    appShell.hidden = true;
    const mode = authMode();
    authRoot.dataset.mode = mode;
    authView.innerHTML = `<section class="auth-card">
      <div id="clerk-auth"></div>
    </section>`;
    const mount = document.querySelector("#clerk-auth");
    const shared = {
      appearance: TIN_AUTH_APPEARANCE,
      routing: "path",
      path: `/${mode}`,
      fallbackRedirectUrl: postAuthUrl(),
    };
    if ((authRoot.dataset.authReturn && !authRoot.dataset.authReturn.startsWith("{{")) || authViewFragment()) {
      shared.forceRedirectUrl = postAuthUrl();
    }
    if (mode === "sign-up") {
      window.Clerk.mountSignUp(mount, {
        ...shared,
        signInUrl: authUrl("sign-in"),
        signInFallbackRedirectUrl: postAuthUrl(),
      });
    } else {
      window.Clerk.mountSignIn(mount, {
        ...shared,
        signUpUrl: authUrl("sign-up"),
        signUpFallbackRedirectUrl: postAuthUrl(),
        withSignUp: false,
      });
    }
    return;
  }

  if (authRoot.dataset.authReturn && !authRoot.dataset.authReturn.startsWith("{{")) {
    window.location.replace(postAuthUrl());
    return;
  }

  if (/^\/sign-(?:in|up)(?:\/|$)/.test(window.location.pathname)) {
    window.location.replace(postAuthUrl());
    return;
  }

  authRoot.hidden = true;
  appShell.hidden = false;
  const user = window.Clerk.user;
  state.signedInUserId = user?.id || null;
  state.signedInName = user?.firstName || user?.fullName || null;
  userName.textContent = user?.firstName || user?.fullName || "Tin user";
  if (user?.imageUrl) userAvatar.src = user.imageUrl;
  adoptConnectRequest();
  const next = new URL(window.location.href).searchParams.get("next");
  if (next && /^\/documents\/runs\/[A-Za-z0-9-]+\/?$/.test(next)) {
    window.location.assign(next);
    return;
  }
  let invitedProject = null;
  try {
    invitedProject = await acceptPendingInvitation();
  } catch (error) {
    showToast(error.message);
  }
  const returnedUrl = new URL(window.location.href);
  const returnedProvider = returnedUrl.searchParams.get("connected_provider");
  let connection = CONNECT_PROVIDERS.has(returnedProvider)
    ? { provider: returnedProvider, projectId: returnedUrl.searchParams.get("project") } : null;
  if (returnedProvider) {
    returnedUrl.searchParams.delete("connected_provider");
    window.history.replaceState(null, "", `${returnedUrl.pathname}${returnedUrl.search}${returnedUrl.hash}`);
  }
  try {
    connection = await completeIntegrationCallback() || connection;
  } catch (error) {
    const callbackUrl = new URL(window.location.href);
    const rememberedProject = storedProjectId();
    callbackUrl.pathname = "/integrations";
    callbackUrl.search = "";
    if (rememberedProject) callbackUrl.searchParams.set("project", rememberedProject);
    callbackUrl.hash = "";
    window.history.replaceState(null, "", `${callbackUrl.pathname}${callbackUrl.search}${callbackUrl.hash}`);
    state.view = "integrations";
    showToast(error.message);
  }
  const integrationReturn = connection || (state.githubInstallationChoice
    ? { provider: "infra.github", projectId: state.githubInstallationChoice.projectId } : null);
  await bootstrap(invitedProject?.id || integrationReturn?.projectId || null, integrationReturn);
  if (connection && connection.projectId === state.project?.id) await promptForIntegrationResource(connection.provider);
  if (state.githubInstallationChoice) chooseGitHubInstallation();
}

document.querySelectorAll(".nav-item").forEach((item) => {
  item.addEventListener("click", () => navigate(item.dataset.view));
});

agentRailToggle?.addEventListener("click", () => {
  const open = agentRailBody.hidden;
  agentRailBody.hidden = !open;
  agentRailToggle.setAttribute("aria-expanded", String(open));
  agentRail.classList.toggle("is-open", open);
  updateAgentRail();
});

agentRail?.querySelectorAll("[data-agent-tab]").forEach((button) => {
  button.addEventListener("click", () => {
    state.agentTab = button.dataset.agentTab;
    updateAgentRail();
  });
});

const AGENT_TAB_LABELS = { codex: "Codex", claude: "Claude Code" };

copyAgentCommand?.addEventListener("click", async () => {
  try {
    // Copy the install command even if the next-step reminder is currently visible.
    await navigator.clipboard.writeText(agentCommandFor(state.agentTab));
  } catch (_error) {
    showToast("Copy did not work. Select the command and copy it from here.");
    return;
  }
  showToast("Command copied.");
  if (!AGENT_TAB_LABELS[state.agentTab] || !agentCommand) return;
  // Flip the command to the next step for a few seconds, then back (see Paper sketch G2).
  agentCommand.textContent = `Copied. Run it, then ask your coding agent: “${AGENT_PROMPT}”`;
  window.clearTimeout(copyAgentCommand.flipTimer);
  copyAgentCommand.flipTimer = window.setTimeout(() => updateAgentRail(), 4000);
});

projectSwitcher.addEventListener("click", () => {
  if (projectMenu.hidden) openProjectMenu();
  else closeProjectMenu();
});

projectSwitcher.addEventListener("keydown", (event) => {
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    openProjectMenu({ focus: event.key === "ArrowUp" ? "last" : "first" });
  }
  if (event.key === "Escape") closeProjectMenu();
});

projectMenu.addEventListener("keydown", handleProjectMenuKeydown);

projectCreateForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const workspaceId = state.projectCreateWorkspaceId;
  const requestId = state.projectCreateRequestId;
  const name = projectCreateName.value.trim();
  if (!name || (!state.projectCreatePersonal && (!workspaceId || !requestId))) return;
  const confirm = projectCreateForm.querySelector("[data-confirm-project-create]");
  confirm.disabled = true;
  confirm.textContent = "Creating…";
  try {
    const created = state.projectCreatePersonal
      ? (await api("/api/workspaces/bootstrap", {
          method: "POST",
          body: JSON.stringify({ name }),
        })).project
      : await api(`/api/workspaces/${encodeURIComponent(workspaceId)}/projects`, {
          method: "POST",
          body: JSON.stringify({ name, request_id: requestId }),
        });
    state.projects = await api("/api/projects");
    projectCreateDialog.close();
    await loadProject(
      state.projects.find((project) => project.id === created.id) || created,
      { announce: true },
    );
  } catch (error) {
    showToast(error.message);
  } finally {
    confirm.disabled = false;
    confirm.textContent = "Create project";
  }
});

projectCreateForm.querySelector("[data-cancel-project-create]").addEventListener("click", () => {
  projectCreateDialog.close();
});

projectCreateDialog.addEventListener("close", () => {
  state.projectCreateWorkspaceId = null;
  state.projectCreateRequestId = null;
  state.projectCreatePersonal = false;
});

projectDeleteForm.addEventListener("submit", (event) => {
  event.preventDefault();
  deleteProject();
});

projectDeleteName.addEventListener("input", updateProjectDeleteConfirm);

projectDeleteForm.querySelector("[data-cancel-project-delete]").addEventListener("click", () => {
  projectDeleteDialog.close();
});

projectDeleteDialog.addEventListener("close", () => {
  state.projectDeleteProjectId = null;
  state.projectDeleteRequestId = null;
  state.projectDeleteName = null;
  projectDeleteName.value = "";
});

projectInviteForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.project || !projectInviteEmail.value.trim()) return;
  const confirm = projectInviteForm.querySelector("[data-create-project-invite]");
  confirm.disabled = true;
  confirm.textContent = "Creating…";
  try {
    const invitation = await api(
      `/api/projects/${encodeURIComponent(state.project.id)}/invitations`,
      {
        method: "POST",
        body: JSON.stringify({ email: projectInviteEmail.value.trim() }),
      },
    );
    projectInviteUrl.textContent = invitation.invitation_url;
    projectInviteResult.hidden = false;
    confirm.hidden = true;
    showToast(`Invite ready for ${invitation.email}.`);
  } catch (error) {
    showToast(error.message);
  } finally {
    confirm.disabled = false;
    confirm.textContent = "Create invite";
  }
});

projectInviteForm.querySelector("[data-cancel-project-invite]").addEventListener("click", () => {
  projectInviteDialog.close();
});

copyProjectInvite.addEventListener("click", async () => {
  if (!projectInviteUrl.textContent) return;
  await navigator.clipboard.writeText(projectInviteUrl.textContent);
  copyProjectInvite.textContent = "Copied";
  showToast("Invite link copied.");
});

projectInviteDialog.addEventListener("close", () => {
  const confirm = projectInviteForm.querySelector("[data-create-project-invite]");
  confirm.hidden = false;
  copyProjectInvite.textContent = "Copy link";
  projectInviteResult.hidden = true;
  projectInviteUrl.textContent = "";
});

integrationProjectForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.repositoryChoice) {
    await confirmGitHubRepository();
    return;
  }
  if (state.githubInstallationChoice) {
    await confirmGitHubInstallation();
    return;
  }
  if (state.googleAdsChoice) {
    await confirmGoogleAdsAccount();
    return;
  }
  if (state.stripeKeyChoice) {
    await confirmStripeKey();
    return;
  }
  const intent = state.integrationConnectIntent;
  if (!intent) return;
  const confirm = integrationProjectForm.querySelector("[data-confirm-integration-project]");
  const integration = state.integrations.find((item) => item.key === intent.providerKey);
  confirm.disabled = true;
  confirm.textContent = "Continuing…";
  await connectIntegration(intent.providerKey, intent.capabilities, intent.projectId);
  if (integrationProjectDialog.open) {
    confirm.disabled = false;
    confirm.textContent = `Continue to ${integration?.name || "provider"}`;
  }
});

integrationProjectForm.querySelector("[data-cancel-integration-project]").addEventListener("click", () => {
  integrationProjectDialog.close();
});

integrationProjectDialog.addEventListener("close", () => {
  state.integrationConnectIntent = null;
  state.githubInstallationChoice = null;
  state.repositoryChoice = null;
  state.googleAdsChoice = null;
  state.stripeKeyChoice = null;
  const stripeKey = integrationProjectOptions.querySelector('input[name="restricted_key"]');
  if (stripeKey) stripeKey.value = "";
  integrationProjectForm.querySelector("[data-confirm-integration-project]").disabled = false;
});

integrationProjectOptions.addEventListener("keydown", (event) => {
  if (!["ArrowDown", "ArrowRight", "ArrowUp", "ArrowLeft"].includes(event.key)) return;
  const options = [...integrationProjectOptions.querySelectorAll("[role=radio]")];
  const current = options.indexOf(document.activeElement);
  const direction = ["ArrowDown", "ArrowRight"].includes(event.key) ? 1 : -1;
  const next = options[(current + direction + options.length) % options.length];
  event.preventDefault();
  next?.click();
});

document.addEventListener("pointerdown", (event) => {
  document.querySelectorAll(".tin-select.is-open").forEach((dropdown) => {
    if (!dropdown.contains(event.target)) closeTinSelect(dropdown);
  });
  if (!projectMenu.hidden && !event.target.closest(".project-menu-shell")) closeProjectMenu();
});

function routeChanged() {
  state.view = viewFromLocation();
  state.documentRoute = documentRouteFromLocation();
  state.taskRoute = taskRouteFromLocation();
  state.fileRoute = fileRouteFromLocation();
  state.compareRoute = compareRouteFromLocation();
  if (state.view !== "task") state.taskDetail = null;
  render();
  schedulePolling({ immediate: state.view === "workflows" });
}
window.addEventListener("popstate", routeChanged);
window.addEventListener("hashchange", () => {
  if (window.location.pathname === "/" && DASHBOARD_ROUTE.test(window.location.hash.replace(/^#\/?/, ""))) {
    normalizeDashboardUrl();
    routeChanged();
  }
});

window.addEventListener("focus", () => {
  if (state.view === "workflows") schedulePolling({ immediate: true });
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    window.clearTimeout(state.pollTimer);
    state.pollTimer = null;
  } else {
    schedulePolling({ immediate: state.view === "workflows" });
  }
});

window.addEventListener("tin:themechange", (event) => {
  TIN_AUTH_APPEARANCE.variables.colorScheme = event.detail.theme;
  if (state.filesTree) {
    disposeFilesTree();
    renderFiles();
  }
});

window.addEventListener(
  "scroll",
  () => {
    if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 240) {
      loadMoreActivity();
    }
  },
  { passive: true },
);

initializeAuth();
