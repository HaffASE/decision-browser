'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { PassThrough } = require('node:stream');
const https = require('node:https');
const http = require('node:http');
const tls = require('node:tls');
const zlib = require('node:zlib');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { download, extractUv, DownloadError } = require('../bin/uv-download.cjs');

function requests(t, handler) {
  const calls = [];
  t.mock.method(https, 'request', (url, options, callback) => {
    const request = new EventEmitter();
    request.destroy = () => {};
    request.end = () => queueMicrotask(() => handler({ url, options, callback, request, index: calls.length - 1 }));
    calls.push({ url, options });
    return request;
  });
  return calls;
}
function reply(callback, { status = 200, headers = {}, body = 'uv bytes' } = {}) {
  const response = new PassThrough();
  response.statusCode = status;
  response.headers = headers;
  callback(response);
  if (!response.destroyed) response.end(body);
  return response;
}
function tar(entries) {
  const parts = [];
  for (const { name = 'uv-target/uv', bytes = Buffer.from('native uv'), type = '0' } of entries) {
    const header = Buffer.alloc(512);
    header.write(name, 0, 100);
    header.write('0000755\0', 100);
    header.write(bytes.length.toString(8).padStart(11, '0') + '\0', 124);
    header.fill(32, 148, 156);
    header.write(type, 156);
    header.write('ustar\0', 257);
    header.write([...header].reduce((a, b) => a + b, 0).toString(8).padStart(6, '0') + '\0 ', 148);
    parts.push(header, bytes, Buffer.alloc((512 - bytes.length % 512) % 512));
  }
  return zlib.gzipSync(Buffer.concat([...parts, Buffer.alloc(1024)]));
}
function zip(entries) {
  const crcTable = Array.from({ length: 256 }, (_, index) => {
    let value = index;
    for (let bit = 0; bit < 8; bit++) value = (value >>> 1) ^ (value & 1 ? 0xedb88320 : 0);
    return value >>> 0;
  });
  const checksum = bytes => {
    let value = 0xffffffff;
    for (const byte of bytes) value = crcTable[(value ^ byte) & 255] ^ (value >>> 8);
    return (value ^ 0xffffffff) >>> 0;
  };
  const locals = [], directory = [];
  let offset = 0;
  for (const { name = 'uv.exe', bytes = Buffer.from('native uv'), method = 8, mode = 0o100755, flags = 0 } of entries) {
    const nameBytes = Buffer.from(name);
    const packed = method === 0 ? bytes : zlib.deflateRawSync(bytes);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50);
    local.writeUInt16LE(flags, 6);
    local.writeUInt16LE(method, 8);
    local.writeUInt32LE(checksum(bytes), 14);
    local.writeUInt32LE(packed.length, 18);
    local.writeUInt32LE(bytes.length, 22);
    local.writeUInt16LE(nameBytes.length, 26);
    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50);
    central.writeUInt16LE(flags, 8);
    central.writeUInt16LE(method, 10);
    central.writeUInt32LE(checksum(bytes), 16);
    central.writeUInt32LE(packed.length, 20);
    central.writeUInt32LE(bytes.length, 24);
    central.writeUInt16LE(nameBytes.length, 28);
    central.writeUInt32LE((mode << 16) >>> 0, 38);
    central.writeUInt32LE(offset, 42);
    locals.push(local, nameBytes, packed);
    directory.push(central, nameBytes);
    offset += local.length + nameBytes.length + packed.length;
  }
  const centralBytes = Buffer.concat(directory);
  const footer = Buffer.alloc(22);
  footer.writeUInt32LE(0x06054b50);
  footer.writeUInt16LE(entries.length, 8);
  footer.writeUInt16LE(entries.length, 10);
  footer.writeUInt32LE(centralBytes.length, 12);
  footer.writeUInt32LE(offset, 16);
  return Buffer.concat([...locals, centralBytes, footer]);
}

