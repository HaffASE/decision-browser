(() => {
  const clean = value => String(value ?? '').replace(/\s+/g, ' ').trim();
  const associatedLabel = e => e.labels ? clean(Array.from(e.labels, l => {
    const copy = l.cloneNode(true);
    for (const control of copy.querySelectorAll('input,select,textarea,button')) control.remove();
    return copy.textContent;
  }).join(' ')) : '';
  const label = e => clean(e.getAttribute('aria-label') || associatedLabel(e) || e.getAttribute('placeholder') || e.textContent || e.getAttribute('title') || e.name || e.id);
  globalThis.__matrixLabel = label;
  globalThis.__matrixNodes ??= new Map();
  globalThis.__matrixIds ??= new WeakMap();
  globalThis.__matrixNextId ??= 0;
  const choices = [];
  for (const e of document.querySelectorAll('a[href],button,input,textarea,select,[role="button"],[role="option"]')) {
    const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    if (e.disabled || e.closest('[hidden],[inert]') || s.display === 'none' || s.visibility === 'hidden' || r.width <= 0 || r.height <= 0 || r.bottom <= 0 || r.top >= innerHeight || r.right <= 0 || r.left >= innerWidth || e.type === 'hidden') continue;
    const x = r.x + r.width / 2, y = r.y + r.height / 2, hit = document.elementFromPoint(x, y);
    if (!(hit === e || e.contains(hit))) continue;
    let id = globalThis.__matrixIds.get(e);
    if (!id) { id = `e${++globalThis.__matrixNextId}`; globalThis.__matrixIds.set(e,id); }
    globalThis.__matrixNodes.set(id,e);
    const operation = e.tagName === 'SELECT' ? 'select' : ((e.tagName === 'INPUT' && !['button','submit','checkbox','radio','range','file'].includes(e.type) && !e.readOnly) || e.tagName === 'TEXTAREA') ? 'fill' : 'click';
    const choice = {id,operation,label:label(e),aliases:Array.from(new Set([e.getAttribute('aria-label'),associatedLabel(e),e.getAttribute('placeholder'),e.getAttribute('name'),e.id].map(clean).filter(Boolean)))};
    if (e.tagName === 'INPUT' || e.tagName === 'TEXTAREA' || e.tagName === 'SELECT') choice.current = e.type === 'checkbox' || e.type === 'radio' ? String(e.checked) : e.value;
    if (e.tagName === 'SELECT') choice.options = Array.from(e.options, o => ({value:o.value,label:clean(o.textContent),disabled:o.disabled}));
    if (e.tagName === 'A') choice.href = e.href;
    choices.push(choice);
  }
  return {url:location.href,text:document.body?.innerText?.slice(0,18000) || '',choices,scroll:{up:scrollY > 0,down:scrollY + innerHeight < document.documentElement.scrollHeight}};
})()
