# Driving the dashboard editor headlessly (CDP + puppeteer)

A practical guide to automating the live Device Builder dashboard in a real
browser: create a device through the wizard, add components through the
"Recommended" catalog, save, and validate the resulting YAML with
`esphome config`. This is the end-to-end path used to verify flows that unit
tests can't reach (wizard DOM, recommended-add prerequisite sequencing,
cross-section pin-conflict UI).

It expands the short "Driving the browser headlessly" note in
[CLAUDE.md](CLAUDE.md) with the concrete selectors, helpers, and the
non-obvious traps that cost the most time.

## 1. Bring up both servers

The frontend dev server proxies `/ws` to the backend, so both must be up.

```bash
# backend on :6052 (dev mode serves index.html no-cache)
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m esphome_device_builder --dev configs

# frontend on a chosen port, in a checkout of esphome/device-builder-frontend
PORT=8392 npm run dev
```

Run both in the background and wait for both to LISTEN before driving:

```bash
for i in $(seq 1 60); do
  b=$(lsof -iTCP:6052 -sTCP:LISTEN -t 2>/dev/null)
  f=$(lsof -iTCP:8392 -sTCP:LISTEN -t 2>/dev/null)
  [ -n "$b" ] && [ -n "$f" ] && { echo "both up"; break; }
  sleep 1
done
```

Deep-link an editor at `http://localhost:8392/device/<configuration>.yaml`.
Drop repro configs in `configs/` (gitignored). Cleanup:
`lsof -iTCP:6052 -iTCP:8392 -sTCP:LISTEN -t | xargs kill`.

Install puppeteer in a scratch dir (it downloads its own browser, no
OS-specific path): `npm i puppeteer`.

## 2. Everything is in shadow DOM

The SPA is Lit + Web Awesome. Ordinary `document.querySelector` misses almost
everything; you must walk shadow roots. Build a tiny helper library once and
reuse it. The two load-bearing primitives:

```js
// Find the first element matching a predicate, crossing every shadow boundary.
export async function deepFind(p, pred) {
  return await p.evaluateHandle((predSrc) => {
    // NOTE: eval of a function declaration returns undefined; wrap in parens
    // so it evaluates as an expression and yields the function.
    const fn = eval("(" + predSrc + ")");
    function walk(root) {
      const els = root.querySelectorAll ? root.querySelectorAll("*") : [];
      for (const el of els) {
        try { if (fn(el)) return el; } catch (e) {}
        if (el.shadowRoot) { const f = walk(el.shadowRoot); if (f) return f; }
      }
      return null;
    }
    return walk(document);
  }, pred.toString());
}

// Click any leaf-ish element whose trimmed text equals `text` (any tag).
export async function deepClickAny(p, text, { maxChildren = 1 } = {}) {
  const h = await p.evaluateHandle((text, maxChildren) => {
    let best = null;
    function walk(root) {
      const els = root.querySelectorAll ? root.querySelectorAll("*") : [];
      for (const el of els) {
        const t = (el.textContent || "").trim().replace(/\s+/g, " ");
        if (t === text && el.children.length <= maxChildren) best = el;
        if (el.shadowRoot) walk(el.shadowRoot);
      }
    }
    walk(document); return best;
  }, text, maxChildren);
  const el = h.asElement();
  if (!el) throw new Error("not found: " + text);
  await el.scrollIntoView().catch(() => {});
  await el.click();
  return el;
}
```

Traps that look like app bugs but are harness mistakes:

- `eval(fn.toString())` inside `page.evaluate` returns `undefined` because a
  function declaration is a statement. Wrap in parens: `eval("(" + src + ")")`.
- Not every clickable is a `<button>` / `<wa-button>`. The sidebar
  "Add component" and the catalog card "Select" / "Add" controls are styled
  `div`s. Match by text + leaf-ness, not by tag.
- `el.textContent` includes child icon text. Trim and collapse whitespace
  before comparing; prefer exact equality on leaf nodes over substring on
  containers.

## 3. Create a device through the wizard

The flow (each step gated on the previous rendering):

1. Click "Create device" (floating action button).
2. Click "Create new project" (the guided card).
3. Type the board name into the `Search boards…` input, then click the
   "Select" control inside the matching board card.
4. Type the device name into the `e.g. my-esp-device` input.
5. Click "Finish setup".

The page navigates to `/device/<name>.yaml` and the file appears in `configs/`.

Selecting inside a specific card means: find the leaf with the control text
("Select"), then climb parents (crossing shadow hosts via
`node.getRootNode().host`) until an ancestor's text contains the board name.

```js
let n = controlLeaf, hops = 0;
while (n && hops < 12) {
  if ((n.textContent || "").includes(boardText)) return controlLeaf;
  n = n.parentElement || (n.getRootNode && n.getRootNode().host) || null;
  hops++;
}
```

## 4. Add a component through the Recommended catalog

In the editor: click "Show components" (expands the section, reveals the
sidebar "Add component" control), then "Add component" opens
`esphome-add-component-dialog`, which hosts `esphome-component-catalog`.

