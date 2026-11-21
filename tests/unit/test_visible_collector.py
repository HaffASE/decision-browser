"""Bounded collector regression without claiming native browser evidence."""
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from decision_browser.dom import Action, DOMSession, GuardResult


@pytest.mark.parametrize('prefix', ['hidden', 'decorative', 'covered', 'unsupported'])
def test_usable_control_survives_prefix_before_cap(prefix):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js required')
    source = Path('src/decision_browser/collector.js').read_text()
    js = r"""
const [source, kind] = JSON.parse(process.argv[1]);
// Model native prototype getters/methods separately from instance fixture data.
const intrinsicPrototype = new Proxy(Object.prototype, {get(target, key, receiver) {
 const own = Object.getOwnPropertyDescriptor(receiver, key);
 if (own) return own.value;
 if (key === 'matches') return () => false;
 return Reflect.get(target, key, receiver);
}});
const intrinsicObject = value => Object.setPrototypeOf(value, intrinsicPrototype);

const make = kind => ({kind, tagName:'DIV', tabIndex:-1,
 getAttribute: name => name === 'role' ? (kind === 'decorative' ? 'presentation' : 'button') : null,
 hasAttribute:()=>false, matches:()=>false, querySelector:()=>null});
const nodes = [...Array.from({length:120},()=>make(kind)),make('usable')];
global.Element={prototype:{getAttribute:function(k){return this.getAttribute(k)}}};
global.document = {documentElement:{clientWidth:800,clientHeight:600},querySelectorAll:()=>nodes, elementFromPoint:()=>null};
global.scrollX=0;global.scrollY=0;global.innerWidth=800;global.innerHeight=600;
const snapshot = el => ({semantic:{unsupported:el.kind==='unsupported'?'iframe':null,
 node:{state:{disabled:false}}},geometry:{visible:el.kind!=='hidden',occluded:el.kind==='covered',point:[1,1]}});
intrinsicObject(document);
for (const el of document.querySelectorAll()) intrinsicObject(el);
const result=eval('('+source+')')('selector',snapshot);
process.stdout.write(JSON.stringify({kinds:result.map(el=>el.kind),diagnostics:result.diagnostics}));
"""
    result = subprocess.run([node, '-e', js, json.dumps([source, prefix])],
                            capture_output=True, text=True, check=True)
    result = json.loads(result.stdout)
    assert result['kinds'][0] == 'usable'
    assert len(result['kinds']) <= 80
    assert result['diagnostics']['counts'][prefix] == 120
    if prefix == 'unsupported':
        assert result['diagnostics']['reasons'] == ['iframe', 'capacity']


@pytest.mark.asyncio
@pytest.mark.parametrize('status,focused', [('stale', True), ('pass', False)])
async def test_focus_recheck_fails_closed_after_dispatch(status, focused):
    dom = object.__new__(DOMSession)
    dom._guard = AsyncMock(return_value=GuardResult(status, 'changed'))
    action = Action('fill', 'target', 'value', None, 'observation', '{}')
    el = SimpleNamespace(evaluate=AsyncMock(return_value=focused))
    with pytest.raises(RuntimeError, match='focused recipient'):
        await dom._verify_focused_recipient(action, el)
    dom._guard.assert_awaited_once_with(action, focused_fill=True)
    assert el.evaluate.await_count == (1 if status == 'pass' else 0)


@pytest.mark.parametrize('type_', ['password', 'file', 'hidden'])
def test_sensitive_or_hidden_values_never_enter_click_snapshot(type_):
    from test_snapshot_semantics import snapshot_spec
    snap = snapshot_spec({'type': type_, 'caption': 'private', 'operation': 'click'})
    assert 'value' not in snap['semantic']['node']


