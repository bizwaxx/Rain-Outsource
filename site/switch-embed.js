/* One-field widget for league websites: /switch/embed?field_id=...&time=HH:MM (viewer's time zone, default next hour).
   After the game time (or with &ask=1) it shows a "Was it on?" row that posts to /v1/outcomes after a confirmation. */
(function () {
  "use strict";
  function pad(n) { return String(n).padStart(2, "0"); }
  function iso(d) {
    var off = -d.getTimezoneOffset(), a = Math.abs(off);
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + "T" + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":00" +
      (off >= 0 ? "+" : "-") + pad(Math.floor(a / 60)) + ":" + pad(a % 60);
  }
  function ago(s) { var m = (Date.now() - new Date(s).getTime()) / 60000; return m < 90 ? Math.max(1, Math.round(m)) + " min ago" : Math.round(m / 60) + " h ago"; }
  function line(cls, text) { var d = document.createElement("div"); if (cls) d.className = cls; d.textContent = text; return d; }
  var box = document.getElementById("box");
  var q = new URLSearchParams(location.search), id = q.get("field_id");
  if (!id) { box.textContent = "Add ?field_id=… to the embed link."; return; }
  var when = new Date();
  if (q.get("time") && /^\d{1,2}:\d{2}$/.test(q.get("time"))) { var t = q.get("time").split(":"); when.setHours(+t[0], +t[1], 0, 0); }
  else { when.setMinutes(0, 0, 0); when.setHours(when.getHours() + 1); }
  fetch("/v1/status?field_id=" + encodeURIComponent(id) + "&game_time=" + encodeURIComponent(iso(when)), {headers: {Accept: "application/json"}})
    .then(function (r) { return r.json(); })
    .then(function (d) {
      box.textContent = "";
      if (d.error) { box.appendChild(line("", d.message || d.error)); return; }
      var p = d.game_time_play_probability_percent, off = String(d.official_status || "unknown");
      var cls = /cancel|closed|postpon/.test(off) ? "stop" : p == null ? "unk" : p >= 75 ? "go" : p >= 45 ? "warn" : "stop";
      box.appendChild(line("name", d.field || id));
      box.appendChild(line("", "Official: " + (off === "unknown" ? "not posted / couldn't read" : off) + "."));
      var chk = d.official_status_check || {};
      if (chk.posted_at) box.appendChild(line("hint", "Official call posted " + ago(chk.posted_at) + "."));
      if (d.official_status_last_checked && d.official_status_source_url) box.appendChild(line("hint", "Official source checked " + ago(d.official_status_last_checked) + "."));
      box.appendChild(line("pct " + cls, (p == null ? "?" : p + "%") + " chance of play (estimate)"));
      box.appendChild(line("hint", "Rain chance " + (d.rain_chance_percent == null ? "?" : d.rain_chance_percent + "%") + " at " + when.toLocaleTimeString([], {hour: "numeric", minute: "2-digit"}) + "."));
      var acc = document.createElement("a");
      acc.href = "/accuracy"; acc.target = "_blank"; acc.rel = "noopener"; acc.textContent = "How accurate is this?";
      var al = line("hint", "Estimate, not an official call. "); al.appendChild(acc); box.appendChild(al);
      var r = (d.community_reports || {}).count;
      if (r) box.appendChild(line("hint", r + " player report(s) recently."));
      if (when <= new Date() || q.get("ask") === "1") {        // after the game: "Was it on?"
        var row = line("hint", "Was it on? "), msg = line("hint", "");
        [["played", "Yes, played"], ["cancelled", "Called off"]].forEach(function (o) {
          var b = document.createElement("button"); b.textContent = o[1]; b.type = "button";
          b.addEventListener("click", function () {
            var day = when.getFullYear() + "-" + pad(when.getMonth() + 1) + "-" + pad(when.getDate());
            if (!window.confirm("Share an anonymous report that the game on " + day + " " + (o[0] === "played" ? "went ahead" : "was called off") + "?")) return;
            b.disabled = true;
            fetch("/v1/outcomes", {method: "POST", headers: {"Content-Type": "application/json", Accept: "application/json"},
                                   body: JSON.stringify({field_id: d.field_id || id, date: day, outcome: o[0]})})
              .then(function (r) { return r.json().then(function (j) { msg.textContent = r.status === 404 ? "Not live yet." : j.error ? (j.message || j.error) : "Thanks!"; }); })
              .catch(function () { msg.textContent = "Couldn't send."; b.disabled = false; });
          });
          row.appendChild(b);
        });
        box.appendChild(row); box.appendChild(msg);
      }
      var a = document.createElement("a");
      a.href = "/switch?fields=" + encodeURIComponent(d.field_id || id); a.target = "_blank"; a.rel = "noopener";
      a.textContent = "Details and free alerts · Rain-Outsource";
      var f = line("hint", ""); f.appendChild(a); box.appendChild(f);
    })
    .catch(function () { box.textContent = "Couldn't reach Rain-Outsource right now."; });
})();
