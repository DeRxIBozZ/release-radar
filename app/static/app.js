"use strict";

const IMG = "https://image.tmdb.org/t/p/";
const state = { user: null, prefs: null, filters: {}, meta: null, feedToken: 0 };
const app = document.getElementById("app");

// ---- tiny DOM helper: builds elements without ever using innerHTML --------
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "checked" || k === "disabled" || k === "value" || k === "open") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return el;
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", "X-Requested-With": "release-radar" },
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
  });
  let data = null;
  try { data = await res.json(); } catch (e) { /* empty body */ }
  if (res.status === 401 && path !== "/api/login") { state.user = null; renderLogin(); throw new Error("Not logged in"); }
  if (!res.ok) throw new Error((data && data.detail) || `Request failed (${res.status})`);
  return data;
}

// ---- boot -----------------------------------------------------------------
async function boot() {
  try {
    const me = await api("GET", "/api/me");
    Object.assign(state, { user: me.user, prefs: me.prefs, filters: me.filters || {} });
    state.meta = await api("GET", "/api/meta");
  } catch (e) { if (!state.user) return; }
  route();
}
window.addEventListener("hashchange", () => state.user && route());
document.addEventListener("click", (e) => {
  for (const d of document.querySelectorAll("details.drop[open]")) if (!d.contains(e.target)) d.open = false;
});

