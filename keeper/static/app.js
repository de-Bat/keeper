"use strict";

// Keeper web app / PWA.
//
// Local-first: the library lives in IndexedDB, so the app opens, browses and searches
// offline (the service worker caches the app itself and images). New screenshots and
// edits are applied locally and queued; the queue is replayed in order whenever the
// self-hosted server is reachable, then changes are pulled with GET /api/sync?since=.

const CATEGORY_LABELS = {
  movie: "🎬 Movie", tv_show: "📺 TV show", github_repo: "💻 GitHub repo", recipe: "🍳 Recipe",
  book: "📚 Book", music: "🎵 Music", podcast: "🎙️ Podcast", video: "▶️ Video", article: "📰 Article",
  product: "🛍️ Product", place: "📍 Place", event: "📅 Event", app: "📱 App", course: "🎓 Course", other: "📌 Other",
};
const WIDE_CATEGORIES = new Set(["github_repo", "article", "video", "product", "app", "other", "place", "event", "course"]);

// Metadata keys shown elsewhere in the detail view (or not useful to show).
const HIDDEN_META = new Set([
  "screenshot_text", "sources", "confidence", "ingredients", "instructions", "imdb_rating", "rotten_tomatoes",
  "metacritic", "tmdb_rating", "stars", "rating", "rating_count", "description", "post_url", "imdb_votes", "tmdb_id",
  "page_description", "page_title", "github_full_name", "year",
]);

const state = {
  q: "", category: null, tags: [], review: false,
  items: new Map(),        // id -> item (mirror of the IndexedDB "items" store)
  ops: [],                 // queued changes, oldest first
  sync: "idle",            // idle | syncing | offline | auth | error
  syncError: null,
  lastSync: null,
};
const blobUrls = new Map(); // id -> object URL for screenshots not uploaded yet

const $ = (sel) => document.querySelector(sel);

// ---- small helpers ---------------------------------------------------------

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function safeUrl(url) {
  if (!url || typeof url !== "string") return null;
  try {
    const u = new URL(url, location.href);
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
  } catch { return null; }
}
function humanize(key) {
  return key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}
