const $ = (sel) => document.querySelector(sel);

function authToken() {
  const fromUrl = new URLSearchParams(location.search).get("token");
  if (fromUrl) {
    sessionStorage.setItem("code_watch_token", fromUrl);
    return fromUrl;
  }
  return sessionStorage.getItem("code_watch_token") || "";
}

function authHeaders() {
  const t = authToken();
  return t ? { "X-Code-Watch-Token": t } : {};
}

function withToken(url) {
  const t = authToken();
  if (!t) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}token=${encodeURIComponent(t)}`;
}

const state = {
  files: [],
  arch: null,
  selectedPath: null,
  selectedPrompt: null,
  view: "working",
  commitRef: null,
  refreshS: 5,
  timer: null,
  loading: false,
  loggedIn: true,
  workspace: "changes",
  chatLastId: 0,
  chatTimer: null,
  chatMode: "direct",
  chatAgents: [],
};

const MODULE_LABELS = {
  orchestra: "Orchestra",
  history: "History",
  channels: "Channels",
  groupchat: "Groupchat",
  providers: "Providers",
  cli: "CLI",
  agent: "Agent",
  tests: "Tests",
  watch: "Code-watch",
  "nanobot-other": "nanobot 其他",
  other: "其他",
};

function badgeClass(status) {
  if (status.includes("modified")) return "modified";
  if (status.includes("deleted")) return "deleted";
  if (status.includes("untracked") || status.includes("added")) return "added";
  return "changed";
}

function moduleForPath(path) {
  const order = [
    ["nanobot/groupchat/orchestra/", "orchestra"],
    ["nanobot/groupchat/history/", "history"],
    ["nanobot/channels/", "channels"],
    ["nanobot/groupchat/", "groupchat"],
    ["nanobot/providers/", "providers"],
    ["nanobot/cli/", "cli"],
    ["nanobot/agent/", "agent"],
    ["tests/", "tests"],
    ["scripts/code-watch/", "watch"],
  ];
  for (const [prefix, id] of order) {
    if (path.startsWith(prefix)) return id;
  }
  if (path.startsWith("nanobot/")) return "nanobot-other";
  return "other";
}

function colorizeDiff(text) {
  return text.split("\n").map((line) => {
    let cls = "";
    if (line.startsWith("+++") || line.startsWith("---")) cls = "meta";
    else if (line.startsWith("@@")) cls = "hunk";
    else if (line.startsWith("+")) cls = "add";
    else if (line.startsWith("-")) cls = "del";
    const esc = line
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
    return `<span class="diff-line ${cls}">${esc || " "}</span>`;
  }).join("\n");
}

async function fetchJson(url, options = {}) {
  const res = await fetch(withToken(url), {
    cache: "no-store",
    credentials: "include",
    headers: { ...authHeaders(), ...(options.headers || {}) },
    ...options,
  });
  if (res.status === 401) {
    showLogin();
    throw new Error("需要登录");
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || res.statusText);
  }
  return res.json();
}

async function fetchText(url) {
  const res = await fetch(withToken(url), {
    cache: "no-store",
    credentials: "include",
    headers: authHeaders(),
  });
  if (res.status === 401) {
    showLogin();
    throw new Error("需要登录");
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || res.statusText);
  }
  return res.text();
}

function showLogin() {
  state.loggedIn = false;
  clearInterval(state.timer);
  $("#login-overlay").classList.remove("hidden");
}

function hideLogin() {
  state.loggedIn = true;
  $("#login-overlay").classList.add("hidden");
  $("#login-error").classList.add("hidden");
}

function setAppView(view) {
  const split = $("#app-split");
  split.classList.remove("view-code", "view-chat");
  split.classList.add(view === "code" ? "view-code" : "view-chat");
  document.querySelectorAll(".view-tab").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.view === view);
  });
}

function setupViewTabs() {
  document.querySelectorAll(".view-tab").forEach((btn) => {
    btn.addEventListener("click", () => setAppView(btn.dataset.view));
  });
  setAppView("chat");
}

function setupLogin() {
  $("#login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const password = $("#login-password").value;
    try {
      const res = await fetch("/api/login", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      });
      if (!res.ok) {
        $("#login-error").classList.remove("hidden");
        return;
      }
      hideLogin();
      $("#login-password").value = "";
      setupAutoRefresh();
      await syncChatCursor();
      await loadChatStatus();
      await loadAll();
    } catch (_) {
      $("#login-error").classList.remove("hidden");
    }
  });
}

function escHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function renderHeadless(rt) {
  const gw = rt.gateway || {};
  const hl = rt.headless || {};
  const modeLabel = gw.mode === "background" ? "后台无头" : gw.mode === "foreground" ? "前台附着" : "已停止";
  const statusCls = gw.running ? "on" : "off";
  const statusText = gw.running ? "运行中" : "未运行";

  $("#headless-status-card").innerHTML = `
    <h3>进程状态</h3>
    <div class="headless-stat">状态: <strong class="${statusCls}">${statusText}</strong></div>
    <div class="headless-stat">模式: <strong>${modeLabel}</strong></div>
    <div class="headless-stat">PID: <strong>${gw.pid || "—"}</strong></div>
    <div class="headless-stat">Detached: <strong>${gw.detached ? "是" : "否"}</strong></div>
    <div class="headless-stat" style="margin-top:0.5rem;font-family:var(--mono);font-size:0.68rem;">
      ${hl.cli || "nanobot gateway"}
    </div>
  `;

  const logTail = (gw.log_tail || []).join("\n") || "（无日志）";
  $("#gateway-log-tail").textContent = logTail;
}

function renderActivity(activity) {
  const feed = $("#activity-feed");
  const events = activity?.events || [];
  if (!events.length) {
    feed.innerHTML = '<li class="empty">暂无 room 事件</li>';
    return;
  }
  feed.innerHTML = [...events].reverse().map((ev) => {
    const ts = (ev.ts || "").replace("T", " ").slice(0, 19);
    const kind = ev.kind || "?";
    const agent = ev.agent ? ` · ${ev.agent}` : "";
    const body = ev.content || (ev.extra?.tool ? `tool: ${ev.extra.tool}` : "") || "";
    return `<li class="activity-item">
      <div class="meta">${ts} · ${kind}${agent}</div>
      <div class="body">${escHtml(body).slice(0, 200)}</div>
    </li>`;
  }).join("");
}

function renderAgents(agents) {
  const grid = $("#agent-grid");
  if (!agents?.length) {
    grid.innerHTML = '<div class="empty">~/.nanobot/agents 下无 agent</div>';
    return;
  }
  grid.innerHTML = agents.map((a) => {
    const live = a.active ? '<span class="badge-live">ACTIVE</span>' : "";
    const model = a.model ? `<div class="model">${escHtml(a.model)}${a.rank ? ` · ${a.rank}` : ""}</div>` : "";
    const soul = a.soul_preview || "（无 SOUL.md）";
    const firstFile = a.files?.[0];
    const dataPath = firstFile ? `data-path="${escHtml(firstFile.path)}"` : "";
    return `<div class="agent-card ${a.active ? "active" : ""}" data-agent="${escHtml(a.name)}" ${dataPath}>
      <div class="name">${escHtml(a.name)} ${live}</div>
      ${model}
      <div class="soul">${escHtml(soul)}</div>
    </div>`;
  }).join("");

  grid.querySelectorAll(".agent-card").forEach((card) => {
    card.addEventListener("click", () => {
      const agent = card.dataset.agent;
      const agentData = agents.find((a) => a.name === agent);
      if (!agentData?.files?.length) return;
      const soul = agentData.files.find((f) => f.filename === "SOUL.md") || agentData.files[0];
      selectPromptPreview(soul.path, `${agent} / ${soul.filename}`);
    });
  });
}

function renderPromptStack(stack, mode) {
  const hint = $("#prompt-mode-hint");
  const skipped = stack?.group_only_skipped || 0;
  hint.textContent = mode === "broadcast"
    ? `群聊模式 · ${stack?.active_count || 0} 个组件全部启用`
    : `1v1 模式 · 跳过 ${skipped} 个群聊专用组件`;

  const el = $("#prompt-stack");
  const comps = stack?.components || [];
  el.innerHTML = comps.map((c) => {
    const cls = [
      "prompt-chip",
      c.active ? "" : "inactive",
      state.selectedPrompt === c.id ? "selected" : "",
    ].filter(Boolean).join(" ");
    const phaseCls = c.phase === "dynamic" ? "phase-dynamic" : "phase-static";
    return `<div class="${cls}" data-id="${c.id}" data-path="${escHtml(c.source_path || "")}">
      <span class="label">${escHtml(c.label)}</span>
      <span class="meta ${phaseCls}">${c.phase}${c.group_only ? " · 群聊" : ""}</span>
    </div>`;
  }).join("");

  el.querySelectorAll(".prompt-chip").forEach((chip) => {
    chip.addEventListener("click", async () => {
      state.selectedPrompt = chip.dataset.id;
      renderPromptStack(stack, mode);
      const path = chip.dataset.path;
      if (path) {
        await selectPromptPreview(path, chip.querySelector(".label").textContent);
      } else {
        $("#prompt-preview-title").textContent = `${chip.querySelector(".label").textContent}（无静态文件，运行时生成）`;
        $("#prompt-preview").textContent = "该组件由 PromptBuilder 动态注入，无独立 .md 文件。";
      }
    });
  });
}

async function selectPromptPreview(path, title) {
  $("#prompt-preview-title").textContent = title;
  $("#prompt-preview").textContent = "加载中…";
  try {
    const text = await fetchText(`/api/prompt?path=${encodeURIComponent(path)}`);
    $("#prompt-preview").textContent = text;
  } catch (e) {
    $("#prompt-preview").textContent = `加载失败: ${e.message}`;
  }
}

function renderRuntimeDetail(arch) {
  const agents = arch.agents || [];
  const stack = arch.prompt_stack || {};
  const activity = arch.activity || {};

  $("#runtime-detail-agents").innerHTML = `
    <h3>Agent (${agents.length})</h3>
    ${agents.map((a) => `<div style="margin:0.3rem 0">${a.active ? "🟢" : "⚪"} <strong>${escHtml(a.name)}</strong> <span style="color:var(--muted);font-size:0.75rem">${escHtml(a.model || "")}</span></div>`).join("")}
  `;

  $("#runtime-detail-prompt").innerHTML = `
    <h3>提示词栈</h3>
    <div style="color:var(--muted);font-size:0.78rem;margin-bottom:0.4rem">活跃 ${stack.active_count || 0} · 跳过群聊组件 ${stack.group_only_skipped || 0}</div>
    ${(stack.components || []).filter((c) => c.active).slice(0, 8).map((c) => `<div style="font-size:0.75rem;margin:0.15rem 0">• ${escHtml(c.label)} <span style="color:var(--muted)">(${c.phase})</span></div>`).join("")}
  `;

  const kinds = Object.entries(activity.kinds || {}).map(([k, v]) => `${k}:${v}`).join(" · ");
  $("#runtime-detail-activity").innerHTML = `
    <h3>最近活动 (${activity.event_count || 0})</h3>
    <div style="color:var(--muted);font-size:0.72rem;margin-bottom:0.5rem">${kinds || "无事件"}</div>
    <ul class="activity-feed" style="max-height:240px">${(activity.events || []).slice(-12).reverse().map((ev) => {
      const ts = (ev.ts || "").slice(11, 19);
      return `<li class="activity-item"><div class="meta">${ts} ${ev.kind}${ev.agent ? ` · ${ev.agent}` : ""}</div><div class="body">${escHtml((ev.content || ev.extra?.tool || "").slice(0, 120))}</div></li>`;
    }).join("")}</ul>
  `;
}

function renderRuntime(rt) {
  const pill = $("#mode-pill");
  pill.textContent = rt.route_label || "—";
  pill.className = `mode-pill ${rt.mode || "idle"}`;

  const agents = rt.active_agents || [];
  $("#active-agents").innerHTML = agents.length
    ? `活跃 agent: <strong>${agents.join("</strong>, <strong>")}</strong>`
    : "无活跃 agent — 用 /addagent 加入";

  $("#route-hint").textContent = rt.route
    ? `当前路径: engine.inject() → ${rt.route}`
    : "engine.inject() 等待 agent";

  const gw = rt.gateway || {};
  const dot = $("#live-dot");
  if (gw.running) {
    dot.classList.remove("offline");
    $("#gateway-status").innerHTML = `Gateway <strong style="color:var(--green)">运行中</strong> pid ${gw.pid}`;
  } else {
    dot.classList.add("offline");
    $("#gateway-status").innerHTML = `Gateway <strong style="color:var(--red)">未运行</strong>`;
  }
}

function renderFlow(arch) {
  const flow = arch.flow;
  const route = arch.active_route;
  const rt = arch.runtime || {};
  const nodes = {};
  flow.nodes.forEach((n) => { nodes[n.id] = n; });

  const mainChain = ["channels", "manager", "engine"];
  let html = "";

  mainChain.forEach((id, i) => {
    const n = nodes[id];
    const active = id === "engine" || (rt.gateway && rt.gateway.running);
    html += flowNodeHtml(n, active, false);
    if (i < mainChain.length - 1) html += '<span class="flow-arrow">→</span>';
  });

  html += '<span class="flow-arrow">→</span><div class="flow-fork">';
  ["direct", "broadcast"].forEach((id) => {
    const n = nodes[id];
    const routeActive = route === "direct_chat" && id === "direct"
      || route === "broadcast_round" && id === "broadcast";
    html += flowNodeHtml(n, true, routeActive);
  });
  html += "</div>";
  html += '<span class="flow-arrow">→</span>';
  html += flowNodeHtml(nodes.outbound, true, Boolean(route));

  $("#flow-diagram").innerHTML = html;

  const shared = arch.shared || [];
  $("#shared-strip").innerHTML = "共享层: " + shared.map((s) => `<span>${s}</span>`).join("");
}

function flowNodeHtml(n, lit, routeActive) {
  if (!n) return "";
  let cls = "flow-node";
  if (lit) cls += " active";
  if (routeActive) cls += " route-active";
  return `<div class="${cls}">
    <span class="label">${n.label}</span>
    <span class="detail">${n.detail}</span>
  </div>`;
}

function renderModules(arch) {
  const grid = $("#module-grid");
  const mods = (arch.modules || []).filter((m) => m.id !== "nanobot-total");
  grid.innerHTML = mods.map((m) => {
    const ch = m.changed || 0;
    const cls = ch ? "module-card has-changes" : "module-card";
    return `<div class="${cls}" data-module="${m.id}">
      <div class="name">${m.label}</div>
      <div class="role">${m.role}</div>
      <div class="stats">
        <span class="lines">${formatLines(m.lines)} 行</span>
        <span class="changed ${ch ? "" : "zero"}">${ch ? `+${ch} 改动` : "—"}</span>
      </div>
    </div>`;
  }).join("");
}

function formatLines(n) {
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

function renderModuleFileGroups() {
  const groups = {};
  state.files.forEach((f) => {
    const mid = moduleForPath(f.path);
    if (!groups[mid]) groups[mid] = [];
    groups[mid].push(f);
  });

  const list = $("#module-file-groups");
  const order = Object.keys(MODULE_LABELS);
  const ids = [...new Set([...order, ...Object.keys(groups)])];

  if (!state.files.length) {
    list.innerHTML = '<li class="empty" style="padding:0.5rem">工作区干净</li>';
    return;
  }

  list.innerHTML = ids.filter((id) => groups[id]?.length).map((id) => {
    const files = groups[id];
    const label = MODULE_LABELS[id] || id;
    return `<li class="mod-group has-files" data-mod="${id}">
      <div class="mod-group-head"><span>${label}</span><span class="count">${files.length}</span></div>
      <ul class="mod-group-files">
        ${files.map((f) => {
          const sel = f.path === state.selectedPath ? "selected" : "";
          return `<li class="${sel}" data-path="${encodeURIComponent(f.path)}">${f.path}</li>`;
        }).join("")}
      </ul>
    </li>`;
  }).join("");

  list.querySelectorAll(".mod-group-files li").forEach((el) => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      selectFile(decodeURIComponent(el.dataset.path));
    });
  });
}

function selectFile(path) {
  state.view = "working";
  state.commitRef = null;
  state.selectedPath = path;
  setActiveTab("tab-working");
  loadDiff();
  renderFiles();
  renderModuleFileGroups();
}

function renderFiles() {
  const list = $("#file-list");
  if (!state.files.length) {
    list.innerHTML = '<li class="empty">工作区干净 ✓</li>';
    return;
  }
  list.innerHTML = state.files.map((f) => {
    const sel = f.path === state.selectedPath ? "selected" : "";
    return `<li class="file-item ${sel}" data-path="${encodeURIComponent(f.path)}">
      <span class="badge ${badgeClass(f.status)}">${f.status}</span>
      <span>${f.path}</span>
    </li>`;
  }).join("");

  list.querySelectorAll(".file-item").forEach((el) => {
    el.addEventListener("click", () => {
      selectFile(decodeURIComponent(el.dataset.path));
    });
  });
}

function renderCommits(log) {
  const list = $("#commit-list");
  list.innerHTML = log.map((c) => {
    const sel = c.short === state.commitRef ? "selected" : "";
    return `<li class="commit-item ${sel}" data-ref="${c.short}">
      <div class="hash">${c.short}</div>
      <div class="subj">${c.subject}</div>
      <div class="when">${c.author} · ${c.date}</div>
    </li>`;
  }).join("");

  list.querySelectorAll(".commit-item").forEach((el) => {
    el.addEventListener("click", async () => {
      state.view = "commit";
      state.commitRef = el.dataset.ref;
      state.selectedPath = null;
      renderCommits(log);
      await loadCommit();
    });
  });
}

function renderSummary(s) {
  $("#branch").textContent = s.branch;
  $("#head").textContent = s.head;
  $("#dirty").textContent = String(s.dirty_count);
  $("#stat-bar").textContent = s.diff_shortstat || (s.dirty_count ? "有未提交改动" : "无未提交 diff");
}

async function loadArchitecture() {
  const arch = await fetchJson("/api/architecture");
  state.arch = arch;
  renderRuntime(arch.runtime);
  renderHeadless(arch.runtime);
  renderActivity(arch.activity);
  renderAgents(arch.agents);
  renderPromptStack(arch.prompt_stack, arch.runtime?.mode);
  renderFlow(arch);
  renderModules(arch);
  renderRuntimeDetail(arch);
}

async function loadSnapshot() {
  const data = await fetchJson("/api/snapshot");
  state.files = data.files;
  renderSummary(data.summary);
  renderFiles();
  renderModuleFileGroups();
  renderCommits(data.log);

  if (state.workspace === "changes") {
    if (state.view === "commit" && state.commitRef) {
      await loadCommit();
    } else if (state.selectedPath || state.view === "working" || state.view === "staged") {
      await loadDiff();
    } else if (!state.files.length) {
      $("#diff-body").innerHTML = '<div class="empty">当前没有未提交的改动</div>';
      $("#diff-title").textContent = "";
    }
  }
}

async function loadAll() {
  if (state.loading || !state.loggedIn) return;
  state.loading = true;
  $("#refresh-btn").classList.add("pulse");
  try {
    await Promise.all([loadArchitecture(), loadSnapshot()]);
  } catch (e) {
    if (e.message !== "需要登录") {
      $("#diff-body").innerHTML = `<div class="empty">加载失败: ${e.message}</div>`;
    }
  } finally {
    state.loading = false;
    $("#refresh-btn").classList.remove("pulse");
  }
}

async function loadDiff() {
  let url = "/api/diff";
  let title = "全部改动";
  if (state.view === "staged") {
    url += "?staged=1";
    title = "已暂存 (staged)";
  } else if (state.selectedPath) {
    url += `?path=${encodeURIComponent(state.selectedPath)}`;
    title = state.selectedPath;
  }
  $("#diff-title").textContent = title;
  try {
    const text = await fetchText(url);
    if (!text.trim()) {
      $("#diff-body").innerHTML = '<div class="empty">（无 diff）</div>';
      return;
    }
    $("#diff-body").innerHTML = `<pre class="diff">${colorizeDiff(text)}</pre>`;
  } catch (e) {
    if (e.message !== "需要登录") {
      $("#diff-body").innerHTML = `<div class="empty">${e.message}</div>`;
    }
  }
}

async function loadCommit() {
  $("#commit-diff-title").textContent = `commit ${state.commitRef}`;
  try {
    const data = await fetchJson(`/api/commit?ref=${encodeURIComponent(state.commitRef)}`);
    const header = `${data.short} ${data.subject}\nAuthor: ${data.author}\nDate: ${data.date}\n`;
    $("#commit-diff-body").innerHTML = `<pre class="diff">${colorizeDiff(header + "\n" + data.patch)}</pre>`;
  } catch (e) {
    if (e.message !== "需要登录") {
      $("#commit-diff-body").innerHTML = `<div class="empty">${e.message}</div>`;
    }
  }
}

function setupTabs() {
  $("#tab-all").addEventListener("click", () => {
    state.view = "working";
    state.selectedPath = null;
    state.commitRef = null;
    setActiveTab("tab-all");
    loadDiff();
  });
  $("#tab-staged").addEventListener("click", () => {
    state.view = "staged";
    state.selectedPath = null;
    state.commitRef = null;
    setActiveTab("tab-staged");
    loadDiff();
  });
  $("#tab-working").addEventListener("click", () => {
    state.view = "working";
    setActiveTab("tab-working");
    if (state.selectedPath) loadDiff();
  });
}

function setActiveTab(id) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
  $(`#${id}`).classList.add("active");
}