def test_click_sees_current_input_value_and_focus_is_not_guard_semantic():
    from test_snapshot_semantics import snapshot_spec
    snap = snapshot_spec({'type': 'text', 'caption': 'JFK', 'operation': 'click'})
    assert snap['semantic']['node']['value'] == 'JFK'
    assert snap['context'] == {'focused': False}
    assert 'context' not in snap['semantic']
    assert 'focused' not in snap['semantic']['node']['state']


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['focus', 'selection', 'recheck'])
async def test_partial_fill_failure_never_inserts_text(failure):
    from decision_browser.dom import Action
    action = Action('fill', 'target', 'secret', None, 'obs', '{}')
    send = AsyncMock()
    if failure == 'focus':
        send.side_effect = RuntimeError('focus failed')
    el = SimpleNamespace(_bind=lambda world: None, backend_id=42,
                         evaluate=AsyncMock())
    if failure == 'selection':
        el.evaluate.side_effect = RuntimeError('selection failed')
    dom = object.__new__(DOMSession)
    dom._world = SimpleNamespace(_send=send)
    dom._handles = {'target': el}
    dom._prepared = {id(action): (action, {'_fill': {'value': 'secret', 'special': False}})}
    dom._verify_focused_recipient = AsyncMock()
    if failure == 'recheck':
        dom._verify_focused_recipient.side_effect = [None, RuntimeError('recipient changed')]
    with pytest.raises(RuntimeError):
        await dom._native(action)
    assert [call.args[0] for call in send.await_args_list] == ['DOM.focus']


@pytest.mark.parametrize('kind', ['anchor-descendant','anchor-ancestor','submit-descendant','label','svg'])
def test_visible_semantic_target_reaches_authoritative_dispatch_guard(kind):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js required')
    source = Path('src/decision_browser/collector.js').read_text()
    js = r"""
const [source, kind] = JSON.parse(process.argv[1]);
// Model native prototype getters/methods separately from instance fixture data.
const intrinsicPrototype = new Proxy(Object.prototype, {get(target, key, receiver) {
 const own = Object.getOwnPropertyDescriptor(receiver, key);
 if (own) return own.value;
 if (key === 'matches') return () => false;
 return Reflect.get(target, key, receiver);
}});
const intrinsicObject = value => Object.setPrototypeOf(value, intrinsicPrototype);

const native = {tagName:kind==='submit-descendant'?'BUTTON':kind==='svg'?'a':'A',
 namespaceURI:kind==='svg'?'http://www.w3.org/2000/svg':'http://www.w3.org/1999/xhtml',
 parentElement:null,matches:()=>true};
const target = {tagName:kind==='label'?'LABEL':kind==='anchor-ancestor'?'SPAN':'DIV',
 tabIndex:0,parentElement:kind==='anchor-ancestor'?native:null,
 getAttribute:name=>name==='role'?'button':null,hasAttribute:()=>false,
 matches:()=>false,querySelector:()=>null};
if(kind!=='anchor-ancestor') native.parentElement=target;
const hit = ['anchor-ancestor','label'].includes(kind)?target:native;
global.Element={prototype:{getAttribute:function(k){return this.getAttribute(k)}}};
global.document={documentElement:{clientWidth:800,clientHeight:600},querySelectorAll:()=>[target],elementFromPoint:()=>hit};
global.scrollX=0;global.scrollY=0;global.innerWidth=800;global.innerHeight=600;
const snapshot=()=>({semantic:{unsupported:null,node:{state:{disabled:false}}},
 geometry:{visible:true,occluded:false,point:[1,1]}});
intrinsicObject(document);
for (const el of document.querySelectorAll()) intrinsicObject(el);
const result=eval('('+source+')')('selector',snapshot);
process.stdout.write(JSON.stringify({retained:result.includes(target),covered:result.diagnostics.counts.covered}));
"""
    result = subprocess.run([node,'-e',js,json.dumps([source,kind])],
                            capture_output=True,text=True,check=True)
    assert json.loads(result.stdout) == {'retained':True,'covered':0}
