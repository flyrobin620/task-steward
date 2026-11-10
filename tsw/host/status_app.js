/* TaskSteward STATUS app: the user's direct actions (DN006 §8.10). Talks only to its own server. */
(() => {
  "use strict";
  const LABELS = {accept: "验收", reject: "退回", cancel: "取消"};

  function start() {
    const meta = document.querySelector('meta[name="tsw-token"]');
    if (!meta) return;
    const token = meta.content;
    // Delegated: the filters (status.js) take cards out of the page and put them back later.
    document.addEventListener("click", event => {
      const target = event.target instanceof Element ? event.target : null;
      const button = target ? target.closest("button[data-act], button[data-reject]") : null;
      const box = button ? button.closest(".acts") : null;
      if (!box || button.disabled) return;
      if (button.dataset.act === "reject") toggleReason(box);
      else if (button.dataset.act) confirmAndSend(box, button.dataset.act, token);
      else if (button.dataset.reject === "send") sendReason(box, token);
      else if (button.dataset.reject === "close") toggleReason(box, false);
    });
  }

  function info(box) {
    return {task: box.dataset.task, revision: Number(box.dataset.revision)};
  }

  /* The reason for a rejection is typed in the card itself and sent with its own button. */
  function toggleReason(box, open) {
    let form = box.querySelector(".reason");
    if (!form) {
      const {task, revision} = info(box);
      form = document.createElement("div");
      form.className = "reason";
      form.hidden = true;
      const label = document.createElement("label");
      label.textContent = `退回 ${task}（revision ${revision}）的理由`;
      const text = document.createElement("textarea");
      text.rows = 3;
      text.placeholder = "写明还缺什么、要改什么；会原样记入账本";
      label.appendChild(text);
      const send = document.createElement("button");
      send.type = "button";
      send.className = "act act-reject-send";
      send.dataset.reject = "send";
      send.textContent = "发送退回";
      const close = document.createElement("button");
      close.type = "button";
      close.className = "act act-cancel";
      close.dataset.reject = "close";
      close.textContent = "收起";
      form.append(label, send, close);
      box.appendChild(form);
    }
    form.hidden = open === undefined ? !form.hidden : !open;
    // An open reason box holds the page's auto-refresh (status.js), so a draft is not lost.
    if (form.hidden) delete box.dataset.holdRefresh;
    else {
      box.dataset.holdRefresh = "";
      form.querySelector("textarea").focus();
    }
  }

  function sendReason(box, token) {
    const text = box.querySelector(".reason textarea");
    const reason = text ? text.value.trim() : "";
    if (!reason) {
      show(box, false, "退回须写明理由");
      if (text) text.focus();
      return;
    }
    send(box, "reject", token, reason);
  }

  function confirmAndSend(box, action, token) {
    const {task, revision} = info(box);
    if (window.confirm(`${LABELS[action]} ${task}（revision ${revision}）？`)) send(box, action, token, null);
  }

  async function send(box, action, token, reason) {
    const {task, revision} = info(box);
    const buttons = box.querySelectorAll("button");
    for (const b of buttons) b.disabled = true;
    box.dataset.holdRefresh = "";
    show(box, null, "正在提交…");
    try {
      const response = await fetch(`/api/tasks/${encodeURIComponent(task)}/${action}`, {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-TSW-Token": token},
        body: JSON.stringify({revision, reason}),
        credentials: "same-origin",
      });
      const body = await response.json().catch(() => ({message: `HTTP ${response.status}`}));
      if (response.ok && body.ok) {
        show(box, true, `已${LABELS[action]}（账本序号 ${body.sequence}），正在刷新…`);
        window.setTimeout(() => window.location.reload(), 1200);
        return;
      }
      show(box, false, body.message || `HTTP ${response.status}`);
    } catch (error) {
      show(box, false, `请求失败：${error}`);
    }
    for (const b of buttons) b.disabled = false;
    const form = box.querySelector(".reason");
    if (!form || form.hidden) delete box.dataset.holdRefresh;
  }

  function show(box, ok, text) {
    const node = box.querySelector(".act-result");
    if (!node) return;
    node.textContent = text;
    node.dataset.ok = ok === null ? "" : String(ok);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
