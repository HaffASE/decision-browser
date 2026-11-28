(checks => {
  const privateControl = element => ['password', 'file', 'hidden'].includes(element.type)
    || /(?:^|\s)(?:current-password|new-password|one-time-code|cc-[\w-]+)(?:\s|$)/i.test(element.autocomplete || '');
  const privateNodes = Array.from(document.querySelectorAll('input,textarea,select')).filter(privateControl);
  let selectorRoot = document;
  let originals;
  if (privateNodes.length) {
    const root = document.documentElement;
    const all = root.querySelectorAll('*');
    if (all.length > 16384) return {url: location.href, results: checks.map(() => false)};

    // CSS, including :has() and attribute predicates, sees only public nodes.
    const sanitized = document.implementation.createHTMLDocument('');
    const tree = sanitized.importNode(root, true);
    const copies = tree.querySelectorAll('*');
    originals = new WeakMap([[tree, root]]);
    const hidden = new Set(privateNodes);
    for (let i = 0; i < copies.length; i += 1) {
      if (hidden.has(all[i])) copies[i].remove();
      else originals.set(copies[i], all[i]);
    }
    sanitized.replaceChild(tree, sanitized.documentElement);
    selectorRoot = sanitized;
  }

  const results = checks.map(check => {
    try {
      if (check.kind === 'route') return location.href === check.expected;
      const nodes = selectorRoot.querySelectorAll(check.target || '');
      if (check.kind === 'count') return nodes.length === check.expected;
      if (check.kind === 'visible' && nodes.length === 0) return check.expected === false;
      if (nodes.length !== 1) return false;
      const matched = nodes[0];
      const element = originals?.get(matched) ?? matched;
      if (check.kind === 'text') return matched.textContent === check.expected;
      if (check.kind === 'value') return ['INPUT', 'TEXTAREA', 'SELECT'].includes(element.tagName) && element.value === check.expected;
      if (check.kind === 'visible') {
        const box = element.getBoundingClientRect();
        const visible = element.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})
          && box.width > 0 && box.height > 0;
        return visible === check.expected;
      }
    } catch (_) {
      return false;
    }
    return false;
  });
  return {url: location.href, results};
})
