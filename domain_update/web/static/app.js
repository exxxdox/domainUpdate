"use strict";

/**
 * 控制台前端。全部数据来自同源的 /api/*，页面不持有任何密钥：
 * 服务端只回传“是否已保存”，密码框永远为空，留空即保留原值。
 */

// 表单字段名与 API 字段名保持一致，读写同源，避免单边改名。
const TEXT_FIELDS = [
  "cloudflare_zone_id",
  "cloudflare_record_name",
  "alibaba_access_key_id",
  "alibaba_record_id",
  "alibaba_rr",
  "gotify_address",
];
const SECRET_FIELDS = [
  "cloudflare_token",
  "alibaba_access_key_secret",
  "gotify_token",
];

/** 取元素；缺失代表 HTML 与脚本不同步，直接抛错比静默失败好排查。 */
function el(id) {
  const node = document.getElementById(id);
  if (node === null) {
    throw new Error(`缺少元素 #${id}`);
  }
  return node;
}

function showNotice(node, message, kind) {
  node.textContent = message;
  node.classList.remove("is-error", "is-success");
  if (kind === "error") {
    node.classList.add("is-error");
  } else if (kind === "success") {
    node.classList.add("is-success");
  }
  node.hidden = !message;
}

function clearNotice(node) {
  showNotice(node, "", "");
}

/**
 * 统一请求封装：返回后端的 {ok, message, data} 信封。
 * message 由服务端提供（例如“已保存，但定时检查同步失败”），前端不再自己编提示文案。
 */
async function request(method, path, body) {
  const options = { method, headers: {} };
  if (method !== "GET") {
    // 服务端要求所有写操作显式声明 JSON，这是它拒绝跨站表单提交（CSRF）的依据。
    // 反过来，前端不能在无请求体时省略这个头：动作接口本就不需要参数，
    // 若按"有 body 才加头"处理，请求会被自己的服务端用 415 挡下来。
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body === undefined ? {} : body);
  }
  const response = await fetch(path, options);
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
        : `请求失败（HTTP ${response.status}）`;
    throw new Error(message);
  }
  return payload;
}

/** 服务端只发 ISO 8601，由浏览器按访问者时区渲染。 */
function formatTime(value) {
  if (!value) {
    return "尚未执行";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "尚未执行";
  }
  return date.toLocaleString("zh-CN", { hour12: false });
}

function setBusy(button, busy, busyLabel) {
  if (busy) {
    button.dataset.label = button.textContent;
    button.textContent = busyLabel;
    button.disabled = true;
  } else {
    if (button.dataset.label) {
      button.textContent = button.dataset.label;
    }
    button.disabled = false;
  }
}

/**
 * 最近一次成功读到的配置。清除按钮的可用性由它决定，
 * 而按钮态需要在 setBusy 结束之后再写一次（setBusy 退出时会启用按钮），
 * 那时请求的返回值已经不在作用域里，所以留一份全局记录。
 */
let latestConfig = null;

/** 按钮被禁用时必须在旁边写明原因，否则用户只看到一个点不动的按钮。 */
function renderClearButton(buttonId, stateId, saved, inUse) {
  const button = el(buttonId);
  const state = el(stateId);
  button.disabled = !saved || inUse;
  if (!saved) {
    state.textContent = "尚未保存凭据";
  } else if (inUse) {
    state.textContent = "当前服务商正在使用，需先切换服务商并保存";
  } else {
    state.textContent = "已保存凭据，可清除";
  }
  button.title = state.textContent;
}

/**
 * 用服务端返回的配置刷新全部清除按钮；表单值本身不动。
 * 清除动作成功后按 syncForm=false 刷新，靠这个函数把按钮态补齐，
 * 否则会出现"凭据已经清掉了、按钮却还亮着"的假象。
 *
 * 传 null 时一律禁用：点这三个按钮就是一次不可撤销的删除，
 * 而配置还没读到时页面根本无法说明"到底存没存过凭据"。
 */
