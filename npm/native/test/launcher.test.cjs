'use strict';

const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const test = require('node:test');
const launcher = path.resolve(__dirname, '../bin/decision-browser.cjs');

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'native-launcher-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.mkdirSync(path.join(root, 'bin'));
  fs.mkdirSync(path.join(root, 'native'));
  fs.copyFileSync(launcher, path.join(root, 'bin/decision-browser.cjs'));
  const bytes = Buffer.from('#!/bin/sh\nprintf "%s\\n" "$@"\nexit 23\n');
  fs.writeFileSync(path.join(root, 'native/decision-browser'), bytes, { mode: 0o755 });
  const manifest = {
    format: 1, platform: process.platform, arch: process.arch,
    file: 'decision-browser', sha256: createHash('sha256').update(bytes).digest('hex'),
  };
  function writeManifest() {
    fs.writeFileSync(path.join(root, 'native/manifest.json'), JSON.stringify(manifest));
  }
  writeManifest();
  return { root, manifest, writeManifest, run(args = []) {
    return spawnSync(process.execPath, [path.join(root, 'bin/decision-browser.cjs'), ...args], { encoding: 'utf8' });
  } };
}

const unix = { skip: process.platform === 'win32' };
test('forwards literal arguments and preserves native exit code', unix, t => {
  const f = fixture(t);
  const result = f.run(['--project', 'project with spaces', '$(printf injected)']);
  assert.equal(result.status, 23);
  assert.equal(result.stdout, '--project\nproject with spaces\n$(printf injected)\n');
});

test('corrupted executable cannot run', unix, t => {
  const f = fixture(t);
  fs.appendFileSync(path.join(f.root, 'native/decision-browser'), '# changed\n');
  const result = f.run();
  assert.equal(result.status, 1);
  assert.match(result.stderr, /integrity check failed/);
  assert.equal(result.stdout, '');
});

test('wrong platform cannot run', unix, t => {
  const f = fixture(t);
  f.manifest.arch = 'unsupported';
  f.writeManifest();
  const result = f.run();
  assert.equal(result.status, 1);
  assert.match(result.stderr, /does not match/);
});

test('manifest cannot select an executable outside the bundle', unix, t => {
  const f = fixture(t);
  f.manifest.file = '../../executable';
  f.writeManifest();
  const result = f.run();
  assert.equal(result.status, 1);
  assert.match(result.stderr, /does not match/);
});

test('symlink executable cannot run', unix, t => {
  const f = fixture(t);
  fs.renameSync(path.join(f.root, 'native/decision-browser'), path.join(f.root, 'outside'));
  fs.symlinkSync('../outside', path.join(f.root, 'native/decision-browser'));
  assert.equal(f.run().status, 1);
});
