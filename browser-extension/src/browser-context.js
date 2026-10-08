"use strict";

(function installLinkedInBrowserContext(global) {
  const STRING_FIELDS = Object.freeze([
    "clientVersion",
    "mpVersion",
    "osName",
    "timezone",
    "deviceFormFactor",
    "mpName",
  ]);
  const NUMBER_FIELDS = Object.freeze([
    "timezoneOffset",
    "displayDensity",
    "displayWidth",
    "displayHeight",
  ]);
  const REQUIRED_FIELDS = Object.freeze([
    "clientVersion",
    "osName",
    "timezoneOffset",
    "timezone",
    "deviceFormFactor",
    "mpName",
  ]);

  class BrowserContextError extends Error {
    constructor() {
      super("Tin could not read LinkedIn's request context. Refresh LinkedIn and try again.");
      this.name = "BrowserContextError";
      this.code = "linkedin_context_missing";
    }
  }

  function safeHeaderString(value, maximumLength) {
    if (typeof value !== "string") return null;
    const normalized = value.trim();
    if (
      normalized.length < 1 ||
      normalized.length > maximumLength ||
      [...normalized].some((character) => {
        const code = character.charCodeAt(0);
        return code < 32 || code > 126;
      })
    ) {
      return null;
    }
    return normalized;
  }

  function normalizeLiTrack(value) {
    let parsed = value;
    if (typeof value === "string") {
      if (value.length > 4096) return null;
      try {
        parsed = JSON.parse(value);
      } catch (_error) {
        return null;
      }
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;

    const normalized = {};
    for (const field of STRING_FIELDS) {
      if (!Object.hasOwn(parsed, field)) continue;
      const fieldValue = safeHeaderString(parsed[field], 128);
      if (!fieldValue) return null;
      normalized[field] = fieldValue;
    }
    for (const field of NUMBER_FIELDS) {
      if (!Object.hasOwn(parsed, field)) continue;
      const fieldValue = parsed[field];
      if (typeof fieldValue !== "number" || !Number.isFinite(fieldValue)) return null;
      if (field === "timezoneOffset" && (fieldValue < -14 || fieldValue > 14)) {
        return null;
      }
      if (field === "displayDensity" && (fieldValue < 0.25 || fieldValue > 16)) {
        return null;
      }
      if (
        (field === "displayWidth" || field === "displayHeight") &&
        (fieldValue < 1 || fieldValue > 100_000)
      ) {
        return null;
      }
      normalized[field] = fieldValue;
    }
    return REQUIRED_FIELDS.every((field) => Object.hasOwn(normalized, field))
      ? normalized
      : null;
  }

  function normalize(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    const acceptLanguage = safeHeaderString(value.accept_language, 256);
    const liLang = safeHeaderString(value.li_lang, 16);
    const liTrack = normalizeLiTrack(value.li_track);
    if (
      !acceptLanguage ||
      !liLang ||
      !/^[A-Za-z]{2,3}_[A-Za-z]{2,3}$/.test(liLang) ||
      !liTrack
    ) {
      return null;
    }
    return {
      accept_language: acceptLanguage,
      li_lang: liLang,
      li_track: liTrack,
    };
  }

  function fromRequest(details) {
    const headers = {};
    for (const header of Array.isArray(details?.requestHeaders)
      ? details.requestHeaders
      : []) {
      const name = typeof header?.name === "string" ? header.name.toLowerCase() : "";
      if (name === "accept-language") headers.accept_language = header.value;
      else if (name === "x-li-lang") headers.li_lang = header.value;
      else if (name === "x-li-track") headers.li_track = header.value;
    }
    return normalize({
      accept_language: headers.accept_language,
      li_lang: headers.li_lang,
      li_track: headers.li_track,
    });
  }

  function create({ chromeApi, storageKey, waitMilliseconds = 12_000 }) {
    const waiters = new Set();

    async function save(context) {
      await chromeApi.storage.local.set({ [storageKey]: context });
      for (const resolve of waiters) resolve(context);
      waiters.clear();
    }

    async function load() {
      const stored = await chromeApi.storage.local.get(storageKey);
      return normalize(stored?.[storageKey]);
    }

    function waitForObservation() {
      return new Promise((resolve) => {
        const finish = (context) => {
          clearTimeout(timeout);
          waiters.delete(finish);
          resolve(context);
        };
        const timeout = setTimeout(() => finish(null), waitMilliseconds);
        waiters.add(finish);
      });
    }

    async function capture() {
      const stored = await load();
      if (stored) return stored;

      const pending = waitForObservation();
      try {
        const tabs = await chromeApi.tabs.query({ url: "https://www.linkedin.com/*" });
        const existing = tabs.find((tab) => Number.isInteger(tab?.id));
        if (existing) {
          await chromeApi.tabs.reload(existing.id);
        } else {
          await chromeApi.tabs.create({
            url: "https://www.linkedin.com/feed/",
            active: false,
          });
        }
      } catch (_error) {
        // An already-loading tab can still satisfy the pending observation.
      }

      const observed = await pending;
      if (observed) return observed;
      throw new BrowserContextError();
    }

    function observe(details) {
      const context = fromRequest(details);
      if (context) void save(context).catch(() => undefined);
    }

    return Object.freeze({ capture, observe });
  }

  global.TinLinkedInBrowserContext = Object.freeze({ create, normalize });
})(globalThis);
