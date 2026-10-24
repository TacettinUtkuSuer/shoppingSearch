"use strict";

const $ = (sel) => document.querySelector(sel);
const POLL_MS = 3000;

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail ? JSON.stringify(data.detail) : `HTTP ${res.status}`);
  return data;
}

function store(key, value) {
  try { value === undefined ? localStorage.removeItem(key) : localStorage.setItem(key, value); } catch (_) {}
}
function load(key) {
  try { return localStorage.getItem(key); } catch (_) { return null; }
}

// ---------------------------------------------------------------- tabs
function showView(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === name));
  $("#view-clarify").classList.toggle("hidden", name !== "clarify");
  $("#view-results").classList.toggle("hidden", name !== "results");
  store("view", name);
  if (name === "results") refreshTasks();
}
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => showView(t.dataset.view)));

// ------------------------------------------------- view 1: clarification
// Only the original description and the Q/A pairs are sent; no chat history is accumulated.
const convo = { description: null, clarifications: [], pendingQuestion: null, scope: null };

function addMsg(text, kind) {
  const div = document.createElement("div");
  div.className = `msg ${kind}`;
  div.textContent = text;
  $("#chat").appendChild(div);
  $("#chat").scrollTop = $("#chat").scrollHeight;
}

function renderChips(id, items) {
  const box = $(`#${id}`);
  box.replaceChildren();
  items.forEach((item, i) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.append(item);
    const x = document.createElement("button");
    x.type = "button";
    x.textContent = "×";
    x.title = "Remove";
    x.addEventListener("click", () => { items.splice(i, 1); renderChips(id, items); });
    chip.append(x);
    box.append(chip);
  });
}

function showScope(scope) {
  convo.scope = { ...scope, search_terms: [...scope.search_terms], negative_keywords: [...scope.negative_keywords] };
  $("#scope").classList.remove("hidden");
  $("#scope-category").textContent = scope.detected_category || "uncategorized";
  renderChips("terms", convo.scope.search_terms);
  convo.scope.requirements = [...(scope.requirements || [])];
  renderChips("requirements", convo.scope.requirements);
  renderChips("negatives", convo.scope.negative_keywords);
}

document.querySelectorAll(".chip-add").forEach((form) => form.addEventListener("submit", (e) => {
  e.preventDefault();
  const input = form.querySelector("input");
  const value = input.value.trim().replace(/^-+/, "");
  if (!value || !convo.scope) return;
  const key = { terms: "search_terms", negatives: "negative_keywords", requirements: "requirements" }[form.dataset.target];
  if (!convo.scope[key].includes(value)) convo.scope[key].push(value);
  renderChips(form.dataset.target, convo.scope[key]);
  input.value = "";
}));

$("#chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("#chat-input").value.trim();
  if (!text) return;
  $("#chat-input").value = "";
  addMsg(text, "user");
  if (convo.pendingQuestion) {
    convo.clarifications.push({ question: convo.pendingQuestion, answer: text });
    convo.pendingQuestion = null;
  } else {
    convo.description = text;
    convo.clarifications = [];
    $("#scope").classList.add("hidden");
  }
  $("#chat-send").disabled = true;
  addMsg("Thinking…", "agent");
  const thinking = $("#chat").lastChild;
  try {
    const r = await api("/api/resolve", { method: "POST", body: { description: convo.description, clarifications: convo.clarifications } });
    thinking.remove();
    if (r.needs_clarification) {
      convo.pendingQuestion = r.clarification_question;
      addMsg(r.clarification_question, "agent");
    } else {
      addMsg(`Scope locked. Search terms:\n- ${r.search_terms.join("\n- ")}\nMust have: ${(r.requirements || []).join(", ") || "none"}\nExcluded: ${r.negative_keywords.join(", ") || "none"}\nEdit below if needed, then launch.`, "agent");
      showScope(r);
    }
  } catch (err) {
    thinking.remove();
    addMsg(`Resolver error: ${err.message}`, "error");
  } finally {
    $("#chat-send").disabled = false;
    $("#chat-input").focus();
  }
});