function applyClearButtonState(config) {
  const provider = config ? config.provider : "";
  const cloudflareSaved = Boolean(config && config.cloudflare_token_saved);
  const alibabaSaved = Boolean(config && config.alibaba_access_key_secret_saved);
  const gotifySaved = Boolean(config && config.gotify_token_saved);

  renderClearButton(
    "clear-cloudflare-token",
    "cloudflare-token-state",
    cloudflareSaved,
    provider === "cloudflare",
  );
  renderClearButton(
    "clear-alibaba-access-key-secret",
    "alibaba-secret-state",
    alibabaSaved,
    provider === "alibaba",
  );
  // Gotify 是可选通知渠道，没有“正在使用不可清除”的限制，只判断是否存过 Token。
  const gotifyClear = el("clear-gotify");
  gotifyClear.disabled = !gotifySaved;
  gotifyClear.title = gotifySaved ? "清除已保存的 Token" : "尚未保存 Token";
}

function selectedProvider() {
  const checked = document.querySelector('input[name="provider"]:checked');
  return checked ? checked.value : "cloudflare";
}

function selectProvider(provider) {
  for (const radio of document.querySelectorAll('input[name="provider"]')) {
    radio.checked = radio.value === provider;
  }
  el("provider-cloudflare").hidden = provider !== "cloudflare";
  el("provider-alibaba").hidden = provider !== "alibaba";
}

function setActionsEnabled(enabled) {
  for (const id of ["detect-button", "dns-button", "update-button"]) {
    el(id).disabled = !enabled;
  }
}

/** 密钥框只更新提示文案，绝不写入值。 */
function applySecretHint(inputId, saved) {
  const input = el(inputId);
  input.value = "";
  input.placeholder = saved ? "已保存，留空保持不变" : "尚未填写";
}

function fillForm(config) {
  selectProvider(config.provider);
  for (const name of TEXT_FIELDS) {
    const value = config[name];
    el(name).value = value === null || value === undefined ? "" : value;
  }
  el("alibaba_ip_type").value = config.alibaba_ip_type || "AAAA";
  el("schedule_enabled").checked = Boolean(config.schedule_enabled);
  el("schedule_interval_minutes").value = config.schedule_interval_minutes || 10;
  el("schedule_interval_minutes").disabled = !config.schedule_enabled;

  applySecretHint("cloudflare_token", config.cloudflare_token_saved);
  applySecretHint(
    "alibaba_access_key_secret",
    config.alibaba_access_key_secret_saved,
  );
  applySecretHint("gotify_token", config.gotify_token_saved);

  // 上一次的测试结果在重新读取配置后已经过时。
  clearNotice(el("gotify-notice"));
  applyClearButtonState(config);
}

function renderIp(state) {
  el("ip-value").textContent = state.ipv6 || "等待检测";
  el("ip-checked-at").textContent = formatTime(state.ipv6_checked_at);
}

/** 尚无查询结果时用配置里的目标记录名占位，页面不会只剩一排“—”。 */
function configRecordName(config) {
  if (!config) {
    return "";
  }
  return config.provider === "cloudflare"
    ? config.cloudflare_record_name
    : config.alibaba_rr;
}

function renderDns(state, config) {
  const dns = state.dns;
  if (dns) {
    el("dns-provider").textContent = dns.provider_label || dns.provider;
    el("dns-record").textContent = dns.record_name || "—";
    // 空值代表记录尚未创建，与“查询失败”区分开。
    el("dns-value").textContent = dns.value || "未创建";
  } else {
    el("dns-provider").textContent = config
      ? config.provider === "cloudflare"
        ? "Cloudflare"
        : "阿里云"
      : "—";
    el("dns-record").textContent = configRecordName(config) || "—";
    el("dns-value").textContent = "—";
  }
  el("dns-checked-at").textContent = formatTime(state.dns_checked_at);
}

function renderSchedule(state, config) {
  const schedule = state.schedule;
  // 调度器状态不可用时退回配置文件里的开关值，页面仍能正确表达“已启用/已关闭”。
  const enabled = schedule
    ? schedule.running
    : Boolean(config && config.schedule_enabled);
  const interval = schedule
    ? schedule.interval_minutes
    : config
      ? config.schedule_interval_minutes
      : null;
  el("schedule-state").textContent = enabled ? "已启用" : "已关闭";
  el("schedule-interval").textContent =
    enabled && interval ? `${interval} 分钟` : "—";
  el("schedule-next").textContent =
    enabled && schedule ? formatTime(schedule.next_run) : "—";
}

