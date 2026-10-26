"""Status page served with ``http.server``.

``/`` is theme D, the aura overview. ``/c`` is theme C, the denser HUD.
``/crt`` is the television layout for theme D. ``/crt/c`` is the same layout
for theme C, including the top-blocked strip. ``?crt=1`` on ``/`` or ``/c``
is the theme D television page.
The documents are static. The browser fills them from ``/api/stats`` and
``/api/crt``.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlparse

from piadblock.crt import LookupFeed, format_local_time
from piadblock.stats import Stats


def format_count(value: object) -> str:
    """Thousands separators for a count shown on the page."""
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        number = 0
    return f"{number:,}"


def format_recent_time(ts: object) -> str:
    """Page clock for one blocked query. A bad timestamp is blank, not the raw epoch."""
    try:
        return format_local_time(float(ts))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


# Shared by both themes. Numbers come from the JSON routes; this script
# never invents a lookup, a total, or a delta.
CLIENT_JS = r"""
(function () {
  var theme = document.documentElement.getAttribute("data-theme") || "d";
  var crtMode = document.documentElement.getAttribute("data-crt") === "1";
  var CRT_ROWS = 6;
  var crtArmFullscreen = true;
  var lastStats = null;
  var lastCrt = null;
  var uptimeAnchor = null;

  function num(value) {
    var n = Number(value);
    return isFinite(n) ? n : 0;
  }

  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function pad(n) {
    n = Math.floor(num(n));
    return (n < 10 ? "0" : "") + n;
  }

  function fmtInt(value) {
    var n = Math.round(num(value));
    var sign = n < 0 ? "-" : "";
    var s = String(Math.abs(n));
    var out = "";
    var i;
    for (i = 0; i < s.length; i++) {
      if (i && (s.length - i) % 3 === 0) out += ",";
      out += s.charAt(i);
    }
    return sign + out;
  }

  function text(id, value) {
    var el = document.getElementById(id);
    if (el) el.textContent = value;
  }

  function clock(ts) {
    var n = Number(ts);
    if (!isFinite(n)) return "—";
    var d = new Date(n * 1000);
    if (isNaN(d.getTime())) return "—";
    return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }

  function uptimeText(total) {
    total = Math.max(0, Math.floor(num(total)));
    var days = Math.floor(total / 86400);
    total = total % 86400;
    var h = Math.floor(total / 3600);
    total = total % 3600;
    var m = Math.floor(total / 60);
    var s = total % 60;
    return days + "d " + pad(h) + ":" + pad(m) + ":" + pad(s);
  }

  function formatPercent(blocked, queries) {
    if (!queries) return "0.0%";
    return (Math.round((1000 * blocked) / queries) / 10).toFixed(1) + "%";
  }

  function relDelta(current, previous) {
    if (previous === null || previous === undefined) return null;
    if (previous === 0) return current === 0 ? 0 : null;
    return ((current - previous) / previous) * 100;
  }

  function rateDelta(curQ, curB, prevQ, prevB) {
    if (!(prevQ > 0)) return null;
    var cur = curQ > 0 ? (100 * curB / curQ) : 0;
    var prev = 100 * prevB / prevQ;
    return cur - prev;
  }

  function formatDelta(value) {
    if (value === null || value === undefined || !isFinite(value)) {
      return { text: "—", kind: "none" };
    }
    if (Math.abs(value) < 0.05) return { text: "0.0%", kind: "flat" };
    var abs = Math.abs(value).toFixed(1);
    if (value > 0) return { text: "\u25b2 +" + abs + "%", kind: "up" };
    return { text: "\u25bc \u2212" + abs + "%", kind: "down" };
  }

  function setPill(id, value, upClass) {
    var el = document.getElementById(id);
    if (!el) return;
    var shown = formatDelta(value);
    var base = theme === "c" ? "delta" : "pill";
    var positive = theme === "c" ? "up" : (upClass || "up");
    var kindClass = "flat";
    if (shown.kind === "up") kindClass = positive;
    else if (shown.kind === "down") kindClass = base === "pill" ? "alert" : "down";
    el.className = base + " " + kindClass;
    el.textContent = shown.text;
  }

  function asList(value) {
    if (!value || typeof value !== "object" || typeof value.length !== "number") return [];
    return value;
  }

  function priorBucket(stats, todayDate) {
    var days = stats && stats.days;
    if (!days || !todayDate || typeof days !== "object") return null;
    var bestKey = "";
    var best = null;
    var key;
    for (key in days) {
      if (!Object.prototype.hasOwnProperty.call(days, key)) continue;
      if (key >= todayDate) continue;
      if (key > bestKey) {
        bestKey = key;
        best = days[key];
      }
    }
    if (!best || typeof best !== "object") return null;
    if (best.queries == null || best.blocked == null) return null;
    return { date: bestKey, queries: num(best.queries), blocked: num(best.blocked) };
  }

  function todayCounts(stats, crt) {
    var today = stats && stats.today;
    if (today && today.queries != null && today.blocked != null) {
      return {
        queries: num(today.queries),
        blocked: num(today.blocked),
        date: String(today.date || (crt && crt.date) || "")
      };
    }
    if (crt && (crt.queries != null || crt.blocked != null)) {
      return {
        queries: num(crt.queries),
        blocked: num(crt.blocked),
        date: String(crt.date || "")
      };
    }
    return null;
  }

  function filterText() {
    var el = document.getElementById("qfilter");
    if (!el) return "";
    return String(el.value || "").replace(/^\s+|\s+$/g, "").toLowerCase();
  }

  function visible(row, filter) {
    if (!filter) return true;
    var blob = [row.name, row.client, row.status, row.qtype].join(" ").toLowerCase();
    return blob.indexOf(filter) !== -1;
  }

  function stateKind(status) {
    if (status === "BLOCKED") return "blocked";
    if (status === "FAIL") return "fail";
    return "ok";
  }

  function field(value) {
    var raw = value == null ? "" : String(value);
    return raw ? esc(raw) : "—";
  }

  function renderLookups(rows) {
    var filter = filterText();
    var html = "";
    var shown = 0;
    var i;
    var row;
    var kind;
    var status;
    for (i = 0; i < rows.length; i++) {
      row = rows[i] || {};
      if (!visible(row, filter)) continue;
      if (crtMode && shown >= CRT_ROWS) break;
      shown++;
      status = row.status == null ? "" : String(row.status);
      kind = stateKind(status);
      if (crtMode) {
        html += '<div class="row ' + kind + '">' +
          '<span class="status">' + field(status) + "</span>" +
          '<span class="domain" dir="rtl"><span class="domain-text" dir="ltr">' +
          field(row.name) + "</span></span>" +
          "</div>";
      } else if (theme === "c") {
        html += '<div class="row ' + kind + '">' +
          '<span class="ts">' + esc(clock(row.ts)) + "</span>" +
          '<span class="status">' + field(status) + "</span>" +
          '<span class="domain" title="' + esc(row.name) + '">' + field(row.name) + "</span>" +
          '<span class="qtype">' + field(row.qtype) + "</span>" +
          '<span class="client">' + field(row.client) + "</span>" +
          "</div>";
      } else {
        html += '<tr class="row-' + kind + '">' +
          '<td class="col-check"><span class="chk"></span></td>' +
          "<td><div class=\"proc\"><span class=\"name\">" + field(row.name) + "</span>" +
          "<span class=\"sub\">" + field(row.client) + "</span></div></td>" +
          '<td class="val">' + field(row.qtype) + "</td>" +
          '<td class="ts">' + esc(clock(row.ts)) + "</td>" +
          '<td><span class="state ' + kind + '">' + field(status) + "</span></td>" +
          "</tr>";
      }
    }
    var empty = rows.length ? "no matching lookups" : "no lookups yet";
    if (!shown) {
      if (crtMode || theme === "c") html = '<div class="row empty">' + empty + "</div>";
      else html = '<tr class="empty"><td></td><td colspan="4">' + empty + "</td></tr>";
    }
    if (crtMode || theme === "c") {
      var feed = document.getElementById("feed");
      if (feed) feed.innerHTML = html;
    } else {
      var body = document.getElementById("tx-body");
      if (body) body.innerHTML = html;
    }
  }

  function renderTops(stats) {
    var el = document.getElementById("tops");
    if (!el) return;
    var rows = asList(stats && stats.today && stats.today.top_blocked);
    if (!rows.length) {
      el.innerHTML = '<span class="none">—</span>';
      return;
    }
    var html = "";
    var n = Math.min(rows.length, crtMode ? 4 : 8);
    var i;
    var item;
    for (i = 0; i < n; i++) {
      item = rows[i] || {};
      if (crtMode) {
        html += '<span title="' + esc(item.domain) + '"><b>' + esc(fmtInt(item.count)) + "</b>" +
          field(item.domain) + "</span>";
      } else {
        html += '<span title="' + esc(item.domain) + '">' + field(item.domain) +
          "<b>" + esc(fmtInt(item.count)) + "</b></span>";
      }
    }
    el.innerHTML = html;
  }

  function paintUptime() {
    if (!uptimeAnchor) return;
    var extra = Math.floor((Date.now() - uptimeAnchor.at) / 1000);
    text("uptime", uptimeText(uptimeAnchor.sec + extra));
  }

  function paintBell(stats) {
    var dot = document.getElementById("bell-dot");
    var bell = document.getElementById("bell");
    if (!dot) return;
    var reasons = stats && stats.today && stats.today.upstream_errors;
    var names = ["timeout", "servfail", "refused", "socket", "truncation"];
    var total = 0;
    var parts = [];
    var i;
    var count;
    for (i = 0; i < names.length; i++) {
      count = reasons ? num(reasons[names[i]]) : 0;
      total += count;
      if (count) parts.push(names[i] + " " + count);
    }
    dot.hidden = total <= 0;
    if (bell) bell.title = total ? parts.join(", ") : "";
  }

  function setStatus(word, online) {
    var el = document.getElementById("status-word");
    if (!el) return;
    el.textContent = word;
    el.className = online ? "online" : "offline";
  }

  function render() {
    var stats = lastStats;
    var crt = lastCrt;
    var counts = todayCounts(stats, crt);
    if (counts) {
      text("kpi-q", fmtInt(counts.queries));
      text("kpi-b", fmtInt(counts.blocked));
      var bld = (stats && stats.blocklist_domains != null) ? stats.blocklist_domains : (crt ? crt.blocklist_domains : null);
      text("kpi-p", bld != null ? fmtInt(bld) : "—");
      text("since-label", counts.date ? ("--since=" + counts.date) : "--since=today");
      var prior = priorBucket(stats, counts.date);
      var note = prior ? ("vs " + prior.date) : "no prior day";
      text("note-q", note);
      text("note-b", note);
      text("note-p", "domains on list");
      setPill("delta-q", prior ? relDelta(counts.queries, prior.queries) : null, "up");
      setPill("delta-b", prior ? relDelta(counts.blocked, prior.blocked) : null, "cyan-up");
      setPill("delta-p", null, "up");
    }
    if (stats) {
      if (stats.uptime_seconds != null) {
        uptimeAnchor = { sec: num(stats.uptime_seconds), at: Date.now() };
        paintUptime();
      }
      text("ver", stats.version ? String(stats.version) : "—");
      if (stats.blocklist_domains != null) text("bl", fmtInt(stats.blocklist_domains));
      if (stats.allow_count != null) text("allow", fmtInt(stats.allow_count));
      if (stats.queries != null && stats.blocked != null) {
        text("proc", fmtInt(stats.queries) + "q " + fmtInt(stats.blocked) + "b " +
          num(stats.blocked_percent).toFixed(1) + "%");
      }
      paintBell(stats);
    } else if (crt && crt.blocklist_domains != null) {
      text("bl", fmtInt(crt.blocklist_domains));
      if (crt.allow_count != null) text("allow", fmtInt(crt.allow_count));
    }
    renderTops(stats);
    renderLookups(asList(crt && crt.lookups));
    var hosts = document.querySelectorAll("[data-host]");
    var host = (location && location.host) ? location.host : "—";
    var h;
    for (h = 0; h < hosts.length; h++) hosts[h].textContent = host;
  }

  function okJson(res) {
    if (!res.ok) throw new Error("http " + res.status);
    return res.json();
  }

  function load() {
    if (!window.fetch) {
      setStatus("NO_FETCH", false);
      return;
    }
    var pending = 2;
    var failures = 0;
    function done() {
      pending -= 1;
      if (pending > 0) return;
      setStatus(failures === 2 ? "OFFLINE" : "ONLINE", failures < 2);
      render();
    }
    fetch("/api/stats", { cache: "no-store" }).then(okJson).then(function (stats) {
      lastStats = stats;
      done();
    }).catch(function () {
      failures += 1;
      done();
    });
    fetch("/api/crt", { cache: "no-store" }).then(okJson).then(function (crt) {
      lastCrt = crt;
      done();
    }).catch(function () {
      failures += 1;
      done();
    });
  }

  function isFullscreen() {
    return !!(document.fullscreenElement || document.webkitFullscreenElement);
  }

  function requestPageFullscreen() {
    if (!crtMode || isFullscreen()) return;
    var root = document.documentElement;
    var req = root.requestFullscreen || root.webkitRequestFullscreen;
    if (!req) return;
    try {
      var pending = req.call(root);
      if (pending && pending.catch) pending.catch(function () {});
    } catch (err) {}
  }

  if (crtMode) {
    document.addEventListener("click", function () {
      if (crtArmFullscreen && !isFullscreen()) requestPageFullscreen();
    });
  }

  var filter = document.getElementById("qfilter");
  if (filter) {
    filter.addEventListener("input", render);
  }
  var refresh = document.getElementById("refresh-btn");
  if (refresh) refresh.addEventListener("click", load);
  document.addEventListener("keydown", function (ev) {
    if (crtMode && crtArmFullscreen && !ev.repeat && !ev.ctrlKey && !ev.metaKey && !ev.altKey && !isFullscreen()) {
      requestPageFullscreen();
    }
    var box = document.getElementById("qfilter");
    if (!box) return;
    if ((ev.ctrlKey || ev.metaKey) && (ev.key === "f" || ev.key === "F")) {
      ev.preventDefault();
      box.focus();
      if (box.select) box.select();
    } else if (ev.key === "Escape" && document.activeElement === box) {
      box.value = "";
      render();
    }
  });

  load();
  setInterval(load, 3000);
  setInterval(paintUptime, 1000);
})();
"""


def _page(html: str) -> bytes:
    return html.replace("/*__SCRIPT__*/", CLIENT_JS).encode("utf-8")


THEME_D_PAGE = r"""<!DOCTYPE html>
<html lang="en" data-theme="d">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>piadblock</title>
<style>
  :root {
    --bg: #010201;
    --panel: rgba(2, 12, 8, 0.72);
    --green: #3dff8a;
    --green-bright: #6fffb0;
    --green-dim: #1a7a45;
    --green-muted: #4a8a62;
    --cyan: #4ad4f0;
    --cyan-bright: #7eecff;
    --orange: #f0b84a;
    --orange-bright: #ffd078;
    --red: #ff4d5e;
    --text: #b8e8c8;
    --muted: #3d6a50;
    --line: rgba(61, 255, 138, 0.28);
    --font: ui-monospace, "SF Mono", Menlo, Consolas, "Liberation Mono", "Courier New", monospace;
    --sidebar-w: 210px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; }
  body {
    font-family: var(--font);
    color: var(--text);
    background: var(--bg);
    overflow: hidden;
    font-size: 12px;
    letter-spacing: 0.02em;
  }
  .grid-bg {
    position: fixed; inset: 0; z-index: 0;
    background:
      radial-gradient(ellipse 90% 60% at 55% 0%, rgba(61,255,138,0.06), transparent 55%),
      radial-gradient(ellipse 50% 40% at 80% 100%, rgba(74,212,240,0.04), transparent 50%),
      #010201;
    overflow: hidden;
  }
  .grid-bg::before {
    content: "";
    position: absolute;
    left: -20%; right: -20%; top: 18%; height: 140%;
    background:
      linear-gradient(rgba(61,255,138,0.09) 1px, transparent 1px),
      linear-gradient(90deg, rgba(61,255,138,0.07) 1px, transparent 1px);
    background-size: 48px 48px, 48px 48px;
    transform: perspective(420px) rotateX(58deg);
    transform-origin: center top;
    opacity: 0.55;
    mask-image: linear-gradient(to bottom, rgba(0,0,0,0.85), transparent 92%);
    -webkit-mask-image: linear-gradient(to bottom, rgba(0,0,0,0.85), transparent 92%);
  }
  .grid-bg::after {
    content: "";
    position: absolute; inset: 0;
    background: repeating-linear-gradient(0deg, transparent 0px, transparent 2px, rgba(0,0,0,0.22) 3px);
    opacity: 0.35;
    pointer-events: none;
  }
  .app {
    position: relative; z-index: 1;
    display: grid;
    grid-template-columns: var(--sidebar-w) 1fr;
    grid-template-rows: auto 1fr auto;
    height: 100vh;
    max-height: 100dvh;
  }
  .sidebar {
    grid-row: 1 / -1;
    display: flex;
    flex-direction: column;
    border-right: 1px solid var(--line);
    background: rgba(0, 6, 4, 0.55);
    box-shadow: inset -1px 0 12px rgba(61,255,138,0.06);
    min-width: 0;
  }
  .brand {
    padding: 14px 14px 12px;
    border-bottom: 1px solid var(--line);
    color: var(--green);
    font-size: 13px;
    font-weight: 700;
    text-shadow: 0 0 6px rgba(61,255,138,0.9), 0 0 18px rgba(61,255,138,0.55), 0 0 36px rgba(61,255,138,0.25);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .nav {
    flex: 1;
    padding: 10px 8px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .nav-item {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 7px 8px;
    color: var(--green-muted);
    font-size: 11px;
    border-radius: 2px;
    border: 1px solid transparent;
    text-decoration: none;
    white-space: nowrap;
    overflow: hidden;
    text-shadow: 0 0 8px rgba(61,255,138,0.15);
  }
  .nav-item .ico { width: 14px; height: 14px; flex-shrink: 0; opacity: 0.85; }
  .nav-item.active {
    color: #041208;
    background: rgba(61,255,138,0.92);
    border-color: rgba(61,255,138,0.6);
    box-shadow: 0 0 10px rgba(61,255,138,0.55), 0 0 24px rgba(61,255,138,0.25), inset 3px 0 0 #041208;
    font-weight: 700;
    text-shadow: none;
  }
  .nav-section {
    margin: 14px 8px 6px;
    color: var(--muted);
    font-size: 10px;
    letter-spacing: 0.08em;
  }
  .user-card {
    margin: 8px;
    padding: 10px;
    border: 1px solid var(--line);
    background: rgba(2, 14, 10, 0.75);
    display: flex;
    align-items: center;
    gap: 10px;
    box-shadow: 0 0 12px rgba(61,255,138,0.12), inset 0 0 20px rgba(61,255,138,0.04);
  }
  .avatar {
    width: 32px; height: 32px;
    border: 1px solid var(--green);
    display: flex; align-items: center; justify-content: center;
    color: var(--green);
    font-size: 11px; font-weight: 700;
    box-shadow: 0 0 10px rgba(61,255,138,0.45);
    flex-shrink: 0;
    background: rgba(61,255,138,0.08);
  }
  .user-meta { flex: 1; min-width: 0; }
  .user-meta .name {
    color: var(--green-bright);
    font-size: 11px;
    font-weight: 700;
    text-shadow: 0 0 8px rgba(61,255,138,0.7);
  }
  .user-meta .ip { color: var(--muted); font-size: 10px; margin-top: 2px; overflow: hidden; text-overflow: ellipsis; }
  .user-card .chev { color: var(--muted); font-size: 10px; }
  .topbar {
    grid-column: 2;
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 16px;
    border-bottom: 1px solid var(--line);
    background: rgba(0, 6, 4, 0.65);
  }
  .search {
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: center;
    border: 1px solid rgba(61,255,138,0.45);
    background: rgba(0, 8, 5, 0.7);
    padding: 0 0 0 12px;
    height: 34px;
    box-shadow: 0 0 12px rgba(61,255,138,0.18), inset 0 0 16px rgba(61,255,138,0.04);
  }
  .search .prompt-char {
    color: var(--green);
    text-shadow: 0 0 8px rgba(61,255,138,0.8);
    margin-right: 6px;
  }
  .search input {
    flex: 1;
    min-width: 0;
    background: transparent;
    border: 0;
    outline: none;
    color: var(--green-bright);
    font-family: var(--font);
    font-size: 12px;
  }
  .search input::placeholder { color: var(--muted); }
  .kbd {
    border-left: 1px solid rgba(61,255,138,0.35);
    padding: 0 10px;
    height: 100%;
    display: flex; align-items: center;
    color: var(--green-muted);
    font-size: 10px;
    letter-spacing: 0.04em;
    background: rgba(61,255,138,0.06);
    flex-shrink: 0;
  }
  .theme-link {
    flex-shrink: 0;
    display: inline-flex;
    align-items: center;
    height: 34px;
    padding: 0 10px;
    border: 1px solid rgba(74,212,240,0.65);
    color: var(--cyan-bright);
    text-decoration: none;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.06em;
    white-space: nowrap;
    background: rgba(74,212,240,0.08);
    box-shadow: 0 0 12px rgba(74,212,240,0.28);
    text-shadow: 0 0 8px rgba(74,212,240,0.6);
  }
  .theme-link:visited { color: var(--cyan-bright); }
  .theme-link:hover { background: rgba(74,212,240,0.18); }
  .bell {
    width: 34px; height: 34px;
    flex-shrink: 0;
    border: 1px solid rgba(61,255,138,0.35);
    display: flex; align-items: center; justify-content: center;
    position: relative;
    color: var(--green);
    background: rgba(0, 8, 5, 0.5);
    box-shadow: 0 0 10px rgba(61,255,138,0.15);
  }
  .bell .dot {
    position: absolute;
    top: 5px; right: 6px;
    width: 7px; height: 7px;
    background: var(--red);
    border-radius: 50%;
    box-shadow: 0 0 8px rgba(255,77,94,0.9);
  }
  .main {
    grid-column: 2;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    padding: 14px 16px 8px;
    min-height: 0;
  }
  .overview-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 14px;
    flex-wrap: wrap;
  }
  .overview-head h1 {
    font-size: clamp(18px, 2.4vw, 26px);
    font-weight: 800;
    color: var(--green-bright);
    letter-spacing: 0.06em;
    text-shadow: 0 0 8px rgba(61,255,138,0.95), 0 0 22px rgba(61,255,138,0.55), 0 0 48px rgba(61,255,138,0.28);
  }
  .status-line {
    margin-top: 4px;
    font-size: 11px;
    color: var(--green);
    text-shadow: 0 0 10px rgba(61,255,138,0.55);
  }
  .status-line .online { color: var(--green-bright); text-shadow: 0 0 12px rgba(61,255,138,0.9); }
  .status-line .offline { color: #ff8a95; text-shadow: 0 0 12px rgba(255,77,94,0.8); }
  .controls { display: flex; align-items: center; gap: 8px; }
  .since {
    display: flex;
    align-items: center;
    gap: 6px;
    height: 32px;
    padding: 0 10px;
    border: 1px solid rgba(61,255,138,0.35);
    background: rgba(0, 8, 5, 0.65);
    color: var(--green-muted);
    font-size: 11px;
    box-shadow: 0 0 8px rgba(61,255,138,0.12);
    white-space: nowrap;
  }
  .btn-exec {
    height: 32px;
    padding: 0 14px;
    border: 1px solid var(--green);
    background: rgba(61,255,138,0.18);
    color: var(--green-bright);
    font-family: var(--font);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.04em;
    cursor: pointer;
    box-shadow: 0 0 14px rgba(61,255,138,0.55), 0 0 32px rgba(61,255,138,0.28), inset 0 0 12px rgba(61,255,138,0.12);
    text-shadow: 0 0 10px rgba(61,255,138,0.8);
  }
  .kpis {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 12px;
    margin-bottom: 14px;
    flex-shrink: 0;
  }
  .kpi {
    position: relative;
    background: var(--panel);
    border: 1px solid;
    padding: 12px 14px 12px 16px;
    min-height: 108px;
    overflow: hidden;
    min-width: 0;
  }
  .kpi::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px; }
  .kpi.green {
    border-color: rgba(61,255,138,0.55);
    box-shadow: 0 0 18px rgba(61,255,138,0.22), 0 0 40px rgba(61,255,138,0.08), inset 0 0 30px rgba(61,255,138,0.04);
  }
  .kpi.green::before { background: var(--green); box-shadow: 0 0 12px rgba(61,255,138,0.9), 0 0 24px rgba(61,255,138,0.5); }
  .kpi.cyan {
    border-color: rgba(74,212,240,0.55);
    box-shadow: 0 0 18px rgba(74,212,240,0.22), 0 0 40px rgba(74,212,240,0.08), inset 0 0 30px rgba(74,212,240,0.04);
  }
  .kpi.cyan::before { background: var(--cyan); box-shadow: 0 0 12px rgba(74,212,240,0.9), 0 0 24px rgba(74,212,240,0.5); }
  .kpi.orange {
    border-color: rgba(240,184,74,0.55);
    box-shadow: 0 0 18px rgba(240,184,74,0.22), 0 0 40px rgba(240,184,74,0.08), inset 0 0 30px rgba(240,184,74,0.04);
  }
  .kpi.orange::before { background: var(--orange); box-shadow: 0 0 12px rgba(240,184,74,0.9), 0 0 24px rgba(240,184,74,0.5); }
  .kpi-top { display: flex; justify-content: space-between; align-items: flex-start; gap: 6px; }
  .kpi-label { font-size: 10px; letter-spacing: 0.08em; font-weight: 600; }
  .kpi.green .kpi-label { color: var(--green); text-shadow: 0 0 8px rgba(61,255,138,0.5); }
  .kpi.cyan .kpi-label { color: var(--cyan); text-shadow: 0 0 8px rgba(74,212,240,0.5); }
  .kpi.orange .kpi-label { color: var(--orange); text-shadow: 0 0 8px rgba(240,184,74,0.5); }
  .kpi-ico { width: 16px; height: 16px; opacity: 0.85; flex-shrink: 0; }
  .kpi-value {
    margin-top: 8px;
    font-size: clamp(22px, 2.8vw, 32px);
    font-weight: 800;
    letter-spacing: 0.02em;
    line-height: 1.1;
    font-variant-numeric: tabular-nums;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .kpi.green .kpi-value { color: var(--green-bright); text-shadow: 0 0 10px rgba(61,255,138,0.95), 0 0 28px rgba(61,255,138,0.45); }
  .kpi.cyan .kpi-value { color: var(--cyan-bright); text-shadow: 0 0 10px rgba(74,212,240,0.95), 0 0 28px rgba(74,212,240,0.45); }
  .kpi.orange .kpi-value { color: var(--orange-bright); text-shadow: 0 0 10px rgba(240,184,74,0.95), 0 0 28px rgba(240,184,74,0.45); }
  .kpi-delta { margin-top: 10px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .pill {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 7px;
    font-size: 10px;
    font-weight: 700;
    border: 1px solid;
    white-space: nowrap;
  }
  .pill.up {
    color: var(--green-bright);
    border-color: rgba(61,255,138,0.7);
    background: rgba(61,255,138,0.12);
    box-shadow: 0 0 10px rgba(61,255,138,0.35);
  }
  .pill.cyan-up {
    color: var(--cyan-bright);
    border-color: rgba(74,212,240,0.7);
    background: rgba(74,212,240,0.12);
    box-shadow: 0 0 10px rgba(74,212,240,0.35);
  }
  .pill.alert {
    color: #ff8a95;
    border-color: rgba(255,77,94,0.75);
    background: rgba(255,77,94,0.14);
    box-shadow: 0 0 12px rgba(255,77,94,0.4);
  }
  .pill.flat {
    color: var(--muted);
    border-color: rgba(61,255,138,0.28);
    background: transparent;
  }
  .delta-note { font-size: 10px; color: var(--muted); }
  .kpi.orange .delta-note { color: rgba(240,184,74,0.7); }
  .tx {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
    border: 1px solid rgba(61,255,138,0.35);
    background: var(--panel);
    box-shadow: 0 0 16px rgba(61,255,138,0.12), inset 0 0 24px rgba(61,255,138,0.03);
    overflow: hidden;
  }
  .tx-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 12px;
    border-bottom: 1px solid rgba(61,255,138,0.25);
  }
  .tx-title {
    display: flex;
    align-items: center;
    gap: 8px;
    color: var(--green);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-shadow: 0 0 8px rgba(61,255,138,0.6);
  }
  .tx-title .led {
    width: 7px; height: 7px;
    border-radius: 50%;
    background: var(--green);
    box-shadow: 0 0 8px rgba(61,255,138,0.95), 0 0 16px rgba(61,255,138,0.5);
  }
  .view-all { color: var(--green-muted); font-size: 11px; letter-spacing: 0.04em; text-shadow: 0 0 6px rgba(61,255,138,0.3); }
  .table-wrap { flex: 1; overflow: auto; min-height: 0; }
  table { width: 100%; border-collapse: collapse; font-size: 11px; }
  thead th {
    text-align: left;
    padding: 8px 10px;
    color: var(--green-muted);
    font-weight: 600;
    letter-spacing: 0.08em;
    font-size: 10px;
    border-bottom: 1px solid rgba(61,255,138,0.2);
    position: sticky;
    top: 0;
    background: rgba(2, 12, 8, 0.96);
  }
  tbody td { padding: 7px 10px; border-bottom: 1px solid rgba(61,255,138,0.08); vertical-align: middle; }
  tbody tr:hover { background: rgba(61,255,138,0.04); }
  .col-check { width: 28px; }
  .chk { width: 12px; height: 12px; border: 1px solid rgba(61,255,138,0.45); display: inline-block; }
  .proc { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .proc .name, .proc .sub {
    display: block;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 36vw;
  }
  .proc .name { color: var(--green-bright); font-weight: 600; text-shadow: 0 0 6px rgba(61,255,138,0.4); }
  .proc .sub { color: var(--muted); font-size: 9px; }
  .val { color: var(--cyan); text-shadow: 0 0 6px rgba(74,212,240,0.35); white-space: nowrap; }
  .ts { color: var(--green-muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
  .state { font-weight: 700; letter-spacing: 0.06em; font-size: 10px; white-space: nowrap; }
  .state.ok { color: var(--green); text-shadow: 0 0 8px rgba(61,255,138,0.5); }
  .state.blocked { color: var(--orange-bright); text-shadow: 0 0 10px rgba(240,184,74,0.7); }
  .state.fail { color: #ff8a95; text-shadow: 0 0 8px rgba(255,77,94,0.55); }
  tr.row-blocked { background: rgba(240,184,74,0.06); }
  tr.row-blocked td { border-bottom-color: rgba(240,184,74,0.12); }
  tr.row-blocked .proc .name { color: var(--orange-bright); text-shadow: 0 0 8px rgba(240,184,74,0.45); }
  tr.row-fail { background: rgba(255,77,94,0.07); }
  tr.row-fail .proc .name { color: #ff8a95; text-shadow: 0 0 8px rgba(255,77,94,0.45); }
  tr.empty td { color: var(--muted); padding: 12px 10px; }
  .footer {
    grid-column: 2;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 6px 16px;
    border-top: 1px solid var(--line);
    background: rgba(0, 6, 4, 0.75);
    color: var(--muted);
    font-size: 10px;
    letter-spacing: 0.04em;
  }
  .footer .left { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .footer .right { display: flex; gap: 14px; flex-shrink: 0; }
  .footer .right span { color: var(--green-dim); }
  .footer a { color: var(--cyan); text-decoration: none; }
  @media (max-width: 700px), (max-height: 520px) {
    .kpi { min-height: 0; padding: 6px 8px 6px 10px; }
    .kpi-value { font-size: 16px; margin-top: 2px; }
    .kpi-label { font-size: 8px; letter-spacing: 0.04em; }
    .kpi-delta { margin-top: 4px; gap: 4px; }
    .pill { font-size: 8px; padding: 1px 4px; }
    .delta-note { display: none; }
    .kpi-ico { width: 12px; height: 12px; }
    .overview-head { margin-bottom: 6px; }
    .overview-head h1 { font-size: 15px; }
    .status-line { font-size: 10px; }
    .main { padding: 6px 8px 2px; }
    .topbar { padding: 4px 6px; gap: 6px; }
    .search { height: 26px; padding-left: 8px; }
    .search input { font-size: 10px; }
    .kbd { font-size: 8px; padding: 0 6px; }
    .theme-link { height: 26px; font-size: 9px; padding: 0 6px; }
    .bell { width: 26px; height: 26px; }
    .btn-exec, .since { height: 26px; font-size: 9px; padding: 0 8px; }
    .kpis { gap: 6px; margin-bottom: 6px; }
    .tx-head { padding: 4px 8px; }
    .tx-title { font-size: 10px; }
    thead th { padding: 4px 6px; font-size: 8px; }
    tbody td { padding: 3px 6px; font-size: 10px; }
    .footer { padding: 3px 8px; font-size: 9px; }
  }
  @media (max-width: 700px) {
    :root { --sidebar-w: 0px; }
    .sidebar { display: none; }
    .app { grid-template-columns: 1fr; }
    .topbar, .main, .footer { grid-column: 1; }
    .footer .right { display: none; }
  }
  @media (max-width: 520px) {
    thead th:nth-child(3),
    tbody td:nth-child(3) { display: none; }
    .proc .sub { display: none; }
    .kbd { display: none; }
  }
  @media (min-width: 701px) and (max-width: 900px) and (min-height: 521px) {
    :root { --sidebar-w: 160px; }
    .nav-item { font-size: 10px; padding: 5px 6px; }
    .brand { font-size: 11px; padding: 10px; }
    .kpis { gap: 8px; }
    .kpi-value { font-size: 22px; }
  }
</style>
</head>
<body>
<div class="grid-bg" aria-hidden="true"></div>
<div class="app">
  <aside class="sidebar">
    <div class="brand">root@piadblock:~#</div>
    <nav class="nav">
      <div class="nav-item active">
        <svg class="ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="1" y="1" width="6" height="6"/><rect x="9" y="1" width="6" height="6"/><rect x="1" y="9" width="6" height="6"/><rect x="9" y="9" width="6" height="6"/></svg>
        ./dashboard.sh
      </div>
      <div class="nav-item">
        <svg class="ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M2 4h12v9H2z"/><path d="M2 4l2-2h8l2 2"/></svg>
        /var/mail/inbox
      </div>
      <div class="nav-item">
        <svg class="ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><circle cx="6" cy="5" r="2.2"/><circle cx="11" cy="6" r="1.8"/><path d="M1.5 13c.5-2.2 2.2-3.5 4.5-3.5S10 10.8 10.5 13"/><path d="M10 13c.3-1.4 1.2-2.2 2.6-2.2 1 0 1.8.5 2.2 1.4"/></svg>
        ./nodes.list
      </div>
      <div class="nav-item">
        <svg class="ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M2 12V8l3-3 3 2 3-4 3 3v6z"/></svg>
        ./telemetry.log
      </div>
      <div class="nav-item">
        <svg class="ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M2 4h5l1.5 1.5H14V13H2z"/></svg>
        /opt/projects/
      </div>
      <div class="nav-section">-- SYSTEM CONFIG --</div>
      <div class="nav-item">
        <svg class="ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M8 2l1.2 2.4L12 5l-2 2 .5 3L8 8.8 5.5 10l.5-3-2-2 2.8-.6z"/></svg>
        chmod &amp; chown
      </div>
      <div class="nav-item">
        <svg class="ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="2" y="4" width="12" height="8" rx="1"/><path d="M4 8h8"/></svg>
        ./tx_billing.bin
      </div>
      <div class="nav-item">
        <svg class="ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><circle cx="8" cy="8" r="2.5"/><path d="M8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.2 3.2l1.4 1.4M11.4 11.4l1.4 1.4M12.8 3.2l-1.4 1.4M4.6 11.4l-1.4 1.4"/></svg>
        ~/.bashrc
      </div>
    </nav>
    <div class="user-card">
      <div class="avatar">π</div>
      <div class="user-meta">
        <div class="name">pi_adblock</div>
        <div class="ip" data-host>—</div>
      </div>
      <span class="chev">⌃</span>
    </div>
  </aside>

  <header class="topbar">
    <div class="search">
      <span class="prompt-char">&gt;</span>
      <input id="qfilter" type="text" placeholder="filter domain..." autocomplete="off" spellcheck="false" aria-label="filter lookups">
      <span class="kbd">CTRL+F</span>
    </div>
    <a class="theme-link" href="/c">THEME_C</a>
    <a class="theme-link" href="/crt">CRT_D</a>
    <a class="theme-link" href="/crt/c">CRT_C</a>
    <div class="bell" id="bell" aria-label="upstream errors">
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4">
        <path d="M4 7a4 4 0 018 0v3l1.5 2H2.5L4 10V7z"/>
        <path d="M6.5 13a1.5 1.5 0 003 0"/>
      </svg>
      <span class="dot" id="bell-dot" hidden></span>
    </div>
  </header>

  <main class="main">
    <div class="overview-head">
      <div>
        <h1>SYSTEM_OVERVIEW</h1>
        <div class="status-line">Status: <span id="status-word">LINKING</span> | Uptime: <span id="uptime">—</span></div>
      </div>
      <div class="controls">
        <div class="since">
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><rect x="2" y="3" width="12" height="11" rx="1"/><path d="M2 6h12M5 1.5v3M11 1.5v3"/></svg>
          <span id="since-label">--since=today</span>
        </div>
        <button class="btn-exec" id="refresh-btn" type="button">&gt; EXECUTE</button>
      </div>
    </div>

    <section class="kpis" aria-label="today totals">
      <article class="kpi green">
        <div class="kpi-top">
          <div class="kpi-label">DNS_QUERIES</div>
          <svg class="kpi-ico" viewBox="0 0 16 16" fill="none" stroke="var(--green)" stroke-width="1.3"><circle cx="8" cy="8" r="5.5"/><path d="M2.5 8h11M8 2.5c1.8 1.8 1.8 9.2 0 11M8 2.5c-1.8 1.8-1.8 9.2 0 11"/></svg>
        </div>
        <div class="kpi-value" id="kpi-q">—</div>
        <div class="kpi-delta">
          <span class="pill flat" id="delta-q">—</span>
          <span class="delta-note" id="note-q">no prior day</span>
        </div>
      </article>
      <article class="kpi cyan">
        <div class="kpi-top">
          <div class="kpi-label">BLOCKED</div>
          <svg class="kpi-ico" viewBox="0 0 16 16" fill="none" stroke="var(--cyan)" stroke-width="1.3"><circle cx="8" cy="8" r="5.5"/><path d="M4.2 4.2l7.6 7.6"/></svg>
        </div>
        <div class="kpi-value" id="kpi-b">—</div>
        <div class="kpi-delta">
          <span class="pill flat" id="delta-b">—</span>
          <span class="delta-note" id="note-b">no prior day</span>
        </div>
      </article>
      <article class="kpi orange">
        <div class="kpi-top">
          <div class="kpi-label">BLOCKLIST</div>
          <svg class="kpi-ico" viewBox="0 0 16 16" fill="none" stroke="var(--orange)" stroke-width="1.3"><rect x="2" y="2" width="4" height="12"/><rect x="7" y="5" width="3" height="9"/><rect x="11" y="8" width="3" height="6"/></svg>
        </div>
        <div class="kpi-value" id="kpi-p">—</div>
        <div class="kpi-delta">
          <span class="pill flat" id="delta-p">—</span>
          <span class="delta-note" id="note-p">no prior day</span>
        </div>
      </article>
    </section>

    <section class="tx" aria-label="TX_LOGS">
      <div class="tx-head">
        <div class="tx-title"><span class="led"></span> TX_LOGS</div>
        <span class="view-all">[VIEW_ALL]</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th class="col-check"><span class="chk"></span></th>
              <th>PROCESS</th>
              <th>VALUE</th>
              <th>TIMESTAMP</th>
              <th>STATE</th>
            </tr>
          </thead>
          <tbody id="tx-body">
            <tr class="empty"><td></td><td colspan="4">no lookups yet</td></tr>
          </tbody>
        </table>
      </div>
    </section>
  </main>

  <footer class="footer">
    <div class="left">
      BLOCKLIST <span id="bl">—</span> // ALLOW <span id="allow">—</span> // SYS_VER <span id="ver">—</span> // <span data-host>—</span> // PROC <span id="proc">—</span> // AUTO-REFRESH 3S
    </div>
    <div class="right">
      <a href="/c">THEME_C</a>
      <a href="/api/stats">/api/stats</a>
      <a href="/api/crt">/api/crt</a>
    </div>
  </footer>
</div>
<noscript>JavaScript reads /api/stats and /api/crt.</noscript>
<script>
/*__SCRIPT__*/
</script>
</body>
</html>
"""


THEME_C_PAGE = r"""<!DOCTYPE html>
<html lang="en" data-theme="c">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>piadblock</title>
<style>
  :root {
    --bg: #020806;
    --panel: rgba(4, 18, 14, 0.92);
    --green: #39ff88;
    --green-dim: #1a9e52;
    --cyan: #2ee6ff;
    --orange: #ff8a1a;
    --red: #ff3d5c;
    --magenta: #ff2bd6;
    --muted: #5a7a6a;
    --text: #c8f0d8;
    --line-g: rgba(57, 255, 136, 0.35);
    --line-c: rgba(46, 230, 255, 0.4);
    --line-o: rgba(255, 138, 26, 0.45);
    --font: ui-monospace, "SF Mono", Menlo, Consolas, "Liberation Mono", "Courier New", monospace;
    --sidebar-w: 168px;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    font-family: var(--font);
    color: var(--text);
    background: var(--bg);
    overflow: hidden;
  }
  body::before {
    content: "";
    position: fixed; inset: 0;
    background:
      linear-gradient(rgba(57,255,136,0.04) 1px, transparent 1px),
      linear-gradient(90deg, rgba(57,255,136,0.04) 1px, transparent 1px),
      radial-gradient(ellipse 70% 50% at 30% 0%, rgba(57,255,136,0.07), transparent 55%),
      radial-gradient(ellipse 50% 40% at 90% 80%, rgba(46,230,255,0.05), transparent 50%),
      var(--bg);
    background-size: 28px 28px, 28px 28px, 100% 100%, 100% 100%, auto;
    z-index: 0;
  }
  body::after {
    content: "";
    pointer-events: none;
    position: fixed; inset: 0;
    background: repeating-linear-gradient(0deg, transparent 0px, transparent 2px, rgba(0,0,0,0.18) 3px);
    opacity: 0.45;
    z-index: 9;
  }
  .app {
    position: relative; z-index: 1;
    display: grid;
    grid-template-columns: var(--sidebar-w) 1fr;
    grid-template-rows: auto 1fr auto;
    height: 100vh;
    max-height: 100dvh;
  }
  .topbar {
    grid-column: 1 / -1;
    display: flex; align-items: center; gap: 0.6rem;
    padding: 0.35rem 0.7rem;
    border-bottom: 1px solid var(--line-g);
    background: rgba(2, 10, 8, 0.95);
    box-shadow: 0 0 18px rgba(57, 255, 136, 0.08);
    min-width: 0;
  }
  .prompt {
    color: var(--green);
    font-weight: 700;
    font-size: 0.78rem;
    letter-spacing: 0.04em;
    text-shadow: 0 0 10px rgba(57, 255, 136, 0.55);
    white-space: nowrap;
    flex-shrink: 0;
  }
  .search {
    flex: 1;
    min-width: 0;
    display: flex; align-items: center; gap: 0.45rem;
    max-width: 420px;
    margin: 0 auto;
    border: 1px solid var(--line-c);
    background: rgba(0, 20, 24, 0.7);
    padding: 0.28rem 0.55rem;
    box-shadow: 0 0 12px rgba(46, 230, 255, 0.15);
    font-size: 0.72rem;
    color: var(--muted);
  }
  .search .cmd { color: var(--cyan); flex-shrink: 0; }
  .search input {
    flex: 1;
    min-width: 0;
    background: transparent;
    border: 0;
    outline: none;
    color: var(--cyan);
    font-family: var(--font);
    font-size: 0.72rem;
  }
  .search input::placeholder { color: var(--muted); }
  .kbd {
    margin-left: auto;
    flex-shrink: 0;
    font-size: 0.58rem;
    border: 1px solid rgba(46,230,255,0.35);
    padding: 0.1rem 0.3rem;
    color: var(--cyan);
  }
  .theme-link {
    flex-shrink: 0;
    font-size: 0.62rem;
    font-weight: 800;
    letter-spacing: 0.08em;
    color: var(--cyan);
    border: 1px solid var(--line-c);
    padding: 0.22rem 0.45rem;
    text-decoration: none;
    white-space: nowrap;
    background: rgba(46, 230, 255, 0.08);
    box-shadow: 0 0 10px rgba(46, 230, 255, 0.28);
  }
  .theme-link:visited { color: var(--cyan); }
  .theme-link:hover { background: rgba(46, 230, 255, 0.16); }
  .node { font-size: 0.65rem; color: var(--muted); white-space: nowrap; flex-shrink: 0; }
  .node strong { color: var(--cyan); font-weight: 600; }
  .sidebar {
    grid-row: 2;
    display: flex; flex-direction: column;
    border-right: 1px solid var(--line-g);
    background: rgba(2, 12, 9, 0.92);
    padding: 0.55rem 0.4rem 0.5rem;
    gap: 0.15rem;
    overflow: hidden;
  }
  .nav-item {
    display: flex; align-items: center; gap: 0.35rem;
    padding: 0.38rem 0.45rem;
    font-size: 0.68rem;
    color: var(--muted);
    border: 1px solid transparent;
    letter-spacing: 0.02em;
    text-decoration: none;
  }
  .nav-item.active {
    color: var(--green);
    border-color: var(--line-g);
    background: rgba(57, 255, 136, 0.08);
    box-shadow: 0 0 14px rgba(57, 255, 136, 0.2), inset 0 0 12px rgba(57, 255, 136, 0.05);
    text-shadow: 0 0 8px rgba(57, 255, 136, 0.45);
  }
  .nav-sec {
    margin: 0.55rem 0.35rem 0.25rem;
    font-size: 0.55rem;
    letter-spacing: 0.12em;
    color: var(--green-dim);
    text-transform: uppercase;
  }
  .sidebar-foot {
    margin-top: auto;
    border: 1px solid var(--line-g);
    padding: 0.4rem;
    background: rgba(57, 255, 136, 0.05);
    box-shadow: 0 0 10px rgba(57, 255, 136, 0.12);
  }
  .avatar {
    width: 28px; height: 28px;
    border: 1px solid var(--green);
    color: var(--green);
    display: flex; align-items: center; justify-content: center;
    font-size: 0.65rem; font-weight: 800;
    box-shadow: 0 0 8px rgba(57, 255, 136, 0.35);
    float: left; margin-right: 0.4rem;
  }
  .who { font-size: 0.62rem; color: var(--green); font-weight: 700; }
  .who-ip { font-size: 0.55rem; color: var(--muted); clear: both; padding-top: 0.15rem; overflow: hidden; text-overflow: ellipsis; }
  .main {
    grid-row: 2;
    display: flex; flex-direction: column;
    gap: 0.45rem;
    padding: 0.5rem 0.65rem 0.4rem;
    min-height: 0;
    min-width: 0;
    overflow: hidden;
  }
  .hero { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 0.4rem 1rem; }
  .hero h1 {
    margin: 0;
    font-size: clamp(1.15rem, 3.2vw, 1.85rem);
    font-weight: 800;
    letter-spacing: 0.06em;
    color: #e8fff0;
    text-shadow: 0 0 18px rgba(57, 255, 136, 0.35);
    line-height: 1;
  }
  .status-line {
    font-size: clamp(0.65rem, 1.6vw, 0.82rem);
    color: var(--green);
    text-shadow: 0 0 10px rgba(57, 255, 136, 0.5);
    font-weight: 600;
  }
  .status-line .dot {
    display: inline-block;
    width: 0.55em; height: 0.55em;
    background: var(--green);
    border-radius: 50%;
    margin-right: 0.3em;
    box-shadow: 0 0 8px var(--green);
  }
  .status-line .online { color: var(--green); }
  .status-line .offline { color: var(--red); text-shadow: 0 0 8px rgba(255,61,92,0.6); }
  .hero-actions { margin-left: auto; display: flex; gap: 0.4rem; align-items: center; }
  .chip-btn {
    font-size: 0.62rem;
    border: 1px solid var(--line-c);
    color: var(--cyan);
    padding: 0.25rem 0.45rem;
    background: rgba(46, 230, 255, 0.06);
    white-space: nowrap;
  }
  .exec-btn {
    font-family: var(--font);
    font-size: 0.65rem;
    font-weight: 800;
    letter-spacing: 0.06em;
    border: 1px solid var(--green);
    color: #041208;
    background: var(--green);
    padding: 0.28rem 0.65rem;
    box-shadow: 0 0 16px rgba(57, 255, 136, 0.55);
    text-transform: uppercase;
    cursor: pointer;
  }
  .kpis { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0.45rem; }
  .kpi { background: var(--panel); padding: 0.45rem 0.55rem 0.4rem; position: relative; min-width: 0; }
  .kpi.g { border: 1px solid var(--line-g); box-shadow: 0 0 16px rgba(57, 255, 136, 0.22), inset 0 0 20px rgba(57, 255, 136, 0.04); }
  .kpi.c { border: 1px solid var(--line-c); box-shadow: 0 0 16px rgba(46, 230, 255, 0.22), inset 0 0 20px rgba(46, 230, 255, 0.04); }
  .kpi.o { border: 1px solid var(--line-o); box-shadow: 0 0 16px rgba(255, 138, 26, 0.28), inset 0 0 20px rgba(255, 138, 26, 0.05); }
  .kpi .label {
    font-size: clamp(0.52rem, 1.3vw, 0.62rem);
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 0.15rem;
  }
  .kpi .num {
    font-size: clamp(1.15rem, 3.5vw, 1.75rem);
    font-weight: 800;
    line-height: 1.1;
    font-variant-numeric: tabular-nums;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .kpi.g .num { color: var(--green); text-shadow: 0 0 12px rgba(57, 255, 136, 0.45); }
  .kpi.c .num { color: var(--cyan); text-shadow: 0 0 12px rgba(46, 230, 255, 0.45); }
  .kpi.o .num { color: var(--orange); text-shadow: 0 0 12px rgba(255, 138, 26, 0.5); }
  .kpi .sub { font-size: 0.55rem; color: var(--muted); margin-top: 0.15rem; }
  .delta {
    display: inline-block;
    margin-top: 0.3rem;
    font-size: clamp(0.52rem, 1.2vw, 0.62rem);
    font-weight: 700;
    padding: 0.12rem 0.35rem;
    letter-spacing: 0.04em;
    white-space: nowrap;
  }
  .delta.up { color: var(--green); border: 1px solid var(--line-g); background: rgba(57, 255, 136, 0.12); box-shadow: 0 0 8px rgba(57, 255, 136, 0.25); }
  .delta.down { color: var(--orange); border: 1px solid var(--line-o); background: rgba(255, 138, 26, 0.12); box-shadow: 0 0 8px rgba(255, 138, 26, 0.25); }
  .delta.flat { color: var(--muted); border: 1px solid rgba(90,122,106,0.45); }
  .topstrip {
    display: flex; align-items: center; gap: 0.35rem;
    flex-wrap: nowrap;
    overflow: hidden;
    border: 1px solid rgba(255, 43, 214, 0.35);
    background: rgba(255, 43, 214, 0.06);
    padding: 0.28rem 0.45rem;
    box-shadow: 0 0 12px rgba(255, 43, 214, 0.12);
  }
  .topstrip .tl {
    font-size: 0.55rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--magenta);
    white-space: nowrap;
    flex-shrink: 0;
    text-shadow: 0 0 8px rgba(255, 43, 214, 0.4);
  }
  .tops { display: flex; gap: 0.3rem; overflow: hidden; flex: 1; min-width: 0; }
  .tops span {
    font-size: clamp(0.52rem, 1.3vw, 0.65rem);
    color: #ffd0f0;
    border: 1px solid rgba(255, 43, 214, 0.4);
    padding: 0.1rem 0.35rem;
    white-space: nowrap;
    background: rgba(20, 4, 16, 0.6);
    overflow: hidden; text-overflow: ellipsis;
    max-width: 11rem;
  }
  .tops span.none { color: var(--muted); border-color: rgba(255, 43, 214, 0.25); }
  .tops span b { color: var(--orange); font-weight: 700; margin-left: 0.25rem; }
  .tx {
    flex: 1;
    min-height: 0;
    display: flex; flex-direction: column;
    border: 1px solid var(--line-c);
    background: rgba(2, 14, 18, 0.85);
    box-shadow: 0 0 18px rgba(46, 230, 255, 0.15);
    overflow: hidden;
  }
  .tx-h {
    display: flex; align-items: center; gap: 0.5rem;
    padding: 0.35rem 0.55rem;
    border-bottom: 1px solid var(--line-c);
    background: linear-gradient(90deg, rgba(46, 230, 255, 0.1), transparent 60%);
  }
  .tx-h h2 {
    margin: 0;
    font-size: clamp(0.7rem, 1.8vw, 0.85rem);
    letter-spacing: 0.14em;
    color: var(--cyan);
    text-shadow: 0 0 10px rgba(46, 230, 255, 0.4);
    white-space: nowrap;
  }
  .tx-h .live { font-size: 0.58rem; color: var(--green); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .tx-h .view {
    margin-left: auto;
    font-size: 0.58rem;
    color: var(--muted);
    border: 1px solid rgba(46,230,255,0.25);
    padding: 0.1rem 0.35rem;
    flex-shrink: 0;
  }
  .tx-cols, .row {
    display: grid;
    grid-template-columns: 6.2rem 4.8rem minmax(0, 1fr) 3.2rem minmax(4.5rem, 7.5rem);
    gap: 0.35rem;
    align-items: center;
  }
  .tx-cols {
    padding: 0.25rem 0.55rem;
    font-size: 0.55rem;
    letter-spacing: 0.1em;
    color: var(--muted);
    text-transform: uppercase;
    border-bottom: 1px solid rgba(46, 230, 255, 0.15);
  }
  .feed { flex: 1; overflow: auto; min-height: 0; }
  .row {
    padding: 0.28rem 0.55rem;
    font-size: clamp(0.65rem, 1.7vw, 0.82rem);
    border-left: 3px solid transparent;
    border-bottom: 1px solid rgba(46, 230, 255, 0.06);
  }
  .row.ok { border-left-color: rgba(57, 255, 136, 0.4); }
  .row.blocked {
    border-left-color: var(--orange);
    background: linear-gradient(90deg, rgba(255, 138, 26, 0.18), rgba(255, 61, 92, 0.06) 50%, transparent);
    box-shadow: inset 0 0 20px rgba(255, 138, 26, 0.08);
  }
  .row.fail {
    border-left-color: var(--red);
    background: linear-gradient(90deg, rgba(255, 61, 92, 0.16), transparent 70%);
  }
  .row.empty {
    display: block;
    color: var(--muted);
    border-left-color: transparent;
    padding: 0.7rem 0.55rem;
  }
  .ts { color: var(--muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
  .status {
    font-size: 0.62rem;
    font-weight: 800;
    letter-spacing: 0.06em;
    text-align: center;
    padding: 0.1rem 0.2rem;
    white-space: nowrap;
  }
  .row.ok .status { color: var(--green); border: 1px solid rgba(57, 255, 136, 0.45); }
  .row.blocked .status { color: #120800; background: var(--orange); box-shadow: 0 0 10px rgba(255, 138, 26, 0.55); }
  .row.fail .status { color: #ffd0d4; border: 1px solid rgba(255, 61, 92, 0.65); }
  .domain { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #e0fff0; }
  .row.blocked .domain { color: #ffe0c0; font-weight: 700; text-shadow: 0 0 8px rgba(255, 138, 26, 0.4); }
  .row.fail .domain { color: #ffd0d4; }
  .qtype { color: var(--muted); white-space: nowrap; }
  .client { color: #7a9a88; font-size: 0.72em; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .foot {
    grid-column: 1 / -1;
    display: flex; justify-content: space-between; align-items: center; gap: 0.6rem;
    padding: 0.25rem 0.7rem;
    border-top: 1px solid var(--line-g);
    font-size: 0.55rem;
    color: var(--muted);
    letter-spacing: 0.06em;
    background: rgba(2, 10, 8, 0.95);
  }
  .foot .left { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .foot .links { flex-shrink: 0; white-space: nowrap; }
  .foot a { color: var(--cyan); text-decoration: none; margin-left: 0.8rem; }
  @media (max-width: 860px), (max-height: 520px) {
    :root { --sidebar-w: 0px; }
    .sidebar { display: none; }
    .app { grid-template-columns: 1fr; }
    .prompt { font-size: 0.68rem; }
    .kpi { padding: 0.28rem 0.4rem 0.32rem; }
    .kpi .sub { display: none; }
    .kpi .num { font-size: 1.15rem; }
    .kpis { gap: 0.3rem; }
    .topstrip { padding: 0.18rem 0.35rem; }
    .main { padding: 0.35rem 0.4rem; gap: 0.3rem; }
    .hero h1 { font-size: 1.05rem; }
    .foot { font-size: 0.5rem; padding: 0.18rem 0.4rem; }
    .tx-h { padding: 0.22rem 0.4rem; }
    .tx-h .live { display: none; }
  }
  @media (max-width: 520px) {
    .search { display: none; }
    .hero-actions { display: none; }
    .node { display: none; }
    .tx-cols { display: none; }
    .row { grid-template-columns: 4.6rem 3.6rem minmax(0, 1fr); }
    .row .qtype, .row .client { display: none; }
  }
</style>
</head>
<body>
<div class="app">
  <header class="topbar">
    <div class="prompt">root@piadblock:~#</div>
    <div class="search">
      <span class="cmd">&gt;</span>
      <input id="qfilter" type="text" placeholder="filter domain..." autocomplete="off" spellcheck="false" aria-label="filter lookups">
      <span class="kbd">CTRL+F</span>
    </div>
    <a class="theme-link" href="/">THEME_D</a>
    <a class="theme-link" href="/crt">CRT_D</a>
    <a class="theme-link" href="/crt/c">CRT_C</a>
    <div class="node">NODE <strong data-host>—</strong></div>
  </header>

  <aside class="sidebar" aria-label="nav chrome">
    <div class="nav-item active">./dashboard.sh</div>
    <a class="nav-item" href="/">./theme_d.sh</a>
    <div class="nav-item">./blocklist.db</div>
    <div class="nav-item">./stats.json</div>
    <div class="nav-sec">-- SYSTEM CONFIG --</div>
    <div class="nav-item">./upstream.conf</div>
    <div class="nav-item">./tx_billing.bin</div>
    <div class="nav-item">~/.bashrc</div>
    <div class="sidebar-foot">
      <div class="avatar">π</div>
      <div class="who">pi_adblock</div>
      <div class="who-ip" data-host>—</div>
    </div>
  </aside>

  <main class="main">
    <div class="hero">
      <div>
        <h1>SYSTEM_OVERVIEW</h1>
        <div class="status-line"><span class="dot"></span>Status: <span id="status-word">LINKING</span> | Uptime: <span id="uptime">—</span></div>
      </div>
      <div class="hero-actions">
        <span class="chip-btn" id="since-label">--since=today</span>
        <button class="exec-btn" id="refresh-btn" type="button">&gt; EXECUTE</button>
      </div>
    </div>

    <section class="kpis" aria-label="today totals">
      <div class="kpi g">
        <div class="label">DNS_QUERIES</div>
        <div class="num" id="kpi-q">—</div>
        <div class="sub">today.queries</div>
        <div class="delta flat" id="delta-q">—</div>
        <div class="sub" id="note-q">no prior day</div>
      </div>
      <div class="kpi o">
        <div class="label">BLOCKED</div>
        <div class="num" id="kpi-b">—</div>
        <div class="sub">today.blocked</div>
        <div class="delta flat" id="delta-b">—</div>
        <div class="sub" id="note-b">no prior day</div>
      </div>
      <div class="kpi c">
        <div class="label">BLOCKLIST</div>
        <div class="num" id="kpi-p">—</div>
        <div class="sub">domains on blocklist</div>
        <div class="delta flat" id="delta-p">—</div>
        <div class="sub" id="note-p">no prior day</div>
      </div>
    </section>

    <div class="topstrip" aria-label="top blocked">
      <span class="tl">TOP_BLOCKED</span>
      <div class="tops" id="tops"><span class="none">—</span></div>
    </div>

    <section class="tx" aria-label="TX_LOGS waterfall">
      <div class="tx-h">
        <h2>TX_LOGS · LIVE_WATERFALL</h2>
        <span class="live">/api/crt lookups · newest first</span>
        <span class="view">[VIEW_ALL]</span>
      </div>
      <div class="tx-cols">
        <span>TIMESTAMP</span><span>STATUS</span><span>DOMAIN</span><span>QTYPE</span><span>CLIENT</span>
      </div>
      <div class="feed" id="feed"><div class="row empty">no lookups yet</div></div>
    </section>
  </main>

  <footer class="foot">
    <span class="left">BLOCKLIST <span id="bl">—</span> // ALLOW <span id="allow">—</span> // SYS_VER <span id="ver">—</span> // PROC <span id="proc">—</span> // AUTO-REFRESH 3S</span>
    <span class="links">
      <a href="/">THEME_D</a>
      <a href="/api/stats">/api/stats</a>
      <a href="/api/crt">/api/crt</a>
    </span>
  </footer>
</div>
<noscript>JavaScript reads /api/stats and /api/crt.</noscript>
<script>
/*__SCRIPT__*/
</script>
</body>
</html>
"""


def render_status_page(snapshot: dict, details: dict) -> bytes:
    """Theme D HTML for ``/``. Live numbers are fetched from the JSON APIs.

    ``snapshot`` and ``details`` stay in the signature. They are not baked
    into the document, so a later poll sees the current counters.
    """
    del snapshot, details
    return _page(THEME_D_PAGE)


def render_status_page_c(snapshot: dict, details: dict) -> bytes:
    """Theme C HTML for ``/c``. Same JSON feeds as theme D, plus top-blocked."""
    del snapshot, details
    return _page(THEME_C_PAGE)


CRT_PAGE = r"""<!DOCTYPE html>
<html lang="en" data-theme="__THEME__" data-crt="1">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>piadblock</title>
<style>
  html[data-theme="d"] {
    --text: #e8ffe4;
    --ok: #3dff6a;
    --q: #3dff6a;
    --b: #4ad4f0;
    --p: #ffb020;
    --blocked-bg: #ffb020;
    --blocked-fg: #140c00;
    --ok-bg: #06210f;
    --fail: #ff4d5e;
    --fail-bg: #2a0008;
    --ink: #041208;
    --stage: #000000;
    --strip: #ffb020;
  }
  html[data-theme="c"] {
    --text: #e7fff3;
    --ok: #39ff88;
    --q: #39ff88;
    --b: #ff8a1a;
    --p: #2ee6ff;
    --blocked-bg: #ff8a1a;
    --blocked-fg: #140800;
    --ok-bg: #00241c;
    --fail: #ff3d5c;
    --fail-bg: #2a0008;
    --ink: #041208;
    --stage: #00110e;
    --strip: #ff2bd6;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body {
    width: 100%;
    height: 100%;
    overflow: hidden;
    background: #000000;
  }
  body {
    position: fixed;
    inset: 0;
    font-family: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
    font-weight: 800;
    font-synthesis: weight;
    color: var(--text);
    background: #000000;
  }
  .stage {
    position: absolute;
    left: 50%;
    top: 50%;
    width: min(100vw, calc(100vh * 4 / 3));
    height: min(100vh, calc(100vw * 3 / 4));
    transform: translate(-50%, -50%);
    padding: 7%;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    gap: clamp(4px, 0.8vmin, 12px);
    background: var(--stage);
  }
  .top {
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    gap: 0.5em;
    min-width: 0;
    overflow: hidden;
  }
  .live {
    display: flex;
    align-items: center;
    gap: 0.4em;
    font-size: clamp(24px, 5vmin, 56px);
    font-weight: 800;
    line-height: 1;
    color: var(--ok);
    white-space: nowrap;
  }
  .led {
    width: 0.72em;
    height: 0.72em;
    background: var(--ok);
    flex: 0 0 auto;
  }
  #status-word { font-weight: 800; }
  #status-word.offline { color: var(--fail); }
  .live:has(#status-word.offline) .led { background: var(--fail); }
  .themes {
    flex: 0 0 auto;
    display: flex;
    gap: 0.35em;
  }
  .themes a, .themes .title {
    flex: 1;
    text-align: center;
    font-weight: 800;
    font-size: clamp(24px, 4.8vh, 52px);
    line-height: 1;
    text-decoration: none;
    color: var(--text);
    background: #000000;
    border: 3px solid var(--ok);
    padding: 0.22em 0.2em;
  }
  .themes .title { background: var(--ok); color: var(--ink); }
  .themes a.on { background: var(--ok); color: var(--ink); }
  #fs-btn {
    margin-left: auto;
    flex: 0 0 auto;
    font-family: inherit;
    font-weight: 800;
    font-size: clamp(28px, 6.5vh, 72px);
    line-height: 1;
    color: var(--ink);
    background: var(--ok);
    border: 0;
    padding: 0.22em 0.42em;
    cursor: pointer;
  }
  .topstrip {
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    gap: 0.4em;
    overflow: hidden;
    min-height: 5.5vh;
    border: 3px solid var(--strip);
    background: #140010;
    padding: 0.12em 0.3em;
  }
  .topstrip .tl {
    flex: 0 0 auto;
    font-size: clamp(22px, 4vh, 40px);
    font-weight: 800;
    color: var(--strip);
  }
  .tops {
    display: flex;
    gap: 0.35em;
    min-width: 0;
    flex: 1;
    overflow: hidden;
  }
  .tops span {
    font-size: clamp(22px, 4vh, 40px);
    font-weight: 800;
    color: #ffd6f4;
    background: #000000;
    border: 3px solid var(--strip);
    padding: 0.06em 0.28em;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 18ch;
  }
  .tops span:nth-child(n+6) { display: none; }
  .tops b { color: #ff8a1a; font-weight: 800; margin-right: 0.3em; }
  html[data-theme="c"] .stage { gap: 2px; }
  html[data-theme="c"] .themes a, html[data-theme="c"] .themes .title { font-size: clamp(22px, 3.8vh, 40px); padding: 0.08em 0.1em; }
  .top .live, .top .themes .title, html[data-theme="c"] .top .themes .title { font-size: min(7.2vh, calc(min(100vw, 133.33vh) * 0.045)); white-space: nowrap; }
  .top .live { flex: 0 0 auto; }
  html[data-theme="c"] #fs-btn { font-size: clamp(22px, 4.2vh, 44px); padding: 0.1em 0.24em; }
  html[data-theme="c"] .kpi { padding: 0.02em 0.16em 0.04em; }
  html[data-theme="c"] .kpi-label { font-size: clamp(18px, 3.2vmin, 28px); }
  html[data-theme="c"] .kpi-value { font-size: clamp(28px, 5.4vmin, 72px); margin-top: 0; }
  html[data-theme="c"] .topstrip { min-height: 0; padding: 0.06em 0.2em; }
  html[data-theme="c"] .tops span { font-size: clamp(20px, 3.6vh, 34px); max-width: 22ch; }
  .kpis {
    flex: 0 0 auto;
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: clamp(6px, 1vmin, 14px);
  }
  .kpi {
    min-width: 0;
    overflow: hidden;
    border: 3px solid var(--q);
    background: #000000;
    padding: 0.1em 0.25em 0.14em;
  }
  .kpi.b { border-color: var(--b); }
  .kpi.p { border-color: var(--p); }
  .kpi-label {
    font-size: clamp(24px, 4.2vmin, 40px);
    font-weight: 800;
    line-height: 1;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    color: var(--q);
  }
  .kpi.b .kpi-label, .kpi.b .kpi-value { color: var(--b); }
  .kpi.p .kpi-label, .kpi.p .kpi-value { color: var(--p); }
  .kpi-value {
    margin-top: 0.05em;
    font-size: clamp(40px, 9vmin, 120px);
    font-weight: 800;
    line-height: 0.95;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    color: var(--q);
  }
  .fall { flex: 1 1 auto; min-height: 0; overflow: hidden; }
  .feed {
    overflow: hidden;
    display: flex;
    flex-direction: column;
    gap: 0.35vh;
  }
  .row {
    height: 5.5vh;
    flex: 0 0 5.5vh;
    display: grid;
    grid-template-columns: 8.2ch minmax(0, 1fr);
    column-gap: 0.5em;
    align-items: center;
    padding: 0 0.3em;
    overflow: hidden;
    font-size: clamp(24px, 4.6vh, 72px);
    font-weight: 800;
    line-height: 1;
    background: var(--ok-bg);
    color: var(--text);
  }
  .row.blocked { background: var(--blocked-bg); color: var(--blocked-fg); }
  .row.fail { background: var(--fail-bg); color: var(--fail); }
  .status, .domain { font-weight: 800; white-space: nowrap; overflow: hidden; }
  .row.ok .status { color: var(--ok); }
  .row.blocked .status, .row.blocked .domain { color: var(--blocked-fg); }
  .row.fail .status, .row.fail .domain { color: var(--fail); }
  .domain {
    min-width: 0;
    text-overflow: ellipsis;
    direction: rtl;
    text-align: left;
  }
  .domain-text { direction: ltr; unicode-bidi: isolate; }
  .row.empty { display: flex; align-items: center; }
</style>
</head>
<body>
<div class="stage">
  <div class="top">
    <div class="themes">
      <div class="title">Grok Bot Pi-Hole</div>
    </div>
    <div class="live" style="margin-left:auto">
      <span class="led" aria-hidden="true"></span>
      <span id="status-word">LINKING</span>
    </div>
  </div>
  __TOPS__
  <section class="kpis" aria-label="today totals">
    <article class="kpi q">
      <div class="kpi-label">QUERIES</div>
      <div class="kpi-value" id="kpi-q">—</div>
    </article>
    <article class="kpi b">
      <div class="kpi-label">BLOCKED</div>
      <div class="kpi-value" id="kpi-b">—</div>
    </article>
    <article class="kpi p">
      <div class="kpi-label">BLOCKLIST</div>
      <div class="kpi-value" id="kpi-p">—</div>
    </article>
  </section>
  <section class="fall" aria-label="lookups">
    <div class="feed" id="feed"><div class="row empty">no lookups yet</div></div>
  </section>
</div>
<noscript>JavaScript reads /api/stats and /api/crt.</noscript>
<script>
/*__SCRIPT__*/
</script>
</body>
</html>
"""


_CRT_TOPS = (
    '<div class="topstrip" aria-label="top blocked">'
    '<span class="tl">TOP</span>'
    '<div class="tops" id="tops"><span class="none">—</span></div>'
    "</div>"
)


def render_crt_page(theme: str = "d") -> bytes:
    """4:3 television page. ``c`` is the HUD palette with the top-blocked strip.

    Counters stay in the JSON APIs. The default is theme D, used by ``/crt``
    and ``?crt=1``.
    """
    chosen = "c" if theme == "c" else "d"
    html = CRT_PAGE.replace("__THEME__", chosen)
    html = html.replace("__D_ON__", "on" if chosen == "d" else "")
    html = html.replace("__C_ON__", "on" if chosen == "c" else "")
    html = html.replace("__TOPS__", _CRT_TOPS if chosen == "c" else "")
    return _page(html)


def _query_crt(raw_path: str) -> bool:
    """True when the query string asks for the television layout."""
    for value in parse_qs(urlparse(raw_path).query).get("crt", ()):
        if value == "1":
            return True
    return False


def crt_payload(snapshot: dict, lookups: LookupFeed | None, details: dict | None = None) -> dict:
    """Today's totals plus the newest lookups. The CRT polls this.

    ``blocklist_domains`` is the effective list size from the server details.
    Lookup timestamps stay numeric here; the display formats them in local time.
    """
    today = snapshot.get("today") if isinstance(snapshot.get("today"), dict) else {}
    try:
        queries = int(today.get("queries", 0) or 0)
        blocked = int(today.get("blocked", 0) or 0)
    except (TypeError, ValueError):
        queries, blocked = 0, 0
    payload = {
        "date": str(today.get("date", "")),
        "queries": queries,
        "blocked": blocked,
        "lookups": lookups.snapshot() if lookups is not None else [],
    }
    if isinstance(details, dict) and "blocklist_domains" in details:
        try:
            payload["blocklist_domains"] = int(details.get("blocklist_domains") or 0)
            payload["allow_count"] = int(details.get("allow_count") or 0)
        except (TypeError, ValueError):
            pass
    return payload


def make_handler(
    stats: Stats,
    details_fn: Callable[[], dict],
    lookups: LookupFeed | None = None,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
            path = urlparse(self.path).path
            crt = _query_crt(self.path)
            details = details_fn()
            snapshot = stats.snapshot()
            if path == "/crt/c":
                body = render_crt_page("c")
                content_type = "text/html; charset=utf-8"
            elif path == "/crt" or (crt and path in ("/", "/index.html", "/c")):
                body = render_crt_page()
                content_type = "text/html; charset=utf-8"
            elif path in ("/", "/index.html"):
                body = render_status_page(snapshot, details)
                content_type = "text/html; charset=utf-8"
            elif path == "/c":
                body = render_status_page_c(snapshot, details)
                content_type = "text/html; charset=utf-8"
            elif path == "/api/stats":
                payload = dict(snapshot)
                payload.update(details)
                body = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
                content_type = "application/json"
            elif path == "/api/crt":
                body = (json.dumps(crt_payload(snapshot, lookups, details)) + "\n").encode("utf-8")
                content_type = "application/json"
            elif path == "/health":
                body = b"ok\n"
                content_type = "text/plain; charset=utf-8"
            else:
                body = b"not found\n"
                self._send(404, "text/plain; charset=utf-8", body)
                return
            self._send(200, content_type, body)

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return Handler


class StatusPage:
    """Background HTTP server. Failure to bind is reported and ignored by the DNS server."""

    def __init__(
        self,
        host: str,
        port: int,
        stats: Stats,
        details_fn: Callable[[], dict],
        lookups: LookupFeed | None = None,
    ):
        self.host = host
        self.port = port
        self.httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        handler = make_handler(stats, details_fn, lookups)
        self._handler = handler

    def start(self) -> None:
        self.httpd = ThreadingHTTPServer((self.host, self.port), self._handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="piadblock-status", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
