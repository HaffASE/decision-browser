(selector, snapshot) => {
  // Resolve DOM interfaces on their native prototype chain, bypassing form named properties.
  const read = (node, key) => Reflect.get(Object.getPrototypeOf(node), key, node);
  const invoke = (node, key, ...args) => Reflect.apply(read(node, key), node, args);
  // No page-world registry: this runs in the same isolated world as the handles.
  const roles = new Set(['button', 'link', 'textbox', 'combobox', 'option',
    'gridcell', 'checkbox', 'radio', 'switch', 'tab', 'menuitem',
    'menuitemcheckbox', 'menuitemradio', 'slider', 'spinbutton']);
  const controls = 'a[href],button,input,textarea,select,summary';
  const active = [], inert = [], reasons = new Set();
  const hiddenReasons = new Set();
  const attr = (el, key) => invoke(el, "getAttribute", key);
  const counts = {scanned: 0, hidden: 0, covered: 0, decorative: 0,
    disabled: 0, unsupported: 0, hidden_unsupported: 0, capacity: 0};
  const nodes = invoke(document, "querySelectorAll", selector);
  for (const el of nodes) {
    if (++counts.scanned > 10000) { reasons.add('capacity'); counts.capacity++; break; }
    const role = (attr(el, 'role') || '').trim().toLowerCase().split(/\s+/)[0];
    const svgLink = read(el, "tagName").toUpperCase() === 'A' && read(el, "namespaceURI") === 'http://www.w3.org/2000/svg' && invoke(el, "hasAttributeNS", 'http://www.w3.org/1999/xlink', 'href');
    const native = invoke(el, "matches", controls) || read(el, "tagName") === 'FORM' || svgLink;
    const diagnostic = invoke(el, "matches", 'iframe,canvas,input[type="file"],input[type="password"],[download]') || read(el, "shadowRoot");
    if (!native && !diagnostic && !roles.has(role) && !(invoke(el, "hasAttribute", 'tabindex') && read(el, "tabIndex") >= 0)) {
      counts.decorative++; continue;
    }
    if (role === 'gridcell' && invoke(el, "querySelector", controls + ',[role="button"]')) {
      counts.decorative++; continue;
    }
    const snap = snapshot(el, 'click');
    const sem = snap.semantic, geo = snap.geometry;
    let reason = sem.unsupported;
    if (reason) {
      reason = ({shadowroot:'shadow', targetblank:'newwindow'})[reason] || reason;
      if (!geo.visible) {
        counts.hidden_unsupported++;
        hiddenReasons.add(reason);
        continue;
      }
      reasons.add(reason); counts.unsupported++;
      // Diagnostic targets are inert and cannot displace usable controls.
      if (inert.length < 80) inert.push(el);
      continue;
    }
    if (!geo.visible) { counts.hidden++; continue; }
    if (geo.occluded) { counts.covered++; continue; }
    // Observation is descriptive, not an activation grant. Keep visible role
    // containers/labels even when a native descendant/ancestor may receive the
    // click. The fresh CDP recipient guard must reject that mismatch before the
    // admission callback and native input; a collector guess cannot replace it.
    if (sem.node.state.disabled) { counts.disabled++; continue; }
    if (active.length < 81) active.push(el);
    else { counts.capacity++; reasons.add('capacity'); }
  }
  if (active.length > 80) { counts.capacity++; reasons.add('capacity'); }
  const result = active.slice(0, 80);
  let retained = 0;
  for (const el of inert) if (result.length < 80) {result.push(el); retained++;}
  const omitted = counts.unsupported - retained;
  if (omitted > 0) {counts.capacity += omitted; reasons.add('capacity');}
  const scroller = document.scrollingElement || document.documentElement;
  const top = scroller ? Number(scroller.scrollTop) || 0 : scrollY;
  const height = scroller ? Number(scroller.clientHeight) || innerHeight : innerHeight;
  const extent = scroller ? Number(scroller.scrollHeight) || height : height;
  result.diagnostics = {counts, reasons: [...reasons], hidden_reasons: [...hiddenReasons], viewport: {
    x: scrollX, y: scrollY, width: document.documentElement.clientWidth, height: document.documentElement.clientHeight,
    can_scroll_up: top > 0, can_scroll_down: top + height < extent - 1
  }};
  return result;
}