function setupWorkspaceTabs() {
  const panels = {
    changes: "#panel-changes",
    commits: "#panel-commits",
    runtime: "#panel-runtime",
    grok: "#panel-grok",
  };
  const tabs = {
    changes: "#wtab-changes",
    commits: "#wtab-commits",
    runtime: "#wtab-runtime",
    grok: "#wtab-grok",
  };

  Object.entries(tabs).forEach(([key, sel]) => {
    $(sel).addEventListener("click", () => {
      state.workspace = key;
      Object.values(tabs).forEach((s) => $(s).classList.remove("active"));
      $(sel).classList.add("active");
      Object.entries(panels).forEach(([k, p]) => {
        const el = $(p);
        if (k === key) {
          el.classList.remove("hidden");
          if (k === "commits") el.style.display = "grid";
        } else {
          el.classList.add("hidden");
        }
      });
    });
  });
}

function setupAutoRefresh() {
  clearInterval(state.timer);
  const toggle = $("#auto-refresh");
  const run = () => {
    if (toggle.checked && state.loggedIn) loadAll();
  };
  if (toggle.checked && state.loggedIn) {
    state.timer = setInterval(run, state.refreshS * 1000);
  }
}

function isCommandReply(text) {
  const t = String(text || "");
  return /^🎭 Agents|^🐈 nanobot commands|^📋 Agent|^⚙️ |^📊 LLM|^🏢 /m.test(t);
}

