// Building blocks for a film's demo world: people, a LinkedIn connection and sending, in the shapes
// the dashboard's APIs return. Everyone and everything here is made up; a film never shows a real
// record. Dates are written relative to the film's own day, so the world reads as recent.
const DAY = 86400000;

export function clock(now) {
  const base = Date.parse(now);
  // "-2d 14:05": two days before the film's day, at that time (UTC).
  return (ago, time = "09:00") => {
    const day = new Date(base - parseInt(ago, 10) * DAY);
    const [hours, minutes] = time.split(":").map(Number);
    day.setUTCHours(hours, minutes, 0, 0);
    return day.toISOString();
  };
}

// One person as the People API lists them. `fit` is Tin's score, `icp` the profile it matched.
export function person(id, name, headline, {icp = null, fit = null, reason = "", relationship = "none", touch = null, notes = [], events = [], customer = false, at}) {
  const scored = {by: "engagers", at, stale: false, stale_reason: null, definition: "strategy/ICP.md", previous: null};
  const fields = {
    icp: {value: icp, reason, ...scored},
    icp_fit: {value: fit, reason, ...scored},
    linkedin_relationship: {value: relationship, reason: "", ...scored, definition: ""},
  };
  if (customer) fields.customer = {value: "yes", reason: "", ...scored, by: "you", definition: ""};
  return {
    id, name, headline, profile_url: `https://www.linkedin.com/in/${id}`, fields,
    last_touch: touch, added: {at, by: "engagers"}, notes, events,
    requestable: relationship === "none" && !customer, request: null,
  };
}

// The member's LinkedIn, connected and read from the cloud.
export function linkedin(actor, {connected, verified}) {
  return {
    key: "network.linkedin", name: "LinkedIn", badge: "in", description: "Read posts and who engaged.", unlocks: ["People who engaged"],
    configured: true, connection_id: "reading-1", status: "connected", connected_at: connected,
    configuration: {actor: {name: actor}, collection_permission: {version: 1, mode: "cloud_preferred"}, session_state: "available",
      session_expires_at: new Date(Date.parse(verified) + 30 * DAY).toISOString(), session_verified_at: verified},
  };
}

// Sending connection requests, on, with this week's numbers.
export function sending({week, next, timezone = "Europe/Berlin"}) {
  return {
    offered: true, reading: true,
    sending: {on: true, paused: false, held: false, paused_reason: null, paused_until: null, daily_cap: 15, weekly_cap: 60, start_minute: 540, end_minute: 1020, timezone},
    pause_text: null,
    consent: {version: 1, text: "Send connection requests from my LinkedIn to people who fit my ICP and engaged with my posts or viewed my profile, and to anyone I choose. A few each weekday, stopping at LinkedIn’s first warning. I can turn it off anytime."},
    week: {...week, next_send: next},
    next_open: next,
  };
}
