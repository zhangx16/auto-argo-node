const form = document.getElementById("form-pass");
const error = document.getElementById("err");
form.addEventListener("submit", async event => {
  event.preventDefault();
  error.hidden = true;
  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    const fields = new FormData(form);
    const response = await fetch("/api/login", {
      method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({username: fields.get("username"), password: fields.get("password")})
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw Error(data.detail || "登录失败");
    location.href = "/static/index.html";
  } catch (exception) {
    error.textContent = exception.message;
    error.hidden = false;
  } finally {
    button.disabled = false;
  }
});
