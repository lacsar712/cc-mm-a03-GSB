const tokenKey = "methane_token";
let token = localStorage.getItem(tokenKey) || "";
let role = localStorage.getItem("methane_role") || "";

const loginBox = document.querySelector("#login");
const viewMain = document.querySelector("#view-main");
const viewBoard = document.querySelector("#view-board");
const rows = document.querySelector("#rows");
const live = document.querySelector("#live");
const form = document.querySelector("#form");
const navMain = document.querySelector("#nav-main");
const navBoard = document.querySelector("#nav-board");
const boardRows = document.querySelector("#board-rows");
const eventRows = document.querySelector("#event-rows");
const boardMsg = document.querySelector("#board-msg");
const thForm = document.querySelector("#th-form");
const thInput = document.querySelector("#th-input");
const thLabel = document.querySelector("#th");
const opHead = document.querySelector("#op-head");

function paint(list) {
  rows.innerHTML = list
    .map(
      (r) =>
        `<tr><td>${r.site}</td><td>${r.ch4_pct}</td><td class="${r.level === "报警" ? "alarm" : "ok"}">${r.level}</td><td>${r.note}</td></tr>`,
    )
    .join("");
}

function paintBoard(board) {
  const writer = role === "writer";
  boardRows.innerHTML =
    board
      .map((b) => {
        const op = writer
          ? `<td><input class="disposal" data-site="${b.site}" placeholder="处置简述" /><button class="confirm" data-site="${b.site}">确认</button></td>`
          : "";
        return `<tr><td>${b.site}</td><td>${fmt(b.urged_at)}</td><td>${b.pending_count} 条</td>${op}</tr>`;
      })
      .join("") || `<tr><td colspan="4">榜上无测点</td></tr>`;
  boardRows.querySelectorAll("button.confirm").forEach((btn) => {
    btn.onclick = () => confirmSite(btn.dataset.site);
  });
}

function paintEvents(events) {
  eventRows.innerHTML =
    events
      .map(
        (e) =>
          `<tr><td>${e.site}</td><td>${fmt(e.urged_at)}</td><td class="${e.status === "催办中" ? "alarm" : "ok"}">${e.status}</td><td>${e.disposal || ""}</td><td>${e.confirmed_by || ""}</td><td>${fmt(e.confirmed_at)}</td></tr>`,
      )
      .join("") || `<tr><td colspan="6">暂无大事记</td></tr>`;
}

function fmt(t) {
  return t ? new Date(t).toLocaleString() : "";
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers || {}),
    },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "请求失败");
  return data;
}

function showView(name) {
  viewMain.hidden = name !== "main";
  viewBoard.hidden = name !== "board";
  if (name === "board") loadBoard();
}

function showApp() {
  loginBox.hidden = true;
  document.querySelector("#who").textContent = role === "writer" ? "检查员" : "查看";
  document.querySelector("#out").hidden = false;
  navMain.hidden = false;
  navBoard.hidden = false;
  form.hidden = role !== "writer";
  thForm.hidden = role !== "writer";
  opHead.hidden = role !== "writer";
  showView("main");
  connect();
  load();
}

async function load() {
  paint(await api("/api/readings"));
}

async function loadBoard() {
  boardMsg.textContent = "";
  try {
    const [board, events, th] = await Promise.all([
      api("/api/urges"),
      api("/api/urge-events"),
      api("/api/settings/confirm-threshold"),
    ]);
    thLabel.textContent = th.minutes;
    thInput.value = th.minutes;
    paintBoard(board);
    paintEvents(events);
  } catch (err) {
    boardMsg.textContent = err.message;
  }
}

async function confirmSite(site) {
  const input = boardRows.querySelector(`input.disposal[data-site="${site}"]`);
  const disposal = input ? input.value.trim() : "";
  if (!disposal) {
    boardMsg.textContent = "请先填写处置简述";
    return;
  }
  try {
    await api(`/api/urges/${encodeURIComponent(site)}/confirm`, {
      method: "POST",
      body: JSON.stringify({ disposal }),
    });
    loadBoard();
  } catch (err) {
    boardMsg.textContent = err.message;
  }
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/alerts`);
  ws.onmessage = (ev) => {
    const row = JSON.parse(ev.data);
    live.textContent = `刚推送：${row.site} ${row.level}`;
    load();
    if (!viewBoard.hidden) loadBoard();
  };
}

navMain.onclick = () => showView("main");
navBoard.onclick = () => showView("board");

thForm.onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/api/settings/confirm-threshold", {
      method: "PUT",
      body: JSON.stringify({ minutes: Number(thInput.value) }),
    });
    loadBoard();
  } catch (err) {
    boardMsg.textContent = err.message;
  }
};

document.querySelector("#go").onclick = async () => {
  const data = await api("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({
      username: document.querySelector("#user").value,
      password: document.querySelector("#pass").value,
    }),
  });
  token = data.access_token;
  role = data.role;
  localStorage.setItem(tokenKey, token);
  localStorage.setItem("methane_role", role);
  showApp();
};

form.onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/api/readings", {
      method: "POST",
      body: JSON.stringify({
        site: document.querySelector("#site").value,
        ch4_pct: Number(document.querySelector("#ch4").value),
      }),
    });
  } catch (err) {
    live.textContent = err.message;
  }
};

document.querySelector("#out").onclick = () => {
  localStorage.clear();
  location.reload();
};

if (token) showApp();
