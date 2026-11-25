'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { ensureUv, cacheDirectory } = require('../bin/uv-bootstrap.cjs');
const manifest = require('../bin/uv-manifest.json');

function fixture(t, first = { error: Error('missing') }) {
  const cache = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'decision-uv-')));
  t.after(() => fs.rmSync(cache, { recursive: true, force: true }));
  const data = Buffer.from('archive placeholder');
  const target = { file: 'uv.tar.gz', format: 'tar.gz', member: 'uv-package/uv',
    sha256: createHash('sha256').update(data).digest('hex') };
  const calls = [], downloads = [];
  const options = {
    cwd: cache, env: { PATH: '/test/path', DECISION_BROWSER_CACHE_DIR: cache },
    platform: 'linux', arch: 'x64', err: { write() {} },
    release: { version: '0.13.7', baseUrl: 'https://example.test/', targets: { 'linux-x64': target } },
    run(command, args, opts) {
      calls.push({ command, args, opts });
      return command === 'uv' ? first : { status: 0, stdout: 'uv 0.13.7\n' };
    },
    async fetchArchive(url) { downloads.push(url); return data; },
    extract(bytes, selected) { assert.equal(selected, target); assert.deepEqual(bytes, data); return Buffer.from('binary placeholder'); },
  };
  return { cache, data, target, options, calls, downloads };
}

test('supported installed uv needs no download or cache writes', async (t) => {
  const f = fixture(t, { status: 0, stdout: 'uv 0.14.0\n' });
  const result = await ensureUv(f.options);
  assert.equal(result.command, 'uv');
  assert.equal(f.downloads.length, 0);
  assert.deepEqual(fs.readdirSync(f.cache), []);
});

test('managed uv preparation and cache reuse preserve the explicit certificate choice', async (t) => {
  const f = fixture(t);
  f.options.env.UV_SYSTEM_CERTS = 'false';
  f.options.env.SSL_CERT_FILE = '/private/corporate-ca.pem';
  const prepared = await ensureUv(f.options);
  const cached = await ensureUv(f.options);
  for (const result of [prepared, cached]) {
    assert.equal(result.env.UV_SYSTEM_CERTS, 'false');
    assert.equal(result.env.SSL_CERT_FILE, '/private/corporate-ca.pem');
  }
  for (const call of f.calls) {
    assert.equal(call.opts.env.UV_SYSTEM_CERTS, 'false');
    assert.equal(call.opts.env.SSL_CERT_FILE, '/private/corporate-ca.pem');
  }
});

for (const result of [{ error: Error('missing') }, { status: 0, stdout: 'uv 0.12.0' }]) {
  test('missing or old uv is downloaded once and reused from private cache', async (t) => {
    const f = fixture(t, result);
    const prepared = await ensureUv(f.options);
    assert.equal(f.downloads.length, 1);
    assert.ok(prepared.command.startsWith(f.cache + path.sep));
    assert.equal(prepared.env.PATH, path.dirname(prepared.command) + path.delimiter + '/test/path');
    if (process.platform !== 'win32') assert.equal(fs.statSync(prepared.command).mode & 0o777, 0o700);
    f.options.fetchArchive = async () => assert.fail('cached version must not download');
    assert.equal((await ensureUv(f.options)).command, prepared.command);
  });
}

test('checksum mismatch prevents extraction and execution and removes the partial attempt', async (t) => {
  const f = fixture(t);
  f.options.fetchArchive = async () => Buffer.from('corrupt');
  f.options.extract = () => assert.fail('must verify archive first');
  await assert.rejects(ensureUv(f.options), /checksum mismatch/);
  assert.equal(f.calls.length, 1);
  assert.deepEqual(fs.readdirSync(path.join(f.cache, 'tools/uv')), []);
});

test('tampered cached binary is refused before execution', async (t) => {
  const f = fixture(t);
  const ready = await ensureUv(f.options);
  fs.writeFileSync(ready.command, 'modified executable');
  f.calls.length = 0;
  await assert.rejects(ensureUv(f.options), /integrity check/);
  assert.equal(f.calls.length, 1);
});

test('wrong downloaded version never becomes an active cache entry', async (t) => {
  const f = fixture(t);
  f.options.run = command => command === 'uv' ? { error: Error('missing') } : { status: 0, stdout: 'uv 9.0.0' };
  await assert.rejects(ensureUv(f.options), /cannot run/);
  assert.deepEqual(fs.readdirSync(path.join(f.cache, 'tools/uv')), []);
});

test('network errors are bounded and do not echo private details', async (t) => {
  const f = fixture(t);
  f.options.fetchArchive = async () => { throw Object.assign(Error('CANARY-PROXY-CREDENTIAL'), { code: 'download_tls_failed' }); };
  await assert.rejects(ensureUv(f.options), error => error.message.includes('TLS verification') && !error.message.includes('CANARY'));
  assert.deepEqual(fs.readdirSync(path.join(f.cache, 'tools/uv')), []);
});

test('concurrent preparations converge without replacing a successful version', async (t) => {
  const f = fixture(t);
  const results = await Promise.all([ensureUv(f.options), ensureUv(f.options)]);
  assert.equal(results[0].command, results[1].command);
  assert.deepEqual(fs.readdirSync(path.join(f.cache, 'tools/uv')), ['0.13.7-linux-x64']);
});

test('unsupported platform is rejected without download', async (t) => {
  const f = fixture(t);
  f.options.arch = 'mips';
  await assert.rejects(ensureUv(f.options), /platform\/architecture/);
  assert.equal(f.downloads.length, 0);
});

test('relative and traversing cache locations are rejected', () => {
  for (const value of ['', 'relative', '/tmp/../cache', '/tmp/unsafe\ncache']) {
    assert.throws(() => cacheDirectory({ DECISION_BROWSER_CACHE_DIR: value }), /Invalid cache/);
  }
});

test('symlinked or shared cache cannot host downloaded programs', { skip: process.platform === 'win32' }, async (t) => {
  const f = fixture(t);
  const link = path.join(f.cache, 'linked');
  fs.symlinkSync(f.cache, link);
  f.options.env.DECISION_BROWSER_CACHE_DIR = link;
  await assert.rejects(ensureUv(f.options), /Symlinked/);
  const shared = path.join(f.cache, 'shared');
  fs.mkdirSync(shared, { mode: 0o755 });
  f.options.env.DECISION_BROWSER_CACHE_DIR = shared;
  await assert.rejects(ensureUv(f.options), /private/);
  assert.equal(f.downloads.length, 0);
});

test('platform table pins official version-specific archives and checksums', () => {
  assert.equal(manifest.version, '0.13.7');
  assert.equal(manifest.baseUrl, 'https://releases.astral.sh/github/uv/releases/download/0.13.7/');
  assert.deepEqual(Object.keys(manifest.targets).sort(), ['darwin-arm64', 'darwin-x64', 'linux-arm64', 'linux-x64', 'win32-arm64', 'win32-x64']);
  for (const [name, target] of Object.entries(manifest.targets)) {
    assert.match(target.sha256, /^[a-f0-9]{64}$/);
    if (name.startsWith('linux')) assert.ok(target.file.endsWith('-musl.tar.gz'));
    if (name.startsWith('win32')) assert.equal(target.member, 'uv.exe');
  }
});