function fmtValue(v) {
  if (Array.isArray(v)) return v.join(", ");
  if (typeof v === "boolean") return v ? "Yes" : "No";
  if (typeof v === "number") return v >= 10000 ? v.toLocaleString() : String(v);
  if (typeof v === "string" && /^\d{4}-\d{2}-\d{2}T/.test(v)) return new Date(v).toLocaleDateString();
  return String(v);
}
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), 3500);
}
function newId() {
  // crypto.randomUUID needs a secure context; getRandomValues works on plain-http LAN servers too.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return "web-" + Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}
function fold(s) {
  return String(s).toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "");
}
function normalizeTag(tag) {
  return tag.trim().toLowerCase().replace(/^#/, "").replace(/\s+/g, "-").replace(/[^\p{L}\p{N}_\-+.]/gu, "").slice(0, 40);
}
function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

// ---- IndexedDB -------------------------------------------------------------

const db = {
  _conn: null,
  open() {
    if (this._conn) return this._conn;
    this._conn = new Promise((resolve, reject) => {
      const req = indexedDB.open("keeper", 1);
      req.onupgradeneeded = () => {
        const d = req.result;
        d.createObjectStore("items", { keyPath: "id" });
        d.createObjectStore("ops", { keyPath: "seq", autoIncrement: true });
        d.createObjectStore("blobs");   // id -> Blob (screenshots waiting to upload)
        d.createObjectStore("kv");      // lastSync, token
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    return this._conn;
  },
  async tx(stores, mode, fn) {
    const d = await this.open();
    return new Promise((resolve, reject) => {
      const t = d.transaction(stores, mode);
      let result;
      Promise.resolve(fn(t)).then((r) => (result = r));
      t.oncomplete = () => resolve(result);
      t.onerror = t.onabort = () => reject(t.error);
    });
  },
  all(store) {
    return this.tx([store], "readonly", (t) => new Promise((res) => {
      const r = t.objectStore(store).getAll();
      r.onsuccess = () => res(r.result);
    }));
  },
  get(store, key) {
    return this.tx([store], "readonly", (t) => new Promise((res) => {
      const r = t.objectStore(store).get(key);
      r.onsuccess = () => res(r.result);
    }));
  },
  put(store, value, key) {
    return this.tx([store], "readwrite", (t) => { t.objectStore(store).put(value, key); });
  },
  del(store, key) {
    return this.tx([store], "readwrite", (t) => { t.objectStore(store).delete(key); });
  },
  add(store, value) {
    return this.tx([store], "readwrite", (t) => new Promise((res) => {
      const r = t.objectStore(store).add(value);
      r.onsuccess = () => res(r.result);
    }));
  },
};

// Persistent storage keeps iOS/Safari from evicting the offline library under storage pressure.
navigator.storage?.persist?.().catch(() => {});

// ---- local store -----------------------------------------------------------

async function loadLocal() {
  const [items, ops, lastSync] = await Promise.all([db.all("items"), db.all("ops"), db.get("kv", "lastSync")]);
  state.items = new Map(items.map((i) => [i.id, i]));
  state.ops = ops.sort((a, b) => a.seq - b.seq);
  state.lastSync = lastSync || null;
  for (const item of items) {
    if (item.pending_upload && !blobUrls.has(item.id)) {
      const blob = await db.get("blobs", item.id);
      if (blob) blobUrls.set(item.id, URL.createObjectURL(blob));
    }
  }
}

async function putItem(item) {
  state.items.set(item.id, item);
  await db.put("items", item);
}

async function removeItem(id, { keepDeleteOp = false } = {}) {
  state.items.delete(id);
  await db.del("items", id);
  await db.del("blobs", id);
  if (blobUrls.has(id)) { URL.revokeObjectURL(blobUrls.get(id)); blobUrls.delete(id); }
  for (const op of state.ops.filter((o) => o.id === id && !(keepDeleteOp && o.type === "delete"))) await dropOp(op);
}

async function enqueue(op) {
  op.seq = await db.add("ops", op);
  state.ops.push(op);
}

async function dropOp(op) {
  state.ops = state.ops.filter((o) => o.seq !== op.seq);
  await db.del("ops", op.seq);
}

function hasPendingOps(id) { return state.ops.some((o) => o.id === id); }

// Merge a server copy without losing local-only fields.
async function mergeServerItem(server) {
  await putItem({ ...server, pending_upload: false });
  if (blobUrls.has(server.id)) { URL.revokeObjectURL(blobUrls.get(server.id)); blobUrls.delete(server.id); }
  await db.del("blobs", server.id);
}

// ---- local changes (all work offline) --------------------------------------

async function addScreenshots(files, note) {
  const images = [...files].filter((f) => f.type.startsWith("image/"));
  if (!images.length) return toast("Only images can be added.");
  const now = new Date().toISOString();
  for (const file of images) {
    const id = newId();
    const blob = file.slice(0, file.size, file.type);   // detach from the <input> FileList
    await db.put("blobs", blob, id);
    blobUrls.set(id, URL.createObjectURL(blob));
    await putItem({
      id, created_at: now, updated_at: now, status: "queued", error: null, note: note || null,
      category: null, title: null, subtitle: null, summary: null, metadata: {}, links: [], tags: [],
      pending_upload: true, mime: file.type, filename: file.name || "screenshot",
    });
    await enqueue({ type: "upload", id });
  }
  toast(navigator.onLine
    ? `Added ${images.length} screenshot${images.length > 1 ? "s" : ""} — analyzing…`
    : `Saved ${images.length} screenshot${images.length > 1 ? "s" : ""} offline — will upload when you're back online`);
  render();
  requestSync();
}

async function editItem(id, patch) {
  const item = state.items.get(id);
  if (!item) return;
  if (patch.tags) patch.tags = [...new Set(patch.tags.map(normalizeTag).filter(Boolean))].sort();
  await putItem({ ...item, ...patch });
  await enqueue({ type: "patch", id, patch });
  render();
  requestSync();
}

async function reanalyzeItem(id) {
  const item = state.items.get(id);
  if (!item || item.pending_upload) return;
  await putItem({ ...item, status: "processing", error: null });
  await enqueue({ type: "reanalyze", id });
  render();
  requestSync();
}

// "The model got it wrong": fix facts directly, or describe it and let Claude look again.
async function correctItem(id, correction) {
  const item = state.items.get(id);
  if (!item) return;
  const fix = Object.fromEntries(Object.entries(correction).filter(([, v]) => v !== "" && v != null));
  if (!Object.keys(fix).length) return toast("Enter what it really is, or describe it.");
  await putItem({
    ...item, ...("title" in fix ? { title: fix.title } : {}), ...("category" in fix ? { category: fix.category } : {}),
    status: item.pending_upload ? item.status : "processing", error: null,
    corrected: true, needs_review: false, confidence: "hint" in fix && Object.keys(fix).length === 1 ? item.confidence : 100,
  });
  await enqueue({ type: "correct", id, correction: fix });
  toast(navigator.onLine ? "Correction saved — updating details…" : "Correction saved — will update when you're back online");
  render();
  requestSync();
}

async function deleteItem(id) {
  const item = state.items.get(id);
  if (!item) return;
  const neverUploaded = item.pending_upload;
  await removeItem(id);
  if (!neverUploaded) await enqueue({ type: "delete", id });  // the server never saw the others
  render();
  requestSync();
}

// ---- sync with the self-hosted server --------------------------------------

class HttpError extends Error {
  constructor(status, detail) { super(detail); this.status = status; }
  get permanent() { return this.status >= 400 && this.status < 500 && ![401, 403, 408, 429].includes(this.status); }
}

async function api(path, opts = {}) {
  const token = await db.get("kv", "token");
  const headers = new Headers(opts.headers || {});
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), opts.timeout || 30000);
  let res;
  try {
    res = await fetch(path, { ...opts, headers, signal: controller.signal, cache: "no-store" });
  } finally {
    clearTimeout(timer);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new HttpError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.status === 204 ? null : res.json();
}

async function setToken(token) {
  token = (token || "").trim();
  await db.put("kv", token, "token");
  // Cookie too, so <img src="/media/..."> requests are authorized.
  document.cookie = `keeper_token=${encodeURIComponent(token)}; path=/; max-age=31536000; SameSite=Strict`;
}

let syncing = false, rerun = false, followUp = null;
let fixing = null;  // id of the item whose correction form is open

function requestSync() { sync().catch((e) => console.error(e)); }

async function sync() {
  if (syncing) { rerun = true; return; }
  syncing = true;
  setSyncState("syncing");
  try {
    do {
      rerun = false;
      await importShared();
      try {
        await api("/api/health", { timeout: 5000 });
      } catch {
        return setSyncState("offline");
      }
      await pushOps();
      const delta = await api(`/api/sync${state.lastSync ? `?since=${encodeURIComponent(state.lastSync)}` : ""}`);
      for (const id of delta.deleted) if (!hasPendingOps(id)) await removeItem(id);
      for (const item of delta.items) if (!hasPendingOps(item.id)) await mergeServerItem(item);
      state.lastSync = delta.server_time;
      await db.put("kv", state.lastSync, "lastSync");
    } while (rerun);
    setSyncState("idle");
  } catch (e) {
    if (e instanceof HttpError && e.status === 401) setSyncState("auth");
    else if (e instanceof HttpError) setSyncState("error", e.message);
    else setSyncState("offline");
  } finally {
    syncing = false;
    render();
    clearTimeout(followUp);
    // While the server is still identifying screenshots, check back soon.
    if ([...state.items.values()].some((i) => i.status === "processing") && state.sync === "idle") {
      followUp = setTimeout(requestSync, 3000);
    }
  }
}

// Replay queued changes in order; stop at the first failure that might succeed later.
async function pushOps() {
  while (state.ops.length) {
    const op = state.ops[0];
    try {
      if (op.type === "upload") {
        const item = state.items.get(op.id);
        const blob = await db.get("blobs", op.id);
        if (!item || !blob) { await dropOp(op); continue; }
        const fd = new FormData();
        fd.append("file", blob, item.filename || `${op.id}.png`);
        fd.append("id", item.id);
        fd.append("created_at", item.created_at);
        if (item.note) fd.append("note", item.note);
        if (item.tags?.length) fd.append("tags", item.tags.join(","));
        const saved = await api("/api/items", { method: "POST", body: fd, timeout: 120000 });
        await dropOp(op);
        await mergeServerItem(saved);
      } else if (op.type === "patch") {
        const saved = await api(`/api/items/${encodeURIComponent(op.id)}`, {
          method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(op.patch),
        });
        await dropOp(op);
        if (!hasPendingOps(op.id)) await mergeServerItem(saved);
      } else if (op.type === "correct") {
        const saved = await api(`/api/items/${encodeURIComponent(op.id)}/correct`, {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(op.correction),
        });
        await dropOp(op);
        if (!hasPendingOps(op.id)) await mergeServerItem(saved);
      } else if (op.type === "reanalyze") {
        const saved = await api(`/api/items/${encodeURIComponent(op.id)}/reanalyze`, { method: "POST" });
        await dropOp(op);
        if (!hasPendingOps(op.id)) await mergeServerItem(saved);
      } else if (op.type === "delete") {
        try {
          await api(`/api/items/${encodeURIComponent(op.id)}`, { method: "DELETE" });
        } catch (e) {
          if (!(e instanceof HttpError && e.status === 404)) throw e;
        }
        await dropOp(op);
      } else {
        await dropOp(op);
      }
    } catch (e) {
      if (!(e instanceof HttpError && e.permanent)) throw e;
      // Retrying would fail forever. 410: deleted on another device while we were offline.
      if (e.status === 410) await removeItem(op.id);
      else toast(`A change was rejected by the server: ${e.message}`);
      await dropOp(op);
    }
    render();
  }
}

// Screenshots shared to the installed app (Web Share Target — Android/desktop Chrome).
async function importShared() {
  if (!("caches" in window)) return;
  const cache = await caches.open("keeper-share-inbox");
  const keys = await cache.keys();
  for (const req of keys) {
    const res = await cache.match(req);
    const blob = await res.blob();
    const note = decodeURIComponent(res.headers.get("X-Keeper-Note") || "");
    await addScreenshots([new File([blob], "shared", { type: blob.type })], note);
    await cache.delete(req);
  }
}

function setSyncState(s, error = null) {
  state.sync = s;
  state.syncError = error;
  renderSyncStatus();
}

// ---- rendering -------------------------------------------------------------

function imageFor(item) {
  return safeUrl(item.image_url) || blobUrls.get(item.id) || (item.image_file ? `/media/${encodeURIComponent(item.image_file)}` : null);
}
function screenshotFor(item) {
  return blobUrls.get(item.id) || (item.image_file ? `/media/${encodeURIComponent(item.image_file)}` : null);
}

function searchBlob(item) {
  const parts = [item.title, item.subtitle, item.summary, item.note, item.category, item.source_platform, ...(item.tags || [])];
  const walk = (v) => {
    if (Array.isArray(v)) v.forEach(walk);
    else if (v && typeof v === "object") Object.values(v).forEach(walk);
    else if (typeof v === "string" && !v.startsWith("http")) parts.push(v);
    else if (typeof v === "number") parts.push(String(v));
  };
  walk(item.metadata || {});
  return fold(parts.filter(Boolean).join(" ").replace(/-/g, " ") + " " + (item.tags || []).join(" "));
}

function filteredItems() {
  const words = fold(state.q).split(/[^\p{L}\p{N}]+/u).filter(Boolean);
  return [...state.items.values()]
    .filter((item) => {
      if (state.category && item.category !== state.category) return false;
      if (state.review && !item.needs_review) return false;
      if (state.tags.some((t) => !(item.tags || []).includes(t))) return false;
      if (!words.length) return true;
      const tokens = searchBlob(item).split(/[^\p{L}\p{N}]+/u);
      return words.every((w) => tokens.some((t) => t.startsWith(w)));
    })
    .sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
}

function cardFacts(item) {
  const m = item.metadata || {};
  const facts = [];
  if (m.imdb_rating) facts.push(`⭐ ${m.imdb_rating.replace("/10", "")}`);
  else if (m.tmdb_rating) facts.push(`⭐ ${m.tmdb_rating.replace("/10", "")}`);
  if (m.rotten_tomatoes) facts.push(`🍅 ${m.rotten_tomatoes}`);
  if (m.stars != null) facts.push(`★ ${Number(m.stars).toLocaleString()}`);
  if (m.programming_language) facts.push(m.programming_language);
  if (m.total_time) facts.push(`⏱ ${m.total_time}`);
  if (m.rating && item.category === "recipe") facts.push(`⭐ ${m.rating}`);
  if (m.year && !["github_repo", "recipe"].includes(item.category)) facts.push(m.year);
  return facts.slice(0, 4);
}

function cardTitle(item) {
  if (item.title) return item.title;
  return { queued: "Waiting to upload", processing: "Analyzing screenshot…", error: "Couldn't identify — open to retry" }[item.status] || "Untitled";
}

function render() {
  renderFilters();
  renderGrid();
  renderSyncStatus();
  const dlg = $("#detail");
  if (dlg.open) {
    const item = state.items.get(dlg.dataset.id);
    if (item) renderDetail(item); else dlg.close();
  }
}

function renderFilters() {
  const items = [...state.items.values()];
  const catCounts = {}, tagCounts = {};
  for (const i of items) {
    if (i.category) catCounts[i.category] = (catCounts[i.category] || 0) + 1;
    for (const t of i.tags || []) tagCounts[t] = (tagCounts[t] || 0) + 1;
  }
  const review = items.filter((i) => i.needs_review).length;
  $("#categories").innerHTML = (review ? `
    <button class="chip warn ${state.review ? "active" : ""}" data-review>⚠ Needs review <span class="count">${review}</span></button>` : "") +
    (Object.entries(catCounts).sort((a, b) => b[1] - a[1]).map(([c, n]) => `
    <button class="chip ${state.category === c ? "active" : ""}" data-category="${esc(c)}">
      ${esc(CATEGORY_LABELS[c] || c)} <span class="count">${n}</span>
    </button>`).join("") || `<span class="count">—</span>`);
  $("#categories").closest("section").classList.toggle("empty", !review && !Object.keys(catCounts).length);
  $("#tags").closest("section").classList.toggle("empty", !Object.keys(tagCounts).length);
  $("#tags").innerHTML = Object.entries(tagCounts).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 40).map(([t, n]) => `
    <button class="chip ${state.tags.includes(t) ? "active" : ""}" data-tag="${esc(t)}">
      #${esc(t)} <span class="count">${n}</span>
    </button>`).join("") || `<span class="count">—</span>`;
}

function renderGrid() {
  const items = filteredItems();
  $("#empty").hidden = items.length > 0;
  $("#empty").textContent = state.q || state.category || state.tags.length ? "No matches." : "Nothing here yet — add your first screenshot above.";
  $("#grid").innerHTML = items.map((item) => {
    const img = imageFor(item);
    const wide = WIDE_CATEGORIES.has(item.category) || !item.category;
    const badge = item.status === "error" ? "⚠ Failed" : CATEGORY_LABELS[item.category] || (item.status === "queued" ? "⏳ Queued" : item.status === "processing" ? "…" : "📌");
    return `
      <article class="card ${wide ? "wide" : ""} ${esc(item.status)}" data-id="${esc(item.id)}">
        <div class="thumb" ${img ? `style="background-image:url('${esc(img)}')"` : ""}><span class="badge">${esc(badge)}</span>${
          item.needs_review ? `<span class="badge warn" title="${esc(item.confidence_reason || "")}">Not sure? ${esc(item.confidence)}%</span>` : ""}</div>
        <div class="body">
          <div class="title">${esc(cardTitle(item))}</div>
          ${item.subtitle ? `<div class="sub">${esc(item.subtitle)}</div>` : ""}
          <div class="facts">${cardFacts(item).map((f) => `<span>${esc(f)}</span>`).join("")}</div>
        </div>
      </article>`;
  }).join("");

  const active = [];
  if (state.review) active.push(`<span class="chip active">⚠ Needs review<button data-review>×</button></span>`);
  if (state.category) active.push(`<span class="chip active">${esc(CATEGORY_LABELS[state.category] || state.category)}<button data-clear-category>×</button></span>`);
  state.tags.forEach((t) => active.push(`<span class="chip active">#${esc(t)}<button data-clear-tag="${esc(t)}">×</button></span>`));
  $("#active-filters").innerHTML = active.join("");
}

function renderSyncStatus() {
  const pending = state.ops.length;
  const el = $("#sync-status");
  const labels = {
    syncing: ["⟳", "Syncing…"],
    offline: ["⚡︎", pending ? `Offline · ${pending} waiting` : "Offline"],
    auth: ["🔒", "Token needed"],
    error: ["⚠", "Sync error"],
    idle: pending ? ["⟳", `${pending} waiting`] : ["✓", "Synced"],
  };
  const [icon, text] = labels[state.sync] || labels.idle;
  el.className = `sync-status ${state.sync}`;
  el.innerHTML = `<span aria-hidden="true">${icon}</span><span class="sync-text">${esc(text)}</span>`;

  const banner = $("#banner");
  let html = "";
  if (state.sync === "auth") {
    html = `This Keeper server needs an access token. <button class="btn" data-action="token">Enter token</button>`;
  } else if (state.sync === "offline" && pending) {
    html = `You're offline. ${pending} change${pending > 1 ? "s" : ""} will sync automatically when the server is reachable.`;
  } else if (state.sync === "error") {
    html = `Sync failed: ${esc(state.syncError)} <button class="btn" data-action="sync">Retry</button>`;
  } else if (showInstallHint()) {
    html = `Install Keeper: tap <b>Share</b> <span aria-hidden="true">⎋</span> then <b>Add to Home Screen</b>. <button class="btn" data-action="dismiss-install">Dismiss</button>`;
  }
  banner.innerHTML = html;
  banner.hidden = !html;
}

function isStandalone() {
  return window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
}
function showInstallHint() {
  const iOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  try { if (localStorage.getItem("keeper.installHintDismissed")) return false; } catch {}
  return iOS && !isStandalone();
}

function scoresHtml(m) {
  const scores = [
    ["IMDb", m.imdb_rating, m.imdb_votes ? `${m.imdb_votes} votes` : ""],
    ["Rotten Tomatoes", m.rotten_tomatoes],
    ["Metacritic", m.metacritic],
    ["TMDB", m.tmdb_rating],
    ["GitHub stars", m.stars != null ? Number(m.stars).toLocaleString() : null],
    ["Rating", m.rating, m.rating_count ? `${m.rating_count} ratings` : ""],
  ].filter(([, v]) => v != null && v !== "");
  if (!scores.length) return "";
  return `<div class="scores">${scores.map(([label, v, extra]) =>
    `<div class="score"><b>${esc(v)}</b><small>${esc(label)}${extra ? `<br>${esc(extra)}` : ""}</small></div>`).join("")}</div>`;
}

function statusHtml(item) {
  if (item.status === "error") return `<div class="error-box">Analysis failed: ${esc(item.error)}</div>`;
  if (item.pending_upload) return `<div class="meta-line">⏳ Saved on this device. It will be uploaded and identified when the server is reachable.</div>`;
  if (item.status === "processing") return `<div class="meta-line">Analyzing… this usually takes 20–60 seconds.</div>`;
  if (hasPendingOps(item.id)) return `<div class="meta-line">⟳ Changes waiting to sync</div>`;
  return "";
}

function confidenceHtml(item) {
  if (item.pending_upload || item.status !== "ready") return "";
  const c = item.confidence;
  const level = item.corrected ? "ok" : c == null ? "unknown" : c >= 85 ? "ok" : c >= 60 ? "mid" : "low";
  const label = item.corrected ? "Corrected by you" : c == null ? "Confidence unknown" : `${c}% sure`;
  const alts = (item.alternatives || []).filter((a) => a && a.title);
  return `
    <div class="confidence ${level}">
      <div class="confidence-head">
        <span class="confidence-label">${esc(label)}</span>
        ${c != null && !item.corrected ? `<span class="meter"><span style="width:${Math.max(4, Math.min(100, c))}%"></span></span>` : ""}
        <button class="btn small" data-action="fix">${item.needs_review ? "Is this wrong? Fix it" : "Wrong? Fix it"}</button>
      </div>
      ${item.confidence_reason && !item.corrected ? `<div class="meta-line">${esc(item.confidence_reason)}</div>` : ""}
      ${alts.length && !item.corrected ? `<div class="alternatives"><span class="meta-line">Did you mean:</span>
        ${alts.map((a, i) => `<button class="chip" data-alt="${i}" title="${esc(a.why || "")}">${esc(a.title)}${a.year ? ` (${esc(a.year)})` : ""} · ${esc((CATEGORY_LABELS[a.category] || a.category || "").replace(/^\S+ /, ""))}</button>`).join("")}
      </div>` : ""}
    </div>`;
}

function correctionFormHtml(item) {
  return `
    <form class="correct-form" id="correct-form">
      <h4>What is it really?</h4>
      <div class="form-grid">
        <label>Title <input name="title" value="${esc(item.title || "")}" autocomplete="off"></label>
        <label>Type <select name="category">
          ${Object.entries(CATEGORY_LABELS).map(([k, label]) => `<option value="${k}" ${k === item.category ? "selected" : ""}>${label}</option>`).join("")}
        </select></label>
        <label>Year <input name="year" inputmode="numeric" pattern="[0-9]{4}" value="${esc((item.metadata || {}).year || "")}"></label>
        <label>Link <input name="canonical_url" type="url" placeholder="IMDb, GitHub, recipe page…" value="${esc(item.canonical_url || "")}"></label>
      </div>
      <label>Or describe it <textarea name="hint" placeholder="e.g. “It's the 2019 remake, not the original” — Claude will look again"></textarea></label>
      <div class="actions">
        <button class="btn primary" type="submit">Save correction</button>
        <button class="btn" type="button" data-action="cancel-fix">Cancel</button>
      </div>
    </form>`;
}

function renderDetail(item) {
  const m = item.metadata || {};
  const poster = safeUrl(item.image_url);
  const shot = screenshotFor(item);
  const canonical = safeUrl(item.canonical_url);
  const facts = Object.entries(m).filter(([k, v]) => {
    if (HIDDEN_META.has(k) || v == null || v === "") return false;
    if (Array.isArray(v)) return v.length > 0 && v.every((x) => typeof x !== "object");
    return typeof v !== "object";
  });
  const links = (item.links || []).filter((l) => safeUrl(l.url));
  const metaLine = [
    CATEGORY_LABELS[item.category] || item.category,
    m.year,
    item.source_platform && `saved from ${item.source_platform}`,
    item.created_at && new Date(item.created_at).toLocaleDateString(),
  ].filter(Boolean).map(esc).join(" · ");

  const dlg = $("#detail");
  dlg.dataset.id = item.id;
  if (fixing && fixing !== item.id) fixing = null;
  if (fixing && document.activeElement?.closest?.("#correct-form")) return;  // don't wipe a form being typed in
  const noteFocused = document.activeElement?.id === "note-edit";
  const noteValue = noteFocused ? document.activeElement.value : item.note || "";
  dlg.innerHTML = `
    <div class="detail" data-id="${esc(item.id)}" tabindex="-1" autofocus>
      <div class="media">
        ${poster ? `<a href="${esc(canonical || poster)}" target="_blank" rel="noopener"><img src="${esc(poster)}" alt="" referrerpolicy="no-referrer"></a>` : ""}
        ${shot ? `<div><div class="shot-label">Your screenshot</div><a href="${esc(shot)}" target="_blank"><img src="${esc(shot)}" alt="Screenshot"></a></div>` : ""}
      </div>
      <div class="info">
        <button class="btn close" data-action="close" aria-label="Close">✕</button>
        <div>
          <h2>${esc(cardTitle(item))}</h2>
          <div class="meta-line">${metaLine}</div>
          ${item.subtitle ? `<div>${esc(item.subtitle)}</div>` : ""}
        </div>
        ${statusHtml(item)}
        ${fixing === item.id ? correctionFormHtml(item) : confidenceHtml(item)}
        ${scoresHtml(m)}
        ${item.summary ? `<p style="margin:0">${esc(item.summary)}</p>` : ""}
        ${m.description && m.description !== item.summary && m.description !== item.subtitle ? `<p class="meta-line" style="margin:0">${esc(m.description)}</p>` : ""}
        ${(canonical || links.length) ? `<div class="links">
          ${canonical ? `<a class="btn primary" href="${esc(canonical)}" target="_blank" rel="noopener">Open source ↗</a>` : ""}
          ${links.map((l) => `<a class="btn" href="${esc(safeUrl(l.url))}" target="_blank" rel="noopener">${esc(l.label)}</a>`).join("")}
        </div>` : ""}
        ${facts.length ? `<dl class="facts-table">${facts.map(([k, v]) => `<dt>${esc(humanize(k))}</dt><dd>${
          typeof v === "string" && /^https?:/.test(v) && safeUrl(v) ? `<a href="${esc(v)}" target="_blank" rel="noopener">${esc(v)}</a>` : esc(fmtValue(v))
        }</dd>`).join("")}</dl>` : ""}
        ${m.ingredients?.length ? `<h4>Ingredients</h4><ul>${m.ingredients.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>` : ""}
        ${m.instructions?.length ? `<h4>Instructions</h4><ol>${m.instructions.map((i) => `<li>${esc(i)}</li>`).join("")}</ol>` : ""}
        <h4>Tags</h4>
        <div class="tag-editor">
          ${(item.tags || []).map((t) => `<span class="chip">#${esc(t)}<button data-remove-tag="${esc(t)}" aria-label="Remove tag">×</button></span>`).join("")}
          <input id="new-tag" placeholder="add tag ↵" enterkeyhint="done" autocapitalize="off">
        </div>
        <h4>Note</h4>
        <textarea id="note-edit" placeholder="Why did you save this?">${esc(noteValue)}</textarea>
        ${m.screenshot_text ? `<details><summary>Text found in screenshot</summary><pre>${esc(m.screenshot_text)}</pre></details>` : ""}
        <div class="actions">
          <select id="category-edit" class="btn">
            ${Object.entries(CATEGORY_LABELS).map(([k, label]) => `<option value="${k}" ${k === item.category ? "selected" : ""}>${label}</option>`).join("")}
          </select>
          ${item.pending_upload ? "" : `<button class="btn" data-action="reanalyze">↻ Re-analyze</button>`}
          <button class="btn danger" data-action="delete">Delete</button>
          ${m.sources ? `<span class="meta-line" style="margin-left:auto">via ${esc(m.sources.join(", "))}</span>` : ""}
        </div>
      </div>
    </div>`;
  if (noteFocused) { const n = $("#note-edit"); n.focus(); n.setSelectionRange(n.value.length, n.value.length); }
  if (!dlg.open) dlg.showModal();
}

// ---- events ----------------------------------------------------------------

$("#file-input").addEventListener("change", (e) => {
  const note = $("#note").value.trim();
  $("#note").value = "";
  addScreenshots(e.target.files, note);
  e.target.value = "";
});

document.addEventListener("paste", (e) => {
  if (e.target.matches("input, textarea")) return;
  const files = [...(e.clipboardData?.files || [])];
  if (files.length) { e.preventDefault(); addScreenshots(files, $("#note").value.trim()); $("#note").value = ""; }
});

const dz = $("#dropzone");
["dragenter", "dragover"].forEach((ev) => document.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => document.addEventListener(ev, (e) => { e.preventDefault(); if (ev === "drop" || !e.relatedTarget) dz.classList.remove("over"); }));
document.addEventListener("drop", (e) => {
  if (e.dataTransfer?.files?.length) { addScreenshots(e.dataTransfer.files, $("#note").value.trim()); $("#note").value = ""; }
});

let searchTimer;
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.q = e.target.value.trim(); renderGrid(); }, 120);
});

$("#sync-status").addEventListener("click", () => (state.sync === "auth" ? askToken() : requestSync()));

async function askToken() {
  const token = prompt("Access token for this Keeper server (KEEPER_API_TOKEN):");
  if (token === null) return;
  await setToken(token);
  requestSync();
}

document.addEventListener("click", async (e) => {
  const t = e.target.closest("[data-review],[data-category],[data-tag],[data-clear-category],[data-clear-tag],.card,[data-action],[data-remove-tag],[data-alt]");
  if (!t) return;
  if (t.dataset.review !== undefined) {
    state.review = !state.review;
  } else if (t.dataset.category !== undefined) {
    state.category = state.category === t.dataset.category ? null : t.dataset.category;
  } else if (t.dataset.tag !== undefined) {
    state.tags = state.tags.includes(t.dataset.tag) ? state.tags.filter((x) => x !== t.dataset.tag) : [...state.tags, t.dataset.tag];
  } else if (t.dataset.clearCategory !== undefined) {
    state.category = null;
  } else if (t.dataset.clearTag !== undefined) {
    state.tags = state.tags.filter((x) => x !== t.dataset.clearTag);
  } else if (t.classList.contains("card")) {
    const item = state.items.get(t.dataset.id);
    if (item) renderDetail(item);
    return;
  } else {
    const id = t.closest(".detail")?.dataset.id;
    const item = id && state.items.get(id);
    if (t.dataset.alt !== undefined && item) {
      const a = (item.alternatives || [])[Number(t.dataset.alt)];
      if (a) return correctItem(id, { title: a.title, category: a.category, year: a.year, canonical_url: a.canonical_url });
    }
    if (t.dataset.removeTag !== undefined && item) {
      return editItem(id, { tags: (item.tags || []).filter((x) => x !== t.dataset.removeTag) });
    }
    switch (t.dataset.action) {
      case "close": return $("#detail").close();
      case "fix": fixing = id; renderDetail(item); return $("#correct-form input[name=title]")?.focus();
      case "cancel-fix": fixing = null; return renderDetail(item);
      case "reanalyze": return reanalyzeItem(id);
      case "delete":
        if (!confirm("Delete this item?")) return;
        $("#detail").close();
        return deleteItem(id);
      case "token": return askToken();
      case "sync": return requestSync();
      case "dismiss-install":
        try { localStorage.setItem("keeper.installHintDismissed", "1"); } catch {}
        return renderSyncStatus();
    }
    return;
  }
  render();
});

$("#detail").addEventListener("keydown", (e) => {
  if (e.target.id !== "new-tag" || e.key !== "Enter") return;
  e.preventDefault();
  const value = e.target.value.trim();
  const id = e.target.closest(".detail").dataset.id;
  const item = state.items.get(id);
  if (value && item) editItem(id, { tags: [...(item.tags || []), ...value.split(",")] });
});

$("#detail").addEventListener("change", (e) => {
  const id = e.target.closest(".detail")?.dataset.id;
  const item = id && state.items.get(id);
  if (!item) return;
  if (e.target.id === "note-edit" && e.target.value !== (item.note || "")) editItem(id, { note: e.target.value });
  if (e.target.id === "category-edit") editItem(id, { category: e.target.value });
});

$("#detail").addEventListener("click", (e) => { if (e.target === e.currentTarget) e.currentTarget.close(); });
$("#detail").addEventListener("close", () => { fixing = null; });

$("#detail").addEventListener("submit", (e) => {
  if (e.target.id !== "correct-form") return;
  e.preventDefault();
  const id = e.target.closest(".detail").dataset.id;
  const item = state.items.get(id);
  const f = Object.fromEntries(new FormData(e.target));
  // Send only what the user changed (plus any description).
  const correction = {};
  if (f.title.trim() && f.title.trim() !== (item.title || "")) correction.title = f.title.trim();
  if (f.category && f.category !== item.category) correction.category = f.category;
  if (f.year && Number(f.year) !== Number((item.metadata || {}).year)) correction.year = Number(f.year);
  if (f.canonical_url.trim() && f.canonical_url.trim() !== (item.canonical_url || "")) correction.canonical_url = f.canonical_url.trim();
  if (f.hint.trim()) correction.hint = f.hint.trim();
  fixing = null;
  correctItem(id, correction);
});

// Sync triggers. iOS has no Background Sync API, so sync whenever the app is in view.
window.addEventListener("online", requestSync);
window.addEventListener("offline", () => setSyncState("offline"));
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") requestSync(); });
setInterval(() => { if (document.visibilityState === "visible" && (state.ops.length || state.sync !== "idle")) requestSync(); }, 30000);

// ---- boot ------------------------------------------------------------------

async function boot() {
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch((e) => console.warn("Service worker not registered:", e));
  }
  // Tokens saved by earlier versions live only in the cookie; carry them over.
  const cookieToken = document.cookie.split("; ").find((c) => c.startsWith("keeper_token="));
  if (cookieToken && !(await db.get("kv", "token"))) await setToken(decodeURIComponent(cookieToken.split("=")[1]));

  await loadLocal();
  render();
  if (new URLSearchParams(location.search).has("shared")) history.replaceState(null, "", "/");
  requestSync();
}

boot().catch((e) => { console.error(e); toast(`Couldn't open the offline library: ${e.message}`); });