for (const format of ['tar.gz', 'zip']) {
  const build = format === 'tar.gz' ? tar : zip;
  const member = format === 'tar.gz' ? 'uv-target/uv' : 'uv.exe';
  test(`${format}: extracts only exact executable and rejects a missing member`, () => {
    const input = build([{ name: '../unrelated', bytes: Buffer.from('ignored') }, { name: member }]);
    assert.equal(extractUv(input, { format, member }).toString(), 'native uv');
    assert.throws(() => extractUv(input, { format, member: 'absent' }), /archive_missing_member/);
  });
  test(`${format}: duplicate, symlink, oversized, truncated and invalid requested paths are rejected`, () => {
    assert.throws(() => extractUv(build([{ name: member }, { name: member }]), { format, member }), /archive_invalid_member/);
    assert.throws(() => extractUv(build([{ name: member, type: '2', mode: 0o120777 }]), { format, member }), /archive_invalid_member/);
    assert.throws(() => extractUv(build([{ name: member }]), { format, member, maxBytes: 2 }), /archive_member_too_large/);
    assert.throws(() => extractUv(build([{ name: member }]).subarray(0, -20), { format, member }), DownloadError);
    for (const invalid of ['../uv', '/uv', 'a/../uv', 'a\\uv', '']) {
      assert.throws(() => extractUv(build([{ name: member }]), { format, member: invalid }), /archive_invalid/);
    }
  });
}

test('tar header checksums and truncated payload are rejected', () => {
  const decoded = zlib.gunzipSync(tar([{}]));
  assert.throws(() => extractUv(zlib.gzipSync(decoded.subarray(0, -512)), { format: 'tar.gz', member: 'uv-target/uv' }), /archive_truncated/);
  decoded[0] ^= 1;
  assert.throws(() => extractUv(zlib.gzipSync(decoded), { format: 'tar.gz', member: 'uv-target/uv' }), /archive_invalid/);
  assert.throws(() => extractUv(zlib.gzipSync(decoded.subarray(0, 600)), { format: 'tar.gz', member: 'uv-target/uv' }), DownloadError);
});

test('zip CRC mismatch is rejected independently of archive authenticity', () => {
  const original = zip([{ bytes: Buffer.from('123456789'), method: 0 }]);
  assert.equal(original.readUInt32LE(14), 0xcbf43926);
  assert.equal(extractUv(original, { format: 'zip', member: 'uv.exe' }).toString(), '123456789');
  for (const part of ['payload', 'local', 'central']) {
    const changed = Buffer.from(original);
    const central = changed.indexOf(Buffer.from('504b0102', 'hex'));
    changed[part === 'payload' ? 30 + Buffer.byteLength('uv.exe') : part === 'local' ? 14 : central + 16] ^= 1;
    assert.throws(() => extractUv(changed, { format: 'zip', member: 'uv.exe' }), /archive_crc_mismatch/);
  }
});

test('Windows mixed-case proxy and CA environment is normalized with explicit duplicate refusal', async (t) => {
  const calls = requests(t, ({ callback }) => reply(callback));
  await download('https://api.example.test/uv', { platform: 'win32', env: {
    Https_Proxy: 'http://proxy.test:8080', No_Proxy: 'api.example.test',
  } });
  assert.equal(Object.hasOwn(calls.at(-1).options.agent, 'createConnection'), false);
  await download('https://api.example.test/uv', { platform: 'win32', env: { Https_Proxy: 'http://proxy.test:8080' } });
  assert.equal(Object.hasOwn(calls.at(-1).options.agent, 'createConnection'), true);
  await assert.rejects(download('https://api.example.test/uv', { platform: 'win32', env: {
    Https_Proxy: 'http://one.test:8080', HTTPS_PROXY: 'http://two.test:8080',
  } }), /download_ambiguous_environment/);
});