/**
 * 一律用 textContent 写入：记录说明来自服务端，可能包含外部返回的原始文本，
 * 拼接 innerHTML 会把其中的标记当成页面结构执行。
 */
function appendCell(row, text, className) {
  const cell = document.createElement("td");
  cell.textContent = text === null || text === undefined ? "" : String(text);
  if (className) {
    cell.className = className;
  }
  row.appendChild(cell);
}

function renderHistory(state) {
  const history = state.history;
  const summary = history.summary;
  el("history-total").textContent = String(summary.total);
  el("history-succeeded").textContent = String(summary.succeeded);
  el("history-failed").textContent = String(summary.failed);
  el("history-changed").textContent = String(summary.changed);
  el("history-last-change").textContent = formatTime(summary.last_change_at);

  const limits = state.limits;
  el("history-caption").textContent =
    `最近一次检查：${formatTime(summary.last_run_at)}；` +
    `记录最多保留 ${limits.history_max_records} 条，超出后自动丢弃最旧的记录。`;

  showNotice(el("history-error"), history.error || "", "error");

  const body = el("history-body");
  body.replaceChildren();
  for (const row of history.records) {
    const tr = document.createElement("tr");
    appendCell(tr, formatTime(row.time), "");
    appendCell(tr, row.source, "");
    appendCell(tr, row.result, row.result === "成功" ? "is-succeeded" : "is-failed");
    appendCell(tr, row.action, "");
    appendCell(tr, row.ipv6, "");
    appendCell(tr, row.previous_value, "");
    appendCell(tr, row.message, "");
    body.appendChild(tr);
  }

  const hasRecords = history.records.length > 0;
  el("history-table-wrap").hidden = !hasRecords;
  el("history-empty").hidden = hasRecords;
}

/**
 * syncForm 只在首次加载和保存成功后为真：
 * 动作按钮触发的刷新若也回填表单，会把用户尚未保存的编辑内容冲掉。
 */
function applyState(state, syncForm) {
  if (state.config) {
    latestConfig = state.config;
    clearNotice(el("config-notice"));
    setActionsEnabled(true);
    if (syncForm) {
      fillForm(state.config);
    }
  } else {
    setActionsEnabled(false);
    // 配置读不到时必须锁住清除按钮：不知道存没存过凭据，
    // 就不能让一次点击真的删掉它；同时清掉缓存避免 finally 里拿旧配置放行。
    latestConfig = null;
    applyClearButtonState(null);
  }
  if (state.config_error) {
    showNotice(el("config-notice"), `读取配置失败：${state.config_error}`, "error");
  } else if (!state.config) {
    showNotice(
      el("config-notice"),
      "尚未读取到已保存的设置，请在左侧完成首次配置后再执行网络操作。",
      "",
    );
  }
  renderIp(state);
  renderDns(state, state.config);
  renderSchedule(state, state.config);
  renderHistory(state);
}

async function refresh(syncForm) {
  try {
    const envelope = await request("GET", "/api/state");
    applyState(envelope.data, syncForm);
  } catch (error) {
    showNotice(el("action-notice"), `无法读取状态：${error.message}`, "error");
  }
}

async function runAction(button, path, busyLabel) {
  clearNotice(el("action-notice"));
  setBusy(button, true, busyLabel);
  try {
    const envelope = await request("POST", path);
    applyState(envelope.data, false);
    showNotice(el("action-notice"), envelope.message, "success");
  } catch (error) {
    showNotice(el("action-notice"), error.message, "error");
    // 失败也要刷新：定时任务或另一个会话可能已经改过状态。
    refresh(false);
  } finally {
    setBusy(button, false);
  }
}

