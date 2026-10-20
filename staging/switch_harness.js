// Runs site/switch.js (read from stdin) with a tiny DOM, fetch and Notification stub, then drives
// the browser re-check loop through alert scenarios. Prints one JSON line: {scenarios: [...]}.
"use strict";
const vm = require("vm");
const src = require("fs").readFileSync(0, "utf8");

function makeEl(id) {
  const listeners = {};
  return {
    id, value: "", textContent: "", style: {}, childNodes: [], disabled: false,
    appendChild(c) { this.childNodes.push(c); return c; },
    setAttribute() {}, addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
    click() { (listeners.click || []).forEach((f) => f({})); }, scrollIntoView() {}, select() {}, remove() {},
    set innerHTML(v) { this.childNodes = []; }, get firstChild() { return this.childNodes[0]; },
  };
}

async function scenario(name, first, second, enable, expected) {
  const els = {};
  const $ = (id) => (els[id] = els[id] || makeEl(id));
  const notes = [];
  let responses = [first, second];
  let docReady = [];
  const ctx = {
    console, Promise, JSON, Object, Array, String, Number, Math, Date, isNaN, encodeURIComponent, URLSearchParams,
    setTimeout, clearTimeout,
    setInterval: () => 1, clearInterval: () => {},
    location: { search: "?fields=test-field", origin: "https://preview.example" },
    navigator: {},
    fetch: (url) => {
      const body = String(url).startsWith("/v1/status") ? (responses.shift() || second) : {};
      return Promise.resolve({ status: 200, json: () => Promise.resolve(JSON.parse(JSON.stringify(body))) });
    },
    document: {
      getElementById: $, createElement: (tag) => makeEl(tag), createTextNode: (t) => ({ text: t }),
      querySelectorAll: () => [], body: makeEl("body"),
      addEventListener: (ev, fn) => { if (ev === "DOMContentLoaded") docReady.push(fn); },
    },
  };
  function Notification(title, opts) { notes.push({ title, body: (opts || {}).body }); }
  Notification.permission = "granted";
  Notification.requestPermission = () => Promise.resolve("granted");
  ctx.Notification = Notification;
  ctx.window = ctx;
  ctx.window.confirm = () => true;
  vm.createContext(ctx);
  vm.runInContext(src, ctx);
  $("checktime").value = "19:00";
  docReady.forEach((f) => f());
  await new Promise((r) => setTimeout(r, 20));        // first board load
  if (enable) { $("notifybtn").click(); await new Promise((r) => setTimeout(r, 20)); }
  $("refreshbtn").click();                              // the 10-minute re-check, run once
  await new Promise((r) => setTimeout(r, 20));
  return { name, expected, notified: notes.length > 0, notifications: notes };
}

const base = { field_id: "test-field", field: "Test Field", official_status: "unknown", game_time_play_probability_percent: 80,
  rain_chance_percent: 20, reason: "", verification_level: "confirmed", official_status_check: {}, community_reports: { count: 0, by_status: {} } };
const w = (o) => Object.assign({}, base, o);
(async () => {
  const out = [];
  out.push(await scenario("official status unknown -> cancelled", base, w({ official_status: "cancelled", game_time_play_probability_percent: 0 }), true, true));
  out.push(await scenario("odds 80% -> 40% (crosses 50%)", base, w({ game_time_play_probability_percent: 40 }), true, true));
  out.push(await scenario("odds 80% -> 55% (stays above 50%)", base, w({ game_time_play_probability_percent: 55 }), true, false));
  out.push(await scenario("new player report", base, w({ community_reports: { count: 1, by_status: { cancelled: 1 } } }), true, true));
  out.push(await scenario("no change", base, base, true, false));
  out.push(await scenario("change but alerts not enabled", base, w({ official_status: "cancelled" }), false, false));
  out.push(await scenario("status request error, then cancelled", { error: "network" }, w({ official_status: "cancelled" }), true, false));
  console.log(JSON.stringify({ scenarios: out }));
})().catch((e) => { console.log(JSON.stringify({ error: String(e && e.stack || e) })); });
