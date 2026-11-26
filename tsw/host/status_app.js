/* TaskSteward Dashboard app: the user's direct actions (DN006 §8.10). Talks only to its own server. */
(() => {
  "use strict";
  const LABELS = {accept: "验收", reject: "退回", cancel: "取消"};
  // Cards with an unsent reason or a request in flight. They are tracked here rather than found
  // in the page: the filters (status.js) take cards out of the document.
  const pending = new Set();
  const sending = new WeakSet();

  /* While anything is unsent or a document is open, the root carries data-hold-refresh and
     status.js skips reloads. */
  function hold() {
    for (const box of pending) {
      // A rejection's reason, or an answer being written, confirmed or followed (T357).
      const text = box.querySelector(".reason textarea, .qa-free textarea");
      const busy = box.dataset.qaBusy !== undefined;
      if (!sending.has(box) && !busy && !(text && text.value.trim())) pending.delete(box);
    }
    const open = document.documentElement.dataset.docOpen !== undefined;
    if (pending.size || open) document.documentElement.dataset.holdRefresh = "";
    else delete document.documentElement.dataset.holdRefresh;
  }

  function start() {
    const meta = document.querySelector('meta[name="tsw-token"]');
    if (!meta) return;
    const token = meta.content;
    documents();
    preferenceForms(token);
    answers(token);
    newSession(token);
    document.addEventListener("input", event => {
      const box = event.target instanceof Element ? event.target.closest(".acts") : null;
      if (box && event.target.matches(".reason textarea")) { pending.add(box); hold(); }
    });
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

  /* Answering a worker's question on its card (DN006 §8.10 "在 Dashboard 中回答问题", T357):
     pick an option or write, confirm, and the app delivers the words to the worker's session,
     where the hook records them and the worker registers the answer. The app records nothing
     itself; the progress after sending is read from the ledger. */
  function answers(token) {
    const state = new WeakMap();  // .qa -> {words, command, late, timer}
    const POLL_MS = 2000, QUIET_MS = 30000, STOP_MS = 120000;

    function commandId() {
      const random = window.crypto && window.crypto.randomUUID
        ? window.crypto.randomUUID()
        : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
      return `dashboard-answer-${random}`;
    }
    function el(tag, cls, text) {
      const node = document.createElement(tag);
      if (cls) node.className = cls;
      if (text !== undefined) node.textContent = text;
      return node;
    }
    function button(label, action, primary) {
      const b = el("button", primary ? "qa-go" : "qa-back", label);
      b.type = "button";
      b.dataset.qa = action;
      return b;
    }
    function quote(words, cls) {
      return el("blockquote", cls || "", words);
    }
    function info(qa) {
      return {task: qa.dataset.task, request: qa.dataset.request};
    }
    function busy(qa, on) {
      if (on) qa.dataset.qaBusy = ""; else delete qa.dataset.qaBusy;
      pending.add(qa);
      hold();
    }
    function step(qa, cls, ...nodes) {
      const ans = qa.querySelector(".qa-ans");
      const box = qa.querySelector(".qa-step");
      if (!box) return null;
      if (ans) ans.hidden = true;
      box.hidden = false;
      box.className = `qa-step ${cls}`;
      box.replaceChildren(...nodes);
      return box;
    }
    function back(qa) {
      const s = state.get(qa);
      if (s && s.timer) window.clearTimeout(s.timer);
      state.delete(qa);
      const ans = qa.querySelector(".qa-ans");
      const box = qa.querySelector(".qa-step");
      if (box) { box.hidden = true; box.replaceChildren(); }
      if (ans) ans.hidden = false;
      busy(qa, false);
    }
    function confirmStep(qa, words) {
      if (!words) return;
      const {task, request} = info(qa);
      state.set(qa, {words, command: commandId(), late: false});
      busy(qa, true);
      const box = step(
        qa, "confirm",
        el("div", "qa-confirm-t", `把下面这句话发给 ${task} 的 worker，作为你的答复？`),
        quote(words),
        el("div", "qa-fine", `回答 ${request} · 原话发到 worker 会话，与你在那里输入的相同，由 worker 登记`),
        Object.assign(el("div", "qa-btns"), {}),
      );
      const row = box.querySelector(".qa-btns");
      row.append(button("确认发送", "go", true), button("返回修改", "back"));
      row.querySelector("button").focus();
    }
    async function deliver(qa, late) {
      const s = state.get(qa);
      if (!s) return;
      if (late) s.late = true;
      const {task, request} = info(qa);
      step(qa, "sending", Object.assign(el("div", "qa-spin"), {}), quote(s.words));
      qa.querySelector(".qa-spin").append(el("i"), document.createTextNode("正在发送…"));
      let response = null, body = {};
      try {
        response = await fetch(`/api/tasks/${encodeURIComponent(task)}/answer`, {
          method: "POST",
          headers: {"Content-Type": "application/json", "X-TSW-Token": token},
          body: JSON.stringify({request_id: request, text: s.words, command_id: s.command, late: s.late}),
          credentials: "same-origin",
          referrerPolicy: "same-origin",
        });
        body = await response.json().catch(() => ({}));
      } catch (_) {
        response = null;
      }
      if (response && response.ok && body.ok) return follow(qa, body, Date.now());
      const code = body.code || (response ? `http_${response.status}` : "network");
      if (code === "answered") return answered(qa, body.effective || {});
      if (code === "changed" || code === "command_reused") {
        return refused(qa, "没有发出：任务在你打开页面之后有变化", body.message, true);
      }
      if (code === "not_running" || code === "failed") {
        return refused(qa, "没有发出：worker 会话不在运行", `${body.message || ""}。可以在 worker 会话中回答，或在主会话中回答。`, false);
      }
      if (code === "unknown" || code === "network" || (response && response.status >= 500)) return unknown(qa);
      return refused(qa, "没有发出", body.message || `HTTP ${response ? response.status : ""}`, true);
    }
    function follow(qa, body, since) {
      const s = state.get(qa);
      const {task} = info(qa);
      // Sent: the words are in the result now, not a draft that holds the refresh (review F4).
      const draft = qa.querySelector(".qa-free textarea");
      if (draft && draft.value.trim() === s.words.trim()) {
        draft.value = "";
        draft.style.height = "";
        qa.querySelector(".qa-send").disabled = true;
      }
      const lines = el("ul", "qa-progress");
      const box = step(
        qa, "done",
        el("div", "qa-res ok", body.duplicate ? `✓ 之前已发到 ${task} 的 worker 会话` : `✓ 已发到 ${task} 的 worker 会话`),
        quote(s.words),
        lines,
      );
      if (!box) return;
      const render = (progress) => {
        const items = [["sent", "已发出"], ["received", "worker 已收到"], ["registered", "已登记答复"]];
        const reached = {sent: 0, received: 1, registered: 2}[progress.state];
        lines.replaceChildren(...items.map(([key, label], i) => {
          const li = el("li", reached !== undefined && i <= reached ? "on" : "", label);
          if (key === "received" && progress.message_id && i <= reached) li.textContent += `（${progress.message_id}）`;
          return li;
        }));
        const note = box.querySelector(".qa-fine");
        if (note) note.remove();
        let text = "";
        if (progress.state === "registered") text = "worker 已按你的答复继续；app 中的提问框会在它这一轮结束时换掉。";
        else if (progress.late) text = "问题此前已有答复：worker 会按迟到答复处理（你的话优先于主会话代答）。";
        else if (progress.state === "answered") text = "问题已由别处答复，worker 会按迟到答复处理。";
        else if (progress.state === "sent" && (body.unconfirmed || Date.now() - since > QUIET_MS)) {
          text = body.unconfirmed
            ? "上次发送时出错，还没看到 worker 会话收到。为免重复，不会再发同一次答复；打开 worker 会话查看，确实没收到时可以「重新发送」。"
            : "已写入，worker 会话还没有收到：它可能在等待其他确认。不会自动重发，可以打开 worker 会话查看。";
        }
        if (text) box.append(el("div", "qa-fine", text));
        const again = box.querySelector('button[data-qa="resend"]');
        if (progress.state === "sent" && body.unconfirmed && !again) {
          const row = el("div", "qa-btns");
          row.append(button("重新发送", "resend", true));
          box.append(row);
        } else if (progress.state !== "sent" && again) {
          again.parentElement.remove();
        }
      };
      render(body);
      busy(qa, true);
      const poll = async () => {
        let progress = null;
        try {
          const {request} = info(qa);
          const query = `request_id=${encodeURIComponent(request)}&command_id=${encodeURIComponent(s.command)}`;
          const r = await fetch(`/api/tasks/${encodeURIComponent(task)}/answer?${query}`, {
            credentials: "same-origin",
            referrerPolicy: "same-origin",
          });
          progress = await r.json();
        } catch (_) { progress = null; }
        if (progress && progress.ok) render(progress);
        const done = progress && (progress.state === "registered" || progress.late || progress.state === "answered");
        if (done || Date.now() - since > STOP_MS || !progress || !progress.ok) {
          busy(qa, false);  // auto-refresh may show the new state from here on
          return;
        }
        s.timer = window.setTimeout(poll, POLL_MS);
      };
      s.timer = window.setTimeout(poll, POLL_MS);
    }
    function answered(qa, effective) {
      const s = state.get(qa);
      const box = step(
        qa, "warn",
        el("div", "qa-res warn", "这个问题已经有答复，你这次的话没有发出"),
        el("div", "qa-prev", `生效的答复（${effective.by || "其他渠道"}）：${effective.text || "—"}`),
        quote(`你刚写的：${s.words}`, "mine"),
        el("div", "qa-btns"),
        el("div", "qa-fine", "「仍发给 worker」把你的话发到 worker 会话，由它按迟到答复处理：你的话优先于主会话代答，与你此前的答复冲突时它会再问你。"),
      );
      box.querySelector(".qa-btns").append(button("仍发给 worker", "late", true), button("知道了", "back"));
    }
    function refused(qa, head, why, reload) {
      const s = state.get(qa);
      const box = step(
        qa, "unknown",
        el("div", "qa-res err", `✗ ${head}`),
        el("div", "qa-fine", why || ""),
        quote(`你写的（没有发出，可复制）：${s.words}`, "mine"),
        el("div", "qa-btns"),
      );
      const row = box.querySelector(".qa-btns");
      if (reload) row.append(button("刷新", "reload", true));
      row.append(button("返回", "back"));
    }
    function unknown(qa) {
      const s = state.get(qa);
      const box = step(
        qa, "unknown",
        el("div", "qa-res err", "⚠ 结果未知：发送时出错，不知道 worker 会话是否收到"),
        quote(s.words),
        el("div", "qa-fine", "重试会先按账本核对 worker 会话是否已收到同样的话，收到了就不再发。"),
        el("div", "qa-btns"),
      );
      box.querySelector(".qa-btns").append(button("重试", "retry", true), button("刷新页面查看", "reload"));
    }

    document.addEventListener("input", event => {
      const area = event.target instanceof Element ? event.target.closest(".qa-free textarea") : null;
      if (!area) return;
      const qa = area.closest(".qa");
      qa.querySelector(".qa-send").disabled = !area.value.trim();
      area.style.height = "auto";
      area.style.height = `${Math.min(area.scrollHeight, 200)}px`;
      pending.add(qa);
      hold();
    });
    document.addEventListener("keydown", event => {
      const area = event.target instanceof Element ? event.target.closest(".qa-free textarea") : null;
      if (area && event.key === "Enter" && (event.ctrlKey || event.metaKey) && area.value.trim()) {
        event.preventDefault();
        confirmStep(area.closest(".qa"), area.value.trim());
      }
    });
    document.addEventListener("click", event => {
      const target = event.target instanceof Element ? event.target : null;
      const b = target ? target.closest(".qa button") : null;
      if (!b || b.disabled) return;
      const qa = b.closest(".qa");
      if (b.classList.contains("qa-opt")) confirmStep(qa, b.textContent);
      else if (b.classList.contains("qa-send")) confirmStep(qa, qa.querySelector(".qa-free textarea").value.trim());
      else if (b.classList.contains("qa-more")) {
        qa.classList.toggle("open");
        b.textContent = qa.classList.contains("open") ? "收起" : "展开全文";
      } else if (b.dataset.qa === "go") deliver(qa, false);
      else if (b.dataset.qa === "late") deliver(qa, true);
      else if (b.dataset.qa === "retry") deliver(qa, false);
      else if (b.dataset.qa === "resend") {
        // A new answer with the same words: the user saw that the session never got them.
        const s = state.get(qa);
        if (s) { if (s.timer) window.clearTimeout(s.timer); s.command = commandId(); deliver(qa, s.late); }
      }
      else if (b.dataset.qa === "back") back(qa);
      else if (b.dataset.qa === "reload") window.location.reload();
    });

    /* The task flow: a card waiting on a question opens the same panel in a dialog. */
    let dialog = null;
    document.addEventListener("click", event => {
      const target = event.target instanceof Element ? event.target : null;
      const node = target ? target.closest('.fnode[data-flags~="asking"]') : null;
      if (!node || event.defaultPrevented) return;
      const source = document.querySelector(`#task-${CSS.escape(node.dataset.id)} .qa`);
      if (!source) return;
      event.preventDefault();
      if (!dialog) {
        dialog = el("dialog", "qa-dlg");
        dialog.addEventListener("click", e => { if (e.target === dialog) dialog.close(); });
        dialog.addEventListener("close", () => { delete document.documentElement.dataset.docOpen; hold(); });
        document.body.append(dialog);
      }
      const bar = el("div", "qa-dlg-bar");
      const link = el("a", "tid", node.dataset.id);
      link.href = `tasks/${encodeURIComponent(node.dataset.id)}`;
      const title = source.closest(".card") ? source.closest(".card").querySelector("h3") : null;
      const close = button("✕", "close");
      close.className = "qa-x";
      close.setAttribute("aria-label", "关闭");
      close.addEventListener("click", () => dialog.close());
      bar.append(link, el("b", "", title ? title.textContent : ""), close);
      const body = el("div", "qa-dlg-body");
      // The panel itself moves into the dialog and back when it closes: a copy would show its
      // state without it (review F3).
      const place = document.createComment("qa");
      source.replaceWith(place);
      body.append(source);
      dialog.addEventListener("close", () => { place.replaceWith(source); }, {once: true});
      dialog.replaceChildren(bar, body);
      document.documentElement.dataset.docOpen = "";
      hold();
      dialog.showModal();
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
    // Folding the box keeps its draft (and the hold on auto-refresh) until it is sent or cleared.
    if (!form.hidden) form.querySelector("textarea").focus();
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
    sending.add(box);
    pending.add(box);
    hold();
    show(box, null, "正在提交…");
    try {
      const response = await fetch(`/api/tasks/${encodeURIComponent(task)}/${action}`, {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-TSW-Token": token},
        body: JSON.stringify({revision, reason}),
        credentials: "same-origin",
        referrerPolicy: "same-origin",
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
    sending.delete(box);
    hold();
  }

  function show(box, ok, text) {
    const node = box.querySelector(".act-result");
    if (!node) return;
    node.textContent = text;
    node.dataset.ok = ok === null ? "" : String(ok);
  }

  /* The standing-preferences page (DN006 §8.10, T290): the forms are in the page, hidden until
     opened; each one is confirmed and posted to /api/preferences/..., where the app runs the same
     tools as tsw preference record / revoke as the user. Every rule is checked there. */
  function preferenceForms(token) {
    const OTHER = "__other__";
    const sent = new WeakMap();
    function commandId() {
      const random = window.crypto && window.crypto.randomUUID
        ? window.crypto.randomUUID()
        : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
      return `dashboard-pref-${random}`;
    }
    function field(form, name) {
      const node = form.querySelector(`[name="${name}"]`);
      return node ? node.value : "";
    }
    function scopeOf(form) {
      const chosen = field(form, "scope");
      return (chosen === OTHER ? field(form, "scope_other") : chosen).trim();
    }
    function open(box, which) {
      for (const form of box.querySelectorAll(".pref-form")) {
        form.hidden = form.dataset.form !== which ? true : !form.hidden;
        if (!form.hidden) {
          const first = form.querySelector("textarea, input");
          if (first) first.focus();
        }
      }
    }
    function close(box) {
      for (const form of box.querySelectorAll(".pref-form")) {
        if (form.dataset.form !== "record") form.hidden = true;
      }
    }
    function request(box, action) {
      const ref = box.dataset.prefRef || "";
      const form = box.querySelector(`.pref-form[data-form="${action}"]`);
      if (!form) return null;
      if (action === "revoke") {
        const reason = field(form, "reason").trim();
        if (!reason) return {error: "撤销须写明理由"};
        const item = box.closest("article.pref");
        const words = item ? (item.querySelector(".pref-text") || {}).textContent || "" : "";
        return {
          url: `/api/preferences/${encodeURIComponent(ref)}/revoke`,
          body: {reason},
          question: `撤销长期偏好 ${ref}（${box.dataset.prefScope}）？\n\n原文：${words}\n\n理由：${reason}`,
        };
      }
      const scope = scopeOf(form), topic = field(form, "topic").trim(), text = field(form, "text").trim();
      if (!scope || !topic || !text) return {error: "范围、主题与原文都要填写"};
      const body = {scope, topic, text, supersedes: action === "replace" ? [ref] : []};
      let head = `记录长期偏好（${scope}）`;
      if (action === "replace") {
        head = scope === box.dataset.prefScope
          ? `替代 ${ref}：新偏好（${scope}）取代它，${ref} 标为已替代`
          : `在 ${scope} 范围内覆盖 ${ref}（${box.dataset.prefScope}）：只在该范围内优先，${ref} 在其他范围仍有效`;
      }
      return {url: "/api/preferences/record", body, question: `${head}？\n\n主题：${topic}\n\n原文：${text}`};
    }
    async function submit(box, action) {
      const ask = request(box, action);
      if (!ask) return;
      if (ask.error) { show(box, false, ask.error); return; }
      if (!window.confirm(ask.question)) return;
      // A retry of the same content (say, after a lost response) keeps its command_id, so the
      // ledger returns the first result instead of recording it twice (PR #53 review).
      const key = ask.url + "\n" + JSON.stringify(ask.body);
      if (!sent.has(box) || sent.get(box).key !== key) sent.set(box, {key, id: commandId()});
      ask.body.command_id = sent.get(box).id;
      const buttons = box.querySelectorAll("button");
      for (const b of buttons) b.disabled = true;
      show(box, null, "正在提交…");
      try {
        const response = await fetch(ask.url, {
          method: "POST",
          headers: {"Content-Type": "application/json", "X-TSW-Token": token},
          body: JSON.stringify(ask.body),
          credentials: "same-origin",
          referrerPolicy: "same-origin",
        });
        const body = await response.json().catch(() => ({message: `HTTP ${response.status}`}));
        if (response.ok && body.ok) {
          const done = action === "revoke" ? `已撤销 ${body.ref}` : `已记录 ${body.ref}`;
          show(box, true, `${done}（账本序号 ${body.sequence}），正在刷新…`);
          window.setTimeout(() => window.location.reload(), 1200);
          return;
        }
        show(box, false, body.message || `HTTP ${response.status}`);
      } catch (error) {
        show(box, false, `请求失败：${error}`);
      }
      for (const b of buttons) b.disabled = false;
    }
    document.addEventListener("click", event => {
      const target = event.target instanceof Element ? event.target : null;
      const button = target ? target.closest("button[data-pref-open], button[data-pref-send], button[data-pref-close]") : null;
      const box = button ? button.closest(".pref-acts") : null;
      if (!box || button.disabled) return;
      if (button.dataset.prefOpen) open(box, button.dataset.prefOpen);
      else if (button.dataset.prefSend) submit(box, button.dataset.prefSend);
      else close(box);
    });
    // "Other" shows the field for a scope of the user's own.
    const sync = select => {
      const form = select.closest(".pref-form");
      const other = form ? form.querySelector('[name="scope_other"]') : null;
      if (other) other.closest("label").hidden = select.value !== OTHER;
    };
    for (const select of document.querySelectorAll('.pref-form select[name="scope"]')) sync(select);
    document.addEventListener("change", event => {
      const target = event.target instanceof Element ? event.target : null;
      if (target && target.matches('.pref-form select[name="scope"]')) sync(target);
    });
  }

  /* Markdown files the app serves (briefs, reports) open in a dialog on the page, rendered by the
     app (?view=html, tsw.host.markdown), instead of as plain text (DN006 §8.10, T278). */
  function documents() {
    let dialog = null, title = null, raw = null, body = null;
    // The latest request: a response to an older one (closed, or replaced by another document)
    // never lands in the dialog. ``shown`` is the document on display, without its fragment.
    let latest = 0, shown = "";
    // A link to a .md file of this app (its fragment kept): same origin; a modified or middle
    // click keeps the browser's way.
    function documentUrl(link) {
      if (!link || link.target === "_blank" || link.hasAttribute("download")) return null;
      let url;
      try { url = new URL(link.getAttribute("href") || "", link.baseURI); } catch (_) { return null; }
      if (url.origin !== location.origin || !/\.md$/i.test(url.pathname)) return null;
      url.search = "";
      return url;
    }
    // The heading a fragment names: the renderer's ids (md-<slug>) or a bare GitHub slug.
    function reveal(hash) {
      if (!hash || hash === "#") return;
      let name;
      try { name = decodeURIComponent(hash.slice(1)); } catch (_) { return; }
      for (const id of [name, "md-" + name.trim().toLowerCase().replace(/ /g, "-")]) {
        const target = body.querySelector(`[id="${CSS.escape(id)}"]`);
        if (target) { target.scrollIntoView({block: "start"}); return; }
      }
    }
    // Closing, by the button, the backdrop or Esc (the close event), lets the page refresh again.
    function released() {
      latest += 1;
      shown = "";
      delete document.documentElement.dataset.docOpen;
      hold();
    }
    function shut() {
      dialog.close();
      released();
    }
    function build() {
      dialog = document.createElement("dialog");
      dialog.className = "doc";
      dialog.setAttribute("aria-labelledby", "doc-title");
      const bar = document.createElement("div");
      bar.className = "doc-bar";
      title = document.createElement("h2");
      title.id = "doc-title";
      raw = document.createElement("a");
      raw.className = "ghost doc-raw";
      raw.target = "_blank";
      raw.rel = "noopener";
      raw.textContent = "打开原文";
      const close = document.createElement("button");
      close.type = "button";
      close.className = "ghost doc-close";
      close.textContent = "关闭";
      close.setAttribute("aria-label", "关闭");
      close.addEventListener("click", shut);
      bar.append(title, raw, close);
      body = document.createElement("div");
      body.className = "doc-body";
      body.tabIndex = 0;
      dialog.append(bar, body);
      // A click on the backdrop lands on the dialog itself, outside its box.
      dialog.addEventListener("click", event => {
        if (event.target !== dialog) return;
        const box = dialog.getBoundingClientRect();
        const inside = event.clientX >= box.left && event.clientX <= box.right
          && event.clientY >= box.top && event.clientY <= box.bottom;
        if (!inside) shut();
      });
      dialog.addEventListener("close", released);
      body.addEventListener("click", event => {
        const link = event.target instanceof Element ? event.target.closest("a[href]") : null;
        if (!link || event.defaultPrevented || event.button !== 0
          || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        // A link within the document scrolls the dialog; the page's own fragment (the board's
        // filters) is left alone.
        const href = link.getAttribute("href");
        if (href.startsWith("#")) { event.preventDefault(); reveal(href); return; }
        const url = documentUrl(link);
        if (url) { event.preventDefault(); open(url); }
      });
      document.body.append(dialog);
    }
    function relink(base) {
      // The fragment's relative links are relative to the file, not to this page.
      for (const a of body.querySelectorAll("a[href]")) {
        const href = a.getAttribute("href");
        if (href.startsWith("#")) continue;  // within the document: handled on click
        let url;
        try { url = new URL(href, base); } catch (_) { a.removeAttribute("href"); continue; }
        a.href = url.href;
        if (!(url.origin === location.origin && /\.md$/i.test(url.pathname))) {
          a.target = "_blank";
          a.rel = "noopener noreferrer";
        }
      }
    }
    function message(text) {
      const p = document.createElement("p");
      p.className = "none";
      p.textContent = text;
      body.replaceChildren(p);
    }
    async function open(url) {
      if (!dialog) build();
      const address = new URL(url.href);
      address.hash = "";
      if (dialog.open && shown === address.href) { reveal(url.hash); return; }
      const mine = ++latest;
      shown = "";
      const path = decodeURIComponent(url.pathname).split("/").slice(-4).join("/");
      title.textContent = path;
      title.title = decodeURIComponent(url.pathname);
      raw.href = address.href;
      message("正在载入…");
      if (!dialog.open) dialog.showModal();
      document.documentElement.dataset.docOpen = "";
      document.documentElement.dataset.holdRefresh = "";
      const ask = new URL(address.href);
      ask.searchParams.set("view", "html");
      try {
        const response = await fetch(ask.href, {credentials: "same-origin"});
        const html = await response.text();
        // Closed meanwhile (its close event may come later), or another document asked for.
        if (mine !== latest || !dialog.open) return;
        if (!response.ok) throw new Error(`HTTP ${response.status}：${html.slice(0, 200)}`);
        // The app escapes every piece of text; the page's CSP runs no script either way.
        const parsed = new DOMParser().parseFromString(html, "text/html");
        body.replaceChildren(...Array.from(parsed.body.childNodes));
        relink(address.href);
        shown = address.href;
        body.scrollTop = 0;
        reveal(url.hash);
      } catch (error) {
        if (mine === latest && dialog.open) message(`无法载入（${error.message || error}），可以打开原文查看。`);
      }
    }
    document.addEventListener("click", event => {
      if (event.defaultPrevented || event.button !== 0
        || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      const link = event.target instanceof Element ? event.target.closest("a[href]") : null;
      if (!link || (dialog && dialog.contains(link))) return;
      const url = documentUrl(link);
      if (!url) return;
      event.preventDefault();
      open(url);
    });
  }

  /* 「新建会话」 (DN006 §8.10, DN002 §5.9, T363): claim a spare session for a new conversation. */
  function newSession(token) {
    const button = document.getElementById("new-session");
    if (!button) return;
    let dialog = null;
    let body = null;
    let state = null;
    const el = (tag, cls, text) => {
      const node = document.createElement(tag);
      if (cls) node.className = cls;
      if (text !== undefined) node.textContent = text;
      return node;
    };
    function shut() {
      // Not while a request is in flight: its answer belongs to this opening (PR #95).
      if (state && state.sending) return;
      if (dialog && dialog.open) dialog.close();
    }
    function build() {
      dialog = el("dialog", "doc ns");
      dialog.id = "new-session-dialog";
      dialog.setAttribute("aria-labelledby", "ns-title");
      const bar = el("div", "doc-bar");
      const title = el("h2", "", "新建会话");
      title.id = "ns-title";
      const close = el("button", "ghost doc-close", "关闭");
      close.type = "button";
      close.addEventListener("click", shut);
      bar.append(title, close);
      body = el("div", "doc-body ns-body");
      dialog.append(bar, body);
      dialog.addEventListener("close", () => {
        delete document.documentElement.dataset.docOpen;
        hold();
      });
      // Esc while sending would close it under the request (PR #95).
      dialog.addEventListener("cancel", event => {
        if (state && state.sending) event.preventDefault();
      });
      dialog.addEventListener("click", event => {
        if (event.target !== dialog) return;
        const box = dialog.getBoundingClientRect();
        const inside = event.clientX >= box.left && event.clientX <= box.right
          && event.clientY >= box.top && event.clientY <= box.bottom;
        if (!inside && !(state && state.sending)) shut();
      });
      document.body.append(dialog);
    }
    function pool(info) {
      const box = el("div", "ns-pool");
      const free = info.available || [];
      box.append(el("b", "", `备用会话：可用 ${free.length} / 目标 ${info.target}`));
      if (free.length) {
        const list = el("ul");
        for (const s of free) list.append(el("li", "", `${s.spare_id} · 建于 ${s.created_at || "—"}`));
        box.append(list);
      }
      return box;
    }
    function write(info, words) {
      const free = (info.available || []).length;
      const area = el("textarea");
      area.id = "ns-text";
      area.rows = 4;
      area.maxLength = 4000;
      area.placeholder = free ? "第一条消息…（Ctrl+Enter 发送）" : "没有可用的备用会话";
      area.value = words || "";
      area.disabled = !free;
      const hint = el("p", "note", "新对话在自己的空目录中开始；要它在某个仓库中工作时告诉它，它会自己建 worktree 并移过去。");
      const parts = [el("label", "", "第一条消息"), area, hint, pool(info)];
      if (!free) {
        const off = info.enabled === false;
        parts.push(el("p", "note warn", off ? "备用会话池已关闭（scheduling.spare.enabled）。" : "没有可用的备用会话。"));
        if (!off) parts.push(el("p", "note", info.hint || ""));
      }
      const go = el("button", "", "发送并开始");
      go.type = "button";
      go.disabled = !free;
      const cancel = el("button", "ghost", "取消");
      cancel.type = "button";
      cancel.addEventListener("click", shut);
      const next = () => {
        if (!area.value.trim()) { area.focus(); return; }
        confirm(info, area.value);
      };
      go.addEventListener("click", next);
      area.addEventListener("keydown", event => {
        if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); next(); }
      });
      const row = el("div", "ns-actions");
      row.append(cancel, go);
      body.replaceChildren(...parts, row);
      if (free) area.focus();
    }
    function confirm(info, words) {
      const back = el("button", "ghost", "返回修改");
      back.type = "button";
      back.addEventListener("click", () => write(info, words));
      const send = el("button", "", "确认发送");
      send.type = "button";
      send.addEventListener("click", () => post(info, words));
      const row = el("div", "ns-actions");
      row.append(back, send);
      body.replaceChildren(el("p", "", "把这句话发给一个备用会话，开始一个新对话？"), el("div", "ns-quote", words), row);
      send.focus();
    }
    function link(result) {
      const a = el("a", "session", `打开会话 ${result.spare_id}`);
      if (result.href) a.href = result.href;
      a.dataset.workdeckSession = result.session_id;
      a.dataset.tswSession = result.session_id;
      return a;
    }
    function commandId() {
      if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
      return `ns-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
    }
    async function post(info, words) {
      const mine = state;
      if (!mine.command) mine.command = commandId();
      mine.sending = true;
      body.replaceChildren(el("p", "", "正在发送…"));
      let result;
      try {
        const response = await fetch("/api/sessions/new", {
          method: "POST",
          credentials: "same-origin",
          referrerPolicy: "same-origin",
          headers: {"Content-Type": "application/json", "X-TSW-Token": token},
          body: JSON.stringify({text: words, command_id: mine.command}),
        });
        result = await response.json();
      } catch (error) {
        result = {ok: false, state: "unknown", message: String((error && error.message) || error)};
      }
      mine.sending = false;
      if (state !== mine) return;  // the dialog was opened again since: not this answer's
      const close = el("button", "", "关闭");
      close.type = "button";
      close.addEventListener("click", shut);
      const row = el("div", "ns-actions");
      row.append(close);
      if (result.ok) {
        const again = result.duplicate ? "（之前已发出）" : "";
        const where = el("p", "");
        where.append(link(result), document.createTextNode("（经 workdeck 时在门户中打开；会话随后按第一条消息改标题）"));
        body.replaceChildren(el("p", "ok", `✓ 已在 ${result.spare_id} 开始新对话${again}`), where, row);
        return;
      }
      if (result.state === "unknown") {
        const where = el("p", "");
        if (result.session_id) where.append(link(result), document.createTextNode(" 看看是否已开始；不会自动重试。"));
        body.replaceChildren(el("p", "note warn", `结果未知：${result.message || ""}`), where, row);
        return;
      }
      mine.command = null;
      if (result.code === "no_spare" || result.code === "spare_disabled") {
        const fresh = await load();
        if (state !== mine) return;
        write({...fresh, hint: result.hint || fresh.hint}, words);
        body.prepend(el("p", "note warn", result.message || "没有可用的备用会话"));
        return;
      }
      const back = el("button", "ghost", "返回修改");
      back.type = "button";
      back.addEventListener("click", () => write(info, words));
      row.prepend(back);
      body.replaceChildren(el("p", "note warn", `没有发出：${result.message || "出错了"}`), el("div", "ns-quote", words), row);
    }
    async function load() {
      try {
        const response = await fetch("/api/spares", {credentials: "same-origin", referrerPolicy: "same-origin"});
        const info = await response.json();
        if (!response.ok) throw new Error(info.message || `HTTP ${response.status}`);
        return info;
      } catch (error) {
        return {available: [], target: 0, hint: `无法读取备用会话池（${error.message || error}）`};
      }
    }
    button.addEventListener("click", async () => {
      if (!dialog) build();
      if (state && state.sending) { if (!dialog.open) dialog.showModal(); return; }
      const mine = {command: null, sending: false};
      state = mine;
      body.replaceChildren(el("p", "", "正在读取备用会话池…"));
      if (!dialog.open) dialog.showModal();
      document.documentElement.dataset.docOpen = "";
      document.documentElement.dataset.holdRefresh = "";
      const info = await load();
      if (state !== mine) return;
      const count = button.querySelector(".n");
      if (count) count.textContent = String((info.available || []).length);
      write(info, "");
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
