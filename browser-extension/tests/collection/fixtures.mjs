// Synthetic people and markup only. Every browser request is fulfilled locally.
export const actor = { key: "https://www.linkedin.com/in/test-owner", profile_url: "https://www.linkedin.com/in/test-owner", name: "Test Owner", source: "browser_profile_link" };
export const friend = { name: "Test Connection", profile_url: "https://www.linkedin.com/in/test-friend", degree: "1st" };
export function searchURL(page = 1, id = "test-friend-id") {
  const url = new URL("https://www.linkedin.com/search/results/people/");
  url.searchParams.set("connectionOf", JSON.stringify([id]));
  if (page !== 1) url.searchParams.set("page", page);
  return url.href;
}
export const selection = { actor, friend, collection_url: searchURL(), tab_id: 7 };
export function person(id, degree = "2nd") {
  return { name: `Test Person ${id}`, profile_url: `https://www.linkedin.com/in/test-person-${id}`, degree, headline: "Synthetic role", location: "Test City" };
}
export function snapshot(page = 1, records = [person(1)], next = "next") {
  return { actor, url: searchURL(page), page, records, next, empty: false, invalid_count: 0, observed_at: "2026-01-01T00:00:00.000Z" };
}
export const escape = value => String(value).replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
export const nav = `<header class="global-nav"><a class="global-nav__me-profile-link" href="${actor.profile_url}">${actor.name}</a></header>`;
export const profileHTML = (degree = "1st", links = true) => `<!doctype html><html lang="en"><body>${nav}<main><section><h1>${friend.name}</h1><span class="dist-value">${degree}</span>${links ? `<a href="${escape(searchURL())}">500+ connections</a>` : ""}</section></main></body></html>`;
export function card(row) {
  return `<li class="reusable-search__result-container"><div class="entity-result__title-text"><a href="${escape(row.profile_url)}"><span aria-hidden="true">${escape(row.name)}</span></a></div><span class="entity-result__badge-text">· ${row.degree}</span><div class="entity-result__primary-subtitle">${escape(row.headline)}</div><div class="entity-result__secondary-subtitle">${escape(row.location)}</div></li>`;
}
export function resultsHTML(page = 1, records = [person(1)], next = "next") {
  return `<!doctype html><html lang="en"><body>${nav}<main><div class="search-results-container"><ul>${records.map(card).join("")}</ul>${!records.length ? '<h2>No results found</h2>' : ""}${next === "unknown" ? "" : `<div class="artdeco-pagination"><button aria-current="page">${page}</button><button aria-label="Next" ${next === "end" ? "disabled" : ""}>Next</button></div>`}</div></main></body></html>`;
}