function chatRouteLabel() {
  const agents = state.chatAgents || [];
  const n = agents.length;
  if (n === 0) return "路由: — · /addagent Harper";
  if (state.chatMode === "direct" || n === 1) {
    const name = agents[0] || "Harper";
    return `路由: direct_chat · 与 ${name} 对话`;
  }
  return `路由: broadcast_round · 群聊 (${n})`;
}

function renderChatBubble(ev) {
  let role = ev.role || (ev.type === "error" ? "error" : "system");
  if (ev.progress && !state.chatShowProgress) return "";
  const raw = ev.content || "";
  if (role === "agent" && isCommandReply(raw)) role = "command";
  let who = "";
  if (role === "agent") {
    const direct = state.chatMode === "direct" || (state.chatAgents?.length === 1);
    const name = direct
      ? (state.chatAgents?.[0] || ev.agent || "Harper")
      : ev.agent;
    if (name) who = `<div class="who">${escHtml(name)}</div>`;
  }
  const content = escHtml(raw);
  if (!content && ev.type !== "connected") return "";
  return `<li class="chat-bubble ${role}${ev.progress ? " progress" : ""}">${who}${content}</li>`;
}

function scrollChatToBottom() {
  const list = $("#chat-messages");
  requestAnimationFrame(() => {
    list.scrollTop = list.scrollHeight;
  });
}