The catalog "Recommended" tab lists one card per featured component. Each
card's title carries the featured id, uppercased and space-joined, e.g.
`GPIO Binary Sensor Featured KINCONY KC868 A16V3.BINARY SENSOR GPIO 11`. Target
a specific card by that label.

### Bound the card before matching

Climbing from a card's "Add" button up to "any ancestor that contains the
label" over-reaches into the scroll container (whose text contains every
card's label) and clicks the wrong card. Instead, bound the card to the
**largest ancestor that still contains exactly one "Add" button**, then test
the label on that bounded subtree:

```js
const countAdds = (node) => {
  let c = 0; const st = [node];
  while (st.length) {
    const x = st.pop();
    for (const k of (x.querySelectorAll ? x.querySelectorAll("*") : [])) {
      if ((k.textContent || "").trim() === "Add" && k.children.length <= 1) c++;
      if (k.shadowRoot) st.push(k.shadowRoot);
    }
  }
  return c;
};
// from the Add button, climb while the parent still bounds exactly one Add
let n = addBtn, card = addBtn, hops = 0;
while (n && hops < 10) {
  const up = n.parentElement || (n.getRootNode && n.getRootNode().host) || null;
  if (!up || countAdds(up) !== 1) break;
  card = up; n = up; hops++;
}
```

Use a digit-boundary in the label regex so "GPIO 1" does not match "GPIO 10":
`new RegExp("BINARY SENSOR GPIO 1(?!\\d)")`.

### The Recommended list is virtualized

Off-screen cards are not in the DOM. To reach a card that isn't currently
rendered, reset the catalog's scroll container to the top, then advance it by
~70% of its viewport in a loop, retrying the match after each step until the
card renders or you reach the bottom. Find the scroll container by walking for
the largest element where `scrollHeight > clientHeight`.

### Adding in order is the robust strategy

Added components are removed from "Recommended", so clicking the **first
remaining card of a given type** walks the list deterministically without
needing to scroll-hunt each specific id. Loop "open catalog -> add first of
type -> save" until no card of that type remains.

## 5. Prerequisite wizard

A featured component that sits on an I/O-expander pin carries a `requires`
chain (the i2c bus, then the hub). Clicking its "Add" starts a step wizard:
"Step 1 of N - Adding prerequisite for ...". Each step shows a form with a
"Back" button and a primary action ("Add" / "Continue"). Drive it by clicking
the primary action repeatedly until the footer disappears.

Identify the primary action as the action button that **shares a footer with a
"Back" button** (the catalog cards' "Add" buttons have no "Back" sibling):

```js
// among leaf buttons, a primary that shares a small ancestor with a Back button
for (const prim of primaries) {
  let n = prim, hops = 0;
  while (n && hops < 6) {
    const up = n.parentElement || (n.getRootNode && n.getRootNode().host) || null;
    if (!up) break;
    if (backs.some((bk) => up.contains && up.contains(bk))) { prim.click(); return; }
    n = up; hops++;
  }
}
```

Step counts confirm prerequisite reuse: the first expander component is 3 steps
(bus + hub + component), siblings on the same hub are 1 step, and the first
component on a second hub is 2 steps (new hub + component). A multi-bus board
also locks `i2c_id` onto the hub so the right bus is reused.

## 6. Persist and validate

Edits live in the editor until you click "Save"; the file on disk does not
change until then (a browser closed mid-sequence loses the in-memory edit).
After each add: close any open dialog (Escape), click "Save", wait, then copy
the on-disk YAML to a snapshot and validate it:

```bash
.venv/bin/esphome config configs/<name>.yaml   # exit 0 + "Configuration is valid!"
```

Validating after **each** add (not just at the end) localizes any regression to
the exact component that broke it.

## 7. Timing: verify state transitions, don't sleep-and-hope

The one race that masquerades as a deterministic bug: after "Save"
re-renders, the next "Add component" click can be swallowed and the dialog
opens late, so the current add fails and the next one succeeds (an alternating
pass/fail pattern). Don't fix it with a longer fixed sleep. After clicking
"Add component", verify the catalog actually opened (at least one featured card
present) and retry the click a few times if not:

```js
async function openCatalog(p) {
  await closeDialogs(p);
  for (let attempt = 0; attempt < 6; attempt++) {
    try { await deepClickAny(p, "Add component"); } catch (e) {}
    await sleep(1300);
    if (await countTypeCards(p, "FEATURED") > 0) return true;
    await sleep(700);
  }
  return false;
}
```

The same principle applies throughout: gate each action on an observable
post-condition (dialog open, card count changed, file on disk changed), not on
a fixed delay.

## 8. Proving a backend command without the wizard

To exercise a backend command without fighting the wizard DOM, open a
`WebSocket` to `/ws` in `page.evaluate` and send the frame the frontend sends
(`{command, message_id, args}`; shapes in the frontend repo's
`src/api/esphome-api.ts`). Gate the first send on the initial server frame
(`server_version`); replies are `{result}` or `{error_code, details}`.

For a before/after against unmodified code, restore one file from `origin/main`
without touching your branch, restart, then restore your branch:

```bash
git show origin/main:<path> > <path>   # run backend, observe pre-fix behaviour
git checkout HEAD -- <path>            # back to your branch
```
