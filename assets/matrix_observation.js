(() => {
  const clean = value => String(value ?? '').replace(/\s+/g, ' ').trim();
  const associatedLabel = e => e.labels ? clean(Array.from(e.labels, l => {
    const copy = l.cloneNode(true);
    for (const control of copy.querySelectorAll('input,select,textarea,button')) control.remove();
    return copy.textContent;
  }).join(' ')) : '';
  const label = e => clean(e.getAttribute('aria-label') || associatedLabel(e) || e.getAttribute('placeholder')
    || (e.tagName === 'INPUT' && ['submit','button','reset'].includes(e.type) ? e.value : '')
    || e.textContent || e.getAttribute('title') || e.name || e.id).slice(0,4001);
  const privateControl = e => ['password','file','hidden'].includes(e.type)
    || /(?:^|\s)(?:current-password|new-password|one-time-code|cc-[\w-]+)(?:\s|$)/i.test(e.autocomplete || '');
  const effect = e => {
    const form = e.form;
    const submitter = form && ['INPUT','TEXTAREA'].includes(e.tagName) && !['submit','image','button'].includes(e.type)
      ? Array.from(form.elements).find(control => !control.disabled && ['submit','image'].includes(control.type)) : null;
    const owner = submitter || e;
    const destination = form ? [owner.hasAttribute('formaction') ? owner.formAction : form.action,
      owner.hasAttribute('formmethod') ? owner.formMethod : form.method,
      owner.hasAttribute('formtarget') ? owner.formTarget : form.target] : null;
    const signals = [label(e),e.id,e.name,form?.id,form?.name,submitter && label(submitter),submitter?.id,submitter?.name]
      .filter(Boolean).map(value => clean(value).slice(0,4000));
    const controls = form ? Array.from(form.elements) : [];
    const formState = controls.slice(0,256).map(control => [
      control.tagName,control.type,control.name,!!control.disabled,
      privateControl(control) && control.type !== 'hidden' ? null : String(control.value ?? '').slice(0,4001),
      !!control.checked,
      control.tagName === 'SELECT' ? Array.from(control.options).slice(0,256).map(option => [option.value,!!option.selected,!!option.disabled]) : null
    ]);
    return {
      href: e.tagName === 'A' ? e.href : null,
      destination,
      linkTarget: e.tagName === 'A' ? e.target : null,
      formContext: clean(form?.textContent).slice(0,2000),
      signals,
      formState,
      oversized: controls.length > 256 || formState.some(state => typeof state[4] === 'string' && state[4].length > 4000),
      sensitiveForm: !!form && Array.from(form.elements).some(control => !control.disabled && control.type !== 'hidden' && privateControl(control)),
      download: e.hasAttribute('download'),
      submitter: submitter ? [submitter.tagName,submitter.type,label(submitter),submitter.id,submitter.name] : null
    };
  };
  globalThis.__matrixLabel = label;
  globalThis.__matrixEffect = effect;
  globalThis.__matrixPrivateControl = privateControl;
  globalThis.__matrixNodes ??= new Map();
  globalThis.__matrixIds ??= new WeakMap();
  globalThis.__matrixNextId ??= 0;
  const choices = [];
  let scanned = 0, snapshotSize = 0, oversized = false;
  for (const e of document.querySelectorAll('a[href],button,input,textarea,select,[role="button"],[role="option"]')) {
    if (++scanned > 10000) { oversized = true; break; }
    const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    if (e.disabled || privateControl(e) || e.closest('[hidden],[inert]') || s.display === 'none' || s.visibility === 'hidden' || r.width <= 0 || r.height <= 0 || r.bottom <= 0 || r.top >= innerHeight || r.right <= 0 || r.left >= innerWidth) continue;
    const x = r.x + r.width / 2, y = r.y + r.height / 2, hit = document.elementFromPoint(x, y);
    if (!(hit === e || e.contains(hit))) continue;
    let id = globalThis.__matrixIds.get(e);
    if (!id) { id = `e${++globalThis.__matrixNextId}`; globalThis.__matrixIds.set(e,id); }
    globalThis.__matrixNodes.set(id,e);
    const operation = e.tagName === 'SELECT' ? 'select' : ((e.tagName === 'INPUT' && !['button','submit','checkbox','radio','range','file'].includes(e.type) && !e.readOnly) || e.tagName === 'TEXTAREA') ? 'fill' : 'click';
    const caption = label(e);
    if (caption.length > 4000) { oversized = true; break; }
    const choice = {id,operation,label:caption,aliases:Array.from(new Set([e.getAttribute('aria-label'),associatedLabel(e),e.getAttribute('placeholder'),e.getAttribute('name'),e.id].map(clean).filter(value => value && value.length <= 128)))};
    if (e.tagName === 'INPUT' || e.tagName === 'TEXTAREA' || e.tagName === 'SELECT') {
      choice.current = e.type === 'checkbox' || e.type === 'radio' ? String(e.checked) : e.value;
      if (choice.current.length > 4000) { oversized = true; break; }
    }
    if (e.tagName === 'SELECT') {
      if (e.options.length > 255) { oversized = true; break; }
      choice.options = Array.from(e.options, o => ({value:o.value,label:clean(o.textContent).slice(0,4000),disabled:o.disabled}));
    }
    if (e.tagName === 'A') choice.href = e.href;
    choice.effect = effect(e);
    if (choice.effect.oversized) { oversized = true; break; }
    choice.context = clean(e.closest('form')?.textContent).slice(0,2000);
    choice.inputType = e.type || '';
    choice.tag = e.tagName;
    snapshotSize += JSON.stringify(choice).length;
    if (choices.length >= 4096 || snapshotSize > 1000000) { oversized = true; break; }
    choices.push(choice);
  }
  return {url:location.href,text:document.body?.innerText?.slice(0,18000) || '',choices,oversized,scroll:{up:scrollY > 0,down:scrollY + innerHeight < document.documentElement.scrollHeight}};
})()
