/* 拾光 GlowFeed 前端：vanilla JS，单页三视图（情报流 / 任务 / 日志） */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

const state = {
  sources: [],          // [{id,name,region,kind}]
  tasks: [],
  feedSort: "score",
  trendSource: "ossinsight",
  trendPeriod: "today",
  pollTimer: null,
  authed: false,        // 是否已凭 token 进入管理态
  lastFeedCount: null,  // 上次情报流条数，用于雷达命中判定
};

// ---------------- 鉴权 ----------------
const auth = {
  token: null,
  // 从 URL ?token= 取出 → 存 sessionStorage → 抹掉地址栏，避免截图/历史泄露
  init() {
    const url = new URL(location.href);
    const t = url.searchParams.get("token");
    if (t) {
      sessionStorage.setItem("nr_token", t);
      url.searchParams.delete("token");
      history.replaceState(null, "", url.pathname + url.hash);
    }
    this.token = sessionStorage.getItem("nr_token");
  },
  async verify() {
    if (!this.token) return false;
    try {
      const r = await api("/api/auth/verify");
      return !!r.authed;
    } catch {
      return false;
    }
  },
};

// ---------------- 基础 ----------------
async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (auth.token) headers["Authorization"] = "Bearer " + auth.token;
  const res = await fetch(path, {
    ...opts,
    headers,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && state.authed) {
    // token 失效：清除失效凭证并降级回只读态
    state.authed = false;
    auth.token = null;
    sessionStorage.removeItem("nr_token");
    document.body.classList.replace("authed", "guest");
    toast("管理凭证已失效，已退出管理态");
  }
  if (!res.ok) throw new Error(data.error || `请求失败 (${res.status})`);
  return data;
}

function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("show"), 2600);
}

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s ?? "";
  return d.innerHTML;
}

function parseDate(iso) {
  if (!iso) return null;
  const d = new Date(iso.includes("T") || iso.includes("Z") ? iso : iso.replace(" ", "T") + "Z");
  return isNaN(d) ? null : d;
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = parseDate(iso);
  if (!d) return iso;
  const diff = (Date.now() - d.getTime()) / 60000;
  if (diff < 1) return "刚刚";
  if (diff < 60) return `${Math.floor(diff)} 分钟前`;
  if (diff < 1440) return `${Math.floor(diff / 60)} 小时前`;
  return `${d.getMonth() + 1}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function fmtHeat(n) {
  if (!n) return "";
  if (n >= 1e8) return (n / 1e8).toFixed(1) + " 亿";
  if (n >= 1e4) return (n / 1e4).toFixed(1) + " 万";
  return String(n);
}

// ---------------- 时钟 ----------------
setInterval(() => {
  $("#clock").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
}, 1000);

// ---------------- 雷达命中 ----------------
function pingRadar() {
  const r = document.getElementById("topRadar");
  if (!r) return;
  r.classList.remove("ping");
  void r.offsetWidth;          // 重启动画
  r.classList.add("ping");
  setTimeout(() => r.classList.remove("ping"), 650);
  flashGlitch(document.querySelector(".side-brand .name"));
}

// ---------------- glitch 故障闪 ----------------
function flashGlitch(el) {
  if (!el || reduceMotion()) return;
  el.classList.remove("glitch");
  void el.offsetWidth;
  el.classList.add("glitch");
  setTimeout(() => el.classList.remove("glitch"), 500); // 须 > --fx-glitch(0.45s)
}

// ---------------- 页签 ----------------
function switchView(view) {
  if (!$(`#view-${view}`)) view = "feed";
  // 访客态不得进入受保护视图
  if (!state.authed && ["tasks", "runs", "settings"].includes(view)) view = "digest";
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $$(".view").forEach((v) => v.classList.remove("active"));
  $(`#view-${view}`).classList.add("active");
  location.hash = view === "feed" ? "" : view;
  const p = document.getElementById("termPath");
  if (p) p.textContent = "~/" + view;
  if (view === "digest") { loadDigest(); loadSysPulse(); }
  if (view === "feed") loadFeed();
  if (view === "trending") loadTrending();
  if (view === "skills") loadSkills();
  if (view === "blog") loadBlog();
  if (view === "runs") loadRuns();
  if (view === "tasks") renderTaskList();
  if (view === "settings") loadSettings();
  setTimeout(mountMatrixInEmpties, 0);
}
$$(".tab").forEach((btn) => {
  if (!btn.dataset.view) return;   // 退出按钮无 data-view，不绑视图切换
  btn.addEventListener("click", () => switchView(btn.dataset.view));
});

// ---------------- 信息源 ----------------
const srcName = (id) => state.sources.find((s) => s.id === id)?.name || id;

async function loadSources() {
  state.sources = await api("/api/sources");
  // 任务表单的源选择 chips
  $("#fSources").innerHTML = state.sources
    .map(
      (s) => `<div class="source-chip" data-id="${s.id}">
        <span>${esc(s.name)}</span><span class="region">${s.region}·${s.kind === "search" ? "检索" : "热榜"}</span>
      </div>`
    )
    .join("");
  $$("#fSources .source-chip").forEach((chip) =>
    chip.addEventListener("click", () => chip.classList.toggle("on"))
  );
  // 情报流来源过滤
  $("#feedSource").innerHTML =
    '<option value="">全部来源</option>' +
    state.sources.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("");
}

// ---------------- 任务 ----------------
async function loadTasks() {
  // 访客只拿到精简 [{id,name}]，足够填充简报/情报流选择器；管理态拿完整任务
  state.tasks = await api(state.authed ? "/api/tasks" : "/api/tasks/public");
  $("#taskCount").textContent = state.authed
    ? state.tasks.filter((t) => t.enabled).length
    : state.tasks.length;
  const opts = state.tasks.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("");
  const feedSel = $("#feedTask");
  const feedPrev = feedSel.value;
  feedSel.innerHTML = '<option value="">全部任务</option>' + opts;
  if ([...feedSel.options].some((o) => o.value === feedPrev)) feedSel.value = feedPrev;
  // 简报必须针对单个任务，默认选第一个
  const dSel = $("#digestTask");
  const dPrev = dSel.value;
  dSel.innerHTML = opts || '<option value="">（暂无任务）</option>';
  if ([...dSel.options].some((o) => o.value === dPrev)) dSel.value = dPrev;
  if (state.authed) renderTaskList();   // 访客态任务对象无完整字段，不渲染管理列表
}

