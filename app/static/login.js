const errEl = document.getElementById("err");
const formPass = document.getElementById("form-pass");
const formTotp = document.getElementById("form-totp");
const formSetup = document.getElementById("form-setup");
const stepHint = document.getElementById("step-hint");

function showErr(msg) {
  errEl.textContent = msg;
  errEl.hidden = !msg;
}

function showStep(name) {
  formPass.hidden = name !== "pass";
  formTotp.hidden = name !== "totp";
  formSetup.hidden = name !== "setup";
}

async function post(path, body) {
  const res = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "请求失败");
  return data;
}

formPass.addEventListener("submit", async (e) => {
  e.preventDefault();
  showErr("");
  const fd = new FormData(formPass);
  try {
    const data = await post("/api/login", {
      username: fd.get("username"),
      password: fd.get("password"),
    });
    if (data.step === "totp") {
      document.getElementById("totp-challenge").value = data.challenge;
      stepHint.textContent = "输入认证器中的 6 位验证码，或一次性备份码。";
      showStep("totp");
      document.getElementById("totp-code").focus();
      return;
    }
    if (data.step === "setup_2fa") {
      document.getElementById("setup-challenge").value = data.challenge;
      document.getElementById("qr").src = data.qr;
      document.getElementById("otpauth-secret").textContent = data.secret;
      stepHint.textContent = "必须绑定认证器后才能进入面板。用 Microsoft Authenticator / Google Authenticator / 1Password 扫描二维码。";
      showStep("setup");
      document.getElementById("setup-code").focus();
      return;
    }
    location.href = "/static/index.html";
  } catch (ex) {
    showErr(ex.message);
  }
});

formTotp.addEventListener("submit", async (e) => {
  e.preventDefault();
  showErr("");
  try {
    await post("/api/login/totp", {
      challenge: document.getElementById("totp-challenge").value,
      code: document.getElementById("totp-code").value,
    });
    location.href = "/static/index.html";
  } catch (ex) {
    showErr(ex.message);
  }
});

formSetup.addEventListener("submit", async (e) => {
  e.preventDefault();
  showErr("");
  try {
    const data = await post("/api/login/setup-2fa", {
      challenge: document.getElementById("setup-challenge").value,
      code: document.getElementById("setup-code").value,
    });
    const box = document.getElementById("backup-box");
    box.hidden = false;
    document.getElementById("backup-codes").textContent = (data.backup_codes || []).join("\n");
    document.getElementById("btn-setup").hidden = true;
    document.getElementById("btn-enter").hidden = false;
    stepHint.textContent = "请立刻把备份码存到密码管理器。每个备份码只能用一次。";
  } catch (ex) {
    showErr(ex.message);
  }
});

document.getElementById("btn-enter").addEventListener("click", () => {
  location.href = "/static/index.html";
});

document.getElementById("btn-copy-backup").addEventListener("click", async () => {
  const text = document.getElementById("backup-codes").textContent;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    prompt("备份码", text);
  }
});
