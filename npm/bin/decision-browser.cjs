#!/usr/bin/env node
'use strict';

// No npm dependencies, install scripts, shell interpolation, or unpinned PyPI lookup.
const { spawnSync } = require('node:child_process');
const { createHash } = require('node:crypto');
const { readFileSync, lstatSync, statSync, realpathSync, readdirSync } = require('node:fs');
const path = require('node:path');
const pkg = require('../package.json');
const { BootstrapError, ensureUv, supportedUv, cacheDirectory, privateDirectory } = require('./uv-bootstrap.cjs');

class SetupError extends Error {
  constructor(message, code = 'launcher_error', changed = false) {
    super(message);
    this.code = code;
    this.changed = changed;
  }
}

const PROVIDER_ENV_NAMES = ['OPENAI_API_KEY', 'TYPESAFE_API_KEY', 'OPENROUTER_API_KEY', 'OPENCODE_API_KEY',
  'DECISION_BROWSER_PROVIDER', 'DECISION_BROWSER_API_KEY', 'DECISION_BROWSER_ENDPOINT',
  'DECISION_BROWSER_MODEL', 'DECISION_BROWSER_MAX_CHOICES', 'DECISION_BROWSER_TEXT_ENDPOINT',
  'DECISION_BROWSER_TEXT_MODEL', 'DECISION_BROWSER_TEXT_API_KEY'];

