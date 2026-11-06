// The stage runtime: what a staged film's page does when a beat asks (`{"do": ["print", …]}`).
// A staged film is a page of photogenic parts (stage.css) that the film kit plays on its virtual
// clock, so everything here schedules its steps with timers and returns at once, saying how many
// seconds it takes; the director holds the film for that long. Nothing here waits on real time.
//
// - show / hide: a part arrives whole (it fades in and rises a little) or leaves. A part with
//   .grow opens its own height as it arrives, carrying what sits below it.
// - away: a part travels up and off the stage as the next scene comes up under it.
// - swap: a card shows its next view in place (one card becomes the next), easing to its size.
// - print: lines arrive in a terminal or list, one after another; a full terminal scrolls.
// - type: words appear a character at a time, with a caret.
// - set: words or a value change in place: the old ones lift away, the new ones rise in, and a
//   part whose size changes eases to its new size.
// - tick: a checkbox, a picker row or a chip turns on.
// - press: a button dips, then its label changes on itself; its width holds.
// - fill: a bar or a progress line grows to a share.
// - page: a window switches to another of its pages.
// - together: several of these at once, as one beat.
(() => {
  const all = selector => [...document.querySelectorAll(selector)];
  const one = selector => {
    const found = document.querySelector(selector);
    if (!found) throw new Error(`The stage has nothing matching ${selector}`);
    return found;
  };
  const later = (ms, work) => setTimeout(work, ms);
  const frame = work => requestAnimationFrame(() => requestAnimationFrame(work));
  const TRAVEL = "cubic-bezier(0.45, 0, 0.1, 1)";
  const ARRIVE = "cubic-bezier(0.25, 0.8, 0.25, 1)";

  function show(selector, {stagger = 0.08} = {}) {
    const found = all(selector);
    found.forEach((element, index) => later(index * stagger * 1000, () => {
      if (element.classList.contains("grow")) {
        element.style.display = "block";
        void element.offsetHeight;
        element.style.height = `${element.scrollHeight}px`;
      }
      element.classList.add("is-on");
    }));
    return found.length ? 0.55 + (found.length - 1) * stagger : 0;
  }

  function hide(selector) {
    for (const element of all(selector)) element.classList.remove("is-on");
    return 0.35;
  }

  // A part travels up and off the stage, opaque all the way, as the next scene comes up under it.
  function away(selector) {
    for (const element of all(selector)) element.classList.add("is-away");
    return 0;
  }

  // A part shows one of its [data-view] children (direct ones) at a time. Swapping fades the current one out,
  // eases the part to the next one's size and fades that in: one card becomes the next.
  function swap(selector, name) {
    const part = one(selector);
    const views = [...part.children].filter(child => child.dataset.view !== undefined);
    const current = views.find(view => view.classList.contains("is-current"));
    const next = views.find(view => view.dataset.view === name);
    if (!next) throw new Error(`${selector} has no view ${name}`);
    const before = part.getBoundingClientRect().height;
    if (current) {
      current.style.transition = "opacity 0.18s ease-in";
      current.style.opacity = "0";
    }
    later(current ? 180 : 0, () => {
      if (current) {
        current.classList.remove("is-current");
        current.style.transition = current.style.opacity = "";
      }
      next.style.transition = "none";
      next.style.opacity = "0";
      next.classList.add("is-current");
      part.style.height = "";
      const after = part.getBoundingClientRect().height;
      part.style.transition = "none";
      part.style.height = `${before}px`;
      void part.offsetHeight;
      part.style.transition = `height 0.45s ${TRAVEL}`;
      part.style.height = `${after}px`;
      later(80, () => {
        next.style.transition = `opacity 0.35s ${ARRIVE}`;
        next.style.opacity = "";
      });
    });
    return 0.75;
  }

  // Each line is HTML for one row; they arrive `gap` seconds apart. A terminal keeps its newest
  // line in view by sliding its lines up, never by jumping.
  function print(selector, lines, {gap = 0.32} = {}) {
    const box = one(selector);
    const list = [].concat(lines);
    list.forEach((html, index) => later(index * gap * 1000, () => {
      const line = document.createElement("div");
      line.className = "line arrive";
      line.innerHTML = html;
      box.append(line);
      // What shows is the pane down to its padding or the stage's foot less a gutter, whichever
      // comes first: a window that runs off the stage keeps its newest line above the edge.
      const pane = box.closest(".window-body");
      const scroll = box.closest(".scroll");
      if (pane && scroll) {
        const style = getComputedStyle(pane);
        const top = pane.getBoundingClientRect().top + parseFloat(style.paddingTop);
        const bottom = Math.min(pane.getBoundingClientRect().bottom - parseFloat(style.paddingBottom), (document.querySelector(".stage") || document.body).getBoundingClientRect().bottom - 24);
        const over = Math.max(0, scroll.scrollHeight - (bottom - top));
        // Scrolled to a line's top, so the first line showing is whole.
        const tops = [...scroll.querySelectorAll(".line")].map(row => row.offsetTop);
        const snapped = over && (tops.find(y => y >= over - 0.5) ?? over);
        scroll.style.transform = `translateY(${-snapped}px)`;
      }
      frame(() => line.classList.add("is-on"));
    }));
    return list.length * gap + 0.25;
  }

  function type(selector, text, {cps = 38} = {}) {
    const element = one(selector);
    element.textContent = "";
    element.classList.add("typing");
    [...text].forEach((character, index) => later(index * (1000 / cps), () => (element.textContent += character)));
    later(text.length * (1000 / cps) + 400, () => element.classList.remove("typing"));
    return text.length / cps + 0.4;
  }

  // Words that change roll: the old ones lift away and the new ones rise in, in the same place.
  // Inside a .sized part (a card, an event) the part eases to its new size, so what sits below
  // it is carried, not pushed.
  function set(selector, html) {
    for (const element of all(selector)) {
      const sized = element.closest(".sized");
      const before = sized?.getBoundingClientRect().height;
      element.style.transition = "opacity 0.18s ease-in, transform 0.18s ease-in";
      element.style.opacity = "0";
      element.style.transform = "translateY(-6px)";
      later(180, () => {
        element.innerHTML = html;
        element.style.transition = "none";
        element.style.transform = "translateY(6px)";
        if (sized) {
          sized.style.height = "";
          const after = sized.getBoundingClientRect().height;
          sized.style.transition = "none";
          sized.style.height = `${before}px`;
          void sized.offsetHeight;
          sized.style.transition = `height 0.42s ${TRAVEL}`;
          sized.style.height = `${after}px`;
        }
        void element.offsetHeight;
        element.style.transition = `opacity 0.3s ${ARRIVE}, transform 0.34s ${ARRIVE}`;
        element.style.opacity = "";
        element.style.transform = "";
      });
    }
    return 0.45;
  }

  function tick(selector, on = true) {
    for (const element of all(selector)) element.classList.toggle("is-ticked", on);
    return 0.3;
  }

  // A press: the button dips under the hand, then its label becomes each of `steps` in turn
  // (a working state, then the result). The button keeps its width, so its neighbours stay put.
  function press(selector, steps = []) {
    const button = one(selector);
    const label = button.querySelector(".label") ? `${selector} .label` : selector;
    button.style.width = `${button.getBoundingClientRect().width}px`;
    button.classList.add("is-pressed");
    later(140, () => button.classList.remove("is-pressed"));
    [].concat(steps).forEach((html, index) => later(220 + index * 750, () => {
      set(label, html);
      if (index === [].concat(steps).length - 1) later(180, () => button.classList.add("is-done"));
    }));
    return 0.6 + [].concat(steps).length * 0.75;
  }

  function fill(selector, share, {seconds = 1.2} = {}) {
    for (const bar of all(selector)) {
      bar.style.transitionDuration = `${seconds}s`;
      bar.style.width = `${Math.round(share * 100)}%`;
    }
    return seconds;
  }

  // A window's pages are its children with data-page; one shows at a time, crossing over.
  function page(selector, name) {
    const window_ = one(selector);
    for (const element of window_.querySelectorAll("[data-page]")) element.classList.toggle("is-current", element.dataset.page === name);
    for (const item of window_.querySelectorAll("[data-go]")) item.classList.toggle("is-current", item.dataset.go === name);
    return 0.5;
  }

  // Several changes as one beat, [[name, ...args], ...]: it takes as long as the longest.
  function together(calls) {
    return Math.max(0, ...calls.map(([name, ...args]) => window.stage[name](...args)));
  }

  window.stage = {show, hide, away, swap, print, type, set, tick, press, fill, page, together};

  // The film's clicks are real clicks, so a stage answers them as the product would:
  // [data-go] opens a page of its window and marks its nav item; [data-then] is a button whose
  // label becomes each of data-then's steps (split on |), and then shows what data-show names;
  // [data-tick] is a box that ticks or unticks what it names (its row).
  document.addEventListener("click", event => {
    const box = event.target.closest("[data-tick]");
    if (box) tick(box.dataset.tick, !one(box.dataset.tick).classList.contains("is-ticked"));
    const go = event.target.closest("[data-go]");
    if (go) page(`#${go.closest(".window").id}`, go.dataset.go);
    const then = event.target.closest("[data-then]");
    if (then && !then.classList.contains("is-done")) {
      const steps = then.dataset.then.split("|");
      press(`#${then.id}`, steps);
      if (then.dataset.show) later(220 + steps.length * 750, () => show(then.dataset.show));
    }
  });
})();