function schedText(t) {
  return t.schedule_type === "interval"
    ? `每 ${t.schedule_value} 分钟`
    : `每日 ${t.schedule_value.join(" / ")}`;
}

function renderTaskList() {
  const wrap = $("#taskList");
  if (!state.tasks.length) {
    wrap.innerHTML = '<div class="empty"><div class="empty-radar"></div><p>尚无侦听任务，在左侧创建第一个</p></div>';
    return;
  }
  wrap.innerHTML = state.tasks
    .map(
      (t) => `<div class="task-card ${t.enabled ? "" : "disabled"}" data-id="${t.id}">
      <div class="task-head">
        <span class="task-name">${esc(t.name)}</span>
        <span class="task-state ${t.enabled ? "on" : "off"}">${t.enabled ? "侦听中" : "已暂停"}</span>
        <div class="task-actions">
          <button class="icon-btn" data-act="run">立即执行</button>
          <button class="icon-btn" data-act="edit">编辑</button>
          <button class="icon-btn" data-act="toggle">${t.enabled ? "暂停" : "启用"}</button>
          <button class="icon-btn danger" data-act="del">删除</button>
        </div>
      </div>
      <div class="task-meta">
        <span>关键词: ${t.keywords.length ? t.keywords.map((k) => `<span class="kw-chip">${esc(k)}</span>`).join("") : "<b>全量</b>"}</span>
        <span>来源: <b>${t.sources.length ? t.sources.map(srcName).join("、") : "全部"}</b></span>
        <span>调度: <b>${schedText(t)}</b></span>
        <span>上次: <b>${fmtTime(t.last_run)}</b></span>
        <span>下次: <b>${t.next_run || "—"}</b></span>
        ${t.preferred_keywords && t.preferred_keywords.length ? `<span>偏好: ${
          t.preferred_keywords.map((k) => `<span class="kw-chip pref" data-kind="preferred" data-word="${esc(k)}">${esc(k)} <i class="kw-del">×</i></span>`).join("")}</span>` : ""}
        ${t.excluded_keywords && t.excluded_keywords.length ? `<span>排除: ${
          t.excluded_keywords.map((k) => `<span class="kw-chip excl" data-kind="excluded" data-word="${esc(k)}">${esc(k)} <i class="kw-del">×</i></span>`).join("")}</span>` : ""}
      </div>
    </div>`
    )
    .join("");

  $$(".task-card [data-act]").forEach((btn) =>
    btn.addEventListener("click", async (e) => {
      const id = Number(e.target.closest(".task-card").dataset.id);
      const task = state.tasks.find((t) => t.id === id);
      const act = btn.dataset.act;
      try {
        if (act === "run") {
          await api(`/api/tasks/${id}/run`, { method: "POST" });
          toast(`「${task.name}」开始侦听，稍后查看情报流`);
        } else if (act === "edit") {
          fillForm(task);
        } else if (act === "toggle") {
          await api(`/api/tasks/${id}`, { method: "PUT", body: { ...task, enabled: !task.enabled } });
          toast(task.enabled ? "已暂停" : "已启用");
          await loadTasks();
        } else if (act === "del") {
          if (!confirm(`删除任务「${task.name}」？其收录的资讯也会一并删除。`)) return;
          await api(`/api/tasks/${id}`, { method: "DELETE" });
          toast("已删除");
          await loadTasks();
        }
      } catch (err) {
        toast(err.message);
      }
    })
  );

  $$(".task-card .kw-del").forEach((x) =>
    x.addEventListener("click", async (e) => {
      const chip = e.target.closest(".kw-chip");
      const card = e.target.closest(".task-card");
      const id = Number(card.dataset.id);
      const task = state.tasks.find((t) => t.id === id);
      const field = chip.dataset.kind === "preferred" ? "preferred_keywords" : "excluded_keywords";
      const next = (task[field] || []).filter((w) => w !== chip.dataset.word);
      try {
        await api(`/api/tasks/${id}`, { method: "PUT", body: { ...task, [field]: next } });
        toast("已删除");
        await loadTasks();
      } catch (err) { toast(err.message); }
    })
  );
}

// ---------------- 任务表单 ----------------
function fillForm(t) {
  $("#formTitle").textContent = t ? `编辑：${t.name}` : "新建侦听任务";
  $("#fId").value = t ? t.id : "";
  $("#fName").value = t ? t.name : "";
  $("#fKeywords").value = t ? t.keywords.join(", ") : "";
  $$("#fSources .source-chip").forEach((c) =>
    c.classList.toggle("on", !!t && t.sources.includes(c.dataset.id))
  );
  $("#fSchedType").value = t ? t.schedule_type : "interval";
  if (t && t.schedule_type === "daily") {
    $("#fSchedDaily").value = t.schedule_value.join(", ");
  } else {
    $("#fSchedInterval").value = t ? t.schedule_value : 60;
  }
  $("#fEnabled").checked = t ? t.enabled : true;
  $("#btnCancel").classList.toggle("hidden", !t);
  $("#formError").textContent = "";
  syncSchedInputs();
  if (t) window.scrollTo({ top: 0, behavior: "smooth" });
}

function syncSchedInputs() {
  const daily = $("#fSchedType").value === "daily";
  $("#fSchedInterval").classList.toggle("hidden", daily);
  $("#fSchedDaily").classList.toggle("hidden", !daily);
}
$("#fSchedType").addEventListener("change", syncSchedInputs);
$("#btnCancel").addEventListener("click", () => fillForm(null));

