// 公共工具：API、toast、SSE 自动刷新、导航高亮
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json" },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `请求失败 ${res.status}`);
  return data;
}

function toast(msg, kind = "info") {
  let wrap = $("#toast-wrap");
  if (!wrap) {
    wrap = document.createElement("div");
    wrap.id = "toast-wrap";
    document.body.appendChild(wrap);
  }
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  wrap.appendChild(el);
  setTimeout(() => el.remove(), 3600);
}

const STATUS_CN = {
  WAITING: ["候船中", "lake"], CALLED: ["叫号中", "vest"], MISSED: ["过号", "danger"],
  READY: ["待登船", "lake"], BOARDING: ["已放行", "ok"], ON_WATER: ["出航中", "lake"],
  RETURNED: ["已回港", "lake"], VEST_RETURNED: ["救生衣已还", "ok"],
  SETTLED: ["已结算", "gray"], CANCELLED: ["已取消", "gray"],
  CREATED: ["已组批", "lake"], BOARDING_B: ["登船中", "vest"],
  DEPARTED: ["出航中", "lake"], SUSPENDED: ["停航中止", "danger"], COMPLETED: ["已完成", "gray"],
  DOCKED: ["停泊", "lake"], LOCKED: ["已锁定", "warn"], OUT: ["出航中", "lake"],
  INSPECTING: ["检查中", "warn"], OVERDUE: ["超时", "danger"],
};
function badge(status) {
  const [text, cls] = STATUS_CN[status] || [status, "gray"];
  return `<span class="badge ${cls}">${text}</span>`;
}
const TYPE_CN = { PEDAL: "脚踏船", ELECTRIC: "电动船", PADDLE: "手划船" };
function yuan(fen) { return `¥${(fen / 100).toFixed(2)}`; }
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function startClock() {
  const el = $("#clock");
  if (!el) return;
  const tick = () => {
    el.textContent = new Date().toLocaleString("zh-CN", { hour12: false });
  };
  tick();
  setInterval(tick, 1000);
}

// SSE：任意页面数据变化后自动重新渲染
function liveRefresh(render) {
  let timer;
  const schedule = () => {
    clearTimeout(timer);
    timer = setTimeout(() => { Promise.resolve(render()).catch(() => {}); }, 60);
  };
  try {
    const es = new EventSource("/api/events");
    es.addEventListener("update", schedule);
    es.onerror = () => { /* 浏览器自动重连 */ };
  } catch {}
  // 兜底轮询（每 15 秒）
  setInterval(schedule, 15000);
}

function nav(active) {
  const links = [
    ["/screen", "候船叫号屏"], ["/workbench", "组批工作台"],
    ["/boarding", "登船核验"], ["/lake", "湖面计时"],
    ["/dock", "回港检查"], ["/deposit", "押金复核"],
  ];
  return `<div class="topbar">
    <div class="brand">🚣 湖影公园游船</div>
    <nav>${links.map(([h, t]) =>
      `<a href="${h}" class="${h === active ? "active" : ""}">${t}</a>`).join("")}
    </nav>
    <div class="clock" id="clock"></div>
  </div>`;
}
