"use strict";

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { webcrypto } = require("node:crypto");

const EXTENSION_ROOT = path.resolve(__dirname, "../..");
const BROWSER_CONTEXT_KEY = "tin.linkedin.browser-context.v1";

function jsonClone(value) {
  if (value === undefined) return undefined;
  return JSON.parse(JSON.stringify(value));
}

function cloneInto(context, value) {
  if (value === undefined) return undefined;
  const encoded = JSON.stringify(JSON.stringify(value));
  return vm.runInContext(`JSON.parse(${encoded})`, context);
}

function eventTarget() {
  const listeners = [];
  return {
    listeners,
    addListener(listener) {
      listeners.push(listener);
    },
  };
}

async function settle(turns = 6) {
  for (let turn = 0; turn < turns; turn += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

async function eventually(predicate, message = "condition was not met") {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (await predicate()) return;
    await settle(1);
  }
  throw new Error(message);
}

function defaultCookies() {
  return [
    {
      name: "li_at",
      value: "linkedin-session-secret",
      domain: ".linkedin.com",
      path: "/",
      secure: true,
      httpOnly: true,
      sameSite: "no_restriction",
      expirationDate: 1999999999,
    },
    {
      name: "JSESSIONID",
      value: '"ajax:123456789"',
      domain: ".linkedin.com",
      path: "/",
      secure: true,
      httpOnly: false,
      sameSite: "no_restriction",
    },
    {
      name: "unrelated_cookie",
      value: "must-never-upload",
      domain: ".linkedin.com",
      path: "/",
    },
  ];
}

function defaultBrowserContext() {
  return {
    accept_language: "en-US,en;q=0.9",
    li_lang: "en_US",
    li_track: {
      clientVersion: "1.13.test",
      mpVersion: "1.13.test",
      osName: "web",
      timezoneOffset: -7,
      timezone: "America/Los_Angeles",
      deviceFormFactor: "DESKTOP",
      mpName: "voyager-web",
      displayDensity: 2,
      displayWidth: 2940,
      displayHeight: 1912,
    },
  };
}

function createBackgroundHarness(options = {}) {
  const runtimeMessages = eventTarget();
  const alarmEvents = eventTarget();
  const cookieEvents = eventTarget();
  const webRequestEvents = eventTarget();
  const alarms = new Map();
  const alarmCreates = [];
  const alarmClears = [];
  const fetchCalls = [];
  const cookieQueries = [];
  const context = {
    AbortController,
    clearTimeout,
    Date,
    Headers,
    TextDecoder,
    TextEncoder,
    URL,
    Uint8Array,
    console,
    crypto: webcrypto,
    navigator: {
      userAgent: "Tin extension integration test",
    },
    setTimeout,
    btoa(value) {
      return Buffer.from(value, "binary").toString("base64");
    },
  };
  vm.createContext(context);

  const storage = new Map(
    Object.entries(options.initialStorage || {}).map(([key, value]) => [
      key,
      cloneInto(context, value),
    ]),
  );
  if (options.browserContext !== null && !storage.has(BROWSER_CONTEXT_KEY)) {
    storage.set(
      BROWSER_CONTEXT_KEY,
      cloneInto(context, options.browserContext || defaultBrowserContext()),
    );
  }
  let cookies = cloneInto(context, options.cookies || defaultCookies());
  const tabCreates = [];
  const tabReloads = [];

  context.fetch = async (url, request = {}) => {
    const call = {
      url: String(url),
      method: request.method || "GET",
      body: request.body ? JSON.parse(request.body) : null,
      authorization: request.headers?.get?.("Authorization") || null,
      credentials: request.credentials,
      redirect: request.redirect,
    };
    fetchCalls.push(call);
    if (!options.fetchHandler) {
      throw new Error(`Unexpected fetch: ${call.method} ${call.url}`);
    }
    const result = await options.fetchHandler(call, fetchCalls.length - 1);
    if (result instanceof Error) throw result;
    const status = result?.status ?? 200;
    return {
      ok: status >= 200 && status < 300,
      status,
      async json() {
        return cloneInto(context, result?.body ?? {});
      },
    };
  };

  async function deliverRuntimeMessage(message, sender) {
    if (runtimeMessages.listeners.length === 0) {
      throw new Error("No runtime message listener is installed");
    }
    return new Promise((resolve, reject) => {
      let responded = false;
      let asynchronous = false;
      const sendResponse = (value) => {
        if (responded) return;
        responded = true;
        resolve(jsonClone(value));
      };
      try {
        for (const listener of runtimeMessages.listeners) {
          const result = listener(
            cloneInto(context, message),
            cloneInto(context, sender),
            sendResponse,
          );
          if (result === true) {
            asynchronous = true;
            break;
          }
          if (responded) break;
        }
        if (!asynchronous && !responded) resolve(undefined);
      } catch (error) {
        reject(error);
      }
    });
  }

  const chrome = {
    alarms: {
      onAlarm: alarmEvents,
      async create(name, alarm) {
        alarms.set(name, jsonClone(alarm));
        alarmCreates.push({ name, alarm: jsonClone(alarm) });
      },
      async clear(name) {
        alarmClears.push(name);
        return alarms.delete(name);
      },
    },
    cookies: {
      onChanged: cookieEvents,
      async getAll(query) {
        cookieQueries.push(jsonClone(query));
        return cookies;
      },
    },
    runtime: {
      id: "tin-extension-test",
      lastError: null,
      onMessage: runtimeMessages,
      onStartup: { addListener() {} },
      getURL: path => `chrome-extension://tin-extension-test/${path}`,
      getManifest() {
        return { version: "0.2.0-test" };
      },
    },
    storage: {
      local: {
        async get(key) {
          return { [key]: storage.get(key) };
        },
        async set(values) {
          for (const [key, value] of Object.entries(values)) {
            storage.set(key, value);
          }
        },
        async remove(keys) {
          for (const key of Array.isArray(keys) ? keys : [keys]) storage.delete(key);
        },
        async setAccessLevel() {},
      },
    },
    tabs: {
      onRemoved: eventTarget(),
      async query() {
        return cloneInto(context, options.linkedinTabs || []);
      },
      async reload(tabId) {
        tabReloads.push(tabId);
      },
      async create(createProperties) {
        tabCreates.push(jsonClone(createProperties));
        return cloneInto(context, { id: 901, ...createProperties });
      },
    },
    webRequest: {
      onBeforeSendHeaders: webRequestEvents,
    },
  };
  context.chrome = chrome;
  context.importScripts = (...scriptNames) => {
    for (const scriptName of scriptNames) {
      const filename = path.join(EXTENSION_ROOT, "src", scriptName);
      vm.runInContext(fs.readFileSync(filename, "utf8"), context, { filename });
    }
  };

  const backgroundFilename = path.join(EXTENSION_ROOT, "src/background.js");
  vm.runInContext(fs.readFileSync(backgroundFilename, "utf8"), context, {
    filename: backgroundFilename,
  });

  return {
    retireLegacyForCollection: () => context.TinLinkedInRetireLegacy(),
    alarmClears,
    alarmCreates,
    alarms,
    cookieQueries,
    async deliverBridgeMessage(type, payload = {}, senderUrl = "https://tin.computer/home") {
      return deliverRuntimeMessage(
        {
          namespace: "tin.linkedin.session.v2",
          protocol_version: 2,
          type,
          request_id: `test-${type.toLowerCase()}`,
          payload,
        },
        { id: "tin-extension-test", tab: { id: 900, url: senderUrl } },
      );
    },
    async deliverPopupMessage(type) {
      return deliverRuntimeMessage(
        { namespace: "tin.linkedin.popup.v2", type },
        { id: "tin-extension-test" },
      );
    },
    async fireAlarm(name) {
      for (const listener of alarmEvents.listeners) {
        listener(cloneInto(context, { name }));
      }
      await settle();
    },
    async emitCookieChange(cookie) {
      for (const listener of cookieEvents.listeners) {
        listener(cloneInto(context, { removed: false, cause: "explicit", cookie }));
      }
      await settle();
    },
    async emitVoyagerRequestHeaders(requestHeaders) {
      for (const listener of webRequestEvents.listeners) {
        listener(cloneInto(context, { requestHeaders }));
      }
      await settle();
    },
    fetchCalls,
    hasStorage(key) {
      return storage.has(key);
    },
    setCookies(next) {
      cookies = cloneInto(context, next);
    },
    storageValue(key) {
      return jsonClone(storage.get(key));
    },
    tabCreates,
    tabReloads,
  };
}

function createBridgeHarness(options = {}) {
  const messageListeners = [];
  const postedMessages = [];
  const forwardedMessages = [];
  const origin = options.origin || "https://tin.computer";
  let context;

  context = {
    URL,
    console,
    location: { origin },
    addEventListener(type, listener) {
      if (type === "message") messageListeners.push(listener);
    },
    postMessage(value, targetOrigin) {
      postedMessages.push({ value: jsonClone(value), targetOrigin });
    },
  };
  vm.createContext(context);
  context.window = context;
  context.chrome = {
    runtime: {
      lastError: null,
      getManifest() {
        return { version: "0.2.0-test" };
      },
      sendMessage(message, callback) {
        forwardedMessages.push(jsonClone(message));
        const response = options.backgroundResponse
          ? options.backgroundResponse(jsonClone(message))
          : { ok: true, payload: { status: "not_connected" } };
        callback(cloneInto(context, response));
      },
    },
  };

  for (const relative of ["src/protocol.js", "src/tin-bridge.js"]) {
    const filename = path.join(EXTENSION_ROOT, relative);
    vm.runInContext(fs.readFileSync(filename, "utf8"), context, { filename });
  }

  return {
    forwardedMessages,
    messageListenerCount: messageListeners.length,
    postedMessages,
    async emitDashboardMessage(message) {
      if (messageListeners.length === 0) return;
      context.__tinTestMessageListener = messageListeners[0];
      context.__tinTestMessageData = cloneInto(context, message);
      await vm.runInContext(
        "__tinTestMessageListener({ source: window, origin: window.location.origin, data: __tinTestMessageData })",
        context,
      );
      delete context.__tinTestMessageListener;
      delete context.__tinTestMessageData;
    },
  };
}

module.exports = {
  createBackgroundHarness,
  defaultBrowserContext,
  createBridgeHarness,
  defaultCookies,
  eventually,
  settle,
};
