'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createHash } = require('node:crypto');
const manifest = require('./uv-manifest.json');

class BootstrapError extends Error {}

function versionOf(result) {
  if (result.error || result.status !== 0) return null;
  const match = /^uv (\d+)\.(\d+)\.(\d+)(?:[^\r\n]*)\r?\n?$/.exec(result.stdout || '');
  return match ? match.slice(1).map(Number) : null;
}

function supportedUv(result) {
  const version = versionOf(result);
  if (!version) return false;
  const [major, minor, patch] = version;
  return major > 0 || minor > 13 || (minor === 13 && patch >= 7);
}

function envValue(env, name) {
  const key = Object.keys(env).find(key => key.toUpperCase() === name);
  return key === undefined ? undefined : env[key];
}

function cacheDirectory(env, platform = process.platform) {
  const override = envValue(env, 'DECISION_BROWSER_CACHE_DIR');
  const home = envValue(env, platform === 'win32' ? 'USERPROFILE' : 'HOME') || os.homedir();
  const base = platform === 'win32' ? (envValue(env, 'LOCALAPPDATA') || path.join(home, 'AppData', 'Local'))
    : platform === 'darwin' ? path.join(home, 'Library', 'Caches')
      : (envValue(env, 'XDG_CACHE_HOME') || path.join(home, '.cache'));
  const directory = override === undefined ? path.join(base, 'decision-browser') : override;
  if (!directory || !path.isAbsolute(directory) || /[\x00-\x1f\x7f]/.test(directory)
      || directory.split(/[\\/]/).includes('..')) {
    throw new BootstrapError('Invalid cache location; use an absolute DECISION_BROWSER_CACHE_DIR.');
  }
  return path.resolve(directory);
}

function privatePath(file, directory = false) {
  const info = fs.lstatSync(file);
  if (info.isSymbolicLink() || (directory ? !info.isDirectory() : !info.isFile())
      || (!directory && info.nlink !== 1)
      || (process.platform !== 'win32' && ((info.mode & 0o077) || info.uid !== process.getuid()))) {
    throw new BootstrapError('Tool cache must contain private, user-owned regular files and directories.');
  }
  return info;
}

function privateDirectory(directory) {
  for (let current = directory; ; current = path.dirname(current)) {
    try {
      if (fs.lstatSync(current).isSymbolicLink()) throw new BootstrapError('Symlinked tool cache paths are not supported.');
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
    if (current === path.dirname(current)) break;
  }
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  privatePath(directory, true);
  return directory;
}

function digest(data) { return createHash('sha256').update(data).digest('hex'); }

function readCached(directory, target, version) {
  if (!fs.existsSync(directory)) return null;
  privatePath(directory, true);
  const receiptFile = path.join(directory, 'receipt.json');
  const executable = path.join(directory, target.format === 'zip' ? 'uv.exe' : 'uv');
  try {
    if (privatePath(receiptFile).size > 4096 || privatePath(executable).size > 128 * 1024 * 1024) {
      throw new Error('size');
    }
    const receipt = JSON.parse(fs.readFileSync(receiptFile, 'utf8'));
    if (receipt.version !== version || receipt.archive !== target.sha256
        || receipt.binary !== digest(fs.readFileSync(executable))) throw new Error('integrity');
  } catch (error) {
    if (error instanceof BootstrapError) throw error;
    throw new BootstrapError('Cached uv failed its integrity check. Remove this version from the Decision Browser tool cache and retry.');
  }
  return executable;
}

function withUvPath(env, executable) {
  const result = { ...env };
  const key = Object.keys(result).find(name => name.toUpperCase() === 'PATH') || 'PATH';
  result[key] = path.dirname(executable) + path.delimiter + (result[key] || '');
  return result;
}

async function ensureUv({ run, env, cwd, err, platform = process.platform, arch = process.arch,
  release = manifest, fetchArchive, extract } = {}) {
  const options = { env, cwd, encoding: 'utf8', timeout: 15000, shell: false };
  if (supportedUv(run('uv', ['--version'], options))) return { command: 'uv', env };
  const target = release.targets[`${platform}-${arch}`];
  if (!target) throw new BootstrapError('Automatic uv installation is unavailable for this platform/architecture.');
  const cache = privateDirectory(cacheDirectory(env, platform));
  const parent = privateDirectory(path.join(cache, 'tools', 'uv'));
  const directory = path.join(parent, `${release.version}-${platform}-${arch}`);
  const cached = readCached(directory, target, release.version);
  if (cached) {
    const result = run(cached, ['--version'], options);
    if (versionOf(result)?.join('.') !== release.version) throw new BootstrapError('Cached uv cannot run on this system.');
    return { command: cached, env: withUvPath(env, cached) };
  }
  err.write(`Preparing uv ${release.version} for ${platform}/${arch}. Downloading from Astral.\n`);
  const stage = fs.mkdtempSync(path.join(parent, '.prepare-'));
  try {
    const downloader = require('./uv-download.cjs');
    const data = await (fetchArchive || downloader.download)(release.baseUrl + target.file, { env });
    if (digest(data) !== target.sha256) throw new BootstrapError('uv download checksum mismatch; no downloaded program was executed. Retry or check your network.');
    const binary = (extract || downloader.extractUv)(data, target);
    const executable = path.join(stage, target.format === 'zip' ? 'uv.exe' : 'uv');
    fs.writeFileSync(executable, binary, { flag: 'wx', mode: 0o700 });
    // Check the exact pinned version only after archive integrity verification.
    if (versionOf(run(executable, ['--version'], options))?.join('.') !== release.version) {
      throw new BootstrapError('Downloaded uv cannot run on this system; check platform compatibility.');
    }
    fs.writeFileSync(path.join(stage, 'receipt.json'), JSON.stringify({
      version: release.version, archive: target.sha256, binary: digest(binary),
    }) + '\n', { flag: 'wx', mode: 0o600 });
    try { fs.renameSync(stage, directory); }
    catch (error) {
      if (!['EEXIST', 'ENOTEMPTY'].includes(error.code)) throw error;
      // A concurrent successful installer may have committed the same version.
      if (!readCached(directory, target, release.version)) throw error;
    }
    const ready = readCached(directory, target, release.version);
    return { command: ready, env: withUvPath(env, ready) };
  } catch (error) {
    if (error instanceof BootstrapError) throw error;
    const code = error.code;
    const hints = {
      download_timeout: 'uv download timed out. Retry or check your proxy.',
      download_tls_failed: 'uv TLS verification failed. Configure NODE_EXTRA_CA_CERTS or SSL_CERT_FILE for your network.',
      download_dns_failed: 'uv download hostname could not be resolved. Check your network or HTTPS_PROXY.',
    };
    throw new BootstrapError(hints[code] || 'Could not prepare uv. Check network/proxy/CA settings and cache permissions, then retry.');
  } finally {
    fs.rmSync(stage, { recursive: true, force: true });
  }
}

module.exports = { BootstrapError, ensureUv, supportedUv, cacheDirectory, privateDirectory, withUvPath };
