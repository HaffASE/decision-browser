'use strict';

// Download only explicitly selected HTTPS assets. Never execute archive contents.
const http = require('node:http');
const https = require('node:https');
const tls = require('node:tls');
const net = require('node:net');
const fs = require('node:fs');
const zlib = require('node:zlib');

class DownloadError extends Error {
  constructor(code) { super(code); this.name = 'DownloadError'; this.code = code; }
}

function failure(error) {
  if (error instanceof DownloadError) return error;
  const code = error && error.code || '';
  if (['ENOTFOUND', 'EAI_AGAIN'].includes(code)) return new DownloadError('download_dns_failed');
  if (/CERT|TLS|SSL|SELF_SIGNED|UNABLE_TO_VERIFY/.test(code)) return new DownloadError('download_tls_failed');
  return new DownloadError('download_failed');
}

function destination(raw, base) {
  try {
    if (typeof raw !== 'string' || /[\s\x00-\x1f\x7f]/.test(raw)) throw new Error();
    const url = new URL(raw, base);
    if (url.protocol !== 'https:' || url.username || url.password) throw new Error();
    return url;
  } catch { throw new DownloadError('download_invalid_url'); }
}

function bypassProxy(url, value) {
  const host = url.hostname.replace(/^\[|\]$/g, '').toLowerCase();
  return value.split(',').some((part) => {
    let entry = part.trim().toLowerCase();
    if (!entry) return false;
    if (entry === '*') return true;
    let port;
    if (entry.startsWith('[')) {
      const match = /^\[([^\]]+)\](?::(\d+))?$/.exec(entry);
      if (!match) return false;
      [, entry, port] = match;
    } else if ((entry.match(/:/g) || []).length === 1) {
      [entry, port] = entry.split(':');
    }
    if (port && port !== (url.port || '443')) return false;
    entry = entry.replace(/^\*?\./, '');
    return host === entry || host.endsWith('.' + entry);
  });
}

function proxyFor(url, env) {
  if (bypassProxy(url, env.no_proxy || env.NO_PROXY || '')) return null;
  const raw = env.https_proxy || env.HTTPS_PROXY || env.all_proxy || env.ALL_PROXY;
  // HTTP_PROXY is intentionally not used for HTTPS; this matches uv/curl routing.
  if (!raw) return null;
  try {
    const proxy = new URL(raw);
    if (!['http:', 'https:'].includes(proxy.protocol) || proxy.search || proxy.hash ||
        (proxy.pathname && proxy.pathname !== '/')) throw new Error();
    return proxy;
  } catch { throw new DownloadError('download_invalid_proxy'); }
}

function windowsEnvironment(env, platform) {
  if (platform !== 'win32') return env;
  const result = {};
  for (const [name, value] of Object.entries(env)) {
    const key = name.toUpperCase();
    if (Object.hasOwn(result, key) && result[key] !== value) throw new DownloadError('download_ambiguous_environment');
    result[key] = value;
  }
  return result;
}

function certificates(env) {
  const paths = [...new Set([env.SSL_CERT_FILE, env.NODE_EXTRA_CA_CERTS].filter(Boolean))];
  if (!paths.length) return undefined;
  const ca = typeof tls.getCACertificates === 'function'
    ? [...tls.getCACertificates('default')] : [...tls.rootCertificates];
  try {
    for (const file of paths) {
      const info = fs.statSync(file);
      if (!info.isFile() || info.size > 4 * 1024 * 1024) throw new Error();
      const pem = fs.readFileSync(file, 'utf8');
      if (!pem.includes('-----BEGIN CERTIFICATE-----')) throw new Error();
      ca.push(pem);
    }
  } catch { throw new DownloadError('download_invalid_ca'); }
  return ca;
}

