(() => {
  const selector = 'button,input,select,textarea,label,p,a';
  const interactive = new Set(['BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'A']);
  const maxNodes = 256;
  const maxFindings = 128;
  const tolerance = 0.5;
  const findings = [];
  const nodes = [];
  const siblings = new Map();

  function visible(element, box) {
    return [box.x, box.y, box.width, box.height].every(Number.isFinite)
      && box.width > 0 && box.height > 0
      && element.checkVisibility({checkOpacity: true, checkVisibilityCSS: true});
  }

  function clipped(element, box) {
    for (let host = element.parentElement; host; host = host.parentElement) {
      const style = getComputedStyle(host);
      const bounds = host.getBoundingClientRect();
      const cuts = axis => axis === 'clip' || axis === 'hidden';
      if (cuts(style.overflowX) && (box.left < bounds.left - tolerance || box.right > bounds.right + tolerance)
          || cuts(style.overflowY) && (box.top < bounds.top - tolerance || box.bottom > bounds.bottom + tolerance)) {
        return true;
      }
    }
    return false;
  }

  function add(kind, index, region, other) {
    if (findings.length < maxFindings) {
      findings.push({kind, element: index, region, ...(other === undefined ? {} : {other})});
    }
  }

  const candidates = document.querySelectorAll(selector);
  for (let index = 0; index < Math.min(candidates.length, 4096) && nodes.length < maxNodes; index += 1) {
    const element = candidates[index];
    if (element.closest('[data-visual-ignore="true"]')) continue;
    const box = element.getBoundingClientRect();
    if (!visible(element, box)) continue;

    const id = nodes.length;
    const region = {x: box.x, y: box.y, width: box.width, height: box.height};
    const isClipped = clipped(element, box);
    nodes.push({...region, clipped: isClipped});
    if (isClipped) add('clipping', id, region);
    if (box.left < -tolerance || box.right > innerWidth + tolerance) add('responsive', id, region);

    if (interactive.has(element.tagName) && element.parentElement) {
      const peers = siblings.get(element.parentElement) ?? [];
      for (const peer of peers) {
        const x = Math.max(box.left, peer.box.left);
        const y = Math.max(box.top, peer.box.top);
        const width = Math.min(box.right, peer.box.right) - x;
        const height = Math.min(box.bottom, peer.box.bottom) - y;
        if (width > tolerance && height > tolerance) add('overlap', id, {x, y, width, height}, peer.id);
      }
      peers.push({box, id});
      siblings.set(element.parentElement, peers);
    }
  }
  return {diagnostic: 'complete', nodes, findings};
})()