function appendChatEvents(events) {
  const list = $("#chat-messages");
  const html = events.map(renderChatBubble).filter(Boolean).join("");
  if (!html) return;
  list.insertAdjacentHTML("beforeend", html);
  scrollChatToBottom();
}

async function pollChat() {
  if (!state.loggedIn) return;
  try {
    const data = await fetchJson(`/api/chat/events?after=${state.chatLastId}`);
    if (data.events?.length) {
      const maxId = Math.max(...data.events.map((e) => e.id || 0));
      state.chatLastId = Math.max(state.chatLastId, maxId);
      appendChatEvents(data.events);
    }
    const dot = $("#chat-dot");
    if (data.connected) {
      dot.classList.add("on");
      dot.classList.remove("off");
    } else {
      dot.classList.add("off");
      dot.classList.remove("on");
    }
  } catch (_) {
    $("#chat-dot").classList.add("off");
  }
}

async function syncChatCursor() {
  try {
    const data = await fetchJson("/api/chat/events?after=0");
    state.chatLastId = data.latest_id || 0;
  } catch (_) {
    state.chatLastId = 0;
  }
}

async function loadChatStatus() {
  try {
    const st = await fetchJson("/api/chat/status");
    const agents = st.active_agents || [];
    state.chatMode = st.mode || (agents.length === 1 ? "direct" : agents.length > 1 ? "broadcast" : "idle");
    state.chatAgents = agents;

    const title = $("#chat-title");
    if (title) {
      if (state.chatMode === "broadcast" && agents.length > 1) {
        title.textContent = `群聊 (${agents.length})`;
      } else if (agents.length === 1) {
        title.textContent = agents[0];
      } else {
        title.textContent = "Harper";
      }
    }

    $("#chat-agents-bar").textContent = chatRouteLabel();

    const hub = st.hub || {};
    const bridge = st.bridge || {};
    const ready = Boolean(st.ready || hub.connected || bridge.connected);
    const conn = ready ? "已连接" : (hub.last_error || bridge.last_error || "等待 gateway");
    $("#chat-status-line").textContent = agents.length === 1
      ? `${conn} · ${agents[0]} · direct_chat`
      : conn;
    const sendBtn = $("#chat-send-btn");
    sendBtn.disabled = !ready;
    const hint = $("#chat-hint");
    if (!ready) {
      hint.textContent = "gateway 未就绪 — nanobot gateway --foreground";
    } else if (agents.length === 1) {
      hint.textContent = `普通文字 → 与 ${agents[0]} 对话 · /help 查看命令`;
    } else if (agents.length > 1) {
      hint.textContent = "普通文字 → 群聊广播 · /help 查看命令";
    } else {
      hint.textContent = "发送 /addagent Harper 开始对话";
    }
  } catch (_) {
    $("#chat-status-line").textContent = "无法获取对话状态";
  }
}

