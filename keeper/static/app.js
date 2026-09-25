"use strict";

const CATEGORY_LABELS = {
  movie: "🎬 Movie", tv_show: "📺 TV show", github_repo: "💻 GitHub repo", recipe: "🍳 Recipe",
  book: "📚 Book", music: "🎵 Music", podcast: "🎙️ Podcast", video: "▶️ Video", article: "📰 Article",
  product: "🛍️ Product", place: "📍 Place", event: "📅 Event", app: "📱 App", course: "🎓 Course", other: "📌 Other",
};
const WIDE_CATEGORIES = new Set(["github_repo", "article", "video", "product", "app", "other", "place", "event", "course"]);

// Metadata keys shown elsewhere in the detail view (or not useful to show).
const HIDDEN_META = new Set([
  "screenshot_text", "sources", "confidence", "ingredients", "instructions", "imdb_rating", "rotten_tomatoes",
  "metacritic", "tmdb_rating", "stars", "rating", "description", "post_url", "imdb_votes", "tmdb_id",
  "page_description", "page_title", "github_full_name", "year",
]);

const state = { q: "", category: null, tags: [], items: [] };
let pollTimer = null;

const $ = (sel) => document.querySelector(sel);

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
  if (typeof v === "number") return v.toLocaleString();
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

async function api(path, opts = {}, retried = false) {
  const res = await fetch(path, opts);
  if (res.status === 401 && !retried) {
    // Self-hosted server with KEEPER_API_TOKEN set: ask once, keep it in a cookie so images load too.
    const token = prompt("This Keeper server needs an access token:");
    if (token) {
      document.cookie = `keeper_token=${encodeURIComponent(token.trim())}; path=/; max-age=31536000; SameSite=Strict`;
      return api(path, opts, true);
    }
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.status === 204 ? null : res.json();
}

// ---- data loading ----------------------------------------------------------

async function loadItems() {
  const params = new URLSearchParams();
  if (state.q) params.set("q", state.q);
  if (state.category) params.set("category", state.category);
  state.tags.forEach((t) => params.append("tag", t));
  state.items = await api(`/api/items?${params}`);
  renderGrid();
  schedulePoll();
}

async function loadFilters() {
  const [cats, tags] = await Promise.all([api("/api/categories"), api("/api/tags")]);
  $("#categories").innerHTML = cats.counts.map((c) => `
    <button class="chip ${state.category === c.category ? "active" : ""}" data-category="${esc(c.category)}">
      ${esc(CATEGORY_LABELS[c.category] || c.category)} <span class="count">${c.count}</span>
    </button>`).join("") || `<span class="count">—</span>`;
  $("#tags").innerHTML = tags.slice(0, 40).map((t) => `
    <button class="chip ${state.tags.includes(t.tag) ? "active" : ""}" data-tag="${esc(t.tag)}">
      #${esc(t.tag)} <span class="count">${t.count}</span>
    </button>`).join("") || `<span class="count">—</span>`;
}

function refresh() {
  return Promise.all([loadItems(), loadFilters()]).catch((e) => toast(e.message));
}

function schedulePoll() {
  clearTimeout(pollTimer);
  if (state.items.some((i) => i.status === "processing")) {
    pollTimer = setTimeout(refresh, 2500);
  }
}

// ---- rendering -------------------------------------------------------------

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

function renderGrid() {
  const grid = $("#grid");
  $("#empty").hidden = state.items.length > 0;
  $("#empty").textContent = state.q || state.category || state.tags.length ? "No matches." : "Nothing here yet — add your first screenshot above.";
  grid.innerHTML = state.items.map((item) => {
    const img = safeUrl(item.image_url) || `/media/${encodeURIComponent(item.image_file)}`;
    const wide = WIDE_CATEGORIES.has(item.category) || !item.category;
    const badge = item.status === "error" ? "⚠ Failed" : CATEGORY_LABELS[item.category] || (item.status === "processing" ? "…" : "📌");
    return `
      <article class="card ${wide ? "wide" : ""} ${item.status}" data-id="${esc(item.id)}">
        <div class="thumb" style="background-image:url('${esc(img)}')"><span class="badge">${esc(badge)}</span></div>
        <div class="body">
          <div class="title">${esc(item.title || (item.status === "processing" ? "Analyzing screenshot…" : item.status === "error" ? "Couldn't identify — open to retry" : "Untitled"))}</div>
          ${item.subtitle ? `<div class="sub">${esc(item.subtitle)}</div>` : ""}
          <div class="facts">${cardFacts(item).map((f) => `<span>${esc(f)}</span>`).join("")}</div>
        </div>
      </article>`;
  }).join("");

  const active = [];
  if (state.category) active.push(`<span class="chip active">${esc(CATEGORY_LABELS[state.category] || state.category)}<button data-clear-category>×</button></span>`);
  state.tags.forEach((t) => active.push(`<span class="chip active">#${esc(t)}<button data-clear-tag="${esc(t)}">×</button></span>`));
  $("#active-filters").innerHTML = active.join("");
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

function renderDetail(item) {
  const m = item.metadata || {};
  const poster = safeUrl(item.image_url);
  const shot = `/media/${encodeURIComponent(item.image_file)}`;
  const canonical = safeUrl(item.canonical_url);
  const facts = Object.entries(m).filter(([k, v]) => !HIDDEN_META.has(k) && v != null && v !== "" && !(Array.isArray(v) && !v.length));
  const links = (item.links || []).filter((l) => safeUrl(l.url));
  const metaLine = [
    CATEGORY_LABELS[item.category] || item.category,
    m.year,
    item.source_platform && `saved from ${item.source_platform}`,
    new Date(item.created_at).toLocaleDateString(),
  ].filter(Boolean).map(esc).join(" · ");

  const dlg = $("#detail");
  dlg.innerHTML = `
    <div class="detail" data-id="${esc(item.id)}">
      <div class="media">
        ${poster ? `<a href="${esc(canonical || poster)}" target="_blank" rel="noopener"><img src="${esc(poster)}" alt="" referrerpolicy="no-referrer"></a>` : ""}
        <div><div class="shot-label">Your screenshot</div><a href="${shot}" target="_blank"><img src="${shot}" alt="Screenshot"></a></div>
      </div>
      <div class="info">
        <button class="btn close" data-action="close" aria-label="Close">✕</button>
        <div>
          <h2>${esc(item.title || "Untitled")}</h2>
          <div class="meta-line">${metaLine}</div>
          ${item.subtitle ? `<div>${esc(item.subtitle)}</div>` : ""}
        </div>
        ${item.status === "error" ? `<div class="error-box">Analysis failed: ${esc(item.error)}</div>` : ""}
        ${item.status === "processing" ? `<div class="meta-line">Analyzing… this usually takes 20–60 seconds.</div>` : ""}
        ${scoresHtml(m)}
        ${item.summary ? `<p style="margin:0">${esc(item.summary)}</p>` : ""}
        ${m.description && m.description !== item.summary && m.description !== item.subtitle ? `<p class="meta-line" style="margin:0">${esc(m.description)}</p>` : ""}
        ${(canonical || links.length) ? `<div class="links">
          ${canonical ? `<a class="btn primary" href="${esc(canonical)}" target="_blank" rel="noopener">Open source ↗</a>` : ""}
          ${links.map((l) => `<a class="btn" href="${esc(safeUrl(l.url))}" target="_blank" rel="noopener">${esc(l.label)}</a>`).join("")}
        </div>` : ""}
        ${facts.length ? `<dl class="facts-table">${facts.map(([k, v]) => `<dt>${esc(humanize(k))}</dt><dd>${
          typeof v === "string" && safeUrl(v) && /^https?:/.test(v) ? `<a href="${esc(v)}" target="_blank" rel="noopener">${esc(v)}</a>` : esc(fmtValue(v))
        }</dd>`).join("")}</dl>` : ""}
        ${m.ingredients?.length ? `<h4>Ingredients</h4><ul>${m.ingredients.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>` : ""}
        ${m.instructions?.length ? `<h4>Instructions</h4><ol>${m.instructions.map((i) => `<li>${esc(i)}</li>`).join("")}</ol>` : ""}
        <h4>Tags</h4>
        <div class="tag-editor">
          ${item.tags.map((t) => `<span class="chip">#${esc(t)}<button data-remove-tag="${esc(t)}" aria-label="Remove tag">×</button></span>`).join("")}
          <input id="new-tag" placeholder="add tag ↵">
        </div>
        <h4>Note</h4>
        <textarea id="note-edit" placeholder="Why did you save this?">${esc(item.note || "")}</textarea>
        ${m.screenshot_text ? `<details><summary>Text found in screenshot</summary><pre>${esc(m.screenshot_text)}</pre></details>` : ""}
        <div class="actions">
          <select id="category-edit" class="btn">
            ${Object.entries(CATEGORY_LABELS).map(([k, label]) => `<option value="${k}" ${k === item.category ? "selected" : ""}>${label}</option>`).join("")}
          </select>
          <button class="btn" data-action="reanalyze">↻ Re-analyze</button>
          <button class="btn danger" data-action="delete">Delete</button>
          ${m.sources ? `<span class="meta-line" style="margin-left:auto">via ${esc(m.sources.join(", "))}</span>` : ""}
        </div>
      </div>
    </div>`;
  if (!dlg.open) dlg.showModal();
}

async function openDetail(id) {
  try { renderDetail(await api(`/api/items/${id}`)); } catch (e) { toast(e.message); }
}

async function patchItem(id, body) {
  const item = await api(`/api/items/${id}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  renderDetail(item);
  refresh();
}

// ---- uploads ---------------------------------------------------------------

async function uploadFiles(files) {
  const images = [...files].filter((f) => f.type.startsWith("image/"));
  if (!images.length) return toast("Only images can be added.");
  const note = $("#note").value.trim();
  for (const file of images) {
    const fd = new FormData();
    fd.append("file", file, file.name || "screenshot.png");
    if (note) fd.append("note", note);
    try { await api("/api/items", { method: "POST", body: fd }); } catch (e) { toast(`${file.name}: ${e.message}`); }
  }
  $("#note").value = "";
  toast(`Added ${images.length} screenshot${images.length > 1 ? "s" : ""} — analyzing…`);
  refresh();
}

// ---- events ----------------------------------------------------------------

$("#file-input").addEventListener("change", (e) => { uploadFiles(e.target.files); e.target.value = ""; });

document.addEventListener("paste", (e) => {
  if (e.target.matches("input, textarea")) return;
  const files = [...(e.clipboardData?.files || [])];
  if (files.length) { e.preventDefault(); uploadFiles(files); }
});

const dz = $("#dropzone");
["dragenter", "dragover"].forEach((ev) => document.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => document.addEventListener(ev, (e) => { e.preventDefault(); if (ev === "drop" || !e.relatedTarget) dz.classList.remove("over"); }));
document.addEventListener("drop", (e) => { if (e.dataTransfer?.files?.length) uploadFiles(e.dataTransfer.files); });

let searchTimer;
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.q = e.target.value.trim(); loadItems().catch((err) => toast(err.message)); }, 200);
});