$("#taskForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = $("#fId").value;
  const daily = $("#fSchedType").value === "daily";
  const body = {
    name: $("#fName").value.trim(),
    keywords: $("#fKeywords").value.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
    sources: $$("#fSources .source-chip.on").map((c) => c.dataset.id),
    schedule_type: daily ? "daily" : "interval",
    schedule_value: daily
      ? $("#fSchedDaily").value.split(/[,，]/).map((s) => s.trim()).filter(Boolean)
      : Number($("#fSchedInterval").value),
    enabled: $("#fEnabled").checked,
  };
  const btn = $("#btnSave");
  btn.disabled = true;
  try {
    if (id) {
      await api(`/api/tasks/${id}`, { method: "PUT", body });
      toast("任务已更新");
    } else {
      await api("/api/tasks", { method: "POST", body });
      toast("任务已创建，首次侦听已自动开始");
    }
    fillForm(null);
    await loadTasks();
  } catch (err) {
    $("#formError").textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

// ---------------- 模型设置 ----------------
const PROVIDER_HINTS = {
  openai: { base: "https://api.openai.com/v1", hint: "填到 /v1（兼容服务如 https://api.deepseek.com/v1）" },
  anthropic: { base: "https://api.anthropic.com", hint: "默认官方地址，可改为中转地址" },
  ollama: { base: "http://localhost:11434", hint: "本地默认地址，远程填对应 IP:端口" },
};

function syncProviderFields() {
  const p = $("#sProvider").value;
  const cloud = p === "openai" || p === "anthropic" || p === "ollama";
  $("#sCloudFields").classList.toggle("hidden", !cloud);
  $("#sKeyLabel").classList.toggle("hidden", p === "ollama"); // Ollama 无需 Key
  const meta = PROVIDER_HINTS[p];
  if (meta) {
    $("#sBaseHint").textContent = meta.hint;
    if (!$("#sBaseUrl").value) $("#sBaseUrl").placeholder = meta.base;
  }
}

async function loadSettings() {
  $("#settingsMsg").textContent = "";
  try {
    const s = await api("/api/settings");
    $("#sProvider").value = s.provider || "auto";
    $("#sBaseUrl").value = s.base_url || "";
    $("#sModel").value = s.model || "";
    $("#sApiKey").value = "";
    $("#sApiKey").placeholder = s.api_key_set ? "已设置（留空表示不修改）" : "粘贴你的 API Key";
    syncProviderFields();
  } catch (err) {
    $("#settingsMsg").textContent = err.message;
  }
}

function settingsPayload() {
  const key = $("#sApiKey").value.trim();
  return {
    provider: $("#sProvider").value,
    base_url: $("#sBaseUrl").value.trim(),
    model: $("#sModel").value.trim(),
    api_key: key || undefined, // 留空 = 不修改已存 Key
  };
}

$("#sProvider").addEventListener("change", syncProviderFields);

$("#settingsForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/settings", { method: "PUT", body: settingsPayload() });
    toast("模型设置已保存");
    await loadSettings();
  } catch (err) {
    $("#settingsMsg").textContent = "保存失败：" + err.message;
  }
});

$("#btnTestConn").addEventListener("click", async () => {
  const msg = $("#settingsMsg");
  msg.textContent = "正在测试连接…";
  msg.className = "settings-msg mono";
  try {
    const r = await api("/api/settings/test", { method: "POST", body: settingsPayload() });
    msg.textContent = (r.ok ? "✓ " : "✗ ") + r.message;
    msg.classList.add(r.ok ? "ok" : "err");
  } catch (err) {
    msg.textContent = "✗ " + err.message;
    msg.classList.add("err");
  }
});

// ---------------- 系统脉搏条 ----------------
const reduceMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

function easeOutCubic(t) { return 1 - Math.pow(1 - t, 3); }

function countUp(el, to) {
  if (!el) return;
  const from = Number(el.dataset.val || 0);
  el.dataset.val = to;
  if (reduceMotion() || from === to) { el.textContent = to; return; }
  const dur = 600, t0 = performance.now();
  (function step(now) {
    if (Number(el.dataset.val) !== to) return; // 被更新的目标值抢占则放弃本链
    const p = Math.min((now - t0) / dur, 1);
    el.textContent = Math.round(from + (to - from) * easeOutCubic(p));
    if (p < 1) requestAnimationFrame(step);
  })(performance.now());
}

function renderSpark(values) {
  const poly = document.querySelector("#pkSpark .spark-line");
  if (!poly) return;
  if (!values.length) { poly.setAttribute("points", ""); return; }
  const max = Math.max(...values), min = Math.min(...values), n = values.length;
  if (n === 1) {
    const y = (31 - ((values[0] - min) / ((max - min) || 1)) * 30).toFixed(1);
    poly.setAttribute("points", `0,${y} 200,${y}`); // 单点渲染为水平线
    return;
  }
  const pts = values.map((v, i) => {
    const x = (i / (n - 1)) * 200;
    const y = 31 - ((v - min) / ((max - min) || 1)) * 30;
    return x.toFixed(1) + "," + y.toFixed(1);
  }).join(" ");
  poly.setAttribute("points", pts);
}

function isToday(iso) {
  const d = parseDate(iso);
  if (!d) return false;
  const n = new Date();
  return d.getFullYear() === n.getFullYear() && d.getMonth() === n.getMonth() && d.getDate() === n.getDate();
}

async function loadSysPulse() {
  if (!document.getElementById("sysPulse")) return;
  let runs;
  try { runs = await api("/api/runs?limit=50"); } catch { return; } // 无数据/失败：保持原值，不造假
  const today = runs.filter((r) => isToday(r.started_at));
  const totalFetched = today.reduce((s, r) => s + (r.stats?.fetched || 0), 0);
  countUp(document.getElementById("pkFetched"), totalFetched);
  const recent = runs.slice(0, 24).reverse().map((r) => r.stats?.fetched || 0);
  renderSpark(recent);
  const srcSet = new Set();
  today.forEach((r) => Object.keys(r.stats?.sources || {}).forEach((s) => srcSet.add(s)));
  const meta = document.getElementById("pkMeta");
  if (meta) meta.textContent = `侦听 ${state.tasks.filter((t) => t.enabled).length} 任务 · ${srcSet.size} 来源`;
}

// ---------------- 每日简报 ----------------
async function loadDigest() {
  const taskId = $("#digestTask").value;
  const body = $("#digestBody");
  if (!taskId) {
    body.innerHTML = '<div class="empty"><div class="empty-radar"></div><p>请先创建侦听任务并采集资讯</p></div>';
    return;
  }
  try {
    const d = await api(`/api/digest?task_id=${taskId}`); // 读快照，秒回
    if (d.empty) {
      body.innerHTML = `<div class="empty"><div class="empty-radar"></div>
        <p>该任务还没有简报<br><span class="mono" style="font-size:12px;opacity:.7">下次定时任务执行后会自动生成，或点上方「重新生成」立即生成</span></p></div>`;
      return;
    }
    renderDigest(d);
  } catch (err) {
    body.innerHTML = `<div class="empty"><p>${esc(err.message)}</p></div>`;
  }
}

