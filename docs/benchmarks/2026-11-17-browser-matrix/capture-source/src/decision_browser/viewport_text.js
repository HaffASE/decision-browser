() => {
  // Resolve DOM interfaces on their native prototype chain, bypassing form named properties.
  const read = (node, key) => Reflect.get(Object.getPrototypeOf(node), key, node);
  const invoke = (node, key, ...args) => Reflect.apply(read(node, key), node, args);
  const parts = [];
  let length = 0, visited = 0;
  const body = read(document, 'body');
  if (!body) return '';
  const parentOf = el => read(el, "parentElement");
  const width = document.documentElement.clientWidth, height = document.documentElement.clientHeight;
  const walker = invoke(document, 'createTreeWalker', body, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (++visited > 20000 || length >= 4096) break;
    const el = parentOf(node);
    if (!el || invoke(el, "closest", 'script,style,noscript,textarea,input,select,[hidden],[aria-hidden="true"]')) continue;
    let hidden = ['hidden', 'collapse'].includes(getComputedStyle(el).visibility);
    let parent = el, depth = 0;
    for (; !hidden && parent && depth < 10000; parent = parentOf(parent), depth++) {
      const style = getComputedStyle(parent);
      if (read(parent, "inert") || style.display === 'none' || Number(style.opacity) === 0) {hidden = true; break;}
    }
    if (hidden || parent) continue;
    const range = document.createRange();
    range.selectNodeContents(node);
    const visible = [...range.getClientRects()].some(r => {
      const left=Math.max(0,r.left), right=Math.min(width,r.right);
      const top=Math.max(0,r.top), bottom=Math.min(height,r.bottom);
      if (right <= left || bottom <= top) return false;
      const hit=invoke(document, 'elementFromPoint', (left+right)/2,(top+bottom)/2);
      return hit && (hit===el || invoke(el, "contains", hit));
    });
    if (!visible) continue;
    const value = String(read(node, "textContent") || '').replace(/\s+/g, ' ').trim();
    if (!value) continue;
    let clipped = '', added = 0;
    for (const point of value) {
      if (length + added >= 4096) break;
      clipped += point; added++;
    }
    parts.push(clipped); length += added + 1;
  }
  return parts.join('\n');
}