async function saveSettings(event) {
  event.preventDefault();
  const button = el("save-button");
  setBusy(button, true, "正在保存…");

  const payload = {
    provider: selectedProvider(),
    schedule_enabled: el("schedule_enabled").checked,
    schedule_interval_minutes: Number(el("schedule_interval_minutes").value || 10),
  };
  for (const name of TEXT_FIELDS) {
    payload[name] = el(name).value;
  }
  for (const name of SECRET_FIELDS) {
    payload[name] = el(name).value;
  }

  try {
    // 提示文案用服务端返回的 message：它能区分“已同步”与“同步失败”两种结果。
    const envelope = await request("POST", "/api/config", payload);
    applyState(envelope.data, true);
    showNotice(el("config-notice"), envelope.message, "success");
  } catch (error) {
    showNotice(el("config-notice"), `保存失败：${error.message}`, "error");
  } finally {
    setBusy(button, false);
  }
}

/**
 * 清除某个已保存的密钥。
 * 请求体只带清除标记：这是一次针对“已保存配置”的定向动作，带上表单当前值会顺带
 * 保存用户还没确认的编辑，也会因为改了 provider 而让服务端的必填校验拦下整个请求。
 * 提交后按 syncForm=false 刷新，表单保持原样，只有按钮状态跟着已保存配置更新。
 */
async function clearSecret(button, flag, noticeNode) {
  clearNotice(noticeNode);
  setBusy(button, true, "正在清除…");
  const payload = {};
  payload[flag] = true;
  try {
    const envelope = await request("POST", "/api/config", payload);
    applyState(envelope.data, false);
    showNotice(noticeNode, envelope.message, "success");
  } catch (error) {
    showNotice(noticeNode, `清除失败：${error.message}`, "error");
  } finally {
    setBusy(button, false);
    // 必须在 setBusy 之后写按钮态：setBusy 退出时会启用按钮，
    // 顺序反过来会让刚清空的凭据按钮重新亮起，像是还能再清一次。
    applyClearButtonState(latestConfig);
  }
}

/**
 * 发送 Gotify 测试消息。只带通知相关的两个字段：
 * 其他字段即使有问题也不该拦住一次通知连通性测试。
 */
async function testNotification(button) {
  const notice = el("gotify-notice");
  clearNotice(notice);
  setBusy(button, true, "正在发送…");
  try {
    const envelope = await request("POST", "/api/notify/test", {
      gotify_address: el("gotify_address").value,
      gotify_token: el("gotify_token").value,
    });
    showNotice(notice, envelope.message, "success");
  } catch (error) {
    showNotice(notice, error.message, "error");
  } finally {
    setBusy(button, false);
  }
}

function init() {
  el("settings-form").addEventListener("submit", saveSettings);
  for (const radio of document.querySelectorAll('input[name="provider"]')) {
    radio.addEventListener("change", () => selectProvider(radio.value));
  }
  el("schedule_enabled").addEventListener("change", (event) => {
    el("schedule_interval_minutes").disabled = !event.target.checked;
  });
  el("detect-button").addEventListener("click", () =>
    runAction(el("detect-button"), "/api/ipv6", "正在检测…"),
  );
  el("dns-button").addEventListener("click", () =>
    runAction(el("dns-button"), "/api/dns", "正在查询…"),
  );
  el("update-button").addEventListener("click", () =>
    runAction(el("update-button"), "/api/update", "正在同步…"),
  );
  // 清除按钮就地生效：用户点了按钮就期望凭据被删掉，不该再要求一次“保存设置”。
  el("clear-cloudflare-token").addEventListener("click", () =>
    clearSecret(
      el("clear-cloudflare-token"),
      "clear_cloudflare_token",
      el("config-notice"),
    ),
  );
  el("clear-alibaba-access-key-secret").addEventListener("click", () =>
    clearSecret(
      el("clear-alibaba-access-key-secret"),
      "clear_alibaba_access_key_secret",
      el("config-notice"),
    ),
  );
  el("gotify-test-button").addEventListener("click", () =>
    testNotification(el("gotify-test-button")),
  );
  el("clear-gotify").addEventListener("click", () =>
    clearSecret(el("clear-gotify"), "clear_gotify", el("gotify-notice")),
  );
  // 首次加载还没有配置，先禁用动作按钮，避免点了只拿到错误。
  setActionsEnabled(false);
  refresh(true);
}

init();