function setupChat() {
  $("#chat-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = $("#chat-input");
    const content = input.value.trim();
    if (!content) return;
    input.value = "";
    appendChatEvents([{ role: "user", content }]);
    try {
      await fetchJson("/api/chat/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content }),
      });
    } catch (err) {
      appendChatEvents([{ role: "error", content: `发送失败: ${err.message}` }]);
    }
  });

  state.chatShowProgress = false;
  clearInterval(state.chatTimer);
  state.chatTimer = setInterval(() => {
    if (state.loggedIn) {
      pollChat();
      loadChatStatus();
    }
  }, 1500);
}

async function init() {
  setupLogin();
  setupViewTabs();
  setupTabs();
  setupWorkspaceTabs();
  setupChat();
  $("#refresh-btn").addEventListener("click", loadAll);
  $("#auto-refresh").addEventListener("change", setupAutoRefresh);

  try {
    const meta = await fetch("/api/meta", { credentials: "include" }).then((r) => r.json());
    state.refreshS = meta.refresh_hint_s || 5;
    state.auth = meta.auth || "none";
    $("#refresh-label").textContent = `${state.refreshS}s`;

    if (meta.auth === "password" && !meta.logged_in) {
      showLogin();
      return;
    }
    hideLogin();
    setupAutoRefresh();
    setActiveTab("tab-all");
    await syncChatCursor();
    await loadChatStatus();
    await loadAll();
  } catch (_) {
    showLogin();
  }
}

init();