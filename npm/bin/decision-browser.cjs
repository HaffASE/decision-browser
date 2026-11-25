#!/usr/bin/env node
'use strict';

// No npm dependencies, install scripts, shell interpolation, or unpinned PyPI lookup.
const { spawnSync } = require('node:child_process');
const { createHash } = require('node:crypto');
const { readFileSync, lstatSync, statSync } = require('node:fs');
const path = require('node:path');
const pkg = require('../package.json');
const { BootstrapError, ensureUv, supportedUv, cacheDirectory, privateDirectory } = require('./uv-bootstrap.cjs');

class SetupError extends Error {}

const PROVIDER_ENV_NAMES = ['OPENAI_API_KEY', 'TYPESAFE_API_KEY', 'OPENROUTER_API_KEY', 'OPENCODE_API_KEY',
  'DECISION_BROWSER_PROVIDER', 'DECISION_BROWSER_API_KEY', 'DECISION_BROWSER_ENDPOINT',
  'DECISION_BROWSER_MODEL', 'DECISION_BROWSER_MAX_CHOICES', 'DECISION_BROWSER_TEXT_ENDPOINT',
  'DECISION_BROWSER_TEXT_MODEL', 'DECISION_BROWSER_TEXT_API_KEY'];

const HELP = `Decision Browser ${pkg.version}
Usage: decision-browser setup [codex|claude|both] [options]
       decision-browser install <codex|claude|both> [options]
       decision-browser configure [provider] [--project <directory>]
       decision-browser doctor [--json] [--project <directory>]

setup chooses your client and provider interactively; install is unattended.

  --project <directory>  Existing project (default: current directory)
  --upgrade              Replace a reviewed installation; include all its clients
  --uninstall            Remove unchanged managed registrations and skills (install only)
  --help                 Show this help without downloads or changes
  --version              Print the launcher version

Requires Node.js 20+ and the selected client CLI(s) for setup/install.
uv and Python 3.12 are prepared automatically when needed, without shell changes.
configure and doctor use a cached package runtime; they do not reinstall Chromium.
Setup downloads Python if needed, locked dependencies, and Chromium. It does not
install client CLIs, invoke sudo, change project trust, or call a model API.
`;

function cleanEnvironment(env, platform = process.platform) {
  const allowed = new Set(['PATH', 'HOME', 'USERPROFILE', 'SYSTEMROOT', 'WINDIR',
    'TMPDIR', 'TMP', 'TEMP', 'LANG', 'LC_ALL', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'NO_PROXY',
    'DECISION_BROWSER_INSTALL_INDEX_URL', 'DECISION_BROWSER_CONFIG_FILE', 'NODE_EXTRA_CA_CERTS', 'UV_SYSTEM_CERTS',
    'DECISION_BROWSER_CACHE_DIR', 'XDG_CACHE_HOME', 'LOCALAPPDATA']);
  const result = {};
  for (const [key, value] of Object.entries(env)) {
    if (!allowed.has(key.toUpperCase())) continue;
    const name = platform === 'win32' ? key.toUpperCase() : key;
    if (Object.hasOwn(result, name) && result[name] !== value) {
      throw new SetupError('Conflicting Windows environment spellings; keep one value for each setting.');
    }
    result[name] = value;
  }
  return result;
}

function runtimeEnvironment(env) {
  const result = cleanEnvironment(env);
  for (const name of PROVIDER_ENV_NAMES) {
    if (Object.hasOwn(env, name)) result[name] = env[name];
  }
  return result;
}