function renderDigest(d) {
  const ai = d.generated_by !== "extractive";
  const badge = ai
    ? `<span class="gen-badge ai">🧠 AI 综述 · ${esc(d.generated_by)}</span>`
    : `<span class="gen-badge algo">⚙ 算法摘要</span>`;

  const topics = d.topics.length
    ? `<div class="topic-grid">${d.topics
        .map(
          (t) => `<div class="topic-card">
        <div class="topic-head"><span class="topic-count">${t.count}</span>
          <a href="${esc(t.url)}" target="_blank" rel="noopener" class="topic-title">${esc(t.title)}</a></div>
        <div class="topic-srcs mono">${t.sources.map(esc).join(" · ")}${t.heat ? " · ♨ " + fmtHeat(t.heat) : ""}</div>
        ${t.items.length > 1 ? `<ul class="topic-items">${t.items
          .slice(1)
          .map((i) => `<li><a href="${esc(i.url)}" target="_blank" rel="noopener">${esc(i.title)}</a> <span class="ti-src">${esc(i.source)}</span></li>`)
          .join("")}</ul>` : ""}
      </div>`
        )
        .join("")}</div>`
    : `<p class="digest-note mono">本期资讯较为分散，未形成明显的话题聚合。</p>`;

  // 热点榜已移到「情报流」(热点/趋势/最新 三档排序)，简报只保留归纳类内容
  const kw = d.keywords.length
    ? `<div class="kw-cloud">${d.keywords
        .map(([w, n]) => `<span class="kw-tag" style="font-size:${Math.min(11 + n, 22)}px">${esc(w)}<i>${n}</i></span>`)
        .join("")}</div>`
    : "";

  $("#digestBody").innerHTML = `
    <div class="digest-hero">
      <div class="digest-hero-top">${badge}
        <span class="digest-stat mono">生成于 ${fmtTime(d.generated_at)} · 归纳 ${d.summarized}/${d.total} 条 · ${d.source_count} 来源</span></div>
      <p class="digest-summary">${esc(d.summary)}</p>
    </div>
    <div class="digest-cols">
      <div class="digest-main">
        <h3 class="digest-h">🗂 主题聚类</h3>
        ${topics}
      </div>
      <aside class="digest-side">
        ${kw ? `<h3 class="digest-h">🏷 关键词</h3>${kw}` : ""}
      </aside>
    </div>`;
}

$("#digestTask").addEventListener("change", loadDigest);

$("#btnRefreshDigest").addEventListener("click", async () => {
  const taskId = $("#digestTask").value;
  if (!taskId) {
    toast("请先创建并选择一个任务");
    return;
  }
  const btn = $("#btnRefreshDigest");
  btn.disabled = true;
  $("#digestBody").innerHTML = '<div class="digest-loading mono"><span class="pulse"></span> 正在归纳分析（调用模型，约需 10–20 秒）…</div>';
  mountMatrixInEmpties();
  try {
    const d = await api(`/api/digest/${taskId}/refresh`, { method: "POST" });
    if (d.empty) {
      $("#digestBody").innerHTML = '<div class="empty"><p>该任务暂无资讯可归纳</p></div>';
    } else {
      renderDigest(d);
      toast("简报已重新生成");
      flashGlitch(document.querySelector(".side-brand .name"));
    }
  } catch (err) {
    $("#digestBody").innerHTML = `<div class="empty"><p>${esc(err.message)}</p></div>`;
  } finally {
    btn.disabled = false;
  }
});

// ---------------- 情报流 ----------------
async function loadFeed() {
  const params = new URLSearchParams({ sort: state.feedSort, limit: 100 });
  if ($("#feedTask").value) params.set("task_id", $("#feedTask").value);
  if ($("#feedSource").value) params.set("source", $("#feedSource").value);
  if ($("#feedSearch").value.trim()) params.set("q", $("#feedSearch").value.trim());
  const items = await api(`/api/articles?${params}`);
  const kw = $("#feedSearch").value.trim();

  // 加载已有反馈用于回显；访客态无权读反馈，直接留空（反馈按钮由 CSS 隐藏）
  let feedback = {};
  if (state.authed) {
    try {
      const ids = $("#feedTask").value ? [$("#feedTask").value] : state.tasks.map((t) => t.id);
      const maps = await Promise.all(ids.map((id) => api(`/api/feedback?task_id=${id}`).catch(() => ({}))));
      feedback = Object.assign({}, ...maps);
    } catch {}
  }

  // 刷新时间取这批情报里最新的后台抓取时刻（fetched_at），而非页面渲染时刻
  const latestFetch = items.reduce((m, a) => (a.fetched_at && a.fetched_at > m ? a.fetched_at : m), "");
  $("#feedMeta").textContent = items.length
    ? `共 ${items.length} 条情报 · 数据更新于 ${fmtTime(latestFetch)}`
    : "";
  // 仅"非搜索态"下、条数较上次增长 → 视为有新情报，雷达命中一次
  if (state.lastFeedCount != null && items.length > state.lastFeedCount && !$("#feedSearch").value.trim()) {
    pingRadar();
  }
  state.lastFeedCount = items.length;
  $("#feedEmpty").classList.toggle("hidden", items.length > 0);

  const hl = (text) => {
    let safe = esc(text);
    if (kw) safe = safe.replace(new RegExp(kw.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi"), (m) => `<mark>${m}</mark>`);
    return safe;
  };

  const fbBtns = (a) => {
    const sig = feedback[a.url] || "";
    return `<span class="fb-btns" data-url="${esc(a.url)}" data-title="${esc(a.title)}" data-task-id="${a.task_id}">
      <button class="fb-btn ${sig === 'like' ? 'on' : ''}" data-sig="like" title="喜欢，多推同类">[+]</button>
      <button class="fb-btn ${sig === 'dislike' ? 'on' : ''}" data-sig="dislike" title="不喜欢，减少同类">[-]</button>
    </span>`;
  };

  $("#feed").innerHTML = items
    .map(
      (a, i) => `<article class="card" data-src="${a.source}" style="animation-delay:${Math.min(i * 28, 500)}ms">
      <div class="card-head">
        <span class="src-badge">${esc(srcName(a.source))}</span>
        <span class="card-date">${fmtTime(a.published_at || a.fetched_at)}</span>
      </div>
      <a class="card-title-link" href="${esc(a.url)}" target="_blank" rel="noopener">${hl(a.title)}</a>
      ${a.summary ? `<p class="card-summary">${hl(a.summary)}</p>` : ""}
      <div class="card-foot">
        <span>${a.author ? esc(a.author) : ""}</span>
        <span class="heat">${a.engagement ? "♨ " + fmtHeat(a.engagement) : ""}</span>
        ${fbBtns(a)}
      </div>
    </article>`
    )
    .join("");

  $("#feed").querySelectorAll(".fb-btn").forEach((btn) =>
    btn.addEventListener("click", async (e) => {
      e.preventDefault();
      const wrap = btn.closest(".fb-btns");
      // 用文章自身的 task_id，任何视图（含"全部任务"）都能标记
      const tid = wrap.dataset.taskId && wrap.dataset.taskId !== "undefined"
        ? wrap.dataset.taskId : $("#feedTask").value;
      if (!tid) { toast("该资讯无所属任务，无法标记"); return; }
      const signal = btn.classList.contains("on") ? "none" : btn.dataset.sig;
      // 乐观更新：立即变色 + 弹跳动效，不等接口返回
      const sibs = [...wrap.querySelectorAll(".fb-btn")];
      const prev = sibs.map((b) => b.classList.contains("on"));
      sibs.forEach((b) => b.classList.remove("on"));
      if (signal !== "none") btn.classList.add("on");
      btn.classList.remove("pop"); void btn.offsetWidth; btn.classList.add("pop");
      try {
        await api("/api/feedback", { method: "POST",
          body: { task_id: Number(tid), url: wrap.dataset.url, title: wrap.dataset.title, signal } });
        toast(signal === "like" ? "👍 已喜欢，将据此优化推送"
            : signal === "dislike" ? "👎 已不喜欢，将减少同类" : "已取消标记");
      } catch (err) {
        sibs.forEach((b, i) => b.classList.toggle("on", prev[i])); // 失败回滚
        toast("标记失败：" + err.message);
      }
    })
  );
}

["feedTask", "feedSource"].forEach((id) => $(`#${id}`).addEventListener("change", loadFeed));
let searchTimer;
$("#feedSearch").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadFeed, 300);
});
// 仅情报流的排序按钮（趋势页的周期按钮虽同样是 .sort-btn，但单独绑定，避免互相串扰）
$$("#view-feed .sort-btn").forEach((b) =>
  b.addEventListener("click", () => {
    $$("#view-feed .sort-btn").forEach((x) => x.classList.toggle("active", x === b));
    state.feedSort = b.dataset.sort;
    loadFeed();
  })
);