$("#chat-reset").addEventListener("click", () => {
  Object.assign(convo, { description: null, clarifications: [], pendingQuestion: null, scope: null });
  $("#chat").replaceChildren();
  addMsg("Describe the product you want, even roughly.", "agent");
  $("#scope").classList.add("hidden");
});

$("#launch").addEventListener("click", async () => {
  if (!convo.scope || !convo.scope.search_terms.length) return addMsg("Add at least one search term.", "error");
  $("#launch").disabled = true;
  try {
    const task = await api("/api/tasks", {
      method: "POST",
      body: {
        description: convo.description || "",
        search_terms: convo.scope.search_terms,
        negative_keywords: convo.scope.negative_keywords,
        requirements: convo.scope.requirements,
        detected_category: convo.scope.detected_category,
        max_duration_seconds: Math.max(1, Number($("#max-minutes").value) || 60) * 60,
      },
    });
    state.taskId = task.task_id;
    store("taskId", task.task_id);
    showView("results");
  } catch (err) {
    addMsg(`Launch failed: ${err.message}`, "error");
  } finally {
    $("#launch").disabled = false;
  }
});

// ---------------------------------------------------- view 2: results
const state = { taskId: load("taskId"), products: [], attrFilters: {}, timer: null };

function fmtDuration(s) {
  s = Math.round(s || 0);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}h ${m}m` : `${m}m ${sec}s`;
}
function fmtMoney(v, cur) {
  return v == null ? "?" : `${Number(v).toFixed(2)}${cur ? " " + cur : ""}`;
}

async function refreshTasks() {
  const tasks = await api("/api/tasks").catch(() => []);
  const sel = $("#task-select");
  sel.replaceChildren();
  if (!tasks.length) {
    sel.append(new Option("No searches yet", ""));
    state.taskId = null;
  }
  tasks.forEach((t) => {
    const label = `${t.status} · ${t.search_terms[0] || t.description || t.task_id} · ${t.product_count} products`;
    sel.append(new Option(label, t.task_id));
  });
  if (!tasks.some((t) => t.task_id === state.taskId)) state.taskId = tasks[0] ? tasks[0].task_id : null;
  sel.value = state.taskId || "";
  await refreshTask();
}

$("#task-select").addEventListener("change", (e) => {
  state.taskId = e.target.value;
  store("taskId", state.taskId);
  state.attrFilters = {};
  refreshTask();
});

async function refreshTask() {
  clearTimeout(state.timer);
  if (!state.taskId) { renderProducts(); return; }
  try {
    const [task, products] = await Promise.all([
      api(`/api/tasks/${state.taskId}`),
      api(`/api/tasks/${state.taskId}/products?sort=${encodeURIComponent($("#sort").value)}&include_rejected=${$("#show-rejected").checked}`),
    ]);
    renderTask(task);
    state.products = products;
    renderAttrFilters();
    renderProducts();
    if (["RUNNING", "PENDING", "PAUSED"].includes(task.status)) {
      state.timer = setTimeout(refreshTask, POLL_MS);
    }
  } catch (err) {
    $("#task-message").textContent = `Error: ${err.message}`;
    state.timer = setTimeout(refreshTask, POLL_MS * 2);
  }
}

function renderTask(t) {
  const badge = $("#status-badge");
  badge.textContent = t.status;
  badge.className = `badge ${t.status}`;
  $("#task-meta").textContent =
    `${t.product_count} matching · ${t.rejected_count || 0} non-matching · ${t.visited_count} visited · ${fmtDuration(t.elapsed_seconds)} of ${fmtDuration(t.max_duration_seconds)}`;
  $("#task-message").textContent =
    `${t.message || ""}  |  Excluding: ${t.negative_keywords.join(", ") || "none"}`;
  const active = ["RUNNING", "PENDING"].includes(t.status);
  $("#btn-pause").disabled = !active;
  $("#btn-resume").disabled = t.status !== "PAUSED";
  $("#btn-stop").disabled = !(active || t.status === "PAUSED");
}

async function control(action) {
  if (!state.taskId) return;
  if (action === "stop" && !confirm("Stop this search? Collected results are kept.")) return;
  try { await api(`/api/tasks/${state.taskId}/${action}`, { method: "POST" }); }
  catch (err) { alert(err.message); }
  refreshTasks();
}
$("#btn-pause").addEventListener("click", () => control("pause"));
$("#btn-resume").addEventListener("click", () => control("resume"));
$("#btn-stop").addEventListener("click", () => control("stop"));

function renderAttrFilters() {
  const values = {};
  state.products.forEach((p) => Object.entries(p.attributes || {}).forEach(([k, v]) => {
    (values[k] = values[k] || new Set()).add(v);
  }));
  const box = $("#attr-filters");
  box.replaceChildren();
  Object.keys(values).sort().forEach((key) => {
    if (values[key].size < 2) return; // a filter with one value filters nothing
    const sel = document.createElement("select");
    sel.append(new Option(`${key.replace(/_/g, " ")}: any`, ""));
    [...values[key]].sort().forEach((v) => sel.append(new Option(v, v)));
    sel.value = state.attrFilters[key] || "";
    sel.addEventListener("change", () => {
      if (sel.value) state.attrFilters[key] = sel.value; else delete state.attrFilters[key];
      renderProducts();
    });
    box.append(sel);
  });
}

function renderProducts() {
  const minTrust = Number($("#min-trust").value);
  const stockOnly = $("#in-stock-only").checked;
  const text = $("#text-filter").value.trim().toLowerCase();
  const list = state.products.filter((p) =>
    (p.trust_score ?? 0) >= minTrust &&
    (!stockOnly || p.in_stock === true) &&
    (!text || `${p.title} ${p.platform}`.toLowerCase().includes(text)) &&
    Object.entries(state.attrFilters).every(([k, v]) => (p.attributes || {})[k] === v));

  $("#product-count").textContent = state.taskId ? `Showing ${list.length} of ${state.products.length} products` : "";
  const tpl = $("#product-tpl");
  const box = $("#products");
  box.replaceChildren();
  list.forEach((p) => {
    const node = tpl.content.cloneNode(true);
    const score = node.querySelector(".score");
    score.textContent = p.trust_score ?? "?";
    score.classList.add(p.trust_score >= 70 ? "good" : p.trust_score >= 45 ? "warn" : "bad");
    score.title = "Trust score";
    if (p.matches === false) node.querySelector(".product").classList.add("rejected");
    const a = node.querySelector(".title");
    a.textContent = p.title || p.product_url;
    a.href = p.product_url;
    const facts = node.querySelector(".facts");
    const fact = (label, value) => {
      const s = document.createElement("span");
      const b = document.createElement("strong");
      b.textContent = value;
      s.append(`${label}: `, b);
      facts.append(s);
    };
    if (p.match_score != null) fact("Match", `${p.match_score}/100`);
    fact("Unit price", fmtMoney(p.unit_price, p.currency));
    fact("Price", fmtMoney(p.price, p.currency));
    fact("Pack", `${p.units_in_pack || 1} unit(s)`);
    fact("Shipping", p.shipping_cost == null ? "?" : p.shipping_cost === 0 ? "free" : fmtMoney(p.shipping_cost, p.currency));
    fact("Stock", p.in_stock == null ? "unknown" : p.in_stock ? "in stock" : "out of stock");
    fact("Platform", p.platform || "?");
    const unmet = node.querySelector(".unmet");
    if ((p.unmet_requirements || []).length) unmet.textContent = "Not met / unverified: " + p.unmet_requirements.join(", ");
    else unmet.remove();
    const ul = node.querySelector(".contradictions");
    (p.contradictions || []).forEach((c) => { const li = document.createElement("li"); li.textContent = c; ul.append(li); });
    const attrs = node.querySelector(".attrs");
    Object.entries(p.attributes || {}).forEach(([k, v]) => {
      const s = document.createElement("span");
      s.textContent = `${k.replace(/_/g, " ")}: ${v}`;
      attrs.append(s);
    });
    box.append(node);
  });
}

$("#sort").addEventListener("change", refreshTask);
$("#show-rejected").addEventListener("change", refreshTask);
$("#min-trust").addEventListener("input", (e) => { $("#min-trust-val").textContent = e.target.value; renderProducts(); });
$("#in-stock-only").addEventListener("change", renderProducts);
$("#text-filter").addEventListener("input", renderProducts);

showView(load("view") === "results" ? "results" : "clarify");