function parseArgs(argv, cwd) {
  if (argv.length === 0 || (argv.length === 1 && ['--help', '-h'].includes(argv[0]))) return { help: true };
  if (argv.length === 1 && argv[0] === '--version') return { version: true };
  if (['configure', 'doctor'].includes(argv[0])) {
    const command = argv[0];
    let project = cwd, provider, json = false;
    let start = 1;
    if (command === 'configure' && argv[1] && !argv[1].startsWith('-')) {
      provider = argv[1]; start = 2;
      if (!['openai', 'typesafe', 'openrouter', 'opencode', 'gemini', 'local', 'jev', 'nimble'].includes(provider)) {
        throw new SetupError('Unknown provider. Run configure without a provider to choose from the menu.');
      }
    }
    const flags = new Set();
    for (let i = start; i < argv.length; i++) {
      const option = argv[i];
      if (['--help', '-h'].includes(option)) return { help: true };
      if (flags.has(option)) throw new SetupError('Duplicate option. See --help.');
      flags.add(option);
      if (option === '--project') {
        const value = argv[++i];
        if (!value || value.startsWith('--')) throw new SetupError('--project needs a directory.');
        project = path.resolve(cwd, value);
      } else if (option === '--json' && command === 'doctor') json = true;
      else throw new SetupError('Unknown option. See --help.');
    }
    return { command, project, provider, json };
  }
  const setup = argv[0] === 'setup';
  if (!setup && argv[0] !== 'install') {
    throw new SetupError('Use setup, install, configure, or doctor. See --help.');
  }
  const client = ['codex', 'claude', 'both'].includes(argv[1]) ? argv[1] : undefined;
  if (!setup && !client) throw new SetupError('Choose install codex, install claude, or install both. See --help.');
  let project = cwd;
  const flags = new Set();
  for (let i = client ? 2 : 1; i < argv.length; i++) {
    const option = argv[i];
    if (option === '--help' || option === '-h') return { help: true };
    if (flags.has(option)) throw new SetupError('Duplicate option. See --help.');
    flags.add(option);
    if (option === '--project') {
      const value = argv[++i];
      if (!value || value.startsWith('--')) throw new SetupError('--project needs a directory.');
      project = path.resolve(cwd, value);
    } else if (!['--upgrade', '--uninstall'].includes(option)) {
      throw new SetupError('Unknown option. See --help.');
    }
  }
  if (flags.has('--upgrade') && flags.has('--uninstall')) throw new SetupError('Choose --upgrade or --uninstall, not both.');
  if (setup && flags.has('--uninstall')) throw new SetupError('setup cannot be combined with --uninstall.');
  return { client, project, setup, upgrade: flags.has('--upgrade'), uninstall: flags.has('--uninstall') };
}

function archivePath(root) {
  const file = path.join(root, 'installer', 'decision-browser.pyz');
  const manifestFile = path.join(root, 'installer', 'manifest.json');
  for (const name of [file, manifestFile]) {
    const stat = lstatSync(name);
    if (!stat.isFile() || stat.isSymbolicLink()) throw new SetupError('Installer files must be regular files.');
  }
  const manifest = JSON.parse(readFileSync(manifestFile, 'utf8'));
  const digest = createHash('sha256').update(readFileSync(file)).digest('hex');
  if (!manifest || manifest.version !== pkg.version ||
      manifest.sha256 !== digest || manifest.file !== 'decision-browser.pyz') {
    throw new SetupError('Installer integrity check failed; obtain the reviewed package again.');
  }
  return file;
}

function installerArgs(archive, options) {
  const args = ['-I', archive];
  if (options.client) args.push('--client', options.client);
  args.push('--project', options.project);
  if (options.setup) args.push('--setup');
  if (options.upgrade) args.push('--upgrade');
  if (options.uninstall) args.push('--uninstall');
  return args;
}