function route() {
  const page = (location.hash.replace(/^#\/?/, "") || "released").split("?")[0];
  const pages = { released: renderFeed, upcoming: renderFeed, settings: renderSettings, users: renderUsers };
  if (!pages[page] || (page === "users" && !state.user.is_admin)) { location.hash = "#/released"; return; }
  app.replaceChildren(header(page), h("main", { id: "main" }), footer());
  pages[page](page);
}

function header(page) {
  const tab = (id, label) => h("a", { href: "#/" + id, class: page === id ? "active" : "" }, label);
  return h("header", { class: "top" }, h("div", { class: "bar" },
    h("a", { class: "brand", href: "#/released" }, h("img", { src: "/static/icon.svg", alt: "" }), h("span", {}, "Release Radar")),
    h("nav", { class: "tabs" }, tab("released", "Released"), tab("upcoming", "Upcoming"), tab("settings", "Settings"),
      state.user.is_admin ? tab("users", "Users") : null),
    h("span", { class: "who" }, state.user.username),
    h("button", { class: "btn small", onclick: async () => { await api("POST", "/api/logout"); state.user = null; renderLogin(); } }, "Log out"),
  ));
}

function footer() {
  return h("footer", {}, "This product uses the TMDB API but is not endorsed or certified by ",
    h("a", { href: "https://www.themoviedb.org", target: "_blank", rel: "noopener" }, "TMDB"),
    ". Streaming availability data by JustWatch.");
}

// ---- login ----------------------------------------------------------------
function renderLogin() {
  const msg = h("div", { class: "msg err" });
  const user = h("input", { type: "text", placeholder: "Username", autocomplete: "username", required: true });
  const pass = h("input", { type: "password", placeholder: "Password", autocomplete: "current-password", required: true });
  const form = h("form", { onsubmit: async (e) => {
    e.preventDefault(); msg.textContent = "";
    try { await api("POST", "/api/login", { username: user.value, password: pass.value }); await boot(); }
    catch (err) { msg.textContent = err.message; }
  } }, user, pass, h("button", { class: "btn primary", type: "submit" }, "Log in"), msg);
  app.replaceChildren(h("div", { class: "login" },
    h("h1", {}, h("img", { src: "/static/icon.svg", alt: "" }), "Release Radar"), form));
  user.focus();
}

// ---- shared helpers ---------------------------------------------------------
const providerById = (id) => (state.meta?.providers || []).find((p) => p.id === id);
function sourceLabel(source) {
  if (source === "theatrical") return "In theaters";
  if (source === "digital") return "Digital";
  const p = providerById(Number(source.slice(2)));
  return p ? p.name : "Streaming";
}
function sourceLogo(source) {
  if (!source.startsWith("p:")) return h("img", { src: `/static/${source}.svg`, alt: "" });
  const p = source.startsWith("p:") ? providerById(Number(source.slice(2))) : null;
  return p && p.logo ? h("img", { src: IMG + "w92" + p.logo, alt: "", loading: "lazy" }) : null;
}
function trackedSources() {
  const p = state.prefs, out = [];
  if (p.theatrical) out.push("theatrical");
  if (p.digital) out.push("digital");
  for (const id of p.providers || []) out.push("p:" + id);
  return out;
}
function statusNotice() {
  const s = state.meta?.status;
  if (!s) return "";
  if (s.error) return h("div", { class: "notice err" }, "Sync problem: " + s.error);
  if (s.running) return h("div", { class: "notice" }, "Fetching release data from TMDB… " + (s.progress || "") +
    ". The first run takes a few minutes; reload to see new entries.");
  return "";
}
function dayLabel(iso, today) {
  const d = new Date(iso + "T12:00:00"), t = new Date(today + "T12:00:00");
  const diff = Math.round((d - t) / 86400000);
  const text = d.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
  let rel = "";
  if (diff === 0) rel = "Today"; else if (diff === 1) rel = "Tomorrow"; else if (diff === -1) rel = "Yesterday";
  else if (diff > 1) rel = diff < 60 ? `in ${diff} days` : `in ${Math.round(diff / 30)} months`;
  else rel = -diff < 60 ? `${-diff} days ago` : `${Math.round(-diff / 30)} months ago`;
  return [text, rel];
}
function episodeLabel(it) {
  if (it.media_type !== "tv") return null;
  const s = "Season " + it.season;
  if (!it.ep_from) return [s, it.season === 1 ? "Series premiere" : "Season premiere"];
  const full = it.ep_from === 1 && it.ep_total > 1 && it.ep_to === it.ep_total;
  if (full) return [s, "Full season"];
  const eps = it.ep_from === it.ep_to ? "Episode " + it.ep_from : `Episodes ${it.ep_from}–${it.ep_to}`;
  return [s + " · " + eps, it.premiere ? (it.season === 1 ? "Series premiere" : "Season premiere") : null];
}

// ---- feed -------------------------------------------------------------------
function renderFeed(tab) {
  const main = document.getElementById("main");
  const f = Object.assign({ type: "all", region: "all", genres: [], sources: [], q: "" }, state.filters);
  const tracked = trackedSources();
  f.sources = f.sources.filter((s) => tracked.includes(s));
  const list = h("div", {}), count = h("span", { class: "count" }), more = h("div", { class: "more" });
  let offset = 0, lastDay = null, grid = null, saveTimer = null;

  const changed = () => {
    state.filters = f;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => api("PUT", "/api/filters", f).catch(() => {}), 600);
    load(true);
  };

  async function load(reset) {
    const token = ++state.feedToken;
    if (reset) { offset = 0; lastDay = null; grid = null; }
    const qs = new URLSearchParams({ tab, type: f.type, region: f.region, genres: f.genres.join(","),
      sources: f.sources.join(","), q: f.q, offset, limit: 60 });
    let data;
    try { data = await api("GET", "/api/feed?" + qs); } catch (e) { return; }
    if (token !== state.feedToken) return;
    if (reset) list.replaceChildren();
    count.textContent = data.total + (data.total === 1 ? " release" : " releases");
    for (const it of data.items) {
      if (it.date !== lastDay) {
        lastDay = it.date;
        const [text, rel] = dayLabel(it.date, data.today);
        grid = h("div", { class: "grid" });
        list.append(h("section", { class: "day" }, h("h2", {}, text, h("small", {}, rel)), grid));
      }
      grid.append(card(it));
    }
    if (!data.total) {
      list.replaceChildren(h("div", { class: "empty" }, tracked.length
        ? "Nothing matches. Try loosening the filters, or wait for the first sync to finish."
        : "You are not tracking anything yet. Pick cinemas or streaming services in Settings."));
    }
    offset = data.next_offset;
    more.replaceChildren(offset != null ? h("button", { class: "btn", onclick: () => load(false) }, "Load more") : "");
  }

  const seg = (key, options) => {
    const box = h("div", { class: "seg" });
    for (const [value, label] of options) {
      box.append(h("button", { class: f[key] === value ? "on" : "", onclick: (e) => {
        f[key] = value; [...box.children].forEach((x) => x.classList.remove("on")); e.currentTarget.classList.add("on"); changed();
      } }, label));
    }
    return box;
  };

  const genreSummary = h("summary", { class: "btn" });
  const setGenreSummary = () => { genreSummary.textContent = f.genres.length ? `Genres (${f.genres.length})` : "Genres"; };
  setGenreSummary();
  const genreMenu = h("details", { class: "drop" }, genreSummary, h("div", { class: "menu" },
    (state.meta?.genres || []).map((g) => h("label", {},
      h("input", { type: "checkbox", checked: f.genres.includes(g.id), onchange: (e) => {
        f.genres = e.target.checked ? [...f.genres, g.id] : f.genres.filter((x) => x !== g.id);
        setGenreSummary(); changed();
      } }), g.name))));

  let searchTimer = null;
  const search = h("input", { type: "search", placeholder: "Search title", value: f.q, oninput: (e) => {
    clearTimeout(searchTimer); searchTimer = setTimeout(() => { f.q = e.target.value; changed(); }, 300);
  } });

  const sourceChips = h("div", { class: "chips" }, tracked.length > 1 ? tracked.map((s) => {
    const logo = sourceLogo(s);
    const chip = h("button", { class: "chip" + (logo ? "" : " plain") + (f.sources.includes(s) ? " on" : ""), onclick: () => {
      f.sources = f.sources.includes(s) ? f.sources.filter((x) => x !== s) : [...f.sources, s];
      chip.classList.toggle("on"); changed();
    } }, logo, sourceLabel(s));
    return chip;
  }) : null);

  main.append(statusNotice(),
    h("div", { class: "filters" },
      seg("type", [["all", "All"], ["movie", "Movies"], ["tv", "Series"]]),
      state.prefs.regions.length > 1 ? seg("region", [["all", "All regions"], ...state.prefs.regions.map((r) => [r, r])]) : null,
      genreMenu, search, count),
    sourceChips, list, more);
  load(true);
}

function card(it) {
  const url = `https://www.themoviedb.org/${it.media_type}/${it.tmdb_id}`;
  const genres = it.genres.map((id) => state.meta?.genres.find((g) => g.id === id)?.name).filter(Boolean).slice(0, 3).join(", ");
  const ep = episodeLabel(it);
  // Merge the same source across regions into one badge: "Netflix DE US"
  const bySource = new Map();
  for (const w of it.where) bySource.set(w.source, [...(bySource.get(w.source) || []), w.region]);
  const badges = [...bySource].map(([source, regions]) => {
    const logo = sourceLogo(source);
    return h("span", { class: "badge" + (logo ? "" : " plain") }, logo, sourceLabel(source), h("b", {}, regions.sort().join(" ")));
  });
  const poster = it.poster
    ? h("img", { class: "poster", src: IMG + "w185" + it.poster, alt: "", loading: "lazy" })
    : h("div", { class: "poster" }, "No poster");
  return h("a", { class: "card", href: url, target: "_blank", rel: "noopener" }, poster,
    h("div", { class: "info" },
      h("h3", {}, it.title),
      h("div", { class: "sub" },
        h("span", { class: "tag " + it.media_type }, it.media_type === "tv" ? "Series" : "Movie"),
        ep && ep[1] ? h("span", { class: "tag hl" }, ep[1]) : null,
        ep ? ep[0] : null),
      h("div", { class: "sub" }, [genres, it.vote ? "★ " + it.vote.toFixed(1) : null].filter(Boolean).join(" · ")),
      h("div", { class: "overview" }, it.overview),
      h("div", { class: "where" }, badges)));
}

// ---- settings -----------------------------------------------------------------
function renderSettings() {
  const main = document.getElementById("main");
  const p = JSON.parse(JSON.stringify(state.prefs));
  const msg = h("span", { class: "msg" });
  const check = (label, checked, onchange) => h("label", { class: "check" }, h("input", { type: "checkbox", checked, onchange }), label);

  const provBox = h("div", { class: "providers" });
  let query = "", showAll = false;
  const moreBtn = h("button", { class: "btn small", onclick: () => { showAll = !showAll; drawProviders(); } });
  function drawProviders() {
    const all = (state.meta?.providers || []).filter((x) => x.regions.some((r) => p.regions.includes(r)) || p.providers.includes(x.id));
    let shown = all.filter((x) => x.name.toLowerCase().includes(query));
    const total = shown.length;
    if (!query && !showAll) shown = shown.filter((x, i) => i < 24 || p.providers.includes(x.id));
    provBox.replaceChildren(...shown.map((x) => {
      const btn = h("button", { class: "prov" + (p.providers.includes(x.id) ? " on" : ""), type: "button", onclick: () => {
        p.providers = p.providers.includes(x.id) ? p.providers.filter((i) => i !== x.id) : [...p.providers, x.id];
        btn.classList.toggle("on");
      } }, x.logo ? h("img", { src: IMG + "w92" + x.logo, alt: "", loading: "lazy" }) : null,
        h("span", {}, x.name), h("small", {}, x.regions.join(" ")));
      return btn;
    }));
    if (!all.length) provBox.replaceChildren(h("div", { class: "sub" }, "The service list appears after the first sync with TMDB."));
    moreBtn.textContent = showAll ? "Show fewer" : `Show all ${total}`;
    moreBtn.classList.toggle("hidden", !!query || total <= 24);
  }

  const tvMode = (value, label) => h("label", { class: "check" },
    h("input", { type: "radio", name: "tvmode", checked: p.tv_mode === value, onchange: () => { p.tv_mode = value; } }), label);

  const save = h("button", { class: "btn primary", onclick: async () => {
    msg.className = "msg"; msg.textContent = "";
    try {
      const res = await api("PUT", "/api/prefs", p);
      state.prefs = res.prefs;
      msg.textContent = res.syncing ? "Saved. New sources are being fetched, this can take a few minutes." : "Saved.";
    } catch (e) { msg.className = "msg err"; msg.textContent = e.message; }
  } }, "Save");

  const cur = h("input", { type: "password", placeholder: "Current password", autocomplete: "current-password" });
  const nw = h("input", { type: "password", placeholder: "New password (min. 8 characters)", autocomplete: "new-password" });
  const pwMsg = h("span", { class: "msg" });

  main.append(statusNotice(),
    h("div", { class: "panel" }, h("h2", {}, "Regions"),
      h("p", { class: "help" }, "Release dates differ per country. Pick the ones you care about."),
      h("div", { class: "row" }, state.meta.regions.map((r) => check(r === "DE" ? "Germany (DE)" : "United States (US)", p.regions.includes(r), (e) => {
        p.regions = e.target.checked ? [...p.regions, r] : p.regions.filter((x) => x !== r); drawProviders();
      })))),
    h("div", { class: "panel" }, h("h2", {}, "Movie releases"),
      h("p", { class: "help" }, "Cinema and digital (buy or rent) release dates in your regions."),
      h("div", { class: "row" },
        check("In theaters", p.theatrical, (e) => { p.theatrical = e.target.checked; }),
        check("Digital / VOD", p.digital, (e) => { p.digital = e.target.checked; }))),
    h("div", { class: "panel" }, h("h2", {}, "Streaming services"),
      h("p", { class: "help" }, "New movies and series on the services you select. The letters show where a service is available."),
      h("div", { class: "row" },
        h("input", { type: "search", placeholder: "Search services", oninput: (e) => { query = e.target.value.trim().toLowerCase(); drawProviders(); } }), moreBtn),
      provBox),
    h("div", { class: "panel" }, h("h2", {}, "Series"),
      h("p", { class: "help" }, "Show only season premieres, or every new episode."),
      h("div", { class: "row" }, tvMode("premieres", "Season premieres only"), tvMode("episodes", "Every episode"))),
    h("div", { class: "row", style: "margin-bottom:28px" }, save, msg),
    h("div", { class: "panel" }, h("h2", {}, "Change password"), h("p", { class: "help" }, "Signed in as " + state.user.username + "."),
      h("form", { class: "row", onsubmit: async (e) => {
        e.preventDefault(); pwMsg.className = "msg"; pwMsg.textContent = "";
        try { await api("POST", "/api/password", { current: cur.value, new: nw.value }); cur.value = nw.value = ""; pwMsg.textContent = "Password changed."; }
        catch (err) { pwMsg.className = "msg err"; pwMsg.textContent = err.message; }
      } }, cur, nw, h("button", { class: "btn", type: "submit" }, "Change"), pwMsg)));
  drawProviders();
}

// ---- users (admin) ------------------------------------------------------------
async function renderUsers() {
  const main = document.getElementById("main");
  const msg = h("span", { class: "msg" });
  const say = (text, err) => { msg.className = "msg" + (err ? " err" : ""); msg.textContent = text; };
  const tbody = h("tbody", {});
  const act = async (fn, done) => { try { await fn(); say(done); await draw(); } catch (e) { say(e.message, true); } };

  async function draw() {
    const users = await api("GET", "/api/users");
    tbody.replaceChildren(...users.map((u) => h("tr", {},
      h("td", {}, u.username, u.id === state.user.id ? " (you)" : ""),
      h("td", {}, u.is_admin ? "Admin" : "User"),
      h("td", {}, u.created_at.slice(0, 10)),
      h("td", { class: "actions" },
        h("button", { class: "btn small", onclick: () => {
          const pw = prompt(`New password for ${u.username} (min. 8 characters):`);
          if (pw) act(() => api("PATCH", "/api/users/" + u.id, { password: pw }), "Password reset. The user was logged out everywhere.");
        } }, "Reset password"),
        h("button", { class: "btn small", onclick: () => act(() => api("PATCH", "/api/users/" + u.id, { is_admin: !u.is_admin }), "Role changed.") },
          u.is_admin ? "Make user" : "Make admin"),
        u.id !== state.user.id ? h("button", { class: "btn small danger", onclick: () => {
          if (confirm(`Delete ${u.username}? Their settings are removed.`)) act(() => api("DELETE", "/api/users/" + u.id), "User deleted.");
        } }, "Delete") : null))));
  }

  const name = h("input", { type: "text", placeholder: "Username", autocomplete: "off" });
  const pass = h("input", { type: "password", placeholder: "Password (min. 8 characters)", autocomplete: "new-password" });
  const adm = h("input", { type: "checkbox" });
  const s = state.meta.status;
  const last = s.last_finished ? new Date(s.last_finished * 1000).toLocaleString("en-GB") : "never";

  main.append(statusNotice(),
    h("div", { class: "panel" }, h("h2", {}, "Users"),
      h("p", { class: "help" }, "Every user has their own tracked services and filters. There is no public sign-up."),
      h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Username"), h("th", {}, "Role"), h("th", {}, "Created"), h("th", {}))), tbody),
      h("div", { style: "margin-top:10px" }, msg)),
    h("div", { class: "panel" }, h("h2", {}, "Add user"), h("p", { class: "help" }, "New users start with the default tracking settings."),
      h("form", { class: "row", onsubmit: (e) => {
        e.preventDefault();
        act(async () => { await api("POST", "/api/users", { username: name.value, password: pass.value, is_admin: adm.checked }); name.value = pass.value = ""; adm.checked = false; }, "User created.");
      } }, name, pass, h("label", { class: "check" }, adm, "Admin"), h("button", { class: "btn primary", type: "submit" }, "Add"))),
    h("div", { class: "panel" }, h("h2", {}, "Data sync"),
      h("p", { class: "help" }, `Last finished: ${last}. ` + (s.warnings.length ? "Warnings: " + s.warnings.join("; ") : "")),
      h("button", { class: "btn", onclick: async (e) => { await api("POST", "/api/sync"); e.target.textContent = "Sync started"; e.target.disabled = true; } }, "Sync now")));
  draw().catch((e) => say(e.message, true));
}

boot();
