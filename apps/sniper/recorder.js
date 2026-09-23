(() => {
  if (window.__bizRecorderInstalled) return;
  window.__bizRecorderInstalled = true;

  const DEBUG = !!window.__recorderDebug;
  const EXCLUDE_PATTERNS = window.__recorderExcludePatterns || [];
  const STRUCTURE_THRESHOLD = window.__recorderStructureThreshold || 3;

  const SUCCESS_SELECTORS = [
    ".swal2-success", ".swal2-icon-success",
    ".swal2-error", ".swal2-icon-error",
    ".toast-success", ".toast-error", ".toast-message",
    ".alert-success", ".alert-danger",
    "[class*='toast']", "[class*='notification']", "[class*='snackbar']",
    "[role='alert']",
  ];

  const INTERACTIVE_SELECTOR =
    'button, a, input, select, textarea, label, summary, option, ' +
    '[role="button"], [role="link"], [role="checkbox"], [role="radio"], ' +
    '[role="switch"], [role="tab"], [role="menuitem"], [role="option"], ' +
    '[role="combobox"], [role="textbox"], [tabindex], [contenteditable="true"]';

  const CLICK_DEDUPE_WINDOW_MS = 2500;
  const lastClickSignature = { key: null, time: 0 };
  const dropdownOpenedNotYetSelected = new WeakSet();
  let openCombobox = null;
  const lastEmittedValueBySelector = new Map();
  const prevPollValue = new Map();
  const positionalLabel = new WeakMap();
  let unlabeledCounter = 0;

  const _sendQueue = [];
  let _flushTimer = null;

  function safeSend(event) {
    if (window.__recordStep) {
      try { window.__recordStep(JSON.stringify(event)); } catch (e) {}
      return;
    }
    _sendQueue.push(event);
    if (!_flushTimer) {
      _flushTimer = setInterval(() => {
        if (!window.__recordStep) return;
        clearInterval(_flushTimer);
        _flushTimer = null;
        while (_sendQueue.length) {
          try { window.__recordStep(JSON.stringify(_sendQueue.shift())); } catch (e) {}
        }
      }, 200);
    }
  }

  function inScope() {
    if (!EXCLUDE_PATTERNS.length) return true;
    const href = location.href;
    return !EXCLUDE_PATTERNS.some((p) => {
      try { return new RegExp(p, "i").test(href); } catch (e) { return href.indexOf(p) !== -1; }
    });
  }

  function cellLabelText(cell) {
    if (!cell) return "";
    if (cell.querySelector("input, select, textarea, button")) return "";
    return cell.textContent.trim();
  }

  function nearestRowLabel(el) {
    const cell = el.closest("td");
    if (!cell) return "";
    let row = cell.parentElement;
    if (row && row.cells && row.cells.length) {
      const first = row.cells[0];
      if (first && first !== cell) {
        const text = cellLabelText(first);
        if (text) return text;
      }
    }
    const prevCell = cell.previousElementSibling;
    if (prevCell) {
      const text = cellLabelText(prevCell);
      if (text) return text;
    }
    return "";
  }

  function associatedLabelText(el) {
    if (el.id) {
      const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl) return lbl.textContent.trim();
    }
    const wrappingLabel = el.closest("label");
    if (wrappingLabel) return wrappingLabel.textContent.trim();
    return "";
  }

  function firstOptionText(el) {
    if (!el.options || !el.options.length) return "";
    const first = el.options[0];
    if (first && (!first.value || first.value === "")) {
      return first.textContent.trim().replace(/^Select\s*/i, "");
    }
    return "";
  }

  function columnHeaderText(el) {
    const cell = el.closest("td");
    if (!cell) return "";
    const table = cell.closest("table");
    if (!table) return "";
    const row = cell.parentElement;
    const colIndex = Array.prototype.indexOf.call(row.children, cell);
    const headerRow = table.querySelector("thead tr") || table.querySelector("tr");
    if (!headerRow || headerRow === row) return "";
    const headerCells = headerRow.querySelectorAll("th, td");
    const headerCell = headerCells[colIndex];
    return headerCell ? cellLabelText(headerCell) : "";
  }

  // Elements whose own visible text is a more meaningful label than a
  // (frequently machine-generated, e.g. "mui-42") id/name -- buttons,
  // links, anything with an ARIA role, or contenteditable regions.
  // Plain value-bearing inputs are deliberately excluded: they rarely
  // carry meaningful textContent, so placeholder/name/id stay best there.
  function prefersOwnText(el) {
    const tag = el.tagName.toLowerCase();
    return tag === "button" || tag === "a" || tag === "summary" ||
      !!el.getAttribute("role") || el.isContentEditable;
  }

  function elementLabel(el) {
    const ownText = (el.textContent || "").trim();
    const shortOwnText = ownText && ownText.length <= 80 ? ownText : "";

    const found =
      el.getAttribute("aria-label") ||
      associatedLabelText(el) ||
      nearestRowLabel(el) ||
      columnHeaderText(el) ||
      firstOptionText(el) ||
      (prefersOwnText(el) ? shortOwnText : "") ||
      el.getAttribute("placeholder") ||
      el.name ||
      el.id;
    if (found) return found;

    if (shortOwnText) return shortOwnText;

    if (!positionalLabel.has(el)) {
      unlabeledCounter += 1;
      const row = el.closest("tr");
      const rowInfo = row && row.parentElement
        ? ` (row ${Array.prototype.indexOf.call(row.parentElement.children, row) + 1})`
        : "";
      positionalLabel.set(el, `Field ${unlabeledCounter}${rowInfo}`);
    }
    return positionalLabel.get(el);
  }

  function isStructurallyInteractive(el) {
    return !!(el && el.nodeType === 1 && el.matches && el.matches(INTERACTIVE_SELECTOR));
  }

  function isInteractive(el) {
    if (!el || el.nodeType !== 1) return false;
    if (isStructurallyInteractive(el)) return true;
    if (el.isContentEditable) return true;
    try { return getComputedStyle(el).cursor === "pointer"; } catch (e) { return false; }
  }

  function closestInteractive(target) {
    const structural = target.closest(INTERACTIVE_SELECTOR);
    if (structural) return structural;
    let node = target, depth = 0;
    while (node && node.nodeType === 1 && depth < 6) {
      if (isInteractive(node)) return node;
      node = node.parentElement;
      depth += 1;
    }
    return null;
  }

  function cssEscapeAttr(v) { return v.replace(/"/g, '\\"'); }

  function isUnique(selector) {
    try { return document.querySelectorAll(selector).length === 1; } catch (e) { return false; }
  }

  function buildXPath(el) {
    if (el.id) return `//*[@id="${el.id}"]`;
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && node !== document.body) {
      let index = 1;
      let sibling = node.previousElementSibling;
      while (sibling) {
        if (sibling.tagName === node.tagName) index += 1;
        sibling = sibling.previousElementSibling;
      }
      parts.unshift(`${node.tagName.toLowerCase()}[${index}]`);
      node = node.parentElement;
    }
    return "//body/" + parts.join("/");
  }

  function bestSelector(el) {
    const candidates = [];

    if (el.id) {
      const sel = `#${CSS.escape(el.id)}`;
      candidates.push({ type: "id", selector: `page.locator("${sel}")`, ok: isUnique(sel) });
    }
    if (el.name) {
      const sel = `[name="${cssEscapeAttr(el.name)}"]`;
      candidates.push({ type: "name", selector: `page.locator('${sel}')`, ok: isUnique(sel) });
    }
    const role = el.getAttribute("role") ||
      (el.tagName.toLowerCase() === "a" ? "link" : el.tagName.toLowerCase() === "button" ? "button" : "");
    if (role) {
      const name = elementLabel(el);
      if (name) {
        candidates.push({
          type: "role",
          selector: `page.get_by_role("${role}", name="${name.replace(/"/g, '\\"')}")`,
          ok: true,
        });
      }
    }
    const testId = el.getAttribute("data-testid");
    if (testId) {
      candidates.push({ type: "data-testid", selector: `page.get_by_test_id("${testId}")`, ok: true });
    }
    const ariaLabel = el.getAttribute("aria-label");
    if (ariaLabel) {
      candidates.push({
        type: "aria-label",
        selector: `page.get_by_label("${ariaLabel.replace(/"/g, '\\"')}")`,
        ok: true,
      });
    }
    const labelText = associatedLabelText(el);
    if (labelText) {
      candidates.push({
        type: "label",
        selector: `page.get_by_label("${labelText.replace(/"/g, '\\"')}")`,
        ok: true,
      });
    }
    const placeholder = el.getAttribute("placeholder");
    if (placeholder) {
      candidates.push({
        type: "placeholder",
        selector: `page.get_by_placeholder("${placeholder.replace(/"/g, '\\"')}")`,
        ok: true,
      });
    }
    if (["button", "a"].includes(el.tagName.toLowerCase()) || el.type === "submit" || role) {
      const text = el.textContent.trim();
      if (text && text.length <= 80) {
        candidates.push({
          type: "text",
          selector: `page.get_by_text("${text.replace(/"/g, '\\"')}", exact=True)`,
          ok: true,
        });
      }
    }
    if (el.classList && el.classList.length) {
      for (const cls of el.classList) {
        const sel = `.${CSS.escape(cls)}`;
        if (isUnique(sel)) {
          candidates.push({ type: "css-class", selector: `page.locator("${sel}")`, ok: true });
          break;
        }
      }
    }

    const best = candidates.find((c) => c.ok) || null;
    const xpath = `page.locator('xpath=${buildXPath(el)}')`;

    return { best: best ? best.selector : xpath, best_type: best ? best.type : "xpath", fallback: xpath };
  }

  function emitFieldEvent(el, actionType, value) {
    const sel = bestSelector(el);
    const key = sel.best;
    if (lastEmittedValueBySelector.get(key) === value) return;
    lastEmittedValueBySelector.set(key, value);

    safeSend({
      kind: "field",
      action_type: actionType,
      description: elementLabel(el),
      value: value,
      tag: el.tagName.toLowerCase(),
      best_selector: sel.best,
      best_selector_type: sel.best_type,
      fallback_selector: sel.fallback,
      wait_condition: "element visible & enabled",
      timestamp: Date.now(),
    });
  }

  function emitActionEvent(el, actionType) {
    emitActionEventWithValue(el, actionType, null);
  }

  function emitActionEventWithValue(el, actionType, value) {
    const sel = bestSelector(el);
    safeSend({
      kind: "action",
      action_type: actionType,
      description: elementLabel(el),
      value: value,
      best_selector: sel.best,
      best_selector_type: sel.best_type,
      fallback_selector: sel.fallback,
      wait_condition: "element clickable",
      timestamp: Date.now(),
    });
  }

  function clickSignature(el) {
    return el.id || el.name || elementLabel(el) || el.outerHTML.slice(0, 80);
  }

  function debugDescribe(el) {
    if (!el || !el.tagName) return "(no element)";
    const tag = el.tagName.toLowerCase();
    const bits = [tag];
    if (el.id) bits.push(`id="${el.id}"`);
    if (el.name) bits.push(`name="${el.name}"`);
    if (el.className && typeof el.className === "string") bits.push(`class="${el.className.slice(0, 60)}"`);
    bits.push(`inScope=${inScope()}`);
    bits.push(`wouldCapture=${inScope() && el.type !== "password" && isInteractive(el)}`);
    return bits.join(" ");
  }

  function sendDebug(eventName, el) {
    if (!DEBUG) return;
    let value = null;
    if (el && el.type === "password") value = "[hidden]";
    else if (el && "value" in el) value = String(el.value).slice(0, 60);
    safeSend({ kind: "debug", action_type: eventName, description: debugDescribe(el), value, timestamp: Date.now() });
  }

  document.addEventListener("click", (e) => {
    const el = closestInteractive(e.target);
    sendDebug("raw-click", el || e.target);
    if (!inScope() || !el || el.type === "password") return;

    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute("role");

    if (tag === "select") {
      dropdownOpenedNotYetSelected.add(el);
      return;
    }
    if (role === "combobox") {
      openCombobox = el;
    }
    if (role === "option") {
      const optionText = (el.textContent || "").trim();
      if (openCombobox && optionText) {
        // Keep the option click (it is what replays a custom combobox),
        // but also associate the selected text with the trigger.
        emitActionEventWithValue(el, "select", optionText);
        openCombobox = null;
        return;
      }
    }
    if (tag === "input" && ["checkbox", "radio"].includes(el.type)) {
      emitFieldEvent(el, "select", el.checked ? "checked" : "unchecked");
      return;
    }
    if (["input", "textarea"].includes(tag)) return;

    const sig = clickSignature(el);
    const now = Date.now();
    if (sig === lastClickSignature.key && now - lastClickSignature.time < CLICK_DEDUPE_WINDOW_MS) return;
    lastClickSignature.key = sig;
    lastClickSignature.time = now;

    emitActionEvent(el, "click");
  }, true);

  document.addEventListener("change", (e) => {
    const el = e.target;
    sendDebug("raw-change", el);
    if (!inScope() || el.type === "password") return;
    const tag = el.tagName.toLowerCase();

    if (tag === "select") {
      dropdownOpenedNotYetSelected.delete(el);
      const selectedOption = el.options[el.selectedIndex];
      const value = selectedOption ? selectedOption.textContent.trim() : el.value;
      emitFieldEvent(el, "select", value);
      return;
    }
    // checkbox/radio: fully handled by the click listener (as a
    // "select" checked/unchecked event). Native browsers also fire a
    // "change" here with el.value defaulting to "on" -- without this
    // guard that produces a second, bogus "type: on" field event for
    // every checkbox/radio click.
    if (tag === "input" && ["checkbox", "radio"].includes(el.type)) return;

    if (tag === "input" || tag === "textarea") emitFieldEvent(el, "type", el.value);
  }, true);

  document.addEventListener("input", (e) => {
    const el = e.target;
    sendDebug("raw-input", el);
    if (!inScope() || el.type === "password") return;
    const tag = el.tagName.toLowerCase();

    // Same reasoning as the "change" listener above: checkbox/radio
    // are owned by the click handler, not the typing-debounce path.
    if (tag === "input" && ["checkbox", "radio"].includes(el.type)) return;

    // Do not emit from every input event.  The page bridge can briefly
    // block while the user is typing, which makes a timer capture a
    // partial value.  The change/blur handlers below record the settled
    // value, while polling still covers programmatic edits.
    if (!el.isContentEditable && tag !== "input" && tag !== "textarea") return;
  }, true);

  document.addEventListener("blur", (e) => {
    const el = e.target;
    if (!el || !inScope() || el.type === "password") return;
    const tag = el.tagName && el.tagName.toLowerCase();
    if (!el.isContentEditable && tag !== "input" && tag !== "textarea") return;
    const value = el.isContentEditable ? el.textContent : el.value;
    if (value) emitFieldEvent(el, "type", value);
  }, true);

  // Re-seed the baseline the instant a field gains focus. This closes
  // a narrower version of the same race the install-time/mutation-time
  // seeding fixes: a field that's inserted into the DOM and filled in
  // the *same* synchronous tick (e.g. a script that renders a modal
  // and immediately programmatically fills it) can still beat the
  // MutationObserver's seed, since MutationObserver callbacks run as a
  // microtask *after* that synchronous block finishes. Real user
  // interaction -- and Playwright's own .fill()/.type() -- always
  // focuses an element before changing its value, and "focus" fires
  // synchronously, so seeding here is guaranteed to land before the
  // edit it's meant to establish a baseline ahead of.
  document.addEventListener("focus", (e) => {
    const el = e.target;
    if (!el || el.nodeType !== 1 || !el.matches || !el.matches(POLLABLE_SELECTOR)) return;
    if (el.type === "checkbox" || el.type === "radio") return;
    prevPollValue.set(bestSelector(el).best, currentFieldValue(el));
    lastEmittedValueBySelector.set(bestSelector(el).best, currentFieldValue(el));
  }, true);

  document.addEventListener("submit", (e) => {
    const form = e.target;
    if (!form || !inScope()) return;
    const now = Date.now();
    if (now - lastClickSignature.time < CLICK_DEDUPE_WINDOW_MS) return;
    safeSend({
      kind: "action", action_type: "submit", description: "Submit form", value: null,
      best_selector: 'page.locator("form")', best_selector_type: "css",
      fallback_selector: 'page.locator("form")', wait_condition: "form valid", timestamp: now,
    });
  }, true);

  const POLL_INTERVAL_MS = 400;
  // checkbox/radio deliberately excluded: their DOM .value is a
  // constant ("on"/"") unrelated to checked state, so polling it
  // against the click handler's "checked"/"unchecked" semantics (a
  // different value-space, but keyed by the same selector in
  // lastEmittedValueBySelector) produces bogus "type: on" events.
  // Checkboxes/radios are already fully handled by the click listener.
  const POLLABLE_SELECTOR = 'input:not([type="checkbox"]):not([type="radio"]), select, textarea, [contenteditable="true"]';

  function currentFieldValue(el) {
    const tag = el.tagName.toLowerCase();
    if (tag === "select") {
      const opt = el.options[el.selectedIndex];
      return opt ? opt.textContent.trim() : el.value;
    }
    if (el.isContentEditable) return el.textContent;
    return el.value;
  }

  // ---------- Field baseline seeding (fixes a real capture-dropping race) ----------
  // BUG THIS FIXES: pollFields()'s old "first sighting" branch seeded
  // BOTH prevPollValue *and* lastEmittedValueBySelector the first time
  // it noticed a field -- which normally happens on the very next poll
  // tick after install (<= POLL_INTERVAL_MS = 400ms away). But a typed
  // edit was previously debounced before it emitted.
  // Since the poll interval can overlap that debounce, it would virtually always see
  // the value the user *just* typed, mark it "already emitted" as if
  // it were a pre-existing default, and the real debounced emit would
  // then silently no-op when it fired 100ms later -- permanently
  // dropping the very first edit to almost every field on the page.
  //
  // Fix: seed the baseline SYNCHRONOUSLY, the instant a field exists
  // (at install, and again the instant it's added to the DOM -- see
  // the MutationObserver below) -- not lazily on a timer that a real
  // edit can race and beat.
  function seedFieldBaseline(el) {
    if (el.type === "password" || el.type === "hidden" || el.disabled) return;
    const value = currentFieldValue(el);
    const sel = bestSelector(el).best;
    if (!prevPollValue.has(sel)) prevPollValue.set(sel, value);
    if (!lastEmittedValueBySelector.has(sel)) lastEmittedValueBySelector.set(sel, value);
  }

  function seedFieldBaselines(root) {
    if (!root || root.nodeType !== 1) return;
    if (root.matches && root.matches(POLLABLE_SELECTOR)) seedFieldBaseline(root);
    if (root.querySelectorAll) root.querySelectorAll(POLLABLE_SELECTOR).forEach(seedFieldBaseline);
  }

  seedFieldBaselines(document.documentElement);

  function pollFields() {
    if (!inScope()) return;
    document.querySelectorAll(POLLABLE_SELECTOR).forEach((el) => {
      if (el.type === "password" || el.type === "hidden" || el.disabled) return;
      // While a user is actively editing, the debounced input/change
      // handlers own the value. Polling at the same time can emit a
      // transient partial string before typing is complete.
      if (document.activeElement === el) return;
      const value = currentFieldValue(el);
      const sel = bestSelector(el).best;

      if (!prevPollValue.has(sel)) {
        // Defensive fallback only -- every field should already be
        // seeded (see seedFieldBaselines above). Deliberately NOT
        // touching lastEmittedValueBySelector here: doing so is what
        // caused the dropped-first-edit race this replaces.
        prevPollValue.set(sel, value);
        return;
      }
      if (!value) { prevPollValue.set(sel, value); return; }
      const prev = prevPollValue.get(sel);
      prevPollValue.set(sel, value);
      if (prev === value && lastEmittedValueBySelector.get(sel) !== value) {
        emitFieldEvent(el, el.tagName.toLowerCase() === "select" ? "select" : "type", value);
      }
    });
  }
  setInterval(pollFields, POLL_INTERVAL_MS);

  let lastKnownHref = location.href;

  function reportNavigateIfChanged(reason) {
    if (location.href === lastKnownHref) return;
    lastKnownHref = location.href;
    if (!inScope()) return;
    safeSend({
      kind: "action", action_type: "navigate", description: `Navigated (${reason})`,
      value: null, url: location.href, best_selector: null, fallback_selector: null,
      wait_condition: "url changed", timestamp: Date.now(),
    });
    scheduleStructureCheck(true);
  }

  const _origPushState = history.pushState;
  history.pushState = function (...args) {
    const ret = _origPushState.apply(this, args);
    reportNavigateIfChanged("pushState");
    return ret;
  };
  const _origReplaceState = history.replaceState;
  history.replaceState = function (...args) {
    const ret = _origReplaceState.apply(this, args);
    reportNavigateIfChanged("replaceState");
    return ret;
  };
  window.addEventListener("popstate", () => reportNavigateIfChanged("popstate"));
  window.addEventListener("hashchange", () => reportNavigateIfChanged("hashchange"));

  function emitStructureSnapshot(trigger) {
    if (!inScope()) return;
    const nodes = Array.from(document.querySelectorAll(INTERACTIVE_SELECTOR)).slice(0, 500);
    const elements = nodes.map((el) => {
      const sel = bestSelector(el);
      return {
        tag: el.tagName.toLowerCase(),
        role: el.getAttribute("role") || null,
        label: elementLabel(el),
        value: el.type === "password" ? null
          : el.isContentEditable ? el.textContent.slice(0, 200)
          : ("value" in el ? String(el.value).slice(0, 200) : null),
        best_selector: sel.best,
        fallback_selector: sel.fallback,
      };
    });
    safeSend({
      kind: "structure", action_type: trigger,
      description: `${elements.length} interactive element(s) on ${location.href}`,
      url: location.href, elements, timestamp: Date.now(),
    });
  }

  let addedInteractiveSinceSnapshot = 0;
  let structureDebounceTimer = null;
  let pendingForceSnapshot = false;
  const STRUCTURE_DEBOUNCE_MS = 400;

  function countInteractiveAdded(node) {
    if (node.nodeType !== 1) return 0;
    let count = isStructurallyInteractive(node) ? 1 : 0;
    if (node.querySelectorAll) count += node.querySelectorAll(INTERACTIVE_SELECTOR).length;
    return count;
  }

  function scheduleStructureCheck(force) {
    if (force) pendingForceSnapshot = true;
    if (structureDebounceTimer) clearTimeout(structureDebounceTimer);
    structureDebounceTimer = setTimeout(() => {
      structureDebounceTimer = null;
      const shouldSnapshot = pendingForceSnapshot || addedInteractiveSinceSnapshot >= STRUCTURE_THRESHOLD;
      pendingForceSnapshot = false;
      addedInteractiveSinceSnapshot = 0;
      if (shouldSnapshot) emitStructureSnapshot("dom-growth");
    }, STRUCTURE_DEBOUNCE_MS);
  }

  function reportOutcome(node, matchedSelector) {
    if (!inScope()) return;
    const isError = /error|danger/i.test(matchedSelector);
    safeSend({
      kind: "outcome", action_type: isError ? "error" : "success",
      description: (node.textContent || "").trim().slice(0, 200) || (isError ? "Error toast shown" : "Success toast shown"),
      value: null, best_selector: null, fallback_selector: null, wait_condition: null, timestamp: Date.now(),
    });
  }

  const domObserver = new MutationObserver((mutations) => {
    for (const m of mutations) {
      for (const node of m.addedNodes) {
        if (node.nodeType !== 1) continue;
        for (const sel of SUCCESS_SELECTORS) {
          if (node.matches && node.matches(sel)) { reportOutcome(node, sel); }
          else {
            const found = node.querySelector && node.querySelector(sel);
            if (found) reportOutcome(found, sel);
          }
        }
        addedInteractiveSinceSnapshot += countInteractiveAdded(node);
        // Modal/wizard-step fields need the same synchronous baseline
        // seed the initial page load gets (see seedFieldBaselines
        // above) -- otherwise a field that appears mid-session hits
        // the exact same first-edit-gets-dropped race.
        seedFieldBaselines(node);
      }
    }
    if (addedInteractiveSinceSnapshot > 0) scheduleStructureCheck(false);
  });
  domObserver.observe(document.documentElement, { childList: true, subtree: true });

  setTimeout(() => { if (inScope()) emitStructureSnapshot("initial"); }, 800);

  if (DEBUG) {
    safeSend({
      kind: "debug", action_type: "installed",
      description: `Recorder installed on ${location.href} (debug=${DEBUG}, threshold=${STRUCTURE_THRESHOLD})`,
      value: null, timestamp: Date.now(),
    });
  }
})();