function preparePython(uv, run, cwd) {
  const args = ['--no-config', 'run', '--no-project', '--managed-python', '--python', '3.12',
    '--', 'python', '-I', '-c', 'import sys; print(sys.executable)'];
  // uv can report environment values and download URLs before Python starts.
  // Capture this preparation separately so the installer can retain its terminal.
  const result = run(uv.command, args, { env: uv.env, cwd, encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'], shell: false, timeout: 900000, maxBuffer: 65536 });
  if (result.error || result.signal || result.status !== 0) {
    if (typeof result.stderr === 'string' && result.stderr.includes('Failed to parse environment variable `UV_SYSTEM_CERTS`')) {
      throw new SetupError('uv_system_certs_invalid: managed Python preparation failed. Set UV_SYSTEM_CERTS to a supported boolean value or unset it. Client settings were not changed.');
    }
    const code = result.error?.code === 'ETIMEDOUT' ? 'managed_python_timeout'
      : result.signal ? 'managed_python_interrupted'
        : result.error ? 'managed_python_start_failed' : 'managed_python_failed';
    throw new SetupError(`${code}: managed Python preparation failed. Check uv availability, network access and explicit CA settings, including UV_SYSTEM_CERTS. Client settings were not changed.`);
  }
  const python = typeof result.stdout === 'string' ? result.stdout.trim() : '';
  let usable = false;
  if (path.isAbsolute(python) && !/[\r\n\x00]/.test(python)) {
    try { usable = statSync(python).isFile(); } catch { /* fixed diagnostic below */ }
  }
  if (!usable) {
    throw new SetupError('managed_python_invalid: uv did not provide a usable Python executable. Check the managed Python installation. Client settings were not changed.');
  }
  return python;
}

function prepareRuntime(uv, archive, cache, run, cwd) {
  privateDirectory(cache);
  const python = preparePython(uv, run, cwd);
  const args = ['-I', archive, '--prepare-runtime', '--cache', cache];
  const result = run(python, args, { env: uv.env, cwd, encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'inherit'], shell: false, timeout: 900000, maxBuffer: 65536 });
  if (result.error || result.signal || result.status !== 0) {
    throw new SetupError('Command runtime preparation failed. Follow the diagnostic above. Project settings were not changed.');
  }
  let response;
  try { response = JSON.parse(result.stdout); } catch { /* fixed diagnostic below */ }
  const runtimePython = response?.python;
  const relative = typeof runtimePython === 'string' && path.isAbsolute(runtimePython)
    ? path.relative(path.join(cache, 'runtimes'), runtimePython) : '..';
  const suffix = process.platform === 'win32' ? path.join('venv', 'Scripts', 'python.exe') : path.join('venv', 'bin', 'python');
  if (!relative || relative.startsWith('..') || path.isAbsolute(relative)
      || !relative.endsWith(path.sep + suffix) || !statSync(runtimePython).isFile()) {
    throw new SetupError('Invalid prepared runtime; no project-supplied executable was started.');
  }
  return runtimePython;
}

async function main(argv = process.argv.slice(2), deps = {}) {
  const run = deps.run || spawnSync;
  const out = deps.out || process.stdout;
  const err = deps.err || process.stderr;
  const root = deps.root || path.resolve(__dirname, '..');
  const cwd = deps.cwd || process.cwd();
  try {
    if (Number(process.versions.node.split('.')[0]) < 20) throw new SetupError('Node.js 20+ is required.');
    const options = parseArgs(argv, cwd);
    if (options.help) { out.write(HELP); return 0; }
    if (options.version) { out.write(`${pkg.version}\n`); return 0; }
    if ((options.setup || options.command === 'configure') && !(deps.stdin || process.stdin).isTTY) {
      throw new SetupError('Interactive setup/configure requires a terminal. Use install with an explicit client for unattended installation.');
    }
    const stat = lstatSync(options.project);
    if (!stat.isDirectory() || stat.isSymbolicLink()) throw new SetupError('Choose an existing regular project directory.');
    const archive = archivePath(root);
    const sourceEnv = deps.env || process.env;
    const uv = await (deps.ensureUv || ensureUv)({ run, env: cleanEnvironment(sourceEnv), cwd, err });
    let command, args, env = uv.env;
    if (options.command) {
      command = (deps.prepareRuntime || prepareRuntime)(uv, archive, cacheDirectory(uv.env), run, cwd);
      args = ['-I', '-B', '-m', 'decision_browser.cli', options.command];
      if (options.command === 'doctor') args.push('--json', '--project', options.project);
      else if (options.provider) args.push(options.provider);
      env = runtimeEnvironment({ ...sourceEnv, ...uv.env });
    } else {
      out.write('Preparing Decision Browser with uv-managed Python 3.12. Downloads may be needed.\n');
      command = preparePython(uv, run, options.project);
      args = installerArgs(archive, options);
    }
    const result = run(command, args, { env, cwd: options.project, stdio: 'inherit', shell: false });
    if (result.error || result.signal || result.status === null) {
      err.write('Setup interrupted or failed to start. Inspect the project and any retained installer lease before another preparation.\n');
      return 1;
    }
    if (options.setup && result.status === 0) {
      const overrides = PROVIDER_ENV_NAMES.filter(name => Object.hasOwn(sourceEnv, name));
      if (overrides.length) {
        err.write('Provider environment settings may still take precedence over the saved profile: '
          + overrides.join(', ') + '. Remove relevant overrides from the coding client environment '
          + 'to use the saved selection, then restart the client.\n');
      }
    }
    return result.status;
  } catch (error) {
    // Never print arbitrary child errors, paths, or environment values.
    const message = error instanceof SetupError || error instanceof BootstrapError ? error.message : 'Missing, invalid or unreadable project/package/cache files.';
    err.write(`Setup stopped: ${message || 'Invalid package or arguments.'}\n`);
    return 1;
  }
}

module.exports = { main, parseArgs, installerArgs, supportedUv, cleanEnvironment, runtimeEnvironment, archivePath, preparePython, prepareRuntime };
if (require.main === module) main().then(code => { process.exitCode = code; });
