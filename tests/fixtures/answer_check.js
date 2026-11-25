/* Drives the answer panel of the Dashboard app's page in a real browser (DN006 §8.10, T357) and
   writes what it saw into #check-result. The page is opened from a file, so sending fails like a
   lost connection: the panel must then say the result is unknown and offer a retry. */
(() => {
  "use strict";
  const mode = new URLSearchParams(location.search).get("mode") || "wide";
  const checks = [], sizes = {};
  const check = (name, ok, detail) => checks.push({name, ok: !!ok, detail: detail === undefined ? "" : String(detail)});
  const box = el => el.getBoundingClientRect();
  const shown = el => !!el && !el.hidden && getComputedStyle(el).display !== "none" && box(el).height > 0;
  const click = el => el.dispatchEvent(new MouseEvent("click", {bubbles: true, cancelable: true, view: window}));
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  async function until(test, ms) {
    for (let waited = 0; waited < ms; waited += 50) {
      if (test()) return true;
      await pause(50);
    }
    return test();
  }

  async function board() {
    const card = document.querySelector(".card.qa-card");
    const qa = card && card.querySelector(".qa");
    check("panel: on the waiting card", qa);
    const other = document.querySelector(".card:not(.qa-card)");
    sizes.card = Math.round(box(card).width);
    sizes.other = Math.round(box(other).width);
    if (mode === "wide") check("wide: the waiting card takes two columns", box(card).width > 1.6 * box(other).width, `${sizes.card} / ${sizes.other}`);
    else check("narrow: the waiting card is one column", Math.abs(box(card).width - box(other).width) < 2, `${sizes.card} / ${sizes.other}`);
    check("panel: no question note besides it", !card.querySelector(".note.ask"));
    const opts = [...qa.querySelectorAll(".qa-opt")];
    check("panel: the options are buttons", opts.length === 2 && opts.every(shown), opts.length);
    if (mode === "narrow") check("narrow: the two options share a row", Math.abs(box(opts[0]).top - box(opts[1]).top) < 2);
    check("panel: the output is listed with its role", qa.querySelector(".qa-ctx .out") && qa.querySelector(".qa-ctx").textContent.includes("设计草稿"));
    check("panel: the session and brief links are not repeated", !qa.querySelector("a.session") && !qa.querySelector('a[href$="brief.md"]'));

    // An option: confirm, then back.
    click(opts[0]);
    const step = qa.querySelector(".qa-step");
    check("option: the confirmation shows the words", shown(step) && step.querySelector("blockquote").textContent === opts[0].textContent);
    check("option: the controls step aside", !shown(qa.querySelector(".qa-ans")));
    check("option: the page holds its refresh", document.documentElement.dataset.holdRefresh !== undefined);
    click([...step.querySelectorAll("button")].find(b => b.textContent === "返回修改"));
    check("back: the controls return", shown(qa.querySelector(".qa-ans")) && !shown(step));

    // Own words: the send button wakes up, confirm, send; the file page cannot reach a server.
    const area = qa.querySelector(".qa-free textarea"), send = qa.querySelector(".qa-send");
    check("text: send waits for words", send.disabled);
    area.value = "用方案 B，另外把图补上";
    area.dispatchEvent(new Event("input", {bubbles: true}));
    check("text: send enabled", !send.disabled);
    click(send);
    check("text: the confirmation shows the words", step.querySelector("blockquote").textContent === area.value);
    click([...step.querySelectorAll("button")].find(b => b.textContent === "确认发送"));
    const unknown = await until(() => step.classList.contains("unknown"), 3000);
    check("send: a lost connection is an unknown result", unknown && step.textContent.includes("结果未知"), step.textContent.slice(0, 120));
    check("send: a retry is offered", [...step.querySelectorAll("button")].some(b => b.textContent === "重试"));
    check("send: the words are still shown", step.textContent.includes("用方案 B，另外把图补上"));
  }

  async function flow() {
    // Start confirming on the board, then go on in the task flow's dialog (review F3).
    const card = document.querySelector(".card.qa-card");
    click(card.querySelector(".qa-opt"));
    document.getElementById("view-flow").click();
    await pause(100);
    const node = document.querySelector('.fnode[data-flags~="asking"]');
    check("flow: the waiting card is marked", node && getComputedStyle(node, "::after").content.includes("?"));
    click(node);
    const dialog = document.querySelector("dialog.qa-dlg");
    check("flow: the card opens the panel in a dialog", dialog && dialog.open && dialog.querySelector(".qa .qa-opt"));
    if (dialog) {
      const inside = box(dialog);
      check("flow: the dialog fits the window", inside.left >= 0 && inside.right <= innerWidth + 1, `${Math.round(inside.left)}..${Math.round(inside.right)} / ${innerWidth}`);
      const step = dialog.querySelector(".qa-step");
      check("flow: the confirmation started on the board goes on in the dialog", shown(step) && step.textContent.includes("确认发送"));
      click([...step.querySelectorAll("button")].find(b => b.textContent === "返回修改"));
      check("flow: its buttons work there", shown(dialog.querySelector(".qa-ans")) && !shown(step));
      click(dialog.querySelector(".qa-x"));
      check("flow: ✕ closes the dialog", !dialog.open);
      check("flow: the panel is back on its card", card.querySelector(".qa") && !dialog.querySelector(".qa"));
    }
    document.getElementById("view-status").click();
    await pause(50);
  }

  async function success() {
    // The server's two replies come from this check (the page is a file); the server itself is
    // tested in tests/component/test_dashboard_answer.py and the surface test (review F4).
    const replies = {post: {ok: true, state: "sent"}, get: {ok: true, state: "registered", message_id: "M-T001-1"}};
    window.fetch = async (url, init) => new Response(JSON.stringify(init && init.method === "POST" ? replies.post : replies.get), {status: 200, headers: {"Content-Type": "application/json"}});
    const qa = document.querySelector(".card.qa-card .qa");
    const area = qa.querySelector(".qa-free textarea");
    area.value = "就用方案 A";
    area.dispatchEvent(new Event("input", {bubbles: true}));
    click(qa.querySelector(".qa-send"));
    click([...qa.querySelectorAll(".qa-step button")].find(b => b.textContent === "确认发送"));
    const step = qa.querySelector(".qa-step");
    const done = await until(() => step.textContent.includes("已登记答复") && step.querySelector("li.on:nth-child(3)"), 4000);
    check("success: sent, then registered", done, step.textContent.slice(0, 120));
    check("success: the sent words are no draft any more", area.value === "");
    const released = await until(() => document.documentElement.dataset.holdRefresh === undefined, 1000);
    check("success: auto-refresh is no longer held", released);
  }

  async function run() {
    try {
      sizes.viewport = `${innerWidth}x${innerHeight}`;
      check(`mode ${mode} matches the window`, (mode === "narrow") === matchMedia("(max-width:640px)").matches, innerWidth);
      await board();
      await flow();
      await success();
    } catch (error) {
      check("no error", false, `${error && error.stack || error}`);
    }
    const out = document.createElement("pre");
    out.id = "check-result";
    out.textContent = JSON.stringify({checks, sizes});
    document.body.append(out);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => setTimeout(run, 0));
  else setTimeout(run, 0);
})();
