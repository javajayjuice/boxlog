// boxlog viewer. Log content is attacker-controlled (request bodies, emails, chat
// messages end up in logs), so every value is rendered with textContent - never as HTML.
(function () {
  "use strict";

  const POLL_MS = 3000;
  const MAX_ROWS = 1000;
  // One token per viewer location, so two mounted viewers don't share credentials.
  const TOKEN_KEY = "boxlog-token:" + location.pathname;

  const $ = (id) => document.getElementById(id);
  const el = {
    login: $("login"), loginForm: $("login-form"), tokenInput: $("token-input"),
    loginError: $("login-error"), app: $("app"), logout: $("logout"),
    level: $("f-level"), service: $("f-service"), serviceWrap: $("service-wrap"),
    source: $("f-source"), event: $("f-event"), q: $("f-q"), live: $("f-live"),
    refresh: $("refresh"), liveDot: $("live-dot"), stats: $("stats"),
    trace: $("trace"), traceKind: $("trace-kind"), traceId: $("trace-id"),
    traceClear: $("trace-clear"), banner: $("banner"), rows: $("rows"), empty: $("empty"),
    table: document.querySelector("table.logs"),
  };

  let traceFields = ["request_id", "job_id"];
  try {
    const parsed = JSON.parse(document.body.dataset.traceFields || "[]");
    if (Array.isArray(parsed) && parsed.length) traceFields = parsed.map(String);
  } catch (_) { /* keep defaults */ }

  const state = {
    token: null,
    records: [],
    latest: null,
    expanded: new Set(),
    trace: null, // { field, id }
    loading: false,
    timer: null,
  };

  // sessionStorage can throw (privacy modes, blocked storage) - fall back to memory.
  const storage = {
    get() { try { return sessionStorage.getItem(TOKEN_KEY); } catch (_) { return null; } },
    set(v) { try { sessionStorage.setItem(TOKEN_KEY, v); } catch (_) { /* memory only */ } },
    clear() { try { sessionStorage.removeItem(TOKEN_KEY); } catch (_) { /* ignore */ } },
  };

  function node(tag, className, text) {
    const n = document.createElement(tag);
    if (className) n.className = className;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  // -- auth -------------------------------------------------------------------

  function showLogin(message) {
    stopPolling();
    el.app.hidden = true;
    el.login.hidden = false;
    el.loginError.hidden = !message;
    el.loginError.textContent = message || "";
    el.tokenInput.value = "";
    el.tokenInput.focus();
  }

  function showApp() {
    el.login.hidden = true;
    el.app.hidden = false;
  }

  el.loginForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const token = el.tokenInput.value.trim();
    if (!token) return;
    state.token = token;
    storage.set(token);
    showApp();
    load(true);
    startPolling();
  });

  el.logout.addEventListener("click", () => {
    state.token = null;
    storage.clear();
    state.records = [];
    render();
    showLogin();
  });

  // -- data ---------------------------------------------------------------------

  function params(tail) {
    const p = new URLSearchParams();
    if (el.level.value) p.set("level", el.level.value);
    if (el.service.value) p.set("service", el.service.value);
    if (el.source.value) p.set("source", el.source.value);
    if (el.event.value.trim()) p.set("event", el.event.value.trim());
    if (el.q.value.trim()) p.set("q", el.q.value.trim());
    if (state.trace) p.set(state.trace.field, state.trace.id);
    p.set("limit", state.trace ? "1000" : "300");
    if (tail && state.latest !== null) p.set("after", String(state.latest));
    return p;
  }

  function fillSelect(select, values) {
    const selected = select.value;
    const wanted = new Set(values);
    if (selected) wanted.add(selected);
    const existing = Array.from(select.options).slice(1).map((o) => o.value);
    const next = Array.from(wanted).sort();
    if (existing.join("\u0000") === next.join("\u0000")) return;
    while (select.options.length > 1) select.remove(1);
    next.forEach((v) => {
      const opt = node("option", null, v);
      opt.value = v;
      select.appendChild(opt);
    });
    select.value = selected;
  }

  function applyFacets(facets) {
    if (!facets) return;
    const services = Array.isArray(facets.services) ? facets.services.map(String) : [];
    const sources = Array.isArray(facets.sources) ? facets.sources.map(String) : [];
    fillSelect(el.service, services);
    fillSelect(el.source, sources);
    const multi = services.length > 1 || !!el.service.value;
    el.serviceWrap.hidden = !multi;
    el.table.classList.toggle("single-service", !multi);
  }

  async function load(full) {
    if (!state.token || state.loading) return;
    state.loading = true;
    try {
      const resp = await fetch("api/logs?" + params(!full).toString(), {
        headers: { Authorization: "Bearer " + state.token },
        cache: "no-store",
      });
      if (resp.status === 401) {
        state.token = null;
        storage.clear();
        showLogin("That token was rejected.");
        return;
      }
      if (resp.status === 404) {
        showBanner("The log viewer is disabled on this server (no token configured).");
        return;
      }
      if (!resp.ok) {
        let detail = "";
        try { detail = (await resp.json()).detail || ""; } catch (_) { /* not JSON */ }
        showBanner("Could not load logs (HTTP " + resp.status + (detail ? ": " + detail : "") + ").");
        return;
      }
      const data = await resp.json();
      hideBanner();
      applyFacets(data.facets);
      const incoming = Array.isArray(data.records) ? data.records : [];
      if (full) {
        state.records = incoming;
      } else if (incoming.length) {
        state.records = incoming.concat(state.records).slice(0, MAX_ROWS);
      }
      if (data.latest !== null && data.latest !== undefined) state.latest = data.latest;
      render();
    } catch (err) {
      showBanner("Network error loading logs: " + err.message);
    } finally {
      state.loading = false;
    }
  }

  function showBanner(text) { el.banner.textContent = text; el.banner.hidden = false; }
  function hideBanner() { el.banner.hidden = true; }

  // -- live tail ----------------------------------------------------------------

  function startPolling() {
    stopPolling();
    if (!el.live.checked || !state.token) return;
    el.liveDot.classList.add("on");
    state.timer = setInterval(() => {
      if (document.visibilityState === "visible") load(false);
    }, POLL_MS);
  }

  function stopPolling() {
    if (state.timer) clearInterval(state.timer);
    state.timer = null;
    el.liveDot.classList.remove("on");
  }

  el.live.addEventListener("change", startPolling);

  // -- filters ------------------------------------------------------------------

  let debounce = null;
  function refilter() {
    clearTimeout(debounce);
    debounce = setTimeout(() => {
      state.latest = null;
      state.expanded.clear();
      load(true);
    }, 250);
  }

  [el.level, el.service, el.source].forEach((c) => c.addEventListener("change", refilter));
  [el.event, el.q].forEach((c) => c.addEventListener("input", refilter));
  el.refresh.addEventListener("click", () => { state.latest = null; load(true); });

  function setTrace(field, id) {
    state.trace = { field, id };
    el.traceKind.textContent = field;
    el.traceId.textContent = id;
    el.trace.hidden = false;
    refilter();
  }

  el.traceClear.addEventListener("click", () => {
    state.trace = null;
    el.trace.hidden = true;
    refilter();
  });

  // -- rendering ----------------------------------------------------------------

  function formatTime(ts) {
    const d = new Date(ts);
    if (Number.isNaN(d.getTime())) return String(ts || "");
    const pad = (n, w) => String(n).padStart(w || 2, "0");
    return (
      pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " +
      pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds()) +
      "." + pad(d.getMilliseconds(), 3)
    );
  }

  function traceButton(field, id) {
    const label = field.replace(/_id$/, "") + " ";
    const b = node("button", "trace-link", label + String(id).slice(0, 10));
    b.type = "button";
    b.title = "Show everything for " + field + " " + id;
    b.addEventListener("click", (e) => { e.stopPropagation(); setTrace(field, String(id)); });
    return b;
  }

  function detailRow(rec) {
    const tr = node("tr", "detail");
    const td = node("td");
    td.colSpan = 7;
    const copy = Object.assign({}, rec);
    const tb = copy.traceback;
    delete copy.traceback;
    td.appendChild(node("div", "label", "Record"));
    td.appendChild(node("pre", null, JSON.stringify(copy, null, 2)));
    if (tb) {
      td.appendChild(node("div", "label", "Traceback"));
      td.appendChild(node("pre", null, tb));
    }
    tr.appendChild(td);
    return tr;
  }

  function levelClass(level) {
    return String(level || "INFO").replace(/[^A-Z]/g, "");
  }

  function render() {
    const frag = document.createDocumentFragment();
    let errors = 0;
    let warnings = 0;

    state.records.forEach((rec) => {
      const level = levelClass(rec.level);
      if (level === "ERROR" || level === "CRITICAL") errors += 1;
      else if (level === "WARNING") warnings += 1;

      const tr = node("tr", "rec lvl-" + level);
      const time = node("td", "time", formatTime(rec.ts));
      time.title = String(rec.ts || "");
      tr.appendChild(time);

      const lvlCell = node("td");
      lvlCell.appendChild(node("span", "badge " + level, level));
      tr.appendChild(lvlCell);

      tr.appendChild(node("td", "src col-service", rec.service || ""));
      tr.appendChild(node("td", "src", rec.source || ""));
      tr.appendChild(node("td", "event", rec.event || rec.logger || ""));

      let message = String(rec.message || "");
      if (rec.exc_type && !message.includes(rec.exc_type)) message += "  [" + rec.exc_type + "]";
      tr.appendChild(node("td", "msg", message));

      const traceCell = node("td");
      traceFields.forEach((field) => {
        if (rec[field] !== undefined && rec[field] !== null && rec[field] !== "") {
          traceCell.appendChild(traceButton(field, rec[field]));
        }
      });
      tr.appendChild(traceCell);

      const key = String(rec.id);
      tr.addEventListener("click", () => {
        if (state.expanded.has(key)) state.expanded.delete(key);
        else state.expanded.add(key);
        render();
      });
      frag.appendChild(tr);
      if (state.expanded.has(key)) frag.appendChild(detailRow(rec));
    });

    el.rows.replaceChildren(frag);
    el.empty.hidden = state.records.length > 0;

    el.stats.replaceChildren();
    el.stats.appendChild(node("span", null, state.records.length + " records"));
    if (errors) {
      el.stats.appendChild(document.createTextNode(" · "));
      el.stats.appendChild(node("span", "err", errors + " failures"));
    }
    if (warnings) {
      el.stats.appendChild(document.createTextNode(" · "));
      el.stats.appendChild(node("span", "warn", warnings + " warnings"));
    }
  }

  // -- boot -----------------------------------------------------------------------

  el.table.classList.add("single-service");
  state.token = storage.get();
  if (state.token) {
    showApp();
    load(true);
    startPolling();
  } else {
    showLogin();
  }
})();