const HELP = `Decision Browser ${pkg.version}
Usage: decision-browser setup [codex|claude|both] [options]
       decision-browser install <codex|claude|both> [options]
       decision-browser configure [provider] [--project <directory>]
       decision-browser doctor [--json] [--project <directory>]
       decision-browser install-status [--json] [--project <directory>]
       decision-browser recover-install [--json] [--project <directory>] [--apply]

setup chooses your client and provider interactively; install is unattended.

  --project <directory>  Existing project (default: current directory)
  --upgrade              Replace a reviewed installation; include all its clients
  --uninstall            Remove unchanged managed registrations and skills (install only)
  --help                 Show this help without downloads or changes
  --version              Print the launcher version

Requires Node.js 20+ and the selected client CLI(s) for setup/install.
uv and Python 3.12 are prepared automatically when needed, without shell changes.
configure and doctor use a cached package runtime; they do not reinstall Chromium.
install-status and recover-install use an existing local Python 3.11+ interpreter.
recover-install checks recovery conditions; --apply explicitly requests recovery.
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

function outsideProject(file, project) {
  const relative = path.relative(project, file);
  return relative === '..' || relative.startsWith('..' + path.sep) || path.isAbsolute(relative);
}

function recoveryEnvironment(source, project) {
  const env = {};
  for (const [name, value] of Object.entries(source)) {
    const key = process.platform === 'win32' ? name.toUpperCase() : name;
    if (['HOME', 'USERPROFILE', 'SYSTEMROOT', 'WINDIR', 'TMPDIR', 'TMP', 'TEMP', 'LANG', 'LC_ALL'].includes(key)) {
      env[key] = value;
    }
    if (key === 'PATH') env.PATH = value;
  }
  const directories = (env.PATH || '').split(path.delimiter).filter(directory => {
    if (!path.isAbsolute(directory) || !outsideProject(path.resolve(directory), project)) return false;
    try { return outsideProject(realpathSync(directory), project); } catch { return false; }
  });
  env.PATH = directories.join(path.delimiter);
  return env;
}

function findRecoveryPython(source, project, run) {
  const env = recoveryEnvironment(source, project);
  const suffix = process.platform === 'win32' ? '.exe' : '';
  const names = ['python3.12', 'python3.11', 'python3', 'python'].map(name => name + suffix);
  const candidates = env.PATH.split(path.delimiter).filter(Boolean)
    .flatMap(directory => names.map(name => path.join(directory, name)));
  const home = source.HOME || source.USERPROFILE;
  const managedRoots = [];
  if (process.platform === 'win32') {
    if (source.APPDATA) managedRoots.push(path.join(source.APPDATA, 'uv', 'data', 'python'));
  } else {
    if (source.XDG_DATA_HOME) managedRoots.push(path.join(source.XDG_DATA_HOME, 'uv', 'python'));
    if (home) managedRoots.push(path.join(home, '.local', 'share', 'uv', 'python'));
  }
  for (const directory of managedRoots) {
    if (!path.isAbsolute(directory) || !outsideProject(path.resolve(directory), project)) continue;
    try {
      if (!outsideProject(realpathSync(directory), project)) continue;
      const entries = readdirSync(directory).filter(name => /^cpython-[a-zA-Z0-9_.-]+$/.test(name));
      if (entries.length > 100) continue;
      for (const name of entries.sort().reverse()) {
        candidates.push(path.join(directory, name, process.platform === 'win32' ? 'python.exe' : 'bin/python3'));
      }
    } catch { /* An absent managed installation is not a preparation request. */ }
  }
  for (const candidate of new Set(candidates)) {
    try {
      if (!outsideProject(candidate, project) || !statSync(candidate).isFile()
          || !outsideProject(realpathSync(candidate), project)) continue;
      const result = run(candidate, ['-I', '-S', '-B', '-c',
        'import sys; print("decision-browser-stdlib" if sys.version_info >= (3, 11) else "unsupported")'],
      { env, cwd: path.parse(process.execPath).root, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'],
        shell: false, timeout: 5000, maxBuffer: 4096 });
      if (!result.error && !result.signal && result.status === 0
          && result.stdout?.trim() === 'decision-browser-stdlib') return { command: candidate, env };
    } catch { /* Try another existing interpreter without printing its details. */ }
  }
  throw new SetupError('Use an existing Python 3.11+ installation for installer diagnostics and recovery.',
    'python_unavailable');
}

function runRecovery(root, options, source, run, out) {
  const helper = path.join(root, 'bin', 'install-recovery.py');
  const info = lstatSync(helper);
  if (!info.isFile() || info.isSymbolicLink()) {
    throw new SetupError('Obtain a reviewed launcher containing the recovery helper.', 'recovery_helper_unavailable');
  }
  const python = findRecoveryPython(source, realpathSync(options.project), run);
  const args = ['-I', '-S', '-B', helper, options.command, '--project', options.project];
  if (options.json) args.push('--json');
  if (options.apply) args.push('--apply');
  let result;
  try {
    result = run(python.command, args, { env: python.env, cwd: path.parse(process.execPath).root,
      encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], shell: false, timeout: 120000, maxBuffer: 65536 });
  } catch {
    throw new SetupError('Installer inspection did not complete. Run install-status to inspect the retained state.',
      'recovery_inspection_failed', options.apply ? null : false);
  }
  if (result.error || result.signal || result.status === null || typeof result.stdout !== 'string') {
    throw new SetupError('Installer inspection did not complete. Run install-status to inspect the retained state.',
      'recovery_inspection_failed', options.apply ? null : false);
  }
  if (options.json) {
    let response;
    try { response = JSON.parse(result.stdout); } catch { /* Fixed error below. */ }
    if (!response || Array.isArray(response) || typeof response !== 'object') {
      throw new SetupError('Installer inspection did not return a valid report. Preserve the retained lease and runtime.',
        'recovery_inspection_failed', options.apply ? null : false);
    }
    out.write(JSON.stringify(response) + '\n');
  } else if (result.stdout) out.write(result.stdout);
  else {
    throw new SetupError('Installer inspection did not complete. Preserve the retained lease and runtime.',
      'recovery_inspection_failed', options.apply ? null : false);
  }
  return result.status;
}

function parseArgs(argv, cwd) {
  if (argv.length === 0 || (argv.length === 1 && ['--help', '-h'].includes(argv[0]))) return { help: true };
  if (argv.length === 1 && argv[0] === '--version') return { version: true };
  if (['install-status', 'recover-install'].includes(argv[0])) {
    const command = argv[0];
    let project = cwd, json = false, apply = false;
    const flags = new Set();
    for (let i = 1; i < argv.length; i++) {
      const option = argv[i];
      if (['--help', '-h'].includes(option)) return { help: true };
      if (flags.has(option)) throw new SetupError('Duplicate option. See --help.');
      flags.add(option);
      if (option === '--project') {
        const value = argv[++i];
        if (!value || value.startsWith('--')) throw new SetupError('--project needs a directory.');
        project = path.resolve(cwd, value);
      } else if (option === '--json') json = true;
      else if (option === '--apply' && command === 'recover-install') apply = true;
      else throw new SetupError('Unknown option. See --help.');
    }
    return { command, project, json, apply };
  }
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
    throw new SetupError('Use setup, install, configure, doctor, install-status, or recover-install. See --help.');
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
    if (['install-status', 'recover-install'].includes(options.command)) {
      return runRecovery(root, options, deps.env || process.env, run, out);
    }
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
    if (['install-status', 'recover-install'].includes(argv[0]) && argv.includes('--json')) {
      out.write(JSON.stringify({ schema: 1, status: 'blocked',
        reason: error instanceof SetupError ? error.code : 'launcher_error',
        recovery: 'unavailable', changed: error instanceof SetupError ? error.changed : false,
        next_action: message }) + '\n');
      return 1;
    }
    err.write(`Setup stopped: ${message || 'Invalid package or arguments.'}\n`);
    return 1;
  }
}

module.exports = { main, parseArgs, installerArgs, supportedUv, cleanEnvironment, runtimeEnvironment, archivePath,
  preparePython, prepareRuntime, findRecoveryPython, recoveryEnvironment };
if (require.main === module) main().then(code => { process.exitCode = code; });
