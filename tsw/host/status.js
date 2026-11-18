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
    const RUNNING = (board.dataset.running || "").split(" ").filter(Boolean);
    const statusLabels = Object.create(null), kindLabels = Object.create(null);
    for (const b of tools.querySelectorAll("[data-st]")) statusLabels[b.dataset.st] = b.dataset.label;
    for (const b of tools.querySelectorAll("[data-kind]")) kindLabels[b.dataset.kind] = b.dataset.label;
    const text = new Map(cards.map(c => [c, c.textContent.toLowerCase()]));
    const byId = new Map(cards.map(c => [c.id, c]));
    const q = $("q"), repo = $("repo"), flag = $("flag"), sort = $("sort"), group = $("group");
    const DEFAULT_CLOSED = {status: ["accepted", "cancelled"]};
    const VIEWS = ["board", "flow"];
    // The task flow's relations: dependencies always shown, the follow-ups hidden by unticking
    // them ("hide"), the references shown by ticking them ("refs", T304); display choices that
    // the filters' reset and the tiles keep, like the line shape.
    const RELATIONS = ["dep", "fu", "ref"];
    let state;

    function parse(raw) {
      const p = new URLSearchParams(raw);
      const list = key => (p.get(key) || "").split(",").filter(Boolean);
      return {q: p.get("q") || "", st: list("st"), kind: list("kind"), repo: p.get("repo") || "",
        flag: p.get("flag") || "", sort: p.get("sort") || "status", group: p.get("group") || "status",
        view: p.get("view") || "board", arch: p.get("arch") === "1",
        hide: list("hide").filter(r => r === "fu"), refs: p.get("refs") === "1",
        shape: p.get("line") === "curve" ? "curve" : "poly"};
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
      if (s.hide.length) p.set("hide", s.hide.join(","));
      if (s.refs) p.set("refs", "1");
      if (s.shape === "curve") p.set("line", "curve");
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
      state.hide = RELATIONS.filter(r => state.hide.includes(r));
    }
    // The quick picks after the kinds (T273 revision 3): the running statuses, and the flag.
    function running() {
      return state.st.length === RUNNING.length && RUNNING.every(s => state.st.includes(s));
    }
    function timeline() {
      return state.group === "none" && state.sort === "activity";
    }
    function reset() {
      state = {...parse(""), sort: state.sort, group: state.group, view: state.view, arch: state.arch,
        hide: state.hide, refs: state.refs, shape: state.shape};
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
      // With the archived tasks in the task flow, kinds and repositories count every task.
      const counted = el => (flowing && state.arch ? el.dataset.na : el.dataset.n) || "0";
      for (const b of tools.querySelectorAll("[data-kind]")) {
        const on = state.kind.includes(b.dataset.kind);
        b.setAttribute("aria-pressed", String(on));
        b.hidden = !on && counted(b) === "0";
        const small = b.querySelector("small");
        if (small) small.textContent = counted(b);
      }
      for (const o of repo.options) {
        if (o.value) o.hidden = o.value !== state.repo && counted(o) === "0";
      }
      for (const b of tools.querySelectorAll("[data-quick]")) {
        const on = b.dataset.quick === "running" ? running() : state.flag === b.dataset.quick;
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

    /* The task flow (DN006 §8.10): cards placed and lines routed by tsw for every choice; the
       script takes the layout of the current choice, shows cards by the filters, closes up the
       columns and lanes left empty and draws the lines along their waypoints. */
    const flow = (() => {
      const pane = $("flow"), grid = $("flow-grid"), canvas = $("flow-canvas"), svg = $("flow-edges");
      const names = $("flow-names"), data = $("flow-layouts");
      const scroller = $("flow-scroll"), sizer = $("flow-size");
      if (!pane || !grid || !canvas || !svg || !names || !scroller || !sizer || !data) return null;
      const SVG = "http://www.w3.org/2000/svg";
      const nodes = Array.from(canvas.querySelectorAll(".fnode"));
      const nodeById = new Map(nodes.map(n => [n.dataset.id, n]));
      const ids = value => (value || "").split(" ").filter(id => nodeById.has(id));
      // The tasks each relation joins a task to, both ways.
      const ATTR = {dep: "deps", fu: "from", ref: "refs"};
      const joined = Object.fromEntries(RELATIONS.map(r => [r, new Map(nodes.map(n => [n.dataset.id, new Set()]))]));
      for (const n of nodes) {
        for (const r of RELATIONS) {
          for (const other of ids(n.dataset[ATTR[r]])) {
            joined[r].get(n.dataset.id).add(other);
            joined[r].get(other).add(n.dataset.id);
          }
        }
      }
      // The tasks joined to each task by the relations shown (set by render).
      let near = new Map();
      // Search reads a task's card where it has one, so both views select the same tasks.
      const haystack = new Map(nodes.map(n => {
        const card = byId.get("task-" + n.dataset.id);
        return [n, card ? text.get(card) : (n.title || "").toLowerCase()];
      }));
      const bands = new Map(Array.from(canvas.querySelectorAll(".lane")).map(l => [l.dataset.repo, l]));
      const labels = new Map(Array.from(names.querySelectorAll(".lane-name")).map(l => [l.dataset.repo, l]));
      // The layouts tsw computed for every choice, with and without the archived tasks; equal
      // ones are written once.
      const layouts = JSON.parse(data.textContent);
      const order = layouts.ids.map(id => nodeById.get(id));
      const index = new Map(layouts.ids.map((id, i) => [id, i]));
      const [W, H] = layouts.card;
      const KIND = ["dep", "fu", "ref"];
      const ZOOMS = [0.3, 0.4, 0.5, 0.6, 0.75, 0.9, 1, 1.15, 1.3, 1.5];
      let zoom = Number(store.get("zoom")) || 1;
      let shown = new Set(), room = new Set(), set = null, peek = "";
      // Each card shows the time of its latest ledger record, in the browser's time zone.
      const clock = new Intl.DateTimeFormat(undefined, {month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false});
      const when = value => {
        const t = Date.parse(value);
        return Number.isFinite(t) ? clock.format(new Date(t)).replace(",", "") : "";
      };
      for (const n of nodes) {
        const t = n.querySelector(".ftime");
        if (t) t.textContent = when(n.dataset.when);
        n.style.width = `${W}px`;
        n.style.height = `${H}px`;
      }

      // Dependencies are always shown; the follow-ups unless unticked, the references if ticked.
      const showing = () => ["dep", ...(state.hide.includes("fu") ? [] : ["fu"]), ...(state.refs ? ["ref"] : [])];
      function arrange() {
        set = layouts.sets[layouts.pick[(state.arch ? "all" : "live") + ":" + showing().join(",")]];
        order.forEach((n, i) => n.classList.toggle("out", !set.cards[i]));
      }
      function inLayout(n) { return !n.classList.contains("out"); }
      function render() {
        arrange();
        $("flow-archived").checked = state.arch;
        for (const box of $("flow-legend").querySelectorAll("input[data-rel]")) {
          box.checked = box.dataset.rel === "ref" ? state.refs : !state.hide.includes(box.dataset.rel);
        }
        for (const radio of pane.querySelectorAll("input[name=flow-shape]")) radio.checked = radio.value === state.shape;
        const kinds = showing();
        near = new Map(nodes.map(n => [n.dataset.id, new Set(kinds.flatMap(r => [...joined[r].get(n.dataset.id)]))]));
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
        // The places kept for the cards the references would add as context: showing the
        // references (or not) never moves a card.
        room = new Set(shown);
        if (!state.refs) {
          for (const n of matched) {
            for (const id of joined.ref.get(n.dataset.id)) {
              const other = nodeById.get(id);
              if (inLayout(other)) room.add(other);
            }
          }
        }
        for (const n of nodes) {
          n.hidden = !shown.has(n);
          n.classList.toggle("ctx", context.has(n));
        }
        const pool = nodes.filter(n => inLayout(n) && (state.arch || n.dataset.status !== "archived")).length;
        $("count").textContent = `显示 ${matched.size} / ${pool} 个任务`
          + (context.size ? `（另有 ${context.size} 个相连任务淡色显示）` : "");
        $("flow-empty").hidden = matched.size !== 0;
        peek = "";
        draw();
        applyZoom();
      }
      // The lines of the current choice: with the references shown, the routes in the widened
      // gaps; with a card hovered and the references not shown, the routes with the references in
      // the same gaps, showing only that card's references.
      function routes() {
        const wide = state.refs;
        const group = wide ? (set.wide || set.lines) : peek ? (set.peek || set.lines) : set.lines;
        return {xs: wide ? set.xw : set.xs, gaps: wide ? set.gw : set.gaps, lines: decode(group, true),
          // Every line between cards shown, the references included whether drawn or not: what
          // closes up does not depend on them, so showing or hovering never moves a card.
          every: decode(wide ? (set.wide || set.lines) : (set.peek || set.lines), false, room)};
      }
      function decode(group, drawn, among = shown) {
        const wide = state.refs;
        const out = [];
        for (const row of group) {
          const kind = KIND[row[0]], a = order[row[1]], b = order[row[2]];
          if (!among.has(a) || !among.has(b)) continue;
          if (drawn && kind === "ref" && !wide && peek !== a.dataset.id && peek !== b.dataset.id) continue;
          // x0, y0, then the coordinate each move changes: x, y, x, ...
          const pts = [[row[3], row[4]]];
          for (let i = 5; i < row.length; i++) {
            const [x, y] = pts[pts.length - 1];
            pts.push(i % 2 ? [row[i], y] : [x, row[i]]);
          }
          out.push({kind, a, b, pts});
        }
        return out;
      }
      // A coordinate with the given spans taken out (a coordinate inside one lands on its start).
      function collapse(spans) {
        spans.sort((p, q) => p[0] - q[0]);
        return v => {
          let out = v;
          for (const [lo, hi] of spans) {
            if (v <= lo) break;
            out -= Math.min(v, hi) - lo;
          }
          return out;
        };
      }
      // Places the cards, the lanes and the headings, and draws the lines.
      function draw() {
        if (pane.hidden || !set) return;
        const {xs, gaps, lines, every} = routes();
        const cols = new Map(), used = new Set();
        for (const n of room) {
          const c = set.cards[index.get(n.dataset.id)][0];
          used.add(c);
          if (!shown.has(n)) continue;
          if (!cols.has(c)) cols.set(c, []);
          cols.get(c).push(n);
        }
        // A column without a card shown closes up, with its gap when no line drawn passes there;
        // so does a lane without a card shown.
        const reach = every.map(l => [Math.min(...l.pts.map(p => p[0])), Math.max(...l.pts.map(p => p[0]))]);
        const xSpans = [];
        for (let c = 0; c < xs.length; c++) {
          if (used.has(c)) continue;
          const g0 = xs[c] + W, g1 = g0 + gaps[c];
          const crossed = reach.some(([lo, hi]) => lo < g1 && hi > g0);
          xSpans.push([xs[c], crossed ? g0 : g1]);
        }
        const lanesShown = new Set(Array.from(room, n => n.dataset.repo));
        // Heights: what a card shown, a line's horizontal run or a lane's name needs is kept,
        // with some room around it; the rest closes up (a lane without a card shown entirely).
        const PAD = 16;
        const kept = [[0, layouts.head]];
        for (const n of room) {
          const top = set.cards[index.get(n.dataset.id)][1];
          kept.push([top - PAD, top + H + PAD]);
        }
        for (const l of every) {
          for (let i = 1; i < l.pts.length; i++) {
            if (l.pts[i][1] === l.pts[i - 1][1]) kept.push([l.pts[i][1] - PAD / 2, l.pts[i][1] + PAD / 2]);
          }
        }
        for (const [i, repo] of layouts.repos.entries()) {
          if (set.lanes[i] && lanesShown.has(repo)) kept.push([set.lanes[i][0], set.lanes[i][0] + 2 * PAD]);
        }
        kept.sort((p, q) => p[0] - q[0]);
        const ySpans = [];
        let reached = kept[0][1];
        for (const [lo, hi] of kept) {
          if (lo > reached) ySpans.push([reached, lo]);
          reached = Math.max(reached, hi);
        }
        const X = collapse(xSpans), Y = collapse(ySpans);
        const lastX = xs.length ? xs[xs.length - 1] + W + gaps[gaps.length - 1] : 0;
        const bottom = reached;
        const width = X(lastX), height = Y(bottom);
        canvas.style.width = `${width}px`;
        canvas.style.height = `${height}px`;
        names.style.height = `${height}px`;
        for (const [i, n] of order.entries()) {
          if (!shown.has(n)) continue;
          const [c, top] = set.cards[i];
          n.style.left = `${X(xs[c])}px`;
          n.style.top = `${Y(top)}px`;
        }
        for (const [i, repo] of layouts.repos.entries()) {
          const lane = set.lanes[i], on = !!lane && lanesShown.has(repo);
          for (const el of [bands.get(repo), labels.get(repo)]) {
            if (!el) continue;
            el.hidden = !on;
            if (on) {
              el.style.top = `${Y(lane[0])}px`;
              el.style.height = `${Y(lane[1]) - Y(lane[0])}px`;
            }
          }
        }
        // Column headings: the range of the latest ledger records of the cards shown.
        for (const h of canvas.querySelectorAll(".col-h")) h.remove();
        for (const [c, members] of cols) {
          const times = members.map(n => n.dataset.when).sort();
          const newest = when(times[times.length - 1]), oldest = when(times[0]);
          const h = document.createElement("div");
          h.className = "col-h";
          h.style.left = `${X(xs[c])}px`;
          h.style.width = `${W}px`;
          h.textContent = newest === oldest ? newest : `${newest} – ${oldest}`;
          h.title = "该列卡片最近修改时间的范围（新 – 旧）";
          canvas.append(h);
        }
        svg.setAttribute("width", String(width));
        svg.setAttribute("height", String(height));
        // The gap a channel lies in, as its left and right x.
        const gapAt = x => {
          for (let c = 0; c < xs.length; c++) {
            if (x > xs[c] + W && x < xs[c] + W + gaps[c]) return [xs[c] + W, xs[c] + W + gaps[c]];
          }
          return [x, x];
        };
        // References first, so they lie under the other lines.
        lines.sort((p, q) => Number(q.kind === "ref") - Number(p.kind === "ref"));
        svg.replaceChildren(...lines.map(l => line(l, gapAt, X, Y)));
        highlight(grid.querySelector(".fnode.on"));
      }
      // A line along its waypoints, as an orthogonal polyline with rounded corners or with a curve
      // through the line's own channel at every turn; the head at the last point, along it.
      function line(l, gapAt, X, Y) {
        const P = l.pts, n = P.length;
        const [lx, ly] = P[n - 1];
        const dir = lx >= P[n - 2][0] ? 1 : -1, tip = lx - dir * 8;
        // The last point stops short of the card, where the head begins.
        const at = i => (i === n - 1 ? [tip, P[i][1]] : P[i]);
        const pt = ([x, y]) => `${X(x).toFixed(1)},${Y(y).toFixed(1)}`;
        let d = `M${pt(P[0])}`;
        if (state.shape === "curve") {
          for (let i = 1; i < n; i++) {
            const [x0, y0] = P[i - 1], [x1] = P[i];
            const turns = i + 1 < n && P[i + 1][0] === x1 && x0 !== x1;
            if (!turns) { d += ` L${pt(at(i))}`; continue; }
            // Out of the horizontal run at the gap's edge, through the channel, into the next
            // run at the gap's other edge (the same edge for a U-turn).
            const [g0, g1] = gapAt(x1);
            const y1 = P[i + 1][1];
            const next = P[i + 2] || P[i + 1];
            const from = x0 < x1 ? g0 : g1, to = next[0] < x1 ? g0 : g1;
            if (Math.abs(from - x0) > 0.05) d += ` L${pt([from, y0])}`;
            const last = i + 2 === n - 1 && Math.abs(next[0] - to) < 0.05;
            d += ` C${pt([x1, y0])} ${pt([x1, y1])} ${pt(last ? [tip, y1] : [to, y1])}`;
            if (i + 2 < n && !last && Math.abs(next[0] - to) > 0.05) d += ` L${pt(at(i + 2))}`;
            i += 2;
          }
        } else {
          for (let i = 1; i < n - 1; i++) {
            const [ax, ay] = P[i - 1], [bx, by] = P[i], [cx, cy] = at(i + 1);
            const r = Math.min(6, Math.hypot(bx - ax, by - ay) / 2, Math.hypot(cx - bx, cy - by) / 2);
            const ux = Math.sign(bx - ax), uy = Math.sign(by - ay), vx = Math.sign(cx - bx), vy = Math.sign(cy - by);
            d += ` L${pt([bx - ux * r, by - uy * r])} Q${pt([bx, by])} ${pt([bx + vx * r, by + vy * r])}`;
          }
          d += ` L${pt(at(n - 1))}`;
        }
        const g = document.createElementNS(SVG, "g");
        g.setAttribute("class", `edge ${l.kind}` + (l.a.classList.contains("ctx") || l.b.classList.contains("ctx") ? " ctx" : ""));
        g.dataset.a = l.a.dataset.id;
        g.dataset.b = l.b.dataset.id;
        const path = document.createElementNS(SVG, "path");
        path.setAttribute("class", "line");
        path.setAttribute("d", d);
        const head = document.createElementNS(SVG, "path");
        head.setAttribute("class", "head");
        const hx = X(lx).toFixed(1), ht = X(tip).toFixed(1), hy = Y(ly);
        head.setAttribute("d", `M${ht},${(hy - 4.5).toFixed(1)} L${hx},${hy.toFixed(1)} L${ht},${(hy + 4.5).toFixed(1)} Z`);
        g.append(path, head);
        return g;
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
        grid.classList.toggle("hl", !!node);
        for (const el of grid.querySelectorAll(".on")) el.classList.remove("on");
        if (!node) return;
        node.classList.add("on");
        for (const id of near.get(node.dataset.id)) nodeById.get(id).classList.add("on");
        for (const p of svg.querySelectorAll("g.edge")) {
          if (p.dataset.a === node.dataset.id || p.dataset.b === node.dataset.id) {
            p.classList.add("on");
            nodeById.get(p.dataset.a === node.dataset.id ? p.dataset.b : p.dataset.a)?.classList.add("on");
          }
        }
      }
      // Hovering or focusing a card shows its references for the moment when they are not all
      // shown; the cards stay where they are.
      function point(node) {
        const id = node && !state.refs ? node.dataset.id : "";
        if (id !== peek) { peek = id; draw(); }
        highlight(node);
      }
      canvas.addEventListener("pointerover", e => point(e.target.closest?.(".fnode") || null));
      canvas.addEventListener("pointerleave", () => point(null));
      canvas.addEventListener("focusin", e => point(e.target.closest?.(".fnode") || null));
      canvas.addEventListener("focusout", () => point(null));
      $("zoom-in").addEventListener("click", () => step(1));
      $("zoom-out").addEventListener("click", () => step(-1));
      $("zoom-reset").addEventListener("click", () => { zoom = 1; applyZoom(); });
      $("zoom-fit").addEventListener("click", () => {
        zoom = Math.min(1, (scroller.clientWidth - 4) / Math.max(1, grid.offsetWidth));
        applyZoom();
      });
      $("flow-archived").addEventListener("change", event => { state.arch = event.target.checked; renderAll(); });
      $("flow-legend").addEventListener("change", event => {
        const rel = event.target.dataset?.rel;
        if (rel === "ref") state.refs = event.target.checked;
        else if (rel === "fu") state.hide = event.target.checked ? [] : ["fu"];
        else return;
        renderAll();
      });
      pane.addEventListener("change", event => {
        if (event.target.name !== "flow-shape") return;
        state.shape = event.target.value === "curve" ? "curve" : "poly";
        renderAll();
      });
      // The page is at its size before the first draw (the cards measure nothing, but the zoom does).
      window.addEventListener("resize", () => applyZoom());
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
      else if (b.dataset.quick === "running") state.st = running() ? [] : [...RUNNING];
      else if (b.dataset.quick) state.flag = state.flag === b.dataset.quick ? "" : b.dataset.quick;
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
          state = {...parse(""), sort: state.sort, group: state.group, view: "flow", arch: true,
            hide: state.hide, refs: state.refs, shape: state.shape};
        } else {
          state = {...parse(""), sort: state.sort, group: state.group, view: state.view, arch: state.arch,
            hide: state.hide, refs: state.refs, shape: state.shape, st: (tile.dataset.tileSt || "").split(",").filter(Boolean), flag: tile.dataset.tileFlag || ""};
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
