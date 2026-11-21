(element, operation) => {
  // Resolve DOM interfaces on their native prototype chain, bypassing form named properties.
  const read = (node, key) => Reflect.get(Object.getPrototypeOf(node), key, node);
  const invoke = (node, key, ...args) => Reflect.apply(read(node, key), node, args);
  const doc = element && read(element, "ownerDocument") ? read(element, "ownerDocument") : document;
  const view = read(doc, "defaultView");
  const base = { origin: view.location.origin, url: view.location.href, timeOrigin: view.performance.timeOrigin };
  if (element == null) return { semantic: base, geometry: null };
  const attr = (el, key) => invoke(el, "getAttribute", key);
  const has = (el, key) => invoke(el, "hasAttribute", key);
  const parentOf = (el) => read(el, "parentElement");
  const utf8len = (s) => {
    let n = 0;
    for (let i = 0; i < s.length; i++) {
      let c = s.charCodeAt(i);
      if (c >= 0xd800 && c <= 0xdbff && i + 1 < s.length) {
        const c2 = s.charCodeAt(i + 1);
        if (c2 >= 0xdc00 && c2 <= 0xdfff) {
          c = ((c - 0xd800) << 10) + (c2 - 0xdc00) + 0x10000;
          i++;
        }
      }
      n += c < 0x80 ? 1 : c < 0x800 ? 2 : c < 0x10000 ? 3 : 4;
      if (n > 4096) return n;
    }
    return n;
  };
  const norm = (s) => String(s || "").replace(/\s+/g, " ").trim();
  const cap = (s) => (s.length > 160 ? s.slice(0, 160) : s);
  const packName = (s) => {
    const named = norm(s);
    if (utf8len(named) > 4096) return { name: cap(named), oversized: true };
    const out = { name: cap(named), oversized: false };
    if (named.length > 160) out.fullName = named;
    return out;
  };
  const inputCaption = (el) => read(el, "tagName") === "INPUT" && ["submit", "button", "reset"].includes((attr(el, "type") || "").toLowerCase()) ? String(read(el, "value") || "") : null;
  const linkHref = (el) => attr(el, "href") ?? (read(el, "namespaceURI") === "http://www.w3.org/2000/svg" ? invoke(el, "getAttributeNS", "http://www.w3.org/1999/xlink", "href") : null);
  const accessibleName = (el) => {
    const by = attr(el, "aria-labelledby");
    let name = by ? by.split(/\s+/).map((id) => invoke(doc, "getElementById", id)).filter(Boolean).map((n) => read(n, "textContent") || "").join(" ") : null;
    if (!norm(name)) name = attr(el, "aria-label");
    if (!norm(name) && read(el, "labels") && read(el, "labels").length) name = [...read(el, "labels")].map((label) => {
      // An associated label names the control, not its current value/options.
      const copy = invoke(label, "cloneNode", true);
      for (const control of invoke(copy, "querySelectorAll", "input,select,textarea,button,option")) invoke(control, "remove");
      return read(copy, "textContent") || "";
    }).join(" ");
    if (!name) name = inputCaption(el);
    if (!name) name = read(el, "innerText") || read(el, "textContent") || attr(el, "alt") || attr(el, "title") || attr(el, "placeholder") || "";
    return name;
  };
  const evalRole = (el) => {
    const elTag = read(el, "tagName").toLowerCase();
    const elType = (attr(el, "type") || "").toLowerCase();
    const explicit = (attr(el, "role") || "").trim().toLowerCase().split(/\s+/).filter(Boolean)[0];
    const implicit = elTag === "a" && linkHref(el) !== null ? "link" : elTag === "button" || elTag === "summary" ? "button" : elTag === "select" ? (read(el, "multiple") ? "listbox" : "combobox") : elTag === "textarea" ? "textbox" : elTag === "option" ? "option" : elTag === "form" ? "form" : elTag === "img" ? "img" : elTag === "input" ? (elType === "number" ? "spinbutton" : elType === "range" ? "slider" : elType === "checkbox" ? "checkbox" : elType === "radio" ? "radio" : elType === "button" || elType === "submit" || elType === "reset" || elType === "image" ? "button" : "textbox") : elTag;
    return { tag: elTag, type: elType, role: explicit || implicit };
  };
  const targetCode = (raw) => {
    if (raw == null) return null;
    const v = String(raw).toLowerCase();
    if (v === "" || v === "_self" || v === "_top") return null;
    if (v === "_blank") return "targetblank";
    return "namedtarget";
  };
  const inheritedTarget = (raw) => {
    if (raw != null) return raw;
    const baseElement = invoke(doc, "querySelector", "base[target]");
    return baseElement ? attr(baseElement, "target") : null;
  };
  const pathOf = (el) => {
    const parts = [];
    let n = el, depth = 0;
    for (; n && parentOf(n) && depth < 10000; n = parentOf(n), depth++) {
      const kids = read(parentOf(n), "children");
      let idx = 0;
      for (let i = 0; i < kids.length; i++) if (kids[i] === n) idx = i;
      parts.push(idx);
    }
    if (n && parentOf(n)) oversized = true;
    return parts.reverse().join("/");
  };
  const nativeType = (el) => {
    if (!el) return null;
    const tag = read(el, "tagName");
    const prototype = tag === "BUTTON" ? view.HTMLButtonElement.prototype : tag === "INPUT" ? view.HTMLInputElement.prototype : null;
    return prototype ? Object.getOwnPropertyDescriptor(prototype, "type").get.call(el) : null;
  };
  const isSubmitControl = (el) => {
    const type = nativeType(el);
    const tag = el && read(el, "tagName");
    return tag === "BUTTON" ? type === "submit" : tag === "INPUT" && (type === "submit" || type === "image");
  };
  const effectivelyDisabled = (el) => !!(read(el, "disabled") || invoke(el, "matches", ":disabled") || (read(el, "tagName") === "OPTION" && (parentOf(el) && read(parentOf(el), "tagName")) === "OPTGROUP" && read(parentOf(el), "disabled")));
  const firstEnabledSubmit = (form) => {
    const root = read(doc, "documentElement");
    if (!root || !read(doc, "createTreeWalker")) return null;
    const walker = invoke(doc, "createTreeWalker", root, 1);
    let scanned = 0;
    for (let n = walker.currentNode; n; n = walker.nextNode()) {
      if (++scanned > 10000) { oversized = true; return null; }
      if (read(n, "form") === form && isSubmitControl(n) && !effectivelyDisabled(n)) return n;
    }
    return null;
  };
  const described = evalRole(element);
  const tag = described.tag;
  const type = described.type;
  const packed = packName(accessibleName(element));
  let oversized = packed.oversized;
  const state = { tag, type: type || null, disabled: !!(effectivelyDisabled(element) || attr(element, "aria-disabled") === "true"), readonly: !!(read(element, "readOnly") || has(element, "readonly") || attr(element, "aria-readonly") === "true"), checked: !!read(element, "checked"), aria: { checked: attr(element, "aria-checked"), selected: attr(element, "aria-selected"), expanded: attr(element, "aria-expanded"), pressed: attr(element, "aria-pressed"), current: attr(element, "aria-current"), busy: attr(element, "aria-busy") } };
  const root = invoke(element, "getRootNode");
  let unsupported = tag === "iframe" ? "iframe" : tag === "canvas" ? "canvas" : read(element, "shadowRoot") || (root && root instanceof view.ShadowRoot) ? "shadowroot" : type === "file" ? "file" : type === "password" ? "password" : has(element, "download") ? "download" : null;
  const node = { role: described.role, name: packed.name, state };
  if (read(element, "willValidate") && read(element, "validity")?.valueMissing) node.valueMissing = true;
  if (packed.fullName) node.fullName = packed.fullName;
  const addCaption = (el, target) => {
    const caption = inputCaption(el);
    if (caption == null) return;
    const packedCaption = packName(caption);
    if (packedCaption.oversized) oversized = true;
    target.caption = packedCaption.name;
    if (packedCaption.fullName) target.fullCaption = packedCaption.fullName;
  };
  addCaption(element, node);
  if (operation === "click" && linkHref(element) !== null) {
    try { node.href = new URL(linkHref(element), read(doc, "baseURI")).href; } catch (e) { node.href = linkHref(element); }
  }
  if (["input", "textarea", "select"].includes(tag) && !["password", "file", "hidden"].includes(type)) {
    const value = read(element, "value") == null ? "" : String(read(element, "value"));
    if (utf8len(value) > 4096) oversized = true;
    else node.value = value;
  }
  if (operation === "select" && read(element, "options")) {
    const opts = read(element, "options");
    if (opts.length > 80) oversized = true;
    else {
      const options = [];
      let bad = false;
      for (let i = 0; i < opts.length; i++) {
        const o = opts[i];
        const value = read(o, "value") == null ? "" : String(read(o, "value"));
        const label = read(o, "label") == null ? "" : String(read(o, "label"));
        if (utf8len(value) > 4096 || utf8len(label) > 4096) { bad = true; break; }
        options.push([value, label, effectivelyDisabled(o)]);
      }
      if (bad) oversized = true;
      else {
        node.options = options;
        const selected = read(element, "selectedOptions");
        const picked = [];
        if (selected) for (let i = 0; i < selected.length; i++) picked.push(read(selected[i], "value"));
        node.selected = picked;
      }
    }
  }
  const routed = operation === "click" || operation === "fill" || operation === "select" || operation === "enter" || operation === "submit";
  let owner = null;
  if (routed) owner = tag === "form" ? element : read(element, "form") || invoke(element, "closest", "form") || null;
  let submitterEl = null;
  if (operation === "enter" && owner) submitterEl = firstEnabledSubmit(owner);
  let form = null;
  let routing = null;
  if (owner) {
    // Keep raw routing mutations fresh without treating ignored attributes as
    // native overrides. This metadata is fingerprint-only, outside node/form.
    const rawAttributes = (el, keys) => {
      const attributes = {};
      for (const key of keys) {
        const value = attr(el, key);
        if (value != null && utf8len(value) > 4096) {
          oversized = true;
          attributes[key] = null;
        } else attributes[key] = value;
      }
      return attributes;
    };
    routing = {
      element: rawAttributes(element, ["type", "formaction", "formmethod", "formtarget"]),
      form: rawAttributes(owner, ["action", "method", "target"]),
    };
    if (operation === "enter") routing.submitter = submitterEl ? rawAttributes(submitterEl, ["type", "formaction", "formmethod", "formtarget"]) : null;
    const overrideEl = operation === "enter" ? submitterEl : (operation === "click" && isSubmitControl(element) ? element : null);
    const present = (k) => {
      if (!overrideEl) return null;
      const v = attr(overrideEl, k);
      return v;
    };
    const formAction = present("formaction");
    const formMethod = present("formmethod");
    const formTarget = present("formtarget");
    const actionAttr = formAction != null ? formAction : (attr(owner, "action") || "");
    const rawMethod = String(formMethod != null ? formMethod : (attr(owner, "method") || "get")).toLowerCase();
    const method = ["post", "dialog"].includes(rawMethod) ? rawMethod : "get";
    let action = actionAttr;
    try { action = new URL(actionAttr || view.location.href, read(doc, "baseURI")).href; } catch (e) {}
    const submits = operation === "enter" || operation === "submit" || (operation === "click" && isSubmitControl(element));
    // Admission and post-navigation checks consume this same native decision.
    form = { path: pathOf(owner), id: attr(owner, "id") || null, name: attr(owner, "name"), action, method, formAction, formMethod, formTarget, submits };
    if (submits && method === "get") {
      // Read successful controls without FormData: its construction fires a
      // page-owned formdata handler during observation. Unsupported cases deny.
      const controls = read(owner, "elements");
      const params = new view.URLSearchParams();
      const crlf = value => String(value).replace(/\r\n|\r|\n/g, "\r\n");
      const charset = attr(owner, "accept-charset");
      let supported = (!charset || charset.trim().toLowerCase() === "utf-8") && String((overrideEl && read(overrideEl, "type")) || "").toLowerCase() !== "image";
      const append = (name, value) => {
        if (utf8len(name) > 4096 || utf8len(value) > 4096) { supported = false; return; }
        params.append(crlf(name), crlf(value));
        if (params.toString().length > 4096) supported = false;
      };
      if (controls.length > 160) supported = false;
      for (let i = 0; supported && i < controls.length; i++) {
        const control = controls[i];
        const name = attr(control, "name");
        const kind = String(read(control, "type") || "").toLowerCase();
        if (effectivelyDisabled(control) || !name || kind === "reset" || kind === "button" || (["checkbox", "radio"].includes(kind) && !read(control, "checked"))) continue;
        if (isSubmitControl(control) && control !== overrideEl) continue;
        if (!["INPUT", "SELECT", "TEXTAREA", "BUTTON", "FIELDSET", "OUTPUT"].includes(read(control, "tagName")) || kind === "file" || kind === "password" || kind === "image" || has(control, "dirname") || name === "_charset_") { supported = false; break; }
        if (["FIELDSET", "OUTPUT"].includes(read(control, "tagName"))) continue;
        if (read(control, "tagName") === "SELECT") {
          if (read(control, "options").length > 80) { supported = false; break; }
          for (const option of read(control, "options")) if (supported && read(option, "selected") && !effectivelyDisabled(option)) append(name, read(option, "value"));
        } else append(name, read(control, "value"));
      }
      try {
        const destination = new view.URL(action);
        destination.search = params.toString();
        if (utf8len(destination.href) > 4096) supported = false;
        if (supported) form.destination = destination.href;
      } catch (e) { supported = false; }
      if (!supported && !unsupported) unsupported = "formserialization";
    }
    if (operation === "enter") {
      if (!submitterEl) form.defaultSubmitter = null;
      else {
        const sub = evalRole(submitterEl);
        const subName = packName(accessibleName(submitterEl));
        if (subName.oversized) oversized = true;
        const submitType = nativeType(submitterEl);
        form.defaultSubmitter = { path: pathOf(submitterEl), role: sub.role, name: subName.name, type: submitType };
        if (subName.fullName) form.defaultSubmitter.fullName = subName.fullName;
        addCaption(submitterEl, form.defaultSubmitter);
      }
    }
    if (!unsupported) {
      const effectiveTarget = inheritedTarget(formTarget != null ? formTarget : attr(owner, "target"));
      const elCode = targetCode(tag === "a" ? inheritedTarget(attr(element, "target")) : attr(element, "target"));
      const formCode = targetCode(effectiveTarget);
      const code = elCode === "targetblank" || formCode === "targetblank" ? "targetblank" : (elCode || formCode);
      if (code) unsupported = code;
    }
  } else if (!unsupported) {
    const code = targetCode(tag === "a" ? inheritedTarget(attr(element, "target")) : attr(element, "target"));
    if (code) unsupported = code;
  }
  if (!unsupported && oversized) unsupported = "oversized";
  const rect = invoke(element, "getBoundingClientRect");
  const cs = view.getComputedStyle(element);
  let visible = !!(read(element, "isConnected") && rect.width > 0 && rect.height > 0 && cs.display !== "none" && !["hidden", "collapse"].includes(cs.visibility) && Number(cs.opacity) !== 0);
  let parent = element, depth = 0;
  for (; visible && parent && depth < 10000; parent = parentOf(parent), depth++) {
    const style = view.getComputedStyle(parent);
    if (read(parent, "hidden") || read(parent, "inert") || attr(parent, 'aria-hidden') === 'true' || style.display === 'none' || Number(style.opacity) === 0) visible = false;
  }
  if (parent) visible = false;
  const width = read(doc, "documentElement").clientWidth;
  const height = read(doc, "documentElement").clientHeight;
  const left = Math.max(0, rect.left), top = Math.max(0, rect.top);
  const right = Math.min(width, rect.left + rect.width), bottom = Math.min(height, rect.top + rect.height);
  visible = visible && right > left && bottom > top;
  const px = Math.floor((left + right) / 2), py = Math.floor((top + bottom) / 2);
  const hit = visible ? invoke(doc, "elementFromPoint", px, py) : null;
  const geometry = { rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height }, visible, occluded: !!(visible && (!hit || (hit !== element && !invoke(element, "contains", hit)))), point: [px, py] };
  // Focus is useful to the planner, but is deliberately outside the guard
  // fingerprint: DOM.focus itself must not invalidate an otherwise stable fill.
  const context = { focused: read(doc, "activeElement") === element };
  if (['list', 'aria-autocomplete', 'aria-controls', 'aria-haspopup'].some(attr => has(element, attr))) context.picker = true;
  return { semantic: Object.assign({}, base, { operation, unsupported, node, form, routing }), geometry, context };
}
