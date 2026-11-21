"""Evaluate both shipped recipient checks with tree doubles; no native click proof."""
from __future__ import annotations

import ast
import json
import shutil
import subprocess
from pathlib import Path

import pytest

_SOURCE = Path(__file__).resolve().parents[2] / "src/decision_browser/dom.py"


def scripts():
    strings = [n.value for n in ast.walk(ast.parse(_SOURCE.read_text()))
               if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    standalone = next(s for s in strings if s.startswith("(el, hit) => {if(!el||!hit)"))
    guarded = next(s for s in strings if "const native = ((el, hit)" in s)
    guarded = guarded.split("const native = (")[1].split(")(element, hit);")[0]
    return [standalone, guarded]


@pytest.mark.parametrize("script_index", [0, 1])
@pytest.mark.parametrize("tags,selected,hit,expected", [
    (["LABEL"], 0, 0, 0),
    (["LABEL", "SPAN"], 1, 1, 0),
    (["DIV", "LABEL"], 0, 1, 0),
    (["LABEL", "INPUT"], 1, 1, 1),
    (["DIV", "SPAN"], 0, 1, 1),
])
def test_label_forwarding_is_rejected_but_direct_controls_are_preserved(
    script_index, tags, selected, hit, expected,
):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js required")
    js = r"""
const [source, tags, selected, hit] = JSON.parse(process.argv[1]);
const nodes = tags.map(tagName=>({tagName,nodeType:1,hasAttribute:()=>false}));
nodes.forEach((n,i)=>{n.parentElement=nodes[i-1] || null;
n.contains=other=>{for(let c=other;c;c=c.parentElement) if(c===n)return true;return false;};});
global.Node={prototype:{contains:function(n){return this.contains(n)}}};
Object.defineProperty(Node.prototype,'parentElement',{get(){return this.parentElement}});
// Model native prototype getters/methods separately from instance fixture data.
const intrinsicPrototype = new Proxy(Object.prototype, {get(target, key, receiver) {
 const own = Object.getOwnPropertyDescriptor(receiver, key);
 if (own) return own.value;
 if (key === 'matches') return () => false;
 return Reflect.get(target, key, receiver);
}});
const intrinsicObject = value => Object.setPrototypeOf(value, intrinsicPrototype);
nodes.forEach(intrinsicObject);
const op = 'click';
process.stdout.write(JSON.stringify(eval('('+source+')')(nodes[selected], nodes[hit])));
"""
    result = subprocess.run([node, "-e", js, json.dumps(
        [scripts()[script_index], tags, selected, hit]
    )], check=True, text=True, capture_output=True)
    assert json.loads(result.stdout) == expected


@pytest.mark.parametrize("script_index", [0, 1])
@pytest.mark.parametrize("kind", ["href", "xlink", "alternate_prefix"])
@pytest.mark.parametrize("selected", [0, 1])
def test_svg_link_recipient_requires_the_selected_link(script_index, kind, selected):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js required")
    js = r"""
const [source, kind, selected] = JSON.parse(process.argv[1]);
const nodes = ['DIV','a','rect'].map(tagName=>({tagName,nodeType:1,
 namespaceURI:'http://www.w3.org/2000/svg',
 hasAttribute:name=>tagName==='a'&&kind==='href'&&name==='href',
 hasAttributeNS:(ns,name)=>tagName==='a'&&kind!=='href'&&ns==='http://www.w3.org/1999/xlink'&&name==='href'}));
nodes.forEach((n,i)=>{n.parentElement=nodes[i-1]||null;
 n.contains=other=>{for(let c=other;c;c=c.parentElement)if(c===n)return true;return false;};});
global.Node={prototype:{contains:function(n){return this.contains(n)}}};
Object.defineProperty(Node.prototype,'parentElement',{get(){return this.parentElement}});
// Model native prototype getters/methods separately from instance fixture data.
const intrinsicPrototype = new Proxy(Object.prototype, {get(target, key, receiver) {
 const own = Object.getOwnPropertyDescriptor(receiver, key);
 if (own) return own.value;
 if (key === 'matches') return () => false;
 return Reflect.get(target, key, receiver);
}});
const intrinsicObject = value => Object.setPrototypeOf(value, intrinsicPrototype);
nodes.forEach(intrinsicObject);
const op='click';
process.stdout.write(JSON.stringify(eval('('+source+')')(nodes[selected],nodes[2])));
"""
    result = subprocess.run([node, "-e", js, json.dumps(
        [scripts()[script_index], kind, selected]
    )], check=True, text=True, capture_output=True)
    assert json.loads(result.stdout) == selected
