const tokenKey = "methane_token";
let token = localStorage.getItem(tokenKey) || "";
let role = localStorage.getItem("methane_role") || "";

const loginBox = document.querySelector("#login");
const navBox = document.querySelector("#nav");
const shiftView = document.querySelector("#view-shift");
const urgentView = document.querySelector("#view-urgent");
const rows = document.querySelector("#rows");
const boardRows = document.querySelector("#board-rows");
const timelineRows = document.querySelector("#timeline-rows");
const live = document.querySelector("#live");
const form = document.querySelector("#form");
const minutesInput = document.querySelector("#minutes");
const saveMinutesBtn = document.querySelector("#save-minutes");
const minutesHint = document.querySelector("#minutes-hint");

const isWriter = () => role === "writer";
// 正在榜上行内填写处置简述时，跳过重绘，避免轮询/推送清掉已输入内容
const typingInBoard = () => boardRows.contains(document.activeElement);

function fmt(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]),
  );
}

function paintReadings(list) {
  rows.innerHTML = list
    .map(
      (r) =>
        `<tr><td>${esc(r.site)}</td><td>${r.ch4_pct}</td>` +
        `<td class="${r.level === "报警" ? "alarm" : "ok"}">${r.level}</td>` +
        `<td>${esc(r.note)}</td><td>${r.confirmed ? '<span class="done">已确认</span>' : ""}</td></tr>`,
    )
    .join("");
}

function paintBoard(list) {
  document.querySelector("#board-empty").hidden = list.length > 0;
  boardRows.innerHTML = list
    .map((r) => {
      const action = isWriter()
        ? `<input class="disp" data-site="${esc(r.site)}" placeholder="处置简述（必填）" maxlength="500" />
           <button data-confirm-site="${esc(r.site)}">确认</button>`
        : '<span class="muted">仅检查员可确认</span>';
      return `<tr>
        <td class="alarm">${esc(r.site)}</td>
        <td>${r.ch4_pct}</td>
        <td>${fmt(r.alarm_at)}</td>
        <td><span class="pending">${fmt(r.urgent_since)}</span></td>
        <td colspan="2">${action}</td>
      </tr>`;
    })
    .join("");
}

function paintTimeline(list) {
  document.querySelector("#timeline-empty").hidden = list.length > 0;
  timelineRows.innerHTML = list
    .map((r) => {
      const pending = r.status !== "确认完毕";
      return `<tr>
        <td>${esc(r.site)}</td>
        <td>${r.ch4_pct}</td>
        <td>${fmt(r.alarm_at)}</td>
        <td>${fmt(r.created_at)}</td>
        <td class="${pending ? "pending" : "done"}">${r.status}</td>
        <td>${esc(r.confirmed_by || "")}</td>
        <td>${fmt(r.confirmed_at)}</td>
        <td>${esc(r.disposition_note || "")}</td>
      </tr>`;
    })
    .join("");
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

async function loadReadings() {
  paintReadings(await api("/api/readings"));
}

async function loadUrgent() {
  const [board, timeline, setting] = await Promise.all([
    api("/api/urgent/board"),
    api("/api/urgent/timeline"),
    api("/api/urgent/settings"),
  ]);
  paintBoard(board);
  paintTimeline(timeline);
  if (document.activeElement !== minutesInput) minutesInput.value = setting.timeout_minutes;
}

function switchTab(tab) {
  const urgent = tab === "urgent";
  document.querySelector("#tab-shift").classList.toggle("active", !urgent);
  document.querySelector("#tab-urgent").classList.toggle("active", urgent);
  shiftView.hidden = urgent;
  urgentView.hidden = !urgent;
  if (urgent) loadUrgent().catch((err) => alert(err.message));
  else loadReadings().catch(() => {});
}

function showApp() {
  loginBox.hidden = true;
  navBox.hidden = false;
  shiftView.hidden = false;
  document.querySelector("#who").textContent = isWriter() ? "检查员" : "查看（旁观）";
  document.querySelector("#out").hidden = false;
  form.hidden = !isWriter();
  // 旁观账号：能看榜和大事记，不能确认，也不能改分钟数
  minutesInput.disabled = !isWriter();
  saveMinutesBtn.hidden = !isWriter();
  minutesHint.hidden = isWriter();
  connect();
  loadReadings();
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/alerts`);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "urgent") {
      if (!typingInBoard()) {
        paintBoard(msg.board);
        paintTimeline(msg.timeline);
      }
    } else {
      live.textContent = `刚推送：${msg.site} ${msg.level}`;
      loadReadings();
      if (!urgentView.hidden) loadUrgent().catch(() => {});
    }
  };
  ws.onclose = () => setTimeout(connect, 3000);
}

document.querySelector("#go").onclick = async () => {
  try {
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
  } catch (err) {
    alert(err.message);
  }
};

form.onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/api/readings", {
      method: "POST",
      body: JSON.stringify({
        site: document.querySelector("#site").value,
        ch4: Number(document.querySelector("#ch4").value),
      }),
    });
    document.querySelector("#site").value = "";
    document.querySelector("#ch4").value = "";
  } catch (err) {
    live.textContent = err.message;
  }
};

saveMinutesBtn.onclick = async () => {
  try {
    const value = Math.max(0, Math.floor(Number(minutesInput.value)));
    if (Number.isNaN(value)) return alert("请输入分钟数");
    await api("/api/urgent/settings", {
      method: "PUT",
      body: JSON.stringify({ timeout_minutes: value }),
    });
    // 改完立刻生效：服务端已即时扫描并推送，这里再拉一次兜底
    await loadUrgent();
  } catch (err) {
    alert(err.message);
  }
};

// 榜上确认：事件委托拿到测点和同一行填写的处置简述
boardRows.addEventListener("click", async (e) => {
  const site = e.target.dataset && e.target.dataset.confirmSite;
  if (!site) return;
  const input = boardRows.querySelector(`input[data-site="${CSS.escape(site)}"]`);
  const note = (input.value || "").trim();
  if (!note) return alert("请填写处置简述");
  try {
    await api("/api/urgent/confirm", {
      method: "POST",
      body: JSON.stringify({ site, disposition_note: note }),
    });
    await loadUrgent();
    loadReadings();
  } catch (err) {
    alert(err.message);
  }
});

document.querySelector("#tab-shift").onclick = () => switchTab("shift");
document.querySelector("#tab-urgent").onclick = () => switchTab("urgent");

document.querySelector("#out").onclick = () => {
  localStorage.clear();
  location.reload();
};

// 兜底轮询：即使丢了 websocket 推送，超时点也会在数秒内进榜
setInterval(() => {
  if (token && !urgentView.hidden && !typingInBoard()) loadUrgent().catch(() => {});
}, 5000);

if (token) showApp();
