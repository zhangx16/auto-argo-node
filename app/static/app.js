const statusText = {
  pending: "待安装",
  installing: "安装中",
  online: "在线",
  error: "失败",
  uninstalling: "卸载中",
  removed: "已卸载",
};

let nodes = [];
let me = {};

const $ = (id) => document.getElementById(id);

async function api(path, opts = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  if (res.status === 401) {
    location.href = "/static/login.html";
    throw new Error("未登录");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "请求失败");
  return data;
}

function show(id) { $(id).classList.add("show"); }
function hide(id) { $(id).classList.remove("show"); }

function copy(text) {
  navigator.clipboard.writeText(text).then(() => toast("已复制")).catch(() => {
    prompt("复制下面内容", text);
  });
}

function toast(msg) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 1400);
}

function render() {
  const q = ($("q").value || "").trim().toLowerCase();
  const filtered = nodes.filter((n) => {
    const blob = `${n.remark} ${n.host_ip} ${n.node_port} ${n.hostname} ${n.status}`.toLowerCase();
    return blob.includes(q);
  });
  $("c-all").textContent = nodes.length;
  $("c-online").textContent = nodes.filter((n) => n.status === "online").length;
  $("c-pending").textContent = nodes.filter((n) => n.status === "pending" || n.status === "installing").length;
  $("c-error").textContent = nodes.filter((n) => n.status === "error").length;
  $("empty").hidden = filtered.length > 0;
  $("list").innerHTML = filtered.map((n) => card(n)).join("");
  $("list").querySelectorAll("[data-act]").forEach((btn) => {
    btn.addEventListener("click", () => onAction(btn.dataset.act, Number(btn.dataset.id)));
  });
}

function card(n) {
  const st = n.status || "pending";
  const cmd = (st === "uninstalling") ? n.uninstall_cmd : n.install_cmd;
  const cmdLabel = (st === "uninstalling") ? "卸载命令" : "安装命令";
  return `
    <article class="card">
      <div class="card-head">
        <div>
          <h3>${escapeHtml(n.remark)}</h3>
          <div class="meta">
            ${n.host_ip ? escapeHtml(n.host_ip) : "IP 待回传"} · 端口 ${n.node_port} · SNI ${escapeHtml(n.sni || "")}
            ${n.hostname ? " · " + escapeHtml(n.hostname) : ""}
            ${n.os_info ? "<br>" + escapeHtml(n.os_info) : ""}
          </div>
        </div>
        <span class="badge st-${st}">${statusText[st] || st}</span>
      </div>
      ${n.share_link ? `<div class="linkbox">${escapeHtml(n.share_link)}</div>` : ""}
      ${n.last_error ? `<div class="err">${escapeHtml(n.last_error)}</div>` : ""}
      <div class="cmd">${escapeHtml(cmdLabel)}：${escapeHtml(cmd)}</div>
      <div class="actions">
        <button class="btn btn-sm btn-primary" data-act="copy-install" data-id="${n.id}">复制安装命令</button>
        ${n.share_link ? `<button class="btn btn-sm" data-act="copy-link" data-id="${n.id}">复制链接</button>` : ""}
        <button class="btn btn-sm" data-act="logs" data-id="${n.id}">日志</button>
        <button class="btn btn-sm" data-act="edit" data-id="${n.id}">备注 / 补链</button>
        <button class="btn btn-sm" data-act="uninstall" data-id="${n.id}">生成卸载命令</button>
        <button class="btn btn-sm btn-danger" data-act="remove" data-id="${n.id}">仅从面板删除</button>
      </div>
    </article>
  `;
}