test('HTTPS proxy uses verified TLS and a rejected tunnel never reaches the destination', async (t) => {
  const proxyCalls = [];
  t.mock.method(https, 'request', (url, options, callback) => {
    const req = new EventEmitter(); req.destroy = () => {};
    if (url.method === 'CONNECT') {
      proxyCalls.push(url);
      req.end = () => queueMicrotask(() => req.emit('connect', { statusCode: 407 }, new PassThrough(), Buffer.alloc(0)));
    } else {
      req.end = () => queueMicrotask(() => options.agent.createConnection({}, (error) => {
        assert.ok(error);
        req.emit('error', error);
      }));
    }
    return req;
  });
  t.mock.method(tls, 'connect', () => assert.fail('destination must not be contacted'));
  await assert.rejects(download('https://release.example/uv', { env: { HTTPS_PROXY: 'https://user:private-marker@proxy.example' } }), /download_proxy_rejected/);
  assert.equal(proxyCalls[0].protocol, 'https:');
  assert.equal(proxyCalls[0].rejectUnauthorized, true);
  assert.equal(proxyCalls[0].port, 443);
});

test('invalid proxy is sanitized and HTTP_PROXY alone is not used for HTTPS', async (t) => {
  const calls = requests(t, ({ callback }) => reply(callback));
  await assert.rejects(download('https://example/uv', { env: { HTTPS_PROXY: 'socks5://private-marker@proxy' } }), (error) => error.message === 'download_invalid_proxy');
  await download('https://example/uv', { env: { HTTP_PROXY: 'http://proxy.example' } });
  assert.equal(Object.hasOwn(calls[0].options.agent, 'createConnection'), false);
});

test('ZIP supports stored binaries and rejects encrypted, incompatible or inconsistent entries', () => {
  assert.equal(extractUv(zip([{ method: 0 }]), { format: 'zip', member: 'uv.exe' }).toString(), 'native uv');
  for (const entry of [{ flags: 1 }, { method: 99 }]) {
    assert.throws(() => extractUv(zip([entry]), { format: 'zip', member: 'uv.exe' }), /archive_invalid_member/);
  }
  const input = zip([{}]);
  input[30] = 'x'.charCodeAt(0);
  assert.throws(() => extractUv(input, { format: 'zip', member: 'uv.exe' }), /archive_invalid/);
  assert.throws(() => extractUv(Buffer.from('private-marker'), { format: 'rar', member: 'uv' }), /archive_unsupported/);
});

test('HTTPS redirects retain verification and never forward arbitrary environment credentials', async (t) => {
  const calls = requests(t, ({ callback, index }) => index === 0
    ? reply(callback, { status: 302, headers: { location: 'https://cdn.example/uv' } })
    : reply(callback, { headers: { 'content-length': '8' } }));
  assert.equal((await download('https://release.example/uv', { env: { OPENAI_API_KEY: 'private-marker' } })).toString(), 'uv bytes');
  assert.equal(calls.length, 2);
  assert.equal(calls[1].url.hostname, 'cdn.example');
  assert.equal(calls[1].options.rejectUnauthorized, true);
  assert.ok(!JSON.stringify(calls.map(({ options }) => options.headers)).includes('private-marker'));
});

test('redirect downgrade, credentials and excessive redirects fail closed', async (t) => {
  let location = 'http://plain.example/uv';
  const calls = requests(t, ({ callback }) => reply(callback, { status: 302, headers: { location } }));
  for (const value of ['http://plain.example/uv', 'https://user:private-marker@example/uv']) {
    location = value;
    await assert.rejects(download('https://release.example/uv'), /download_invalid_url/);
  }
  location = '/again';
  const before = calls.length;
  await assert.rejects(download('https://release.example/uv'), /download_redirect_limit/);
  assert.equal(calls.length - before, 6);
});

test('content size, truncation and missing redirect are bounded', async (t) => {
  let scenario;
  requests(t, ({ callback }) => reply(callback, scenario));
  scenario = { headers: { 'content-length': '100' }, body: '' };
  await assert.rejects(download('https://example/uv', { maxBytes: 5 }), /download_too_large/);
  scenario = { body: 'too many bytes' };
  await assert.rejects(download('https://example/uv', { maxBytes: 5 }), /download_too_large/);
  scenario = { headers: { 'content-length': '5' }, body: 'uv' };
  await assert.rejects(download('https://example/uv'), /download_truncated/);
  scenario = { status: 302 };
  await assert.rejects(download('https://example/uv'), /download_invalid_redirect/);
});