// ---------------- GitHub 趋势榜 ----------------
async function loadTrending() {
  const source = state.trendSource || "ossinsight";
  const period = state.trendPeriod || "today";
  const lang = $("#trendLang") ? $("#trendLang").value : "All";
  const body = $("#trendBody");
  // 副标题随信源变（OSSInsight 趋势分 / GitHub 官方当期新增 star）
  const subEl = $("#trendSub");
  if (subEl) subEl.textContent = source === "github" ? "GitHub 官方 · 免 key" : "OSSInsight · 免 key";
  body.innerHTML = '<li class="trend-loading mono">正在拉取 GitHub 趋势…</li>';
  $("#trendEmpty").classList.add("hidden");
  try {
    const data = await api(`/api/trending?source=${source}&period=${period}&language=${encodeURIComponent(lang)}`);
    const rows = data.rows || [];
    const periodLabel = period === "today" ? "今日" : period === "week" ? "本周" : "本月";
    // 刷新时间取后端真实抓取时刻（OSSInsight 快照），而非页面渲染时刻
    $("#trendMeta").textContent = rows.length
      ? `共 ${rows.length} 个仓库 · ${periodLabel}趋势 · 数据更新于 ${fmtTime(data.fetched_at)}`
      : "";
    if (!rows.length) {
      body.innerHTML = "";
      $("#trendEmpty").classList.remove("hidden");
      return;
    }
    body.innerHTML = rows
      .map(
        (r) => `<li class="trend-item">
        <span class="trend-rank mono">${r.rank}</span>
        <div class="trend-main">
          <a class="trend-name" href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.name)}</a>
          ${r.description ? `<p class="trend-desc">${esc(r.description)}</p>` : ""}
          <div class="trend-tags mono">
            ${r.language ? `<span class="trend-lang">${esc(r.language)}</span>` : ""}
            <span class="trend-stars">★ ${fmtHeat(r.stars) || r.stars}</span>
            ${r.forks ? `<span class="trend-forks">⑂ ${fmtHeat(r.forks) || r.forks}</span>` : ""}
            ${r.score ? `<span class="trend-score">${source === "github" ? `${periodLabel}+${fmtHeat(r.score) || r.score}★` : `趋势 ${r.score}`}</span>` : ""}
            ${r.contributors && r.contributors.length ? `<span class="trend-by">by ${r.contributors.map(esc).join(", ")}</span>` : ""}
          </div>
        </div>
      </li>`
      )
      .join("");
  } catch (err) {
    body.innerHTML = "";
    $("#trendEmpty").classList.remove("hidden");
    $("#trendEmpty").querySelector("p").textContent = "拉取失败：" + err.message;
  }
}

$$("#trendSource .sort-btn").forEach((b) =>
  b.addEventListener("click", () => {
    $$("#trendSource .sort-btn").forEach((x) => x.classList.toggle("active", x === b));
    state.trendSource = b.dataset.source;
    loadTrending();
  })
);
$$("#trendPeriod .sort-btn").forEach((b) =>
  b.addEventListener("click", () => {
    $$("#trendPeriod .sort-btn").forEach((x) => x.classList.toggle("active", x === b));
    state.trendPeriod = b.dataset.period;
    loadTrending();
  })
);
$("#trendLang") && $("#trendLang").addEventListener("change", loadTrending);
// 刷新 = 强制重抓当前信源并落盘（管理员专属，走带 token 的 POST）；重抓后再读新快照渲染
$("#btnTrendRefresh") && $("#btnTrendRefresh").addEventListener("click", async () => {
  const btn = $("#btnTrendRefresh");
  btn.disabled = true;
  try {
    await api("/api/trending/refresh", {
      method: "POST",
      body: {
        source: state.trendSource || "ossinsight",
        period: state.trendPeriod || "today",
        language: $("#trendLang") ? $("#trendLang").value : "All",
      },
    });
    await loadTrending();
    toast("趋势榜已刷新");
  } catch (err) {
    toast("刷新失败：" + err.message);
  } finally {
    btn.disabled = false;
  }
});

