/* 登录页脚本。
   刻意不复用 app.js 的 request()：那个函数在收到 401 时会跳转到登录页，而这里 401 的
   含义正是「密码错了」——跳转会让用户看不到任何提示，只看到页面刷新了一下。
   错误文案一律用服务端返回的 message，前端不自己编。 */

function el(id) {
  const node = document.getElementById(id);
  if (!node) {
    throw new Error(`缺少元素 #${id}`);
  }
  return node;
}

function showNotice(node, message, kind) {
  node.textContent = message;
  node.classList.toggle("is-error", kind === "error");
  node.classList.toggle("is-success", kind === "success");
  node.hidden = false;
}

function setBusy(button, busy, busyLabel) {
  if (busy) {
    button.dataset.label = button.textContent;
    button.textContent = busyLabel;
  } else if (button.dataset.label) {
    button.textContent = button.dataset.label;
  }
  button.disabled = busy;
}

async function login(event) {
  event.preventDefault();
  const notice = el("login-notice");
  const button = el("login-button");
  notice.hidden = true;
  setBusy(button, true, "正在登录…");
  try {
    const response = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: el("username").value,
        password: el("password").value,
      }),
    });
    let payload = null;
    try {
      payload = await response.json();
    } catch (error) {
      payload = null;
    }
    if (!response.ok || payload === null || payload.ok !== true) {
      const message =
        payload && payload.message
          ? payload.message
          : `登录失败（HTTP ${response.status}）`;
      showNotice(notice, message, "error");
      return;
    }
    // replace 而不是 assign：用 assign 的话登录成功后按返回键又回到登录页。
    window.location.replace("/");
  } catch (error) {
    // fetch 本身失败（服务没起来、网络断了）不会有响应体，只能在这里兜底。
    showNotice(notice, "无法连接服务，请检查网络后重试", "error");
  } finally {
    setBusy(button, false);
  }
}

el("login-form").addEventListener("submit", login);
