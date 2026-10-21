/* Rain-Outsource "Switch free in 5 minutes" page. Uses only the public API on this origin. */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var MAX_FIELDS = 25;
  var board = [];          // field_ids on the board
  var lastSeen = {};       // field_id -> {official, pct, reports}
  var notifyTimer = null;

  function el(tag, attrs, children) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "class") n.className = attrs[k];
      else if (k === "text") n.textContent = attrs[k];
      else n.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { if (c) n.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
    return n;
  }
  function getJSON(url, opts) {
    return fetch(url, Object.assign({headers: {"Accept": "application/json"}}, opts || {}))
      .then(function (r) { return r.json().then(function (d) { d.__status = r.status; return d; }); });
  }
  function ago(iso) {
    if (!iso) return null;
    var s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (isNaN(s)) return null;
    if (s < 90) return "just now";
    if (s < 5400) return Math.round(s / 60) + " min ago";
    if (s < 172800) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " days ago";
  }
  function pad(n) { return String(n).padStart(2, "0"); }
  function isoWithOffset(dateStr, timeStr) {
    var d = new Date(dateStr + "T" + (timeStr || "19:00") + ":00");
    if (isNaN(d.getTime())) return null;
    var off = -d.getTimezoneOffset(), sign = off >= 0 ? "+" : "-", a = Math.abs(off);
    return dateStr + "T" + (timeStr || "19:00") + ":00" + sign + pad(Math.floor(a / 60)) + ":" + pad(a % 60);
  }
  function today() { var d = new Date(); return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()); }

  // ---------------------------------------------------------------- parsing
  function parseCSV(text) {
    var rows = [], row = [], cell = "", q = false;
    for (var i = 0; i < text.length; i++) {
      var c = text[i];
      if (q) { if (c === '"') { if (text[i + 1] === '"') { cell += '"'; i++; } else q = false; } else cell += c; }
      else if (c === '"') q = true;
      else if (c === ",") { row.push(cell); cell = ""; }
      else if (c === "\n" || c === "\r") { if (c === "\r" && text[i + 1] === "\n") i++; row.push(cell); rows.push(row); row = []; cell = ""; }
      else cell += c;
    }
    if (cell || row.length) { row.push(cell); rows.push(row); }
    return rows.filter(function (r) { return r.some(function (x) { return x.trim(); }); });
  }
  function parseFieldList(text) {
    text = (text || "").replace(/^\ufeff/, "");
    var rows = parseCSV(text);
    if (!rows.length) return [];
    var head = rows[0].map(function (h) { return h.trim().toLowerCase(); });
    var nameCol = head.findIndex(function (h) { return ["name", "field", "field_name", "venue", "location", "park"].indexOf(h) >= 0; });
    if (nameCol >= 0) {
      var col = function (names) { return head.findIndex(function (h) { return names.indexOf(h) >= 0; }); };
      var st = col(["state", "st"]), lat = col(["lat", "latitude"]), lon = col(["lon", "lng", "long", "longitude"]), city = col(["city", "town"]);
      return rows.slice(1).map(function (r) {
        return {raw: r.join(", "), name: (r[nameCol] || "").trim(), state: st >= 0 ? (r[st] || "").trim().toUpperCase() : "",
                city: city >= 0 ? (r[city] || "").trim() : "",
                lat: lat >= 0 ? parseFloat(r[lat]) : NaN, lon: lon >= 0 ? parseFloat(r[lon]) : NaN};
      }).filter(function (x) { return x.name || !isNaN(x.lat); });
    }
    return text.split(/\r?\n/).map(function (line) {
      line = line.trim();
      if (!line) return null;
      var m = line.match(/^(-?\d{1,2}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)\s*,?\s*(.*)$/);
      if (m) return {raw: line, name: m[3].trim(), state: "", city: "", lat: parseFloat(m[1]), lon: parseFloat(m[2])};
      var parts = line.split(",").map(function (p) { return p.trim(); }).filter(Boolean);
      var state = "";
      if (parts.length > 1 && /^[A-Za-z]{2}$/.test(parts[parts.length - 1])) state = parts.pop().toUpperCase();
      return {raw: line, name: parts[0] || "", city: parts.slice(1).join(", "), state: state, lat: NaN, lon: NaN};
    }).filter(Boolean);
  }

  // ---------------------------------------------------------------- step 1: find fields
  function searchField(item) {
    var p = new URLSearchParams({verification: "all", limit: "5"});
    if (!isNaN(item.lat) && !isNaN(item.lon)) { p.set("near", item.lat + "," + item.lon); p.set("radius_km", "3"); }
    else { p.set("q", item.name); if (item.state) p.set("state", item.state); }
    return getJSON("/v1/fields?" + p.toString()).then(function (d) {
      var found = (d.fields || []);
      if (!found.length && item.city && isNaN(item.lat)) {   // second try: name + city as one query
        p.set("q", item.name + " " + item.city);
        return getJSON("/v1/fields?" + p.toString()).then(function (d2) { return d2.fields || []; });
      }
      if (item.name && !isNaN(item.lat)) {  // near search: put name matches first
        var n = item.name.toLowerCase();
        var hit = function (f) { return (f.name || "").toLowerCase().indexOf(n) >= 0 ? 0 : 1; };
        found.sort(function (a, b) { return hit(a) - hit(b); });
      }
      return found;
    }).catch(function () { return []; });
  }
  function label(f) {
    var where = [f.city, f.state].filter(Boolean).join(", ");
    return (f.name || f.field_id) + (where ? " (" + where + ")" : "") + (f.verification_level === "confirmed" ? ", hand-checked" : "");
  }
  function findFields() {
    var items = parseFieldList($("fieldlist").value).slice(0, MAX_FIELDS);
    var box = $("matches");
    box.textContent = "";
    if (!items.length) { box.appendChild(el("p", {class: "err", text: "Add at least one field first."})); return; }
    box.appendChild(el("p", {class: "hint", text: "Searching " + items.length + " field(s)…"}));
    Promise.all(items.map(searchField)).then(function (results) {
      box.textContent = "";
      box.appendChild(el("p", {class: "hint", text: "Check each match. Pick \"Skip\" if it's wrong."}));
      results.forEach(function (found, i) {
        var sel = el("select", {"data-row": String(i), "aria-label": "Match for " + items[i].raw});
        found.forEach(function (f) { var o = el("option", {value: f.field_id, text: label(f)}); sel.appendChild(o); });
        sel.appendChild(el("option", {value: "", text: found.length ? "Skip this one" : "No match found (skip)"}));
        box.appendChild(el("div", {class: "match"}, [el("span", {text: items[i].raw + " →"}), sel]));
      });
      $("matchactions").style.display = "block";
    });
  }

  // ---------------------------------------------------------------- step 2: board
  var WORDS = {on: "On", playing: "On", open: "On", cancelled: "Cancelled", canceled: "Cancelled", closed: "Closed",
               delayed: "Delayed", postponed: "Postponed", unknown: "Not posted / couldn't read"};
  function pctClass(p, official) {
    if (official === "cancelled" || official === "closed" || official === "postponed") return "stop";
    if (p == null) return "unk";
    return p >= 75 ? "go" : p >= 45 ? "warn" : "stop";
  }
  function gameTime() {
    return isoWithOffset($("checkdate").value.trim() || today(), $("checktime").value || "19:00");
  }
  function report(fieldId, status, btn) {
    var words = {cancelled: "rained out / cancelled", playing: "playing"};
    if (!window.confirm("Share an anonymous report that this field is " + words[status] + " right now? Only send what you see yourself.")) return;
    btn.disabled = true;
    getJSON("/v1/reports", {method: "POST", headers: {"Content-Type": "application/json", "Accept": "application/json"},
                            body: JSON.stringify({field_id: fieldId, status: status})})
      .then(function (d) { btn.textContent = d.error ? (d.message || d.error) : "Thanks, report shared"; refreshBoard(); })
      .catch(function () { btn.textContent = "Couldn't send, try again"; btn.disabled = false; });
  }
  // "Was it on?": after the game, tell us whether it went ahead (feeds the venue's drainage memory and the scoreboard).
  function wasItOn(fieldId, outcome, btn, msg) {
    var day = $("checkdate").value.trim() || today();
    if (day > today()) { msg.textContent = "Ask again after the game: outcomes are for today or the last 7 days."; return; }
    var words = {played: "went ahead (played)", cancelled: "was called off"};
    if (!window.confirm("Share an anonymous report that the game on " + day + " " + words[outcome] + "? Only send what you know yourself.")) return;
    btn.disabled = true;
    getJSON("/v1/outcomes", {method: "POST", headers: {"Content-Type": "application/json", "Accept": "application/json"},
                             body: JSON.stringify({field_id: fieldId, date: day, outcome: outcome})})
      .then(function (d) {
        msg.textContent = d.__status === 404 ? "Outcome reports aren't live on this server yet." :
          d.error ? (d.message || d.error) : "Thanks! That helps us learn how this field drains.";
        if (d.error) btn.disabled = false;
      })
      .catch(function () { msg.textContent = "Couldn't send, try again"; btn.disabled = false; });
  }
  function card(fieldId, d) {
    if (d.error) return el("div", {class: "card"}, [el("div", {class: "name", text: fieldId}), el("p", {class: "err", text: d.message || d.error})]);
    var official = String(d.official_status || "unknown").toLowerCase();
    var pct = d.game_time_play_probability_percent;
    var chk = d.official_status_check || {};
    var fresh = [];
    if (chk.posted_at) fresh.push("Official call posted " + ago(chk.posted_at));
    if (d.official_status_last_checked && d.official_status_source_url) fresh.push("we checked the official source " + ago(d.official_status_last_checked));
    var src = d.official_status_source_url ? el("a", {href: d.official_status_source_url, target: "_blank", rel: "noopener", text: d.official_status_source_name || "official source"}) : null;
    var rep = d.community_reports || {}, by = rep.by_status || {};
    var repText = rep.count ? (rep.count + " player report(s) in the last " + (rep.window_hours || 12) + " h: " +
      Object.keys(by).filter(function (k) { return by[k]; }).map(function (k) { return by[k] + " " + k.replace("_", " "); }).join(", ") +
      (rep.latest_reported_at ? " (latest " + ago(rep.latest_reported_at) + ")" : "")) : "No player reports yet.";
    var b1 = el("button", {class: "secondary", text: "Rained out"}), b2 = el("button", {class: "secondary", text: "We're playing"});
    b1.addEventListener("click", function () { report(d.field_id || fieldId, "cancelled", b1); });
    b2.addEventListener("click", function () { report(d.field_id || fieldId, "playing", b2); });
    var wMsg = el("span", {class: "hint", text: ""});
    var w1 = el("button", {class: "secondary", text: "It was on"}), w2 = el("button", {class: "secondary", text: "It was called off"});
    w1.addEventListener("click", function () { wasItOn(d.field_id || fieldId, "played", w1, wMsg); });
    w2.addEventListener("click", function () { wasItOn(d.field_id || fieldId, "cancelled", w2, wMsg); });
    var embed = el("button", {class: "secondary", text: "Embed code"});
    embed.addEventListener("click", function () {
      $("embedcode").textContent = '<iframe src="' + location.origin + "/switch/embed?field_id=" + encodeURIComponent(d.field_id || fieldId) +
        '" width="340" height="290" style="border:0" title="Field status and rain odds"></iframe>';
      $("keep").scrollIntoView({behavior: "smooth"});
    });
    return el("div", {class: "card"}, [
      el("div", {class: "top"}, [
        el("div", {}, [el("div", {class: "name", text: d.field || fieldId}),
                       el("div", {class: "hint", text: d.verification_level === "confirmed" ? "Hand-checked venue" : "Map venue (weather-based odds, no official feed)"})])
      ]),
      el("div", {}, [el("strong", {text: "Official status: "}), WORDS[official] || official, src ? " · " : "", src]),
      el("div", {class: "hint", text: fresh.length ? fresh.join("; ") + "." : (chk.message || "No freshness info for this source.")}),
      fresh.length && chk.message ? el("div", {class: "hint", text: chk.message}) : null,
      el("div", {}, [el("span", {class: "pct " + pctClass(pct, official), text: pct == null ? "?" : pct + "%"}),
                     el("span", {class: "hint", text: " estimate, not an official call · "}),
                     el("a", {href: "/accuracy", target: "_blank", rel: "noopener", text: "How accurate is this?"})]),
      el("div", {text: "Rain chance at game time: " + (d.rain_chance_percent == null ? "?" : d.rain_chance_percent + "%") + ". " + (d.reason || "")}),
      el("div", {class: "hint", text: repText}),
      el("div", {class: "row"}, [b1, b2, embed]),
      el("div", {class: "row"}, [el("strong", {text: "Was it on? "}), w1, w2, wMsg])
    ]);
  }
  function refreshBoard() {
    var gt = gameTime(), box = $("cards");
    if (!board.length) return Promise.resolve();
    if (!box.childNodes.length) box.appendChild(el("p", {class: "hint", text: "Checking " + board.length + " field(s)…"}));
    return Promise.all(board.map(function (id) {
      return getJSON("/v1/status?field_id=" + encodeURIComponent(id) + "&game_time=" + encodeURIComponent(gt))
        .catch(function () { return {error: "network", message: "Couldn't reach the service."}; });
    })).then(function (results) {
      box.textContent = "";
      results.forEach(function (d, i) { box.appendChild(card(board[i], d)); checkForAlert(board[i], d); });
      updateShare();
    });
  }
  function updateShare() {
    var p = new URLSearchParams({fields: board.join(",")});
    if ($("checkdate").value.trim()) p.set("date", $("checkdate").value.trim());
    if ($("checktime").value && $("checktime").value !== "19:00") p.set("time", $("checktime").value);
    $("sharelink").value = location.origin + "/switch?" + p.toString();
  }
  function showBoard(ids) {
    ids.forEach(function (id) { if (id && board.indexOf(id) < 0 && board.length < MAX_FIELDS) board.push(id); });
    if (!board.length) return;
    $("board").style.display = "block";
    refreshBoard();
  }

  // ---------------------------------------------------------------- alerts (in-tab notifications)
  function notify(title, body) {
    try { if (window.Notification && Notification.permission === "granted") new Notification(title, {body: body, icon: "/icon.png"}); } catch (e) { /* ignore */ }
  }
  function checkForAlert(id, d) {
    if (d.error) return;
    var now = {official: d.official_status, pct: d.game_time_play_probability_percent, reports: (d.community_reports || {}).count || 0};
    var prev = lastSeen[id];
    lastSeen[id] = now;
    if (!prev || !notifyTimer) return;
    var name = d.field || id;
    if (prev.official !== now.official) notify(name + ": official status changed", "Now: " + (WORDS[now.official] || now.official));
    else if (prev.pct >= 50 && now.pct < 50) notify(name + ": odds dropped to " + now.pct + "%", d.reason || "");
    else if (now.reports > prev.reports) notify(name + ": new player report", "Check the board for details.");
  }
  function enableNotifications() {
    if (!window.Notification) { $("notifystate").textContent = "This browser doesn't support notifications."; return; }
    Notification.requestPermission().then(function (p) {
      if (p !== "granted") { $("notifystate").textContent = "Notifications are blocked in this browser."; return; }
      if (notifyTimer) clearInterval(notifyTimer);
      notifyTimer = setInterval(refreshBoard, 10 * 60 * 1000);
      $("notifystate").textContent = board.length ? "On. Checking every 10 minutes while this tab is open." : "On. Add fields to your board first.";
    });
  }

  // ---------------------------------------------------------------- step 3: schedule
  function scheduleTable(d, subscribe) {
    var out = $("schedout");
    out.textContent = "";
    if (d.error) { out.appendChild(el("p", {class: "err", text: d.message || d.error})); return; }
    var s = d.summary || {};
    out.appendChild(el("p", {text: (s.evaluated || 0) + " game(s) checked in the next " + ((d.window || {}).days || 14) + " days, " + (s.at_risk || 0) + " at risk" +
      (s.unmatched_venue ? ", " + s.unmatched_venue + " with a venue we couldn't match" : "") + "."}));
    var tbl = el("table", {}, [el("tr", {}, [el("th", {text: "When"}), el("th", {text: "Game"}), el("th", {text: "Field"}), el("th", {text: "Odds (estimate)"}), el("th", {text: "Why"})])]);
    var ids = [];
    (d.games || []).forEach(function (g) {
      var v = g.venue || {};
      if (v.field_id && ids.indexOf(v.field_id) < 0) ids.push(v.field_id);
      var when = g.start ? new Date(g.start).toLocaleString([], {weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit"}) : "";
      var p = g.play_probability_percent;
      tbl.appendChild(el("tr", {}, [el("td", {text: when}), el("td", {text: g.title || ""}), el("td", {text: v.name || g.location_text || "?"}),
        el("td", {class: pctClass(p, g.official_status), text: p == null ? (g.status || "?") : p + "%" + (g.at_risk ? " (at risk)" : "")}),
        el("td", {class: "hint", text: g.reason || (g.risk_reasons || []).join("; ")})]));
    });
    out.appendChild(tbl);
    out.appendChild(el("p", {class: "hint"}, ["Odds are estimates; an official status always comes first. ",
      el("a", {href: "/accuracy", target: "_blank", rel: "noopener", text: "How accurate is this?"})]));
    if (subscribe) out.appendChild(subscribe);
    if (ids.length) {
      var add = el("button", {class: "secondary", text: "Add these " + ids.length + " field(s) to my board"});
      add.addEventListener("click", function () { showBoard(ids); $("board").scrollIntoView({behavior: "smooth"}); });
      out.appendChild(add);
    }
  }
  function checkIcsUrl() {
    var url = $("icsurl").value.trim().replace(/^webcal:\/\//i, "https://");
    if (!/^https?:\/\//i.test(url)) { scheduleTable({error: "bad_url", message: "Paste a link that starts with https:// or webcal://"}); return; }
    $("schedout").textContent = "Checking…";
    var qs = "ics_url=" + encodeURIComponent(url);
    getJSON("/v1/schedule/check?" + qs).then(function (d) {
      var feed = location.host + "/v1/schedule/check?" + qs + "&format=ics";
      var sub = el("div", {}, [
        el("h3", {text: "Subscribe to the risk calendar (updates on its own)"}),
        el("div", {class: "row"}, [
          el("a", {href: "webcal://" + feed, text: "Subscribe (Apple / Outlook)"}), " · ",
          el("a", {href: "https://calendar.google.com/calendar/r?cid=" + encodeURIComponent("webcal://" + feed), target: "_blank", rel: "noopener", text: "Add to Google Calendar"}), " · ",
          el("a", {href: location.protocol + "//" + feed, text: "Plain .ics link"})
        ]),
        el("p", {class: "hint", text: "Share this with players. Calendar apps refresh subscribed calendars on their own schedule, often every few hours."})
      ]);
      scheduleTable(d, d.error ? null : sub);
    }).catch(function () { scheduleTable({error: "network", message: "Couldn't reach the service."}); });
  }
  function postSchedule(fmt) {
    var text = $("schedtext").value;
    var ics = /BEGIN:VCALENDAR/i.test(text);
    return fetch("/v1/schedule/check" + (fmt === "ics" ? "?format=ics" : ""), {method: "POST",
      headers: {"Content-Type": ics ? "text/calendar" : "text/csv"}, body: text});
  }
  function checkPasted() {
    if (!$("schedtext").value.trim()) { scheduleTable({error: "empty", message: "Paste a schedule or upload a file first."}); return; }
    $("schedout").textContent = "Checking…";
    postSchedule("json").then(function (r) { return r.json(); }).then(function (d) {
      var dl = el("button", {class: "secondary", text: "Download risk calendar (.ics, one-time file)"});
      dl.addEventListener("click", function () {
        postSchedule("ics").then(function (r) { return r.blob(); }).then(function (b) {
          var a = el("a", {href: URL.createObjectURL(b), download: "rain-odds-schedule.ics"});
          document.body.appendChild(a); a.click(); a.remove();
        });
      });
      scheduleTable(d, d.error ? null : el("div", {}, [dl, el("p", {class: "hint", text: "A downloaded file doesn't update. For live updates, use a calendar link (option A)."})]));
    }).catch(function () { scheduleTable({error: "network", message: "Couldn't reach the service."}); });
  }
  function readFile(input, target, then) {
    var f = input.files && input.files[0];
    if (!f) return;
    if (f.size > 1024 * 1024) { window.alert("That file is over 1 MB."); return; }
    f.text().then(function (t) { $(target).value = t; if (then) then(); });
  }

  // ---------------------------------------------------------------- wiring
  document.addEventListener("DOMContentLoaded", function () {
    $("checkdate").value = today();
    $("findbtn").addEventListener("click", findFields);
    $("fieldfile").addEventListener("change", function () { readFile($("fieldfile"), "fieldlist", findFields); });
    $("buildbtn").addEventListener("click", function () {
      var ids = Array.prototype.map.call(document.querySelectorAll("#matches select"), function (s) { return s.value; });
      showBoard(ids.filter(Boolean));
      $("board").scrollIntoView({behavior: "smooth"});
    });
    $("refreshbtn").addEventListener("click", function () { $("cards").textContent = ""; refreshBoard(); });
    $("copyshare").addEventListener("click", function () {
      $("sharelink").select();
      if (navigator.clipboard) navigator.clipboard.writeText($("sharelink").value);
    });
    $("icsbtn").addEventListener("click", checkIcsUrl);
    $("schedbtn").addEventListener("click", checkPasted);
    $("schedfile").addEventListener("change", function () { readFile($("schedfile"), "schedtext", checkPasted); });
    $("notifybtn").addEventListener("click", enableNotifications);
    var qp = new URLSearchParams(location.search);
    if (qp.get("date")) $("checkdate").value = qp.get("date");
    if (qp.get("time")) $("checktime").value = qp.get("time");
    if (qp.get("fields")) showBoard(qp.get("fields").split(",").map(function (s) { return s.trim(); }));
    if (qp.get("ics_url")) { $("icsurl").value = qp.get("ics_url"); checkIcsUrl(); }
  });

  // exposed for tests in a JS runtime, harmless in browsers
  window.RainSwitch = {parseFieldList: parseFieldList, isoWithOffset: isoWithOffset};
})();