$("#btnRunNow").addEventListener("click", async () => {
  const taskId = $("#feedTask").value;
  const targets = taskId ? state.tasks.filter((t) => t.id === Number(taskId)) : state.tasks;
  if (!targets.length) {
    toast("请先在「侦听任务」页创建任务");
    return;
  }
  const btn = $("#btnRunNow");
  btn.disabled = true;
  try {
    await Promise.all(targets.map((t) => api(`/api/tasks/${t.id}/run`, { method: "POST" })));
    toast(`已触发 ${targets.length} 个任务，情报抵达后自动刷新`);
    // 25 秒内每 5 秒轮询一次新结果
    let n = 0;
    clearInterval(state.pollTimer);
    state.pollTimer = setInterval(async () => {
      await loadFeed();
      if (++n >= 5) clearInterval(state.pollTimer);
    }, 5000);
  } catch (err) {
    toast(err.message);
  } finally {
    setTimeout(() => (btn.disabled = false), 3000);
  }
});

// ---------------- 执行日志 ----------------
async function loadRuns() {
  const runs = await api("/api/runs?limit=50");
  $("#runsEmpty").classList.toggle("hidden", runs.length > 0);
  const taskName = (id) => state.tasks.find((t) => t.id === id)?.name || `#${id}`;
  const stCls = { ok: "st-ok", running: "st-running", error: "st-error" };
  const stTxt = { ok: "完成", running: "执行中", error: "失败" };
  $("#runsBody").innerHTML = runs
    .map((r) => {
      const srcDetail = Object.entries(r.stats.sources || {})
        .map(([s, n]) => `${srcName(s)}:${n}`)
        .join(" · ");
      return `<tr>
        <td>${r.id}</td><td>${esc(taskName(r.task_id))}</td>
        <td>${fmtTime(r.started_at)}</td>
        <td class="${stCls[r.status] || ""}">${stTxt[r.status] || r.status}</td>
        <td>${r.stats.fetched ?? "—"}</td><td>${r.stats.kept ?? "—"}</td><td>${r.stats.new ?? "—"}</td>
        <td>${srcDetail || "—"}</td>
      </tr>`;
    })
    .join("");
}

// ---------------- 矩阵雨（仅空/加载）----------------
function initMatrix(container) {
  if (!container || reduceMotion()) return;
  if (container.querySelector("canvas.mtx")) return;
  const w = container.clientWidth || 320, h = container.clientHeight || 160;
  if (w < 40 || h < 40) return;
  const cv = document.createElement("canvas");
  cv.className = "mtx"; cv.width = w; cv.height = h;
  container.prepend(cv);
  const cx = cv.getContext("2d");
  const cols = Math.max(1, Math.floor(w / 12));
  const drops = Array(cols).fill(1);
  const chars = "01ｱｲｳｶ10";
  let raf, last = 0;
  function frame(t) {
    if (t - last > 90) {
      last = t;
      cx.fillStyle = "rgba(7,11,9,.16)";
      cx.fillRect(0, 0, w, h);
      cx.fillStyle = "#00FF66";
      cx.font = "12px monospace";
      for (let i = 0; i < cols; i++) {
        cx.fillText(chars[Math.floor(Math.random() * chars.length)], i * 12, drops[i] * 14);
        if (drops[i] * 14 > h && Math.random() > 0.96) drops[i] = 0;
        drops[i]++;
      }
    }
    raf = requestAnimationFrame(frame);
  }
  raf = requestAnimationFrame(frame);
  const mo = new MutationObserver(() => {
    if (!document.body.contains(cv)) { cancelAnimationFrame(raf); mo.disconnect(); }
  });
  mo.observe(document.body, { childList: true, subtree: true });
}

function mountMatrixInEmpties() {
  document.querySelectorAll(".view.active .empty, .digest-loading").forEach(initMatrix);
}

// ---------------- 命令面板 ⌘K ----------------
function initCmdk() {
  const ov = document.getElementById("cmdk");
  const input = document.getElementById("cmdkInput");
  const list = document.getElementById("cmdkList");
  if (!ov || !input || !list) return;

  const cmds = [
    { label: "立即侦听（当前任务）", hint: "run",      kw: ["run", "侦听", "now", "立即"],      act: () => $("#btnRunNow").click() },
    { label: "重新生成简报",         hint: "regen",    kw: ["regen", "简报", "refresh", "重新"], act: () => $("#btnRefreshDigest").click() },
    { label: "新建侦听任务",         hint: "new",      kw: ["new", "新建", "task", "任务"],     act: () => { switchView("tasks"); fillForm(null); $("#fName").focus(); } },
    { label: "跳转 · 简报",          hint: "digest",   kw: ["go", "digest", "简报"],            act: () => switchView("digest") },
    { label: "跳转 · 情报流",        hint: "feed",     kw: ["go", "feed", "情报"],              act: () => switchView("feed") },
    { label: "跳转 · 博客",          hint: "blog",     kw: ["go", "blog", "博客"],              act: () => switchView("blog") },
    { label: "跳转 · 任务",          hint: "tasks",    kw: ["go", "tasks", "任务"],             act: () => switchView("tasks") },
    { label: "跳转 · 日志",          hint: "runs",     kw: ["go", "runs", "日志"],              act: () => switchView("runs") },
    { label: "跳转 · 设置",          hint: "settings", kw: ["go", "settings", "设置"],          act: () => switchView("settings") },
    { label: "在情报流中搜索…",      hint: "search",   kw: ["search", "搜", "find"],            act: () => { switchView("feed"); $("#feedSearch").focus(); } },
  ];
  let filtered = cmds, active = 0, prevFocus = null;

  const hl = (label, q) => q ? esc(label).replace(new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi"), (m) => `<mark>${m}</mark>`) : esc(label);

  function render() {
    const q = input.value.trim().toLowerCase();
    filtered = !q ? cmds : cmds.filter((c) => c.label.toLowerCase().includes(q) || c.kw.some((k) => k.includes(q)));
    if (active >= filtered.length) active = 0;
    list.innerHTML = filtered.length
      ? filtered.map((c, i) => `<li class="cmdk-item ${i === active ? "active" : ""}" data-i="${i}"><span>${hl(c.label, q)}</span><span class="hint">${esc(c.hint)}</span></li>`).join("")
      : '<li class="cmdk-item" style="opacity:.5;cursor:default">无匹配命令</li>';
  }
  function open()  { prevFocus = document.activeElement; ov.classList.remove("hidden"); ov.setAttribute("aria-hidden", "false"); input.value = ""; active = 0; render(); input.focus(); }
  function close() { ov.classList.add("hidden"); ov.setAttribute("aria-hidden", "true"); if (prevFocus && prevFocus.focus) prevFocus.focus(); }
  function exec()  { const c = filtered[active]; if (!c) return; close(); c.act(); }

  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      ov.classList.contains("hidden") ? open() : close();
      return;
    }
    if (ov.classList.contains("hidden")) return;
    if (e.key === "Escape")        { e.preventDefault(); close(); }
    else if (e.key === "ArrowDown"){ e.preventDefault(); active = Math.min(active + 1, filtered.length - 1); render(); }
    else if (e.key === "ArrowUp")  { e.preventDefault(); active = Math.max(active - 1, 0); render(); }
    else if (e.key === "Enter")    { e.preventDefault(); exec(); }
  });
  input.addEventListener("input", () => { active = 0; render(); });
  list.addEventListener("click", (e) => {
    const li = e.target.closest(".cmdk-item[data-i]");
    if (!li) return;
    active = Number(li.dataset.i);
    exec();
  });
  ov.addEventListener("click", (e) => { if (e.target === ov) close(); });
}