function requestAsset(url, env, ca, state) {
  return new Promise((resolve, reject) => {
    const proxy = proxyFor(url, env);
    const agent = new https.Agent({ keepAlive: false, ...(ca ? { ca } : {}) });
    state.resources.add(agent);
    if (proxy) {
      agent.createConnection = (_options, callback) => {
        let completed = false;
        const complete = (error, socket) => {
          if (completed) return;
          completed = true;
          callback(error, socket);
        };
        const authority = url.host + (url.port ? '' : ':443');
        const headers = { Host: authority };
        if (proxy.username || proxy.password) {
          headers['Proxy-Authorization'] = 'Basic ' + Buffer.from(
            decodeURIComponent(proxy.username) + ':' + decodeURIComponent(proxy.password),
          ).toString('base64');
        }
        const transport = proxy.protocol === 'https:' ? https : http;
        const connect = transport.request({
          protocol: proxy.protocol, hostname: proxy.hostname.replace(/^\[|\]$/g, ''),
          port: proxy.port || (proxy.protocol === 'https:' ? 443 : 80),
          method: 'CONNECT', path: authority, headers, agent: false,
          maxHeaderSize: 16384, signal: state.controller.signal,
          rejectUnauthorized: true, ...(ca ? { ca } : {}),
        });
        state.resources.add(connect);
        connect.once('error', (error) => complete(error));
        connect.once('connect', (response, socket, head) => {
          state.resources.add(socket);
          if (response.statusCode !== 200) {
            socket.destroy();
            complete(new DownloadError('download_proxy_rejected'));
            return;
          }
          if (head.length) socket.unshift(head);
          const hostname = url.hostname.replace(/^\[|\]$/g, '');
          const secure = tls.connect({
            socket, servername: net.isIP(hostname) ? undefined : hostname,
            host: hostname, rejectUnauthorized: true, ...(ca ? { ca } : {}),
          });
          state.resources.add(secure);
          secure.once('error', (error) => complete(error));
          secure.once('secureConnect', () => complete(null, secure));
        });
        connect.end();
      };
    }
    const request = https.request(url, {
      method: 'GET', agent, signal: state.controller.signal, maxHeaderSize: 16384,
      rejectUnauthorized: true, headers: { 'User-Agent': 'decision-browser-bootstrap', 'Accept-Encoding': 'identity' },
      ...(ca ? { ca } : {}),
    }, (response) => {
      state.resources.add(response);
      if ([301, 302, 303, 307, 308].includes(response.statusCode)) {
        const location = response.headers.location;
        response.destroy();
        resolve({ location });
        return;
      }
      if (response.statusCode !== 200) {
        response.destroy();
        reject(new DownloadError('download_http_failed'));
        return;
      }
      const length = response.headers['content-length'];
      if (length !== undefined && (!/^\d+$/.test(length) || Number(length) > state.maxBytes)) {
        response.destroy();
        reject(new DownloadError('download_too_large'));
        return;
      }
      const chunks = [];
      let size = 0;
      response.on('data', (chunk) => {
        size += chunk.length;
        if (size > state.maxBytes) {
          reject(new DownloadError('download_too_large'));
          response.destroy();
          return;
        }
        chunks.push(chunk);
      });
      response.once('error', reject);
      response.once('aborted', () => reject(new DownloadError('download_truncated')));
      response.once('end', () => {
        if (length !== undefined && size !== Number(length)) reject(new DownloadError('download_truncated'));
        else resolve({ bytes: Buffer.concat(chunks, size) });
      });
    });
    state.resources.add(request);
    request.once('error', reject);
    request.end();
  });
}

async function download(url, { env = {}, maxBytes = 64 * 1024 * 1024, timeoutMs = 120000, platform = process.platform } = {}) {
  if (!Number.isSafeInteger(maxBytes) || maxBytes < 1 || !Number.isFinite(timeoutMs) || timeoutMs <= 0) {
    throw new DownloadError('download_invalid_limits');
  }
  const state = { controller: new AbortController(), resources: new Set(), maxBytes };
  let timer;
  try {
    env = windowsEnvironment(env, platform);
    const ca = certificates(env);
    const task = async () => {
      let target = destination(url);
      for (let redirects = 0; redirects <= 5; redirects++) {
        const result = await requestAsset(target, env, ca, state);
        if (result.bytes) return result.bytes;
        if (!result.location) throw new DownloadError('download_invalid_redirect');
        target = destination(result.location, target);
      }
      throw new DownloadError('download_redirect_limit');
    };
    return await Promise.race([task(), new Promise((_, reject) => {
      timer = setTimeout(() => reject(new DownloadError('download_timeout')), timeoutMs);
    })]);
  } catch (error) { throw failure(error); }
  finally {
    clearTimeout(timer);
    state.controller.abort();
    for (const resource of state.resources) resource.destroy();
  }
}

function tarNumber(field) {
  const value = field.toString('ascii').replace(/\0.*$/, '').trim();
  if (!/^[0-7]+$/.test(value)) throw new DownloadError('archive_invalid');
  const number = parseInt(value, 8);
  if (!Number.isSafeInteger(number)) throw new DownloadError('archive_invalid');
  return number;
}

function tarMember(buffer, member, maxBytes) {
  const tar = zlib.gunzipSync(buffer, { maxOutputLength: Math.min(512 * 1024 * 1024, maxBytes * 3 + 1024 * 1024) });
  let found;
  let position = 0;
  let ended = false;
  while (position + 512 <= tar.length) {
    const header = tar.subarray(position, position + 512);
    if (header.every((value) => value === 0)) {
      if (position + 1024 > tar.length || !tar.subarray(position).every((value) => value === 0)) {
        throw new DownloadError('archive_truncated');
      }
      ended = true;
      break;
    }
    let checksum = 0;
    for (let index = 0; index < 512; index++) checksum += index >= 148 && index < 156 ? 32 : header[index];
    if (checksum !== tarNumber(header.subarray(148, 156))) throw new DownloadError('archive_invalid');
    const name = header.subarray(0, 100).toString('utf8').replace(/\0.*$/, '');
    const prefix = header.subarray(345, 500).toString('utf8').replace(/\0.*$/, '');
    const fullName = prefix ? prefix + '/' + name : name;
    const size = tarNumber(header.subarray(124, 136));
    const start = position + 512;
    const end = start + size;
    if (end > tar.length) throw new DownloadError('archive_truncated');
    if (fullName === member) {
      if (found !== undefined || ![0, 48].includes(header[156])) throw new DownloadError('archive_invalid_member');
      if (size === 0 || size > maxBytes) throw new DownloadError('archive_member_too_large');
      found = Buffer.from(tar.subarray(start, end));
    }
    position = start + Math.ceil(size / 512) * 512;
  }
  if (!ended) throw new DownloadError('archive_truncated');
  if (found === undefined) throw new DownloadError('archive_missing_member');
  return found;
}