function escapeHtml(s) {
  return String(s || "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

async function onAction(act, id) {
  const n = nodes.find((x) => x.id === id);
  if (!n) return;
  if (act === "copy-install") return copy(n.install_cmd);
  if (act === "copy-link") return copy(n.share_link);
  if (act === "logs") {
    const logs = await api(`/api/nodes/${id}/logs`);
    $("logs-title").textContent = `${n.remark} 日志`;
    $("logs-body").textContent = logs.map((l) => `${l.created_at}  ${l.line}`).join("\n") || "暂无日志";
    show("modal-logs");
    return;
  }
  if (act === "edit") {
    const form = $("form-edit");
    form.id.value = n.id;
    form.remark.value = n.remark;
    form.share_link.value = n.share_link || "";
    show("modal-edit");
    return;
  }
  if (act === "uninstall") {
    const data = await api(`/api/nodes/${id}?mode=command`, { method: "DELETE" });
    $("cmd-title").textContent = "在该服务器执行卸载";
    $("cmd-hint").textContent = "root 执行后会删除 singbox-lite 里对应端口的 Reality 节点，并回告面板。";
    $("cmd-text").textContent = data.uninstall_cmd;
    show("modal-cmd");
    await load();
    return;
  }
  if (act === "remove") {
    if (!confirm(`从面板删除「${n.remark}」？远端节点不会被卸载。`)) return;
    await api(`/api/nodes/${id}?mode=panel`, { method: "DELETE" });
    await load();
  }
}

async function load() {
  me = await api("/api/me");
  nodes = await api("/api/nodes");
  render();
}

$("btn-add").onclick = () => show("modal-add");
$("btn-settings").onclick = async () => {
  const s = await api("/api/settings");
  $("set-panel-url").value = s.panel_url;
  $("set-origin-ip").value = s.origin_ip || "";
  $("set-sub-url").value = s.sub_url;
  $("set-2fa-status").textContent = s.totp_enabled
    ? `两步验证已启用，剩余备份码 ${s.backup_remaining} 个`
    : "两步验证未启用（下次登录会强制绑定）";
  show("modal-settings");
};
$("btn-sub").onclick = async () => {
  const s = await api("/api/settings");
  copy(s.sub_url);
};
$("btn-logout").onclick = async () => {
  await api("/api/logout", { method: "POST" });
  location.href = "/static/login.html";
};
$("q").oninput = render;
$("btn-copy-cmd").onclick = () => copy($("cmd-text").textContent);

document.querySelectorAll("[data-close]").forEach((btn) => {
  btn.addEventListener("click", () => hide(btn.dataset.close));
});

$("form-add").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("add-err");
  err.hidden = true;
  const fd = new FormData(e.target);
  try {
    const node = await api("/api/nodes", {
      method: "POST",
      body: JSON.stringify({
        remark: fd.get("remark"),
        node_port: Number(fd.get("node_port")),
        sni: fd.get("sni"),
        client_address: fd.get("client_address") || "",
      }),
    });
    hide("modal-add");
    e.target.reset();
    e.target.sni.value = "www.amd.com";
    $("cmd-title").textContent = "到目标服务器用 root 执行";
    $("cmd-hint").textContent = "命令会绕过 Cloudflare 直连源站，避免节点机 curl TLS 被重置。请用 root 执行。";
    $("cmd-text").textContent = node.install_cmd;
    show("modal-cmd");
    await load();
  } catch (ex) {
    err.textContent = ex.message;
    err.hidden = false;
  }
});

$("form-edit").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  await api(`/api/nodes/${fd.get("id")}`, {
    method: "PATCH",
    body: JSON.stringify({
      remark: fd.get("remark"),
      share_link: fd.get("share_link"),
    }),
  });
  hide("modal-edit");
  await load();
});

$("form-settings").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const body = { panel_url: fd.get("panel_url"), origin_ip: fd.get("origin_ip") };
  if (fd.get("admin_password")) {
    body.admin_password = fd.get("admin_password");
    body.current_password = fd.get("current_password");
    body.totp_code = fd.get("totp_code");
  }
  await api("/api/settings", { method: "PUT", body: JSON.stringify(body) });
  hide("modal-settings");
  toast("已保存");
  await load();
});

$("btn-backup").onclick = async () => {
  const totp = $("form-settings").totp_code.value;
  if (!totp) {
    toast("请先填写当前两步验证码");
    return;
  }
  const data = await api("/api/2fa/backup-codes", {
    method: "POST",
    body: JSON.stringify({ code: totp }),
  });
  alert("新的备份码（只显示一次）：\n\n" + (data.backup_codes || []).join("\n"));
};

load().catch((e) => {
  if (String(e.message) !== "未登录") alert(e.message);
});
setInterval(() => { load().catch(() => {}); }, 8000);