// ---------------- 开机自检 ----------------
function runBoot() {
  const ov = document.getElementById("boot");
  if (!ov) return;
  if (sessionStorage.getItem("nr_booted") || reduceMotion()) { ov.remove(); return; }
  sessionStorage.setItem("nr_booted", "1");
  const N = state.sources.length;
  const M = state.tasks.filter((t) => t.enabled).length;
  const lines = [
    ["GLOWFEED BIOS v0 ............. ", "OK", "ok"],
    [`init sources [${N}] ........... `, "OK", "ok"],
    ["sched daemon ................. ", "OK", "ok"],
    [`load tasks [${M}] ............ `, "OK", "ok"],
    ["listening .................... ", "●", "ok"],
    ["> ready", "", "ready"],
  ];
  const log = document.getElementById("bootLog");
  let i = 0;
  (function step() {
    if (i >= lines.length) {
      setTimeout(() => { ov.classList.add("done"); setTimeout(() => ov.remove(), 550); }, 360);
      return;
    }
    const [head, tail, cls] = lines[i];
    const div = document.createElement("div");
    if (cls === "ready") {
      div.className = "ready";
      div.textContent = head;
    } else {
      div.innerHTML = esc(head) + `<span class="${cls}">${esc(tail)}</span>`;
    }
    log.appendChild(div);
    i++;
    setTimeout(step, 420);
  })();
}

// ---------------- 启动 ----------------
(async function init() {
  auth.init();
  state.authed = await auth.verify();
  document.body.classList.add(state.authed ? "authed" : "guest");
  await loadSources();
  await loadTasks();
  const hash = location.hash.replace("#", "");
  switchView(hash || "digest");
  runBoot();
  initCmdk();
  // 情报流常驻轻量轮询（60s），定时任务产出后自动出现
  setInterval(() => {
    if ($("#view-feed").classList.contains("active")) loadFeed();
    loadTasks();
    loadSysPulse();
  }, 60000);
})();

// ---------------- 热门 Skill 榜 ----------------
state.skillsType = "hot";

async function loadSkills() {
  const type = state.skillsType || "hot";
  const body = $("#skillsBody");
  const empty = $("#skillsEmpty");
  const meta = $("#skillsMeta");
  try {
    const data = await api(`/api/skills/board?type=${type}&period=recent`);
    if (meta) {
      const srcTxt = (data.sources || [])
        .map((s) => `${esc(s.id)}:<b class="src-${esc(s.status)}">${esc(s.status)}</b>`)
        .join(" · ");
      const snap = data.snapshot_time ? `快照 ${fmtTime(data.snapshot_time)}` : "尚无快照";
      const note = type === "hot" ? " · 按 GitHub stars · 近期"
        : type === "rising" ? " · 按近 2 天 stars 增量"
        : " · 多源交叉推荐";
      meta.innerHTML = `${snap}${note}${srcTxt ? " · " + srcTxt : ""}`;
    }
    if (type === "rising") return renderSkillsRising(data, body, empty);
    if (type === "praise") return renderSkillsPraise(data, body, empty);
    return renderSkillsHot(data.rows || [], body, empty);
  } catch (err) {
    if (body) body.innerHTML = "";
    if (empty) { empty.classList.remove("hidden"); empty.innerHTML = `<p>加载失败：${esc(err.message)}</p>`; }
  }
}

function renderSkillsHot(rows, body, empty) {
  empty.classList.toggle("hidden", rows.length > 0);
  if (!rows.length) { body.innerHTML = ""; empty.innerHTML = "<p>暂无数据 — 点「重新生成」抓取（管理员）</p>"; return; }
  body.innerHTML = rows.map((r, i) => {
    const collection = r.type === "collection";
    const kids = r.children || [];
    const childBlock = collection && kids.length
      ? `<details class="skill-children"><summary>📦 合集 · ${kids.length} 个 skill</summary>
           <div class="skill-child-names">${kids.map((c) => `<span>${esc(c)}</span>`).join("")}</div></details>`
      : "";
    return `<li class="trend-item">
      <span class="trend-rank mono">${i + 1}</span>
      <div class="trend-main">
        <a class="trend-name" href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.name)}</a>
        ${r.description ? `<p class="trend-desc">${esc(r.description)}</p>` : ""}
        <div class="trend-tags mono">
          <span class="trend-stars">★ ${fmtHeat(r.stars) || r.stars}</span>
          <span class="skill-kind">${collection ? "合集仓库" : "单体 skill"}</span>
        </div>
        ${childBlock}
      </div>
    </li>`;
  }).join("");
}

function renderSkillsRising(data, body, empty) {
  if (data.status === "warming-up") {
    body.innerHTML = "";
    empty.classList.remove("hidden");
    empty.innerHTML = "<p>📈 历史积累中 — 攒够 2 天快照后开放飙升榜</p>";
    return;
  }
  const rows = data.rows || [];
  empty.classList.toggle("hidden", rows.length > 0);
  if (!rows.length) { body.innerHTML = ""; empty.innerHTML = "<p>暂无飙升数据</p>"; return; }
  body.innerHTML = rows.map((r, i) => `<li class="trend-item">
      <span class="trend-rank mono">${i + 1}</span>
      <div class="trend-main">
        <a class="trend-name" href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.name)}</a>
        ${r.description ? `<p class="trend-desc">${esc(r.description)}</p>` : ""}
        <div class="trend-tags mono">
          <span class="skill-delta">+${fmtHeat((r.delta && r.delta.stars) || r.delta_stars || 0)} ★ 近 2 天</span>
          ${r.stars ? `<span class="trend-stars">★ ${fmtHeat(r.stars) || r.stars} 总</span>` : ""}
        </div>
      </div>
    </li>`).join("");
}