function crc32(bytes) {
  let crc = 0xffffffff;
  for (const byte of bytes) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function zipMember(zip, member, maxBytes) {
  let footer = -1;
  for (let index = zip.length - 22; index >= Math.max(0, zip.length - 65557); index--) {
    if (zip.readUInt32LE(index) === 0x06054b50 && index + 22 + zip.readUInt16LE(index + 20) === zip.length) {
      footer = index; break;
    }
  }
  if (footer < 0 || zip.readUInt16LE(footer + 4) !== 0 || zip.readUInt16LE(footer + 6) !== 0) {
    throw new DownloadError('archive_invalid');
  }
  const count = zip.readUInt16LE(footer + 10);
  const directorySize = zip.readUInt32LE(footer + 12);
  let cursor = zip.readUInt32LE(footer + 16);
  const directoryEnd = cursor + directorySize;
  if (count !== zip.readUInt16LE(footer + 8) || count === 65535 || directoryEnd !== footer) throw new DownloadError('archive_invalid');
  let found;
  for (let index = 0; index < count; index++) {
    if (cursor + 46 > directoryEnd || zip.readUInt32LE(cursor) !== 0x02014b50) throw new DownloadError('archive_invalid');
    const flags = zip.readUInt16LE(cursor + 8);
    const method = zip.readUInt16LE(cursor + 10);
    const packed = zip.readUInt32LE(cursor + 20);
    const size = zip.readUInt32LE(cursor + 24);
    const nameSize = zip.readUInt16LE(cursor + 28);
    const extraSize = zip.readUInt16LE(cursor + 30);
    const commentSize = zip.readUInt16LE(cursor + 32);
    const end = cursor + 46 + nameSize + extraSize + commentSize;
    if (end > directoryEnd) throw new DownloadError('archive_invalid');
    const name = zip.subarray(cursor + 46, cursor + 46 + nameSize).toString('utf8');
    if (name === member) {
      const mode = zip.readUInt32LE(cursor + 38) >>> 16;
      if (found !== undefined || (flags & 1) || (mode & 0xf000) && (mode & 0xf000) !== 0x8000 || ![0, 8].includes(method)) {
        throw new DownloadError('archive_invalid_member');
      }
      if (!size || size > maxBytes) throw new DownloadError('archive_member_too_large');
      const local = zip.readUInt32LE(cursor + 42);
      if (local + 30 > directoryEnd || zip.readUInt32LE(local) !== 0x04034b50) throw new DownloadError('archive_invalid');
      const localNameSize = zip.readUInt16LE(local + 26);
      const start = local + 30 + localNameSize + zip.readUInt16LE(local + 28);
      if (start + packed > zip.readUInt32LE(footer + 16) || zip.readUInt16LE(local + 6) !== flags ||
          zip.readUInt16LE(local + 8) !== method || zip.subarray(local + 30, local + 30 + localNameSize).toString('utf8') !== member) {
        throw new DownloadError('archive_invalid');
      }
      const data = zip.subarray(start, start + packed);
      found = method === 0 ? Buffer.from(data) : zlib.inflateRawSync(data, { maxOutputLength: maxBytes });
      if (found.length !== size) throw new DownloadError('archive_truncated');
      const checksum = zip.readUInt32LE(cursor + 16);
      if (crc32(found) !== checksum || (!(flags & 8) && zip.readUInt32LE(local + 14) !== checksum)) {
        throw new DownloadError('archive_crc_mismatch');
      }
    }
    cursor = end;
  }
  if (cursor !== directoryEnd) throw new DownloadError('archive_invalid');
  if (found === undefined) throw new DownloadError('archive_missing_member');
  return found;
}

function extractUv(buffer, { format, member, maxBytes = 128 * 1024 * 1024 }) {
  try {
    if (!Buffer.isBuffer(buffer) || typeof member !== 'string' || !member || /[\x00-\x1f\x7f]/.test(member) ||
        member.startsWith('/') || member.includes('\\') || member.split('/').some((part) => !part || ['.', '..'].includes(part)) ||
        !Number.isSafeInteger(maxBytes) || maxBytes < 1) throw new DownloadError('archive_invalid');
    if (format === 'tar.gz') return tarMember(buffer, member, maxBytes);
    if (format === 'zip') return zipMember(buffer, member, maxBytes);
    throw new DownloadError('archive_unsupported');
  } catch (error) {
    if (error instanceof DownloadError) throw error;
    throw new DownloadError('archive_invalid');
  }
}

module.exports = { DownloadError, download, extractUv };
