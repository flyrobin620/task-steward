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
      const text = box.querySelector(".reason textarea");
      if (!sending.has(box) && !(text && text.value.trim())) pending.delete(box);
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

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
