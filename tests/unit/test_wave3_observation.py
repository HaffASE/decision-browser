"""F10 acquisition and malformed-link regressions; browser proof lives beside these."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Error
from test_stale_refresh_diagnostics import scoped_run

from decision_browser.dom import DOMSession, _CDPHandle, _CDPWorld, _decorate


async def test_rejected_adoption_releases_acquired_remote():
    world = object.__new__(_CDPWorld)
    world._context_id = 1
    world._send = AsyncMock(side_effect=[{'node': {'backendNodeId': 4}}, {'result': {'value': False}}, {}, {}])
    world._evaluate_document = AsyncMock(return_value={'objectId': 'doc'})
    with pytest.raises(Error, match='foreign frame'):
        await world._adopt({'objectId': 'rejected', 'subtype': 'node'}, True)
    released = [call.args[1]['objectId'] for call in world._send.await_args_list if call.args[0] == 'Runtime.releaseObject']
    assert released.count('doc') == released.count('rejected') == 1


@pytest.mark.parametrize('failure', ['diagnostics', 'properties', 'candidate', 'dispose'])
async def test_observation_partial_failure_releases_every_acquired_handle(failure):
    handles = [SimpleNamespace(as_element=lambda: None, dispose=AsyncMock()) for _ in range(3)]
    for handle in handles:
        handle.as_element = lambda handle=handle: handle
    raw = SimpleNamespace(evaluate=AsyncMock(return_value={}), get_properties=AsyncMock(return_value=dict(zip(['0', '1', '2'], handles))), dispose=AsyncMock())
    if failure == 'diagnostics':
        raw.evaluate.side_effect = Error('diagnostics')
    if failure == 'properties':
        raw.get_properties.side_effect = Error('properties')
    if failure == 'dispose':
        raw.dispose.side_effect = Error('dispose')
    for handle in handles:
        handle.evaluate_handle = AsyncMock(return_value=SimpleNamespace(dispose=AsyncMock()))
    dom = object.__new__(DOMSession)
    dom._handles, dom._owners, dom._prepared = {}, {}, {}
    dom._bind_document = AsyncMock()
    dom._world = SimpleNamespace(evaluate=AsyncMock(side_effect=[{'semantic': {}}, '']), evaluate_handle=AsyncMock(return_value=raw))
    dom._candidate = AsyncMock(side_effect=Error('candidate'))
    with pytest.raises(Error):
        await dom.observe()
    assert raw.dispose.await_count == 1
    expected = 0 if failure in {'diagnostics', 'properties'} else 1
    assert [h.dispose.await_count for h in handles] == [expected] * 3
    assert not dom._handles and not dom._owners


async def test_partial_property_adoption_releases_all_remote_values():
    world = object.__new__(_CDPWorld)
    world._context_id = 1
    world._send = AsyncMock(return_value={'result': [
        {'name': '0', 'value': {'objectId': 'first'}},
        {'name': '1', 'value': {'objectId': 'bad'}},
        {'name': '2', 'value': {'objectId': 'last'}},
    ]})
    first = SimpleNamespace(dispose=AsyncMock())
    world._adopt = AsyncMock(side_effect=[first, Error('foreign frame')])
    world._release_ids = AsyncMock()
    handle = _CDPHandle(world, 'raw', 1, None, None, 'object', None, None)
    with pytest.raises(Error):
        await handle.get_properties()
    first.dispose.assert_awaited_once()
    world._release_ids.assert_awaited_once_with(['last'])


def test_malformed_link_skips_only_that_route():
    from decision_browser.dom import Candidate
    host, run, obs, _ = scoped_run()
    from dataclasses import replace
    candidates = {token: Candidate(token, ('click',), {'click': {'node': {'href': href, 'name': token}}}, None)
                  for token, href in [('bad', 'http://[::1'), ('safe', 'https://fixture.test/safe')]}
    _, _, bindings = host._questions(run, replace(obs, candidates=candidates))
    assert {r['destination'] for r in bindings['navigate'].values()} == {run.request.start_url, 'https://fixture.test/safe'}
    assert 'observation:malformed_link' in run.result.evidence
    assert 'bad' not in bindings.get('click', {})


def test_namedtarget_mapping_is_explicit():
    from decision_browser.dom import _UNSUP
    assert _UNSUP['namedtarget'] == 'namedtarget'
    assert _decorate({'semantic': {'unsupported': 'namedtarget'}}, None, 'click', '')['unsupported'] == 'namedtarget'


@pytest.mark.parametrize('failure', ['describe', 'backend', 'document', 'context'])
async def test_adoption_partial_failures_release_incoming_object(failure):
    world = object.__new__(_CDPWorld)
    world._context_id = None if failure == 'context' else 1
    world._send = AsyncMock(return_value={'node': {'backendNodeId': 7}, 'result': {'value': True}})
    world._evaluate_document = AsyncMock(return_value={'objectId': 'doc'})
    world._release_ids = AsyncMock()
    if failure == 'describe':
        world._send.side_effect = Error('describe')
    elif failure == 'backend':
        world._send.return_value = {'node': {}}
    elif failure == 'document':
        world._evaluate_document.side_effect = Error('document')
    with pytest.raises(Error):
        await world._adopt({'objectId': 'incoming', 'subtype': 'node'}, True)
    assert world._release_ids.await_args_list[-1].args == (['incoming'],)


async def test_drop_attempts_all_handles_after_disposal_error():
    dom = object.__new__(DOMSession)
    failed = SimpleNamespace(dispose=AsyncMock(side_effect=RuntimeError('dispose')))
    other = SimpleNamespace(dispose=AsyncMock())
    owner = SimpleNamespace(dispose=AsyncMock())
    dom._prepared = {1: 'prepared'}
    dom._handles, dom._owners = {'bad': failed, 'good': other}, {'owner': owner}
    with pytest.raises(RuntimeError):
        await dom._drop()
    for h in (failed, other, owner):
        h.dispose.assert_awaited_once()
    assert not dom._handles and not dom._owners and not dom._prepared


def test_snapshot_uses_layout_width_at_scrollbar_edge():
    from test_snapshot_semantics import snapshot_spec
    edge = snapshot_spec({'tag': 'BUTTON', 'viewportWidth': 90, 'innerWidth': 100,
                          'rect': {'x': 85, 'left': 85, 'width': 20}})
    assert edge['geometry']['visible']
    assert edge['geometry']['point'][0] == 87
    outside = snapshot_spec({'tag': 'BUTTON', 'viewportWidth': 90, 'innerWidth': 100,
                             'rect': {'x': 95, 'left': 95, 'width': 20}})
    assert not outside['geometry']['visible']


async def test_release_batch_continues_after_transport_error():
    world = object.__new__(_CDPWorld)
    world._send = AsyncMock(side_effect=[RuntimeError('release'), {}])
    with pytest.raises(RuntimeError):
        await world._release_ids(['first', 'last'])
    assert [c.args[1]['objectId'] for c in world._send.await_args_list] == ['first', 'last']


async def test_bind_document_releases_unregistered_fresh_handle():
    dom = object.__new__(DOMSession)
    fresh = SimpleNamespace(dispose=AsyncMock())
    dom._doc = SimpleNamespace(dispose=AsyncMock())
    dom._world = SimpleNamespace(refresh=AsyncMock(), evaluate_handle=AsyncMock(return_value=fresh), evaluate=AsyncMock(side_effect=RuntimeError('identity')))
    with pytest.raises(RuntimeError):
        await dom._bind_document()
    fresh.dispose.assert_awaited_once()


async def test_close_releases_document_even_when_drop_fails():
    dom = object.__new__(DOMSession)
    dom._drop = AsyncMock(side_effect=RuntimeError('drop'))
    doc = SimpleNamespace(dispose=AsyncMock())
    dom._doc = doc
    dom._world = SimpleNamespace(close=AsyncMock())
    with pytest.raises(RuntimeError):
        await dom.close()
    doc.dispose.assert_awaited_once()
    dom._world.close.assert_awaited_once()
    assert dom._doc is None


@pytest.mark.parametrize('name,role,expected', [
    ('ordinary', '', ['FORM', 'BUTTON']),
    ('tagName', '', ['FORM', 'BUTTON']),
    ('matches', '', ['FORM', 'BUTTON']),
    ('querySelector', 'gridcell', ['BUTTON']),
])
def test_followup_collector_native_interface_wins_over_named_properties(name, role, expected):
    import json
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which('node')
    if node is None:
        pytest.skip('Node required')
    script = r'''
const [source,name,role] = JSON.parse(process.argv[1]);
global.Element={prototype:{getAttribute:function(key){return this.getAttribute(key)}}};
const proto = {get tagName(){return 'FORM'},get namespaceURI(){return 'http://www.w3.org/1999/xhtml'},
 get shadowRoot(){return null}, getAttribute:key=>key==='role'?role:null,
 matches:()=>false, hasAttribute:()=>false, querySelector:()=>({})};
const form = Object.create(proto);
Object.defineProperty(form,name,{value:{tagName:'INPUT'}});
const sibling = Object.create({get tagName(){return 'BUTTON'},get namespaceURI(){return ''},
 get shadowRoot(){return null}, getAttribute:()=>null,matches:()=>true,hasAttribute:()=>false});
global.document = Object.assign(Object.create({querySelectorAll:()=>[form,sibling]}),
 {documentElement:{clientWidth:800,clientHeight:600}});
global.scrollX=0;global.scrollY=0;global.innerWidth=800;global.innerHeight=600;
const snapshot=()=>({semantic:{unsupported:null,node:{state:{disabled:false}}},geometry:{visible:true,occluded:false}});
const result=eval('('+source+')')('selector',snapshot);
process.stdout.write(JSON.stringify(result.map(el=>Reflect.get(Object.getPrototypeOf(el),'tagName',el))));
'''
    source = Path('src/decision_browser/collector.js').read_text()
    output = subprocess.run([node, '-e', script, json.dumps([source, name, role])], check=True, capture_output=True, text=True)
    assert json.loads(output.stdout) == expected
