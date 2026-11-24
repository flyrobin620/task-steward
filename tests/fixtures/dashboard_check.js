/* Drives the Dashboard page in a real browser and writes what it saw into #check-result (T353).
   Loaded after the page's own script, so the page has started when this runs; ?mode=wide or
   ?mode=narrow picks the checks for the window the test opened. */
(() => {
  "use strict";
  const mode = new URLSearchParams(location.search).get("mode") || "wide";
  const $ = id => document.getElementById(id);
  const checks = [], sizes = {};
  const check = (name, ok, detail) => checks.push({name, ok: !!ok, detail: detail === undefined ? "" : String(detail)});
  const box = el => el.getBoundingClientRect();
  const shown = el => !!el && getComputedStyle(el).display !== "none" && !el.hidden && box(el).height > 0;
  const click = el => el.dispatchEvent(new MouseEvent("click", {bubbles: true, cancelable: true, view: window}));
  const wheel = (el, dy) => {
    const event = new WheelEvent("wheel", {deltaY: dy, bubbles: true, cancelable: true, view: window});
    el.dispatchEvent(event);
    return event.defaultPrevented;
  };
  const oneRow = items => items.length > 0 && items.every(el => Math.abs(box(el).top - box(items[0]).top) < 2);
  const inside = (outer, x, y) => outer.contains(document.elementFromPoint(x, y));

  function header() {
    const hero = $("hero"), info = document.querySelector(".hero-info .meta");
    sizes.header = Math.round(box(hero).height);
    if (mode === "narrow") {
      check("narrow: header is the title row", sizes.header <= 44, sizes.header);
      check("narrow: ledger line folded", !shown(info));
      check("narrow: 「⋯」 shown", shown($("hero-more")));
      click($("hero-more"));
      check("narrow: 「⋯」 opens the ledger line and buttons", shown(info) && shown($("theme")) && box(info).top >= 0);
      check("narrow: 「⋯」 is expanded", $("hero-more").getAttribute("aria-expanded") === "true");
      click(document.body);
      check("narrow: a click elsewhere folds it", !shown(info));
    } else {
      check("wide: ledger line in the header", shown(info));
      check("wide: no 「⋯」", !shown($("hero-more")));
      check("wide: buttons in the header", shown($("theme")) && shown($("refresh")));
    }
    // The tiles come right under the title.
    const tiles = document.querySelector(".tiles");
    sizes.title_to_tiles = Math.round(box(tiles).top - box(hero).bottom);
    check("tiles right under the header", sizes.title_to_tiles <= 16, sizes.title_to_tiles);
  }

  function tiles() {
    const all = Array.from(document.querySelectorAll(".tiles .tile"));
    check("tiles in one row", oneRow(all), all.map(t => Math.round(box(t).top)).join(","));
    check("only status tiles", all.every(t => t.dataset.tileSt || t.dataset.tileArchived) && !all.some(t => t.dataset.tileFlag));
    const row = document.querySelector(".tiles");
    sizes.tiles = Math.round(box(row).height);
    check("tile row swipes when it does not fit", row.scrollWidth <= row.clientWidth || getComputedStyle(row).overflowX === "auto");
    if (row.scrollWidth > row.clientWidth) {
      const from = row.scrollLeft;
      const pointer = (type, x) => row.dispatchEvent(new PointerEvent(type, {bubbles: true, cancelable: true, pointerType: "mouse", pointerId: 7, button: 0, clientX: x, clientY: box(row).top + 10}));
      pointer("pointerdown", 300); pointer("pointermove", 240); pointer("pointermove", 200); pointer("pointerup", 200);
      check("mouse drags the tile row", row.scrollLeft > from, `${from}->${row.scrollLeft}`);
      row.scrollLeft = 0;
    }
  }

  function filters() {
    const tools = $("tools"), pop = $("filter-pop"), open = $("filter-open");
    sizes.filters = Math.round(box(tools).height);
    check("filters: one row when nothing is chosen", sizes.filters <= 60, sizes.filters);
    check("filters: panel closed", !shown(pop));
    click(open);
    const r = box(pop);
    check("filters: 「筛选」 opens the panel", shown(pop) && open.getAttribute("aria-expanded") === "true");
    check("filters: the panel is in the window", r.top >= 0 && r.bottom <= innerHeight + 1 && r.left >= 0 && r.right <= innerWidth + 1,
      [r.top, r.bottom, innerHeight].map(Math.round).join(","));
    check("filters: nothing covers the panel", inside(pop, r.left + r.width / 2, r.top + 30) && inside(pop, r.left + r.width / 2, r.bottom - 20));
    if (mode === "narrow") {
      const wide = document.documentElement.clientWidth;
      check("narrow: the panel is a bottom sheet", Math.abs(r.bottom - innerHeight) < 2 && r.width >= wide - 2,
        [r.bottom, r.width, wide].map(Math.round).join(","));
      check("narrow: a scrim behind the sheet", shown($("filter-scrim")));
    }
    const pick = pop.querySelector("[data-st]:not([hidden])");
    click(pick);
    const chips = () => Array.from($("chosen").querySelectorAll(".chip-x"));
    check("filters: a chosen status shows as a chip", chips().length === 1 && chips()[0].textContent.includes(pick.dataset.label));
    check("filters: the badge counts it", $("filter-n").textContent === "1");
    check("filters: the panel stays open while choosing", shown(pop));
    click(pop.querySelector("[data-kind]:not([hidden])"));
    check("filters: two chips and 「清除全部」", chips().length === 2 && !!$("chosen").querySelector(".clear-all"));
    document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", bubbles: true}));
    check("filters: Esc closes the panel", !shown(pop));
    click(chips()[0].querySelector("button"));
    check("filters: × drops a chip", chips().length === 1 && !location.hash.includes("st="), location.hash);
    click($("chosen").querySelector(".chip-x button"));
    check("filters: nothing chosen, no chips", chips().length === 0 && $("filter-n").textContent === "");
    sizes.filters_after = Math.round(box(tools).height);
  }

  function flow() {
    click($("view-flow"));
    const bar = $("flow-bar"), scroller = $("flow-scroll"), slot = $("flow-slot");
    const items = Array.from(bar.children);
    sizes.flow_bar = Math.round(box(bar).height);
    check("flow: toolbar in one row", oneRow(items) && sizes.flow_bar <= 40, sizes.flow_bar);
    check("flow: legend folded", !shown($("legend-pop")));
    click($("legend-open"));
    const lp = box($("legend-pop"));
    check("flow: 「图例与关系」 opens the legend", shown($("legend-pop")) && $("legend-pop").contains($("flow-legend")));
    check("flow: nothing covers the legend", inside($("legend-pop"), lp.left + 20, lp.top + 20));
    click(document.body);
    check("flow: a click elsewhere folds the legend", !shown($("legend-pop")));
    check("flow: footer hidden in the task flow", !shown(document.querySelector("footer")));
    // Larger, so the area has something to scroll both ways.
    for (let i = 0; i < 3; i++) click($("zoom-in"));
    check("flow: zoomed in", $("zoom-level").textContent === "150%", $("zoom-level").textContent);
    window.scrollTo(0, 0);
    flowFit();
    const s0 = box(scroller), top0 = box(slot).top;
    sizes.flow_area_start = Math.round(s0.height);
    check("flow: wider than the area", scroller.scrollWidth > scroller.clientWidth, `${scroller.scrollWidth}/${scroller.clientWidth}`);
    check("flow: the area ends at the window's lower edge (its scrollbar in view)", Math.abs(s0.bottom - innerHeight) < 2, `${Math.round(s0.bottom)}/${innerHeight}`);
    if (mode === "narrow") return;
    // Down: the page first, the area grows; it stops when the area reaches the top.
    let guard = 0;
    while (box(slot).top > 0.5 && guard++ < 50) {
      if (!wheel(scroller, 100)) break;
      flowFit();
    }
    const s1 = box(scroller);
    check("flow: wheel down scrolls the page until the area reaches the top", Math.abs(box(slot).top) < 1 && Math.abs(window.scrollY - top0) < 2,
      `${box(slot).top}/${window.scrollY}/${top0}`);
    check("flow: the area fills the window", Math.abs(s1.height - innerHeight) < 2, Math.round(s1.height));
    sizes.flow_area_full = Math.round(s1.height);
    const max = document.scrollingElement.scrollHeight - innerHeight;
    check("flow: the page ends with the area (no scrolling past it)", Math.abs(max - window.scrollY) < 2, `${max}/${window.scrollY}`);
    check("flow: taller than the area", scroller.scrollHeight > scroller.clientHeight + 40, `${scroller.scrollHeight}/${scroller.clientHeight}`);
    check("flow: then the wheel goes to the area", !wheel(scroller, 100));
    scroller.scrollTop = 40;
    check("flow: up, the area first", !wheel(scroller, -100));
    scroller.scrollTop = 0;
    const y = window.scrollY;
    check("flow: up at the area's top scrolls the page", wheel(scroller, -100) && window.scrollY < y, `${y}->${window.scrollY}`);
    // Drag: the view moves, and the click that ends the drag opens nothing.
    window.scrollTo(0, top0);
    flowFit();
    const node = scroller.querySelector(".fnode:not([hidden])");
    const at = box(node);
    const left = scroller.scrollLeft, topIn = scroller.scrollTop;
    const pointer = (type, x, y2) => node.dispatchEvent(new PointerEvent(type, {bubbles: true, cancelable: true, pointerType: "mouse", pointerId: 9, button: 0, clientX: x, clientY: y2}));
    const x0 = at.left + 10, y0 = at.top + 10;
    pointer("pointerdown", x0, y0); pointer("pointermove", x0 - 40, y0 - 10); pointer("pointermove", x0 - 120, y0 - 30); pointer("pointerup", x0 - 120, y0 - 30);
    check("flow: mouse drag pans the view", scroller.scrollLeft > left, `${left}->${scroller.scrollLeft}/${topIn}->${scroller.scrollTop}`);
    const clickEnd = new MouseEvent("click", {bubbles: true, cancelable: true, view: window});
    node.dispatchEvent(clickEnd);
    check("flow: the click ending a drag opens nothing", clickEnd.defaultPrevented && location.hash.includes("view=flow"), location.hash);
    // The other views are as they were.
    click($("view-status"));
    check("board: the task flow's slot is gone", !shown(slot) && !shown($("flow")));
    check("board: footer back", shown(document.querySelector("footer")));
    window.scrollTo(0, 0);
    check("board: the wheel is the page's", !wheel(document.querySelector("#board"), 100));
  }
  function flowFit() { window.dispatchEvent(new Event("scroll")); }

  function run() {
    try {
      sizes.viewport = `${innerWidth}x${innerHeight}`;
      check(`mode ${mode} matches the window`, (mode === "narrow") === matchMedia("(max-width:640px)").matches, innerWidth);
      header();
      tiles();
      filters();
      flow();
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
