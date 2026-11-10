/* TaskSteward STATUS app: the user's direct actions (DN006 §8.10). Talks only to its own server. */
(() => {
  "use strict";
  const LABELS = {accept: "验收", reject: "退回", cancel: "取消"};

  function start() {
    const meta = document.querySelector('meta[name="tsw-token"]');
    if (!meta) return;
    const token = meta.content;
    for (const box of document.querySelectorAll(".acts")) {
      for (const button of box.querySelectorAll("button[data-act]")) {
        button.addEventListener("click", () => act(box, button, token));
      }
    }
  }

  async function act(box, button, token) {
    const action = button.dataset.act;
    const task = box.dataset.task;
    const revision = Number(box.dataset.revision);
    const result = box.querySelector(".act-result");
    let reason = null;
    if (action === "reject") {
      reason = window.prompt(`退回 ${task}（revision ${revision}）的理由：`);
      if (reason === null) return;
      if (!reason.trim()) { show(result, false, "退回须写明理由"); return; }
    } else if (!window.confirm(`${LABELS[action]} ${task}（revision ${revision}）？`)) {
      return;
    }
    for (const b of box.querySelectorAll("button")) b.disabled = true;
    show(result, null, "正在提交…");
    try {
      const response = await fetch(`/api/tasks/${encodeURIComponent(task)}/${action}`, {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-TSW-Token": token},
        body: JSON.stringify({revision, reason}),
        credentials: "same-origin",
      });
      const body = await response.json().catch(() => ({message: `HTTP ${response.status}`}));
      if (response.ok && body.ok) {
        show(result, true, `已${LABELS[action]}（账本序号 ${body.sequence}），正在刷新…`);
        window.setTimeout(() => window.location.reload(), 1200);
        return;
      }
      show(result, false, body.message || `HTTP ${response.status}`);
    } catch (error) {
      show(result, false, `请求失败：${error}`);
    }
    for (const b of box.querySelectorAll("button")) b.disabled = false;
  }

  function show(node, ok, text) {
    if (!node) return;
    node.textContent = text;
    node.dataset.ok = ok === null ? "" : String(ok);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
