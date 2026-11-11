/* TaskSteward Dashboard: filtering, sorting, grouping, folding and the task flow. No network access. */
(() => {
  "use strict";
  const root = document.documentElement;
  // The storage keys keep their old prefix, so the choices made before the rename still apply.
  const PREFIX = "tsw-status:";
  const store = {
    get(key) { try { return localStorage.getItem(PREFIX + key); } catch (_) { return null; } },
    set(key, value) { try { localStorage.setItem(PREFIX + key, value); } catch (_) { /* private mode */ } },
  };
  const session = {
    take(key) {
      try {
        const value = sessionStorage.getItem(PREFIX + key);
        sessionStorage.removeItem(PREFIX + key);
        return value;
      } catch (_) { return null; }
    },
    set(key, value) { try { sessionStorage.setItem(PREFIX + key, value); } catch (_) { /* ignore */ } },
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
    // Read before the first render rewrites the address: a task anchor to go to, and whether
    // this load is a reload (automatic or not), which keeps the view as it was instead.
    const anchor = (() => {
      const raw = location.hash.slice(1);
      try { return raw.includes("=") ? "" : decodeURIComponent(raw); } catch (_) { return ""; }
    })();
    const navigation = performance.getEntriesByType?.("navigation")?.[0];
    const reloaded = session.take("reload") === "1" || navigation?.type === "reload";
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
    const VIEWS = ["board", "flow"];
    let state;

    function parse(raw) {
      const p = new URLSearchParams(raw);
      const list = key => (p.get(key) || "").split(",").filter(Boolean);
      return {q: p.get("q") || "", st: list("st"), kind: list("kind"), repo: p.get("repo") || "",
        flag: p.get("flag") || "", sort: p.get("sort") || "status", group: p.get("group") || "status",
        view: p.get("view") || "board", arch: p.get("arch") === "1"};
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
      if (s.view !== "board") p.set("view", s.view);
      if (s.arch) p.set("arch", "1");
      return p.toString();
    }
    function initial() {
      const raw = location.hash.slice(1);
      return parse(raw.includes("=") ? raw : store.get("filters") || "");
    }
    function save() {
      // The address always carries the choices (a task anchor is followed once, on arrival), so
      // a reload, automatic or not, comes back to the same view.
      const raw = serialize(state);
      store.set("filters", raw);
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
    function selects(d, s, haystack, words) {
      if (s.st.length && !s.st.includes(d.status)) return false;
      if (s.kind.length && !s.kind.includes(d.kind)) return false;
      if (s.repo && d.repo !== s.repo) return false;
      if (s.flag && !(d.flags || "").split(" ").includes(s.flag)) return false;
      return words.every(w => haystack.includes(w));
    }
    const matches = (card, s, words) => selects(card.dataset, s, text.get(card), words);
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
      // A choice that has no task at the moment stays chosen (its pick shows 0), so a reload
      // never drops a filter; only values the page does not know at all are dropped.
      const offered = select => Array.from(select.options).map(o => o.value);
      if (state.repo && !offered(repo).includes(state.repo)) repo.add(new Option(state.repo, state.repo));
      if (!offered(flag).includes(state.flag)) state.flag = "";
      if (!Object.hasOwn(SORTS, state.sort)) state.sort = "status";
      if (!Object.hasOwn(GROUPS, state.group)) state.group = "status";
      if (!VIEWS.includes(state.view)) state.view = "board";
      state.st = state.st.filter(v => v in statusLabels);
      state.kind = state.kind.filter(v => v in kindLabels);
    }
    function timeline() {
      return state.group === "none" && state.sort === "activity";
    }
    function reset() {
      state = {...parse(""), sort: state.sort, group: state.group, view: state.view, arch: state.arch};
    }
    function words() {
      return state.q.toLowerCase().split(/\s+/).filter(Boolean);
    }
    function renderBoard() {
      const shown = cards.filter(c => matches(c, state, words())).sort(SORTS[state.sort]);
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
    }
    function render() {
      normalize();
      const flowing = state.view === "flow" && flow !== null;
      board.hidden = flowing;
      tools.dataset.view = flowing ? "flow" : "board";
      if (flow) $("flow").hidden = !flowing;
      if (flowing) { $("empty").hidden = true; flow.render(); }
      else renderBoard();
      for (const b of tools.querySelectorAll("[data-st]")) {
        const on = state.st.includes(b.dataset.st);
        b.setAttribute("aria-pressed", String(on));
        b.hidden = !on && b.dataset.n === "0";
      }
      for (const b of tools.querySelectorAll("[data-kind]")) {
        const on = state.kind.includes(b.dataset.kind);
        b.setAttribute("aria-pressed", String(on));
        b.hidden = !on && b.dataset.n === "0";
      }
      $("mine").setAttribute("aria-pressed", String(state.flag === "user"));
      $("view-status").setAttribute("aria-pressed", String(!flowing && !timeline()));
      $("timeline").setAttribute("aria-pressed", String(!flowing && timeline()));
      $("view-flow").setAttribute("aria-pressed", String(flowing));
      if (q.value !== state.q) q.value = state.q;
      repo.value = state.repo;
      flag.value = state.flag; sort.value = state.sort; group.value = state.group;
      save();
    }
    function toggle(list, value) {
      return list.includes(value) ? list.filter(v => v !== value) : [...list, value];
    }
    function reveal(id) {
      const card = byId.get(id); // filtered-out cards are detached, so not in the document
      if (!card) return;
      if (state.view !== "board") { state.view = "board"; render(); }
      if (!board.contains(card)) {
        reset();
        render();
      }
      const details = card.closest("details.group");
      if (details) details.open = true;
      card.scrollIntoView({block: "center"});
      // The anchor has been followed: the address goes back to the choices, and the card is
      // marked for a moment instead of by :target.
      card.classList.add("flash");
      window.setTimeout(() => card.classList.remove("flash"), 2500);
      save();
    }

    /* The task flow: nodes placed by tsw (DN006 §8.10); shown by the filters, arrows drawn here. */
    const flow = (() => {
      const pane = $("flow"), grid = $("flow-grid"), svg = $("flow-edges");
      const scroller = $("flow-scroll"), sizer = $("flow-size");
      if (!pane || !grid || !svg || !scroller || !sizer) return null;
      const SVG = "http://www.w3.org/2000/svg";
      const nodes = Array.from(grid.querySelectorAll(".fnode"));
      const nodeById = new Map(nodes.map(n => [n.dataset.id, n]));
      const ids = value => (value || "").split(" ").filter(id => nodeById.has(id));
      const near = new Map(nodes.map(n => [n.dataset.id, new Set()]));
      for (const n of nodes) {
        for (const other of [...ids(n.dataset.deps), ...ids(n.dataset.from)]) {
          near.get(n.dataset.id).add(other);
          near.get(other).add(n.dataset.id);
        }
      }
      // Search reads a task's card where it has one, so both views select the same tasks.
      const haystack = new Map(nodes.map(n => {
        const card = byId.get("task-" + n.dataset.id);
        return [n, card ? text.get(card) : (n.title || "").toLowerCase()];
      }));
      const lanes = Array.from(grid.querySelectorAll(".lane, .lane-name"));
      const ZOOMS = [0.3, 0.4, 0.5, 0.6, 0.75, 0.9, 1, 1.15, 1.3, 1.5];
      let zoom = Number(store.get("zoom")) || 1;
      let shown = new Set();

      function inLayout(n) { return state.arch || !n.classList.contains("no-live"); }
      function render() {
        grid.classList.toggle("all", state.arch);
        $("flow-archived").checked = state.arch;
        const w = words();
        const matched = new Set(nodes.filter(n => inLayout(n)
          && (n.dataset.status !== "archived" || (state.arch && !state.st.length))
          && selects(n.dataset, state, haystack.get(n), w)));
        const context = new Set();
        for (const n of matched) {
          for (const id of near.get(n.dataset.id)) {
            const other = nodeById.get(id);
            if (!matched.has(other) && inLayout(other)) context.add(other);
          }
        }
        shown = new Set([...matched, ...context]);
        const repos = new Set();
        for (const n of nodes) {
          n.hidden = !shown.has(n);
          n.classList.toggle("ctx", context.has(n));
          if (shown.has(n)) repos.add(n.dataset.repo);
        }
        for (const lane of lanes) lane.hidden = !repos.has(lane.dataset.repo) || !inLayout(lane);
        headings();
        const pool = nodes.filter(n => inLayout(n) && (state.arch || n.dataset.status !== "archived")).length;
        $("count").textContent = `显示 ${matched.size} / ${pool} 个任务`
          + (context.size ? `（另有 ${context.size} 个相连任务淡色显示）` : "");
        $("flow-empty").hidden = matched.size !== 0;
        applyZoom();
        draw();
      }
      function column(n) {
        return Number(n.style.getPropertyValue(state.arch ? "--agc" : "--gc")) || 0;
      }
      function headings() {
        for (const h of grid.querySelectorAll(".col-h")) h.remove();
        const columns = new Map();
        for (const n of shown) {
          const c = column(n);
          if (!columns.has(c)) columns.set(c, []);
          columns.get(c).push(n);
        }
        for (const [c, members] of columns) {
          members.sort((a, b) => Number(b.dataset.seq) - Number(a.dataset.seq));
          const first = members[0].dataset.id, last = members[members.length - 1].dataset.id;
          const h = document.createElement("div");
          h.className = "col-h";
          h.style.gridColumn = String(c);
          h.textContent = first === last ? first : `${first} – ${last}`;
          grid.append(h);
        }
      }
      // An arrow from a task to what it depends on (or came from): a curve and its head, both
      // plain paths, so the page needs no references between SVG elements.
      function edge(from, to, kind) {
        const rightward = to.offsetLeft >= from.offsetLeft + from.offsetWidth;
        const sign = rightward ? 1 : -1; // leftward only for a cycle or an old record
        const x1 = rightward ? from.offsetLeft + from.offsetWidth : from.offsetLeft;
        const x2 = rightward ? to.offsetLeft : to.offsetLeft + to.offsetWidth;
        const y1 = from.offsetTop + from.offsetHeight / 2, y2 = to.offsetTop + to.offsetHeight / 2;
        const dx = sign * Math.max(30, Math.abs(x2 - x1) / 2), tip = x2 - sign * 8;
        const g = document.createElementNS(SVG, "g");
        g.setAttribute("class", `edge ${kind}` + (from.classList.contains("ctx") || to.classList.contains("ctx") ? " ctx" : ""));
        g.dataset.a = from.dataset.id;
        g.dataset.b = to.dataset.id;
        const line = document.createElementNS(SVG, "path");
        line.setAttribute("class", "line");
        line.setAttribute("d", `M${x1},${y1} C${x1 + dx},${y1} ${tip - dx},${y2} ${tip},${y2}`);
        const head = document.createElementNS(SVG, "path");
        head.setAttribute("class", "head");
        head.setAttribute("d", `M${tip},${y2 - 4.5} L${x2},${y2} L${tip},${y2 + 4.5} Z`);
        g.append(line, head);
        return g;
      }
      function draw() {
        if (pane.hidden) return;
        svg.setAttribute("width", String(grid.offsetWidth));
        svg.setAttribute("height", String(grid.offsetHeight));
        const paths = [];
        for (const n of shown) {
          for (const id of ids(n.dataset.deps)) {
            const to = nodeById.get(id);
            if (shown.has(to)) paths.push(edge(n, to, "dep"));
          }
          for (const id of ids(n.dataset.from)) {
            const to = nodeById.get(id);
            if (shown.has(to)) paths.push(edge(n, to, "fu"));
          }
        }
        svg.replaceChildren(...paths);
        sizer.style.width = `${grid.offsetWidth * zoom}px`;
        sizer.style.height = `${grid.offsetHeight * zoom}px`;
      }
      function applyZoom() {
        zoom = Math.min(1.5, Math.max(0.1, zoom));
        grid.style.transform = zoom === 1 ? "" : `scale(${zoom})`;
        sizer.style.width = `${grid.offsetWidth * zoom}px`;
        sizer.style.height = `${grid.offsetHeight * zoom}px`;
        $("zoom-level").textContent = `${Math.round(zoom * 100)}%`;
        store.set("zoom", String(zoom));
      }
      function step(direction) {
        const i = direction > 0 ? ZOOMS.findIndex(z => z > zoom + 1e-6) : ZOOMS.findLastIndex(z => z < zoom - 1e-6);
        if (i >= 0) zoom = ZOOMS[i];
        applyZoom();
      }
      function highlight(node) {
        grid.classList.toggle("hl", node !== null);
        for (const el of grid.querySelectorAll(".on")) el.classList.remove("on");
        if (!node) return;
        node.classList.add("on");
        for (const id of near.get(node.dataset.id)) nodeById.get(id).classList.add("on");
        for (const p of svg.querySelectorAll("g.edge")) {
          if (p.dataset.a === node.dataset.id || p.dataset.b === node.dataset.id) p.classList.add("on");
        }
      }
      grid.addEventListener("pointerover", e => highlight(e.target.closest?.(".fnode") || null));
      grid.addEventListener("pointerleave", () => highlight(null));
      grid.addEventListener("focusin", e => highlight(e.target.closest?.(".fnode") || null));
      grid.addEventListener("focusout", () => highlight(null));
      $("zoom-in").addEventListener("click", () => step(1));
      $("zoom-out").addEventListener("click", () => step(-1));
      $("zoom-reset").addEventListener("click", () => { zoom = 1; applyZoom(); });
      $("zoom-fit").addEventListener("click", () => {
        zoom = Math.min(1, (scroller.clientWidth - 4) / Math.max(1, grid.offsetWidth));
        applyZoom();
      });
      $("flow-archived").addEventListener("change", event => { state.arch = event.target.checked; renderAll(); });
      window.addEventListener("resize", () => draw());
      return {
        render,
        scroll: () => `${scroller.scrollLeft},${scroller.scrollTop}`,
        restore(raw) {
          const [x, y] = (raw || "").split(",").map(Number);
          if (Number.isFinite(x) && Number.isFinite(y)) scroller.scrollTo(x, y);
        },
      };
    })();
    const renderAll = () => render();

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
      else if (b.id === "view-status") { state.view = "board"; state.group = "status"; state.sort = "status"; }
      else if (b.id === "timeline") {
        // One switch between the grouped board and a single list, newest change first.
        const on = state.view === "board" && timeline();
        state.view = "board";
        state.group = on ? "status" : "none";
        state.sort = on ? "status" : "activity";
      }
      else if (b.id === "view-flow") state.view = "flow";
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
        if (tile.dataset.tileArchived) {
          state = {...parse(""), sort: state.sort, group: state.group, view: "flow", arch: true};
        } else {
          state = {...parse(""), sort: state.sort, group: state.group, view: state.view, arch: state.arch,
            st: (tile.dataset.tileSt || "").split(",").filter(Boolean), flag: tile.dataset.tileFlag || ""};
        }
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
      else if (raw) { try { reveal(decodeURIComponent(raw)); } catch (_) { /* not an anchor */ } }
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
        // [data-hold-refresh] on the root: the Dashboard app has an unsent reason or a request.
        const busy = document.activeElement === q || !(window.getSelection()?.isCollapsed ?? true)
          || document.querySelector("[data-hold-refresh]") !== null;
        if (busy || document.hidden) return;
        session.set("reload", "1");
        location.reload();
      }, 30000);
    }
    refresh.addEventListener("click", () => arm(refresh.getAttribute("aria-pressed") !== "true"));
    arm(store.get("refresh") === "1");
    // Any reload keeps where the user was: the page and the task flow scroll back.
    window.addEventListener("pagehide", () => {
      session.set("scroll", String(window.scrollY));
      if (flow) session.set("flow-scroll", flow.scroll());
    });

    render();
    const y = Number(session.take("scroll"));
    const flowAt = session.take("flow-scroll");
    if (reloaded) {
      if (flow && state.view === "flow") flow.restore(flowAt);
      if (y > 0) window.scrollTo(0, y);
    } else if (anchor) reveal(anchor);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