test('absolute deadline terminates a stalled request and errors remain sanitized', async (t) => {
  let error;
  requests(t, ({ request }) => { if (error) request.emit('error', error); });
  await assert.rejects(download('https://example/uv', { timeoutMs: 10 }), /download_timeout/);
  for (const [code, expected] of [['ENOTFOUND', 'download_dns_failed'], ['CERT_HAS_EXPIRED', 'download_tls_failed'], ['PRIVATE', 'download_failed']]) {
    error = Object.assign(new Error('private-marker'), { code });
    await assert.rejects(download('https://example/uv'), (caught) => caught.message === expected && !String(caught).includes('private-marker'));
  }
});

test('custom CA settings preserve default roots and malformed files do not leak paths', async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'uv-ca-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const ca = path.join(directory, 'ca.pem');
  fs.writeFileSync(ca, tls.rootCertificates[0]);
  const calls = requests(t, ({ callback }) => reply(callback));
  await download('https://example/uv', { env: { SSL_CERT_FILE: ca } });
  assert.ok(calls[0].options.ca.length > 1);
  assert.equal(calls[0].options.ca.at(-1), tls.rootCertificates[0]);
  await assert.rejects(download('https://example/uv', { env: { SSL_CERT_FILE: '/private-marker/absent' } }), (caught) => caught.message === 'download_invalid_ca');
});

test('NO_PROXY bypasses CONNECT for matching domains and ports, not lookalikes', async (t) => {
  const calls = requests(t, ({ callback }) => reply(callback));
  for (const noProxy of ['.example.test', 'api.example.test:443', '*', 'other.test,example.test']) {
    await download('https://api.example.test/uv', { env: { HTTPS_PROXY: 'http://proxy.test:8080', NO_PROXY: noProxy } });
    assert.equal(Object.hasOwn(calls.at(-1).options.agent, 'createConnection'), false);
  }
  for (const noProxy of ['example.test', 'api.example.test:444']) {
    await download('https://notexample.test/uv', { env: { HTTPS_PROXY: 'http://proxy.test:8080', NO_PROXY: noProxy } });
    assert.equal(Object.hasOwn(calls.at(-1).options.agent, 'createConnection'), true);
  }
});

test('HTTP proxy CONNECT carries auth only to proxy; destination TLS stays verified', async (t) => {
  const proxyCalls = [], tlsCalls = [], originCalls = [];
  t.mock.method(http, 'request', (options) => {
    proxyCalls.push(options);
    const req = new EventEmitter(); req.destroy = () => {};
    req.end = () => queueMicrotask(() => {
      const socket = new PassThrough();
      req.emit('connect', { statusCode: 200 }, socket, Buffer.alloc(0));
    });
    return req;
  });
  t.mock.method(tls, 'connect', (options) => {
    tlsCalls.push(options);
    const socket = new PassThrough();
    queueMicrotask(() => socket.emit('secureConnect'));
    return socket;
  });
  requests(t, ({ options, callback, request }) => {
    originCalls.push(options);
    options.agent.createConnection({}, (error) => error ? request.emit('error', error) : reply(callback));
  });
  await download('https://release.example/uv', { env: { HTTPS_PROXY: 'http://user:private-marker@proxy.example:8080' } });
  assert.equal(proxyCalls[0].method, 'CONNECT');
  assert.equal(proxyCalls[0].path, 'release.example:443');
  assert.equal(proxyCalls[0].headers['Proxy-Authorization'], 'Basic ' + Buffer.from('user:private-marker').toString('base64'));
  assert.equal(originCalls[0].headers['Proxy-Authorization'], undefined);
  assert.equal(tlsCalls[0].rejectUnauthorized, true);
  assert.equal(tlsCalls[0].servername, 'release.example');
});