document.addEventListener("click", async (e) => {
  const t = e.target.closest("[data-category],[data-tag],[data-clear-category],[data-clear-tag],.card,[data-action],[data-remove-tag]");
  if (!t) return;
  if (t.dataset.category !== undefined) {
    state.category = state.category === t.dataset.category ? null : t.dataset.category;
  } else if (t.dataset.tag !== undefined) {
    state.tags = state.tags.includes(t.dataset.tag) ? state.tags.filter((x) => x !== t.dataset.tag) : [...state.tags, t.dataset.tag];
  } else if (t.dataset.clearCategory !== undefined) {
    state.category = null;
  } else if (t.dataset.clearTag !== undefined) {
    state.tags = state.tags.filter((x) => x !== t.dataset.clearTag);
  } else if (t.classList.contains("card")) {
    return openDetail(t.dataset.id);
  } else {
    const id = t.closest(".detail")?.dataset.id;
    try {
      if (t.dataset.removeTag !== undefined) {
        const item = await api(`/api/items/${id}`);
        return patchItem(id, { tags: item.tags.filter((x) => x !== t.dataset.removeTag) });
      }
      switch (t.dataset.action) {
        case "close": return $("#detail").close();
        case "reanalyze":
          renderDetail(await api(`/api/items/${id}/reanalyze`, { method: "POST" }));
          return refresh();
        case "delete":
          if (!confirm("Delete this item?")) return;
          await api(`/api/items/${id}`, { method: "DELETE" });
          $("#detail").close();
          return refresh();
      }
    } catch (err) { return toast(err.message); }
    return;
  }
  refresh();
});

$("#detail").addEventListener("keydown", async (e) => {
  if (e.target.id !== "new-tag" || e.key !== "Enter") return;
  const value = e.target.value.trim();
  if (!value) return;
  const id = e.target.closest(".detail").dataset.id;
  const item = await api(`/api/items/${id}`);
  patchItem(id, { tags: [...item.tags, ...value.split(",")] }).catch((err) => toast(err.message));
});

$("#detail").addEventListener("change", (e) => {
  const id = e.target.closest(".detail")?.dataset.id;
  if (e.target.id === "note-edit") patchItem(id, { note: e.target.value }).catch((err) => toast(err.message));
  if (e.target.id === "category-edit") patchItem(id, { category: e.target.value }).catch((err) => toast(err.message));
});

$("#detail").addEventListener("click", (e) => { if (e.target === e.currentTarget) e.currentTarget.close(); });

refresh();