// 口碑榜：后端结构为 {name, source_repo, mention_count, mentions:[{reason,domain,author,...}]}
//（顶层无 reason 字段——理由取自首条 mention）
function praiseReason(it) {
  return (it.mentions && it.mentions[0] && it.mentions[0].reason) || it.reason || "";
}

// 可点击跳转目标：优先 GitHub 仓库，无则跳「推荐它的那篇文章」（首条 mention 的 url）
function praiseLink(it) {
  if (it.source_repo) return `https://github.com/${it.source_repo}`;
  const m = (it.mentions && it.mentions[0]) || {};
  return m.url || "";
}

function renderSkillsPraise(data, body, empty) {
  const rows = data.rows || [];
  const pending = data.pending || [];
  const hasAny = rows.length || pending.length;
  empty.classList.toggle("hidden", !!hasAny);
  if (!hasAny) {
    body.innerHTML = "";
    empty.innerHTML = data.status === "warming-up"
      ? "<p>⭐ 口碑榜需在「设置」配置模型后启用</p>" : "<p>暂无口碑数据</p>";
    return;
  }

  const mainHtml = rows.map((r, i) => {
    const reason = praiseReason(r);
    const sources = r.mention_count || (r.mentions || []).length;
    const href = praiseLink(r);
    const nameEl = href
      ? `<a class="trend-name" href="${esc(href)}" target="_blank" rel="noopener">${esc(r.name)}</a>`
      : `<span class="trend-name">${esc(r.name)}</span>`;
    return `<li class="trend-item">
      <span class="trend-rank mono">${i + 1}</span>
      <div class="trend-main">
        ${nameEl}
        ${reason ? `<p class="trend-desc">${esc(reason)}</p>` : ""}
        <div class="trend-tags mono">
          <span class="trend-score">📣 ${sources} 个独立来源推荐</span>
          ${r.source_repo ? `<span class="trend-by">${esc(r.source_repo)}</span>` : ""}
        </div>
      </div>
    </li>`;
  }).join("");

  // 主榜为空、只剩单源候选时，先解释口碑榜规则，避免「待证实」孤零零无上下文
  const introHtml = rows.length ? ""
    : `<li class="skill-praise-intro">口碑榜按「多源交叉推荐」收录：需 ≥2 个独立来源（不同站点 / 作者）共同提到才进主榜。当前还没有 skill 达标，以下为单源候选。</li>`;

  const pendingHtml = pending.length
    ? `<li class="skill-pending"><details ${rows.length ? "" : "open"}>
         <summary>待证实 · 仅单一来源提到（${pending.length}）</summary>
         <p class="skill-pending-hint">需第 2 个独立来源交叉印证才会进主榜，借此过滤单篇软文。</p>
         ${pending.map((p) => {
           const reason = praiseReason(p);
           const href = praiseLink(p);
           const nameEl = href
             ? `<a href="${esc(href)}" target="_blank" rel="noopener"><b>${esc(p.name)}</b></a>`
             : `<b>${esc(p.name)}</b>`;
           return `<div class="skill-pending-row">${nameEl}${reason ? ` <span class="mono">${esc(reason)}</span>` : ""}</div>`;
         }).join("")}
       </details></li>`
    : "";

  body.innerHTML = introHtml + mainHtml + pendingHtml;
}

async function refreshSkills() {
  const btn = $("#btnSkillsRefresh");
  if (btn) btn.disabled = true;
  try {
    await api("/api/skills/refresh", { method: "POST", body: { type: state.skillsType || "hot" } });
    await loadSkills();
    toast("热门 Skill 榜已刷新");
  } catch (err) {
    toast(err.message);   // 冷却中(429) 等错误带后端文案
  } finally {
    if (btn) btn.disabled = false;
  }
}

// 三 Tab 切换 + 刷新按钮
$$("#skillsType .sort-btn").forEach((b) =>
  b.addEventListener("click", () => {
    $$("#skillsType .sort-btn").forEach((x) => x.classList.toggle("active", x === b));
    state.skillsType = b.dataset.stype;
    loadSkills();
  })
);
$("#btnSkillsRefresh") && $("#btnSkillsRefresh").addEventListener("click", refreshSkills);

// ---------------- 博客（瀑布流）----------------
// 列表来自后端实时扫描 web/blog/ 的每篇 HTML 头部元数据；点卡片整体跳转到独立 HTML 长文。
async function loadBlog() {
  const grid = $("#blogGrid");
  const empty = $("#blogEmpty");
  const meta = $("#blogMeta");
  if (!grid) return;
  try {
    const posts = await api("/api/blog/list");
    if (meta) meta.textContent = posts.length ? `共 ${posts.length} 篇 · 点封面进入全文` : "";
    empty.classList.toggle("hidden", posts.length > 0);
    if (!posts.length) { grid.innerHTML = ""; return; }
    grid.innerHTML = posts
      .map((p, i) => {
        const cover = p.cover
          ? `<img class="blog-cover" src="${esc(p.cover)}" alt="${esc(p.title)} 封面" loading="lazy">`
          : `<div class="blog-cover blog-cover-ph" data-initial="${esc((p.title || "·").slice(0, 1))}"></div>`;
        const tags = (p.tags || [])
          .map((t) => `<span class="blog-tag">${esc(t)}</span>`)
          .join("");
        // 整张卡片（含封面）即链接，点击进入博客 HTML 全文页
        return `<a class="blog-card" href="${esc(p.url)}" style="animation-delay:${Math.min(i * 40, 400)}ms">
          <div class="blog-cover-wrap">${cover}</div>
          <div class="blog-body">
            <h3 class="blog-title">${esc(p.title)}</h3>
            ${p.description ? `<p class="blog-desc">${esc(p.description)}</p>` : ""}
            <div class="blog-foot mono">
              <span class="blog-date">${esc(p.date)}</span>
              ${tags ? `<span class="blog-tags">${tags}</span>` : ""}
            </div>
          </div>
        </a>`;
      })
      .join("");
  } catch (err) {
    grid.innerHTML = "";
    empty.classList.remove("hidden");
    empty.innerHTML = `<p>加载失败：${esc(err.message)}</p>`;
  }
}
