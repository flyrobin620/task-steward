/* TaskSteward STATUS page: filtering, sorting, grouping and folding. No network access. */
(() => {
  "use strict";
  const root = document.documentElement;
  const PREFIX = "tsw-status:";
  const store = {
    get(key) { try { return localStorage.getItem(PREFIX + key); } catch (_) { return null; } },
    set(key, value) { try { localStorage.setItem(PREFIX + key, value); } catch (_) { /* private mode */ } },
  };
  const THEMES = ["auto", "light", "dark"];
  const THEME_LABELS = {auto: "主题：跟随系统", light: "主题：浅色", dark: "主题：深色"};
  function theme(value) {
    if (!THEMES.includes(value)) value = "auto";
    if (value === "auto") delete root.dataset.theme; else root.dataset.theme = value;
    store.set("theme", value);
    const button = document.getElementById("theme");
    if (button) button.textContent = THEME_LABELS[value];
  }
  theme(store.get("theme") || "auto");
  const STYLES = ["plain", "vivid"];
  const STYLE_LABELS = {plain: "风格：朴素", vivid: "风格：鲜艳"};
  function style(value) {
    if (!STYLES.includes(value)) value = "plain";
    if (value === "plain") delete root.dataset.style; else root.dataset.style = value;
    store.set("style", value);
    const button = document.getElementById("style");
    if (button) button.textContent = STYLE_LABELS[value];
  }
  style(store.get("style") || "plain");

  function start() {
    const board = document.getElementById("board");
    const tools = document.getElementById("tools");
    if (!board || !tools) return;
    const $ = id => document.getElementById(id);
    const cards = Array.from(board.querySelectorAll("article.card"));
    const ORDER = board.dataset.order.split(" ");
    const KINDS = board.dataset.kinds.split(" ");
    const statusLabels = Object.create(null), kindLabels = Object.create(null);
    for (const b of tools.querySelectorAll("[data-st]")) statusLabels[b.dataset.st] = b.dataset.label;
    for (const b of tools.querySelectorAll("[data-kind]")) kindLabels[b.dataset.kind] = b.dataset.label;
    const text = new Map(cards.map(c => [c, c.textContent.toLowerCase()]));
    const byId = new Map(cards.map(c => [c.id, c]));
    const q = $("q"), repo = $("repo"), flag = $("flag"), sort = $("sort"), group = $("group");
    const DEFAULT_CLOSED = {status: ["accepted", "cancelled"]};
    let state;

    function parse(raw) {
      const p = new URLSearchParams(raw);
      const list = key => (p.get(key) || "").split(",").filter(Boolean);
      return {q: p.get("q") || "", st: list("st"), kind: list("kind"), repo: p.get("repo") || "",
        flag: p.get("flag") || "", sort: p.get("sort") || "status", group: p.get("group") || "status"};
    }
    function serialize(s) {
      const p = new URLSearchParams();
      if (s.q) p.set("q", s.q);
      if (s.st.length) p.set("st", s.st.join(","));
      if (s.kind.length) p.set("kind", s.kind.join(","));
      if (s.repo) p.set("repo", s.repo);
      if (s.flag) p.set("flag", s.flag);
      if (s.sort !== "status") p.set("sort", s.sort);
      if (s.group !== "status") p.set("group", s.group);
      return p.toString();
    }
    function initial() {
      const raw = location.hash.slice(1);
      return parse(raw.includes("=") ? raw : store.get("filters") || "");
    }
    function save() {
      const raw = serialize(state);
      store.set("filters", raw);
      if (location.hash && !location.hash.includes("=")) return; // keep a task anchor
      try { history.replaceState(null, "", raw ? "#" + raw : location.pathname + location.search); }
      catch (_) { /* some file: contexts refuse */ }
    }
    function closed(mode) {
      try {
        const v = JSON.parse(store.get("closed:" + mode) || "null");
        if (Array.isArray(v)) return new Set(v);
      } catch (_) { /* ignore a damaged entry */ }
      return new Set(Object.hasOwn(DEFAULT_CLOSED, mode) ? DEFAULT_CLOSED[mode] : []);
    }
    function remember(mode, key, open) {
      const set = closed(mode);
      if (open) set.delete(key); else set.add(key);
      store.set("closed:" + mode, JSON.stringify(Array.from(set)));
    }
    function matches(card, s, words) {
      const d = card.dataset;
      if (s.st.length && !s.st.includes(d.status)) return false;
      if (s.kind.length && !s.kind.includes(d.kind)) return false;
      if (s.repo && d.repo !== s.repo) return false;
      if (s.flag && !d.flags.split(" ").includes(s.flag)) return false;
      const t = text.get(card);
      return words.every(w => t.includes(w));
    }
    const rank = card => { const i = ORDER.indexOf(card.dataset.status); return i < 0 ? 99 : i; };
    const seq = card => Number(card.dataset.seq) || 0;
    const act = card => card.dataset.activity || "";
    const SORTS = {
      status: (a, b) => rank(a) - rank(b) || (act(b) > act(a) ? 1 : act(b) < act(a) ? -1 : 0) || seq(b) - seq(a),
      activity: (a, b) => (act(b) > act(a) ? 1 : act(b) < act(a) ? -1 : 0) || seq(b) - seq(a),
      stalest: (a, b) => (act(a) > act(b) ? 1 : act(a) < act(b) ? -1 : 0) || seq(a) - seq(b),
      newest: (a, b) => seq(b) - seq(a),
      oldest: (a, b) => seq(a) - seq(b),
      repo: (a, b) => a.dataset.repo.localeCompare(b.dataset.repo) || rank(a) - rank(b) || seq(b) - seq(a),
    };
    const GROUPS = {
      status: c => c.dataset.status,
      repo: c => c.dataset.repo,
      kind: c => c.dataset.kind,
      none: () => "all",
    };
    function groupOrder(mode, keys) {
      if (mode === "status") return keys.sort((a, b) => ORDER.indexOf(a) - ORDER.indexOf(b));
      if (mode === "kind") return keys.sort((a, b) => KINDS.indexOf(a) - KINDS.indexOf(b));
      return keys.sort();
    }
    function heading(mode, key) {
      if (mode === "status") return statusLabels[key] || key;
      if (mode === "kind") return kindLabels[key] || key;
      if (mode === "none") return "全部任务";
      return key;
    }
    function section(mode, key, items, shut) {
      const details = document.createElement("details");
      details.className = "group";
      if (mode === "status") details.classList.add("st-" + key);
      if (mode === "kind") details.classList.add("k-" + key);
      if (mode === "repo") {
        const sample = items[0].querySelector(".chip.repo");
        if (sample) details.style.setProperty("--c", getComputedStyle(sample).getPropertyValue("--c"));
      }
      details.open = !shut.has(key);
      const summary = document.createElement("summary");
      const name = document.createElement("span");
      name.textContent = heading(mode, key);
      const n = document.createElement("span");
      n.className = "n";
      n.textContent = String(items.length);
      summary.append(name, n);
      const grid = document.createElement("div");
      grid.className = "grid";
      grid.append(...items);
      details.append(summary, grid);
      details.addEventListener("toggle", () => remember(mode, key, details.open));
      return details;
    }
    function normalize() {
      // A restored choice that is no longer on the board (or never was) is dropped before
      // filtering, so the controls, the cards and the count always come from one state.
      const offered = select => Array.from(select.options).map(o => o.value);
      if (!offered(repo).includes(state.repo)) state.repo = "";
      if (!offered(flag).includes(state.flag)) state.flag = "";
      if (!Object.hasOwn(SORTS, state.sort)) state.sort = "status";
      if (!Object.hasOwn(GROUPS, state.group)) state.group = "status";
      state.st = state.st.filter(v => v in statusLabels);
      state.kind = state.kind.filter(v => v in kindLabels);
    }
    function reset() {
      state = {...parse(""), sort: state.sort, group: state.group};
    }
    function render() {
      normalize();
      const words = state.q.toLowerCase().split(/\s+/).filter(Boolean);
      const shown = cards.filter(c => matches(c, state, words)).sort(SORTS[state.sort]);
      const mode = state.group;
      const buckets = new Map();
      for (const card of shown) {
        const key = GROUPS[mode](card);
        if (!buckets.has(key)) buckets.set(key, []);
        buckets.get(key).push(card);
      }
      const shut = closed(mode);
      board.replaceChildren(...groupOrder(mode, Array.from(buckets.keys()))
        .map(key => section(mode, key, buckets.get(key), shut)));
      $("count").textContent = `显示 ${shown.length} / ${cards.length} 个任务`;
      $("empty").hidden = shown.length !== 0 || cards.length === 0;
      for (const b of tools.querySelectorAll("[data-st]")) b.setAttribute("aria-pressed", String(state.st.includes(b.dataset.st)));
      for (const b of tools.querySelectorAll("[data-kind]")) b.setAttribute("aria-pressed", String(state.kind.includes(b.dataset.kind)));
      $("mine").setAttribute("aria-pressed", String(state.flag === "user"));
      if (q.value !== state.q) q.value = state.q;
      repo.value = state.repo;
      flag.value = state.flag; sort.value = state.sort; group.value = mode;
      save();
    }
    function toggle(list, value) {
      return list.includes(value) ? list.filter(v => v !== value) : [...list, value];
    }
    function reveal(id) {
      const card = byId.get(id); // filtered-out cards are detached, so not in the document
      if (!card) return;
      if (!board.contains(card)) {
        reset();
        render();
      }
      const details = card.closest("details.group");
      if (details) details.open = true;
      card.scrollIntoView({block: "center"});
    }

    theme(store.get("theme") || "auto");
    style(store.get("style") || "plain");
    state = initial();
    for (const id of ["tools", "refresh", "style", "theme"]) $(id).hidden = false;
    tools.addEventListener("click", event => {
      const b = event.target.closest("button");
      if (!b) return;
      if (b.dataset.st) state.st = toggle(state.st, b.dataset.st);
      else if (b.dataset.kind) state.kind = toggle(state.kind, b.dataset.kind);
      else if (b.id === "mine") state.flag = state.flag === "user" ? "" : "user";
      else if (b.id === "clear") reset();
      else if (b.id === "open-all" || b.id === "close-all") {
        for (const d of board.querySelectorAll("details.group")) d.open = b.id === "open-all";
        return;
      } else if (b.id === "cards-open" || b.id === "cards-close") {
        for (const d of board.querySelectorAll("details.more")) d.open = b.id === "cards-open";
        return;
      } else return;
      render();
    });
    for (const tile of document.querySelectorAll("button.tile")) {
      tile.addEventListener("click", () => {
        state = {...parse(""), sort: state.sort, group: state.group,
          st: (tile.dataset.tileSt || "").split(",").filter(Boolean), flag: tile.dataset.tileFlag || ""};
        render();
        tools.scrollIntoView({block: "start"});
      });
    }
    $("clear-empty").addEventListener("click", () => { reset(); render(); });
    q.addEventListener("input", () => { state.q = q.value; render(); });
    repo.addEventListener("change", () => { state.repo = repo.value; render(); });
    flag.addEventListener("change", () => { state.flag = flag.value; render(); });
    sort.addEventListener("change", () => { state.sort = sort.value; render(); });
    group.addEventListener("change", () => { state.group = group.value; render(); });
    $("style").addEventListener("click", () => style(root.dataset.style === "vivid" ? "plain" : "vivid"));
    $("theme").addEventListener("click", () => theme(THEMES[(THEMES.indexOf(root.dataset.theme || "auto") + 1) % 3]));
    document.addEventListener("keydown", event => {
      const typing = ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName);
      if (event.key === "/" && !typing) { event.preventDefault(); q.focus(); q.select(); }
      else if (event.key === "Escape" && document.activeElement === q) { state.q = ""; render(); q.blur(); }
    });
    window.addEventListener("hashchange", () => {
      const raw = location.hash.slice(1);
      if (raw.includes("=")) { state = parse(raw); render(); }
      else if (raw) reveal(decodeURIComponent(raw));
    });

    const now = Date.now();
    for (const t of document.querySelectorAll("time[datetime]")) {
      const at = Date.parse(t.getAttribute("datetime"));
      if (!Number.isFinite(at)) continue;
      const minutes = Math.round((now - at) / 60000);
      const rel = document.createElement("span");
      rel.className = "rel";
      rel.textContent = minutes < 1 ? " · 刚刚" : minutes < 60 ? ` · ${minutes} 分钟前`
        : minutes < 2880 ? ` · ${Math.round(minutes / 60)} 小时前` : ` · ${Math.round(minutes / 1440)} 天前`;
      t.after(rel);
    }

    const refresh = $("refresh");
    let timer = null;
    function arm(on) {
      store.set("refresh", on ? "1" : "0");
      refresh.setAttribute("aria-pressed", String(on));
      clearInterval(timer);
      if (on) timer = setInterval(() => {
        // [data-hold-refresh]: something on the page (the STATUS app's reason box) is unsent.
        const busy = document.activeElement === q || !(window.getSelection()?.isCollapsed ?? true)
          || document.querySelector("[data-hold-refresh]") !== null;
        if (busy || document.hidden) return;
        try { sessionStorage.setItem(PREFIX + "scroll", String(window.scrollY)); } catch (_) { /* ignore */ }
        location.reload();
      }, 30000);
    }
    refresh.addEventListener("click", () => arm(refresh.getAttribute("aria-pressed") !== "true"));
    arm(store.get("refresh") === "1");

    render();
    const hash = location.hash.slice(1);
    if (hash && !hash.includes("=")) reveal(decodeURIComponent(hash));
    try {
      const y = Number(sessionStorage.getItem(PREFIX + "scroll"));
      sessionStorage.removeItem(PREFIX + "scroll");
      if (y > 0) window.scrollTo(0, y);
    } catch (_) { /* ignore */ }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
