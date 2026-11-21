'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { main, parseArgs, uvArgs, supportedUv, cleanEnvironment } = require('../bin/decision-browser.cjs');
const { version } = require('../package.json');

test('Windows environment has canonical spellings and refuses conflicting aliases', () => {
  const clean = cleanEnvironment({ Path: 'tools', Https_Proxy: 'http://proxy', No_Proxy: 'localhost',
    OpenAI_Api_Key: 'not-forwarded' }, 'win32');
  assert.deepEqual(clean, { PATH: 'tools', HTTPS_PROXY: 'http://proxy', NO_PROXY: 'localhost' });
  assert.throws(() => cleanEnvironment({ Https_Proxy: 'one', HTTPS_PROXY: 'two' }, 'win32'), /Conflicting Windows/);
  assert.deepEqual(cleanEnvironment({ Https_Proxy: 'same', HTTPS_PROXY: 'same' }, 'win32'), { HTTPS_PROXY: 'same' });
  assert.deepEqual(cleanEnvironment({ https_proxy: 'lower', HTTPS_PROXY: 'upper' }, 'linux'), {
    https_proxy: 'lower', HTTPS_PROXY: 'upper',
  });
});

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'decision launcher '));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.mkdirSync(path.join(root, 'installer'));
  const data = Buffer.from('fixture archive, never executed');
  fs.writeFileSync(path.join(root, 'installer/decision-browser.pyz'), data);
  fs.writeFileSync(path.join(root, 'installer/manifest.json'), JSON.stringify({
    file: 'decision-browser.pyz', version, sha256: createHash('sha256').update(data).digest('hex'),
  }));
  return root;
}

function capture(options = {}) {
  let output = '';
  const stream = { write(text) { output += text; } };
  return { deps: { out: stream, err: stream, ...options }, text: () => output };
}

test('help and version have no setup, files, or process dependencies', async () => {
  for (const args of [[], ['--help'], ['-h'], ['--version'], ['install', 'codex', '--help']]) {
    const c = capture({ root: '/does-not-exist', run() { assert.fail('no subprocess'); } });
    assert.equal(await main(args, c.deps), 0);
    assert.ok(c.text().includes(version));
  }
});

test('project defaults to cwd; spaces and shell text stay one argument', async () => {
  const cwd = path.resolve('project with spaces;echo secret');
  const options = parseArgs(['install', 'both'], cwd);
  assert.equal(options.project, cwd);
  const args = uvArgs('/archive with spaces.pyz', options);
  assert.deepEqual(args.slice(-4), ['--client', 'both', '--project', cwd]);
  assert.ok(args.includes('--no-project'));
  assert.ok(args.includes('--managed-python'));
  assert.deepEqual(args.slice(args.indexOf('--') + 1, -4), ['python', '-I', '/archive with spaces.pyz']);
});

test('reject unknown arguments, missing values, duplicates and contradictory operations', async () => {
  for (const args of [ ['run'], ['install', 'other'], ['install', 'codex', '--source', '.'],
    ['install', 'codex', '--project'], ['install', 'codex', '--project', '--upgrade'],
    ['install', 'codex', '--upgrade', '--upgrade'], ['install', 'codex', '--upgrade', '--uninstall'] ]) {
    assert.throws(() => parseArgs(args, process.cwd()));
  }
});

test('upgrade and uninstall forwarded independently for every client', async () => {
  for (const client of ['codex', 'claude', 'both']) {
    for (const flag of ['--upgrade', '--uninstall']) {
      const options = parseArgs(['install', client, '--project', 'other project', flag], process.cwd());
      assert.equal(uvArgs('archive.pyz', options).at(-1), flag);
      assert.equal(options.project, path.resolve('other project'));
    }
  }
});

test('accept supported uv versions and reject missing, old or malformed uv', async () => {
  for (const version of ['0.13.7', '0.13.8', '0.14.0', '1.0.0']) {
    assert.equal(supportedUv({ status: 0, stdout: `uv ${version} (platform)\n` }), true);
  }
  for (const result of [{ error: Error('missing') }, { status: 1, stdout: 'uv 0.13.7' },
    { status: 0, stdout: 'uv 0.13.5' }, { status: 0, stdout: 'something else' },
    { status: 0, stdout: 'uv 0.13.7\nextra' }]) assert.equal(supportedUv(result), false);
});

test('explicit network settings reach children without provider keys or injection variables', async () => {
  const env = cleanEnvironment({ PATH: '/bin', Path: 'Windows', HOME: '/home', SystemRoot: 'C:/Windows',
    OPENAI_API_KEY: 'secret', TYPESAFE_API_KEY: 'secret', UV_INDEX_URL: 'secret',
    UV_PYTHON: 'untrusted', PYTHONPATH: 'untrusted', NODE_OPTIONS: 'untrusted', HTTPS_PROXY: 'http://proxy:8080',
    DECISION_BROWSER_INSTALL_INDEX_URL: 'https://mirror.example/simple', SSL_CERT_FILE: '/ca.pem',
    DECISION_BROWSER_CONFIG_FILE: '/private profiles/provider.json',
    NODE_EXTRA_CA_CERTS: '/corporate-ca.pem' });
  assert.deepEqual(env, { PATH: '/bin', Path: 'Windows', HOME: '/home', SystemRoot: 'C:/Windows',
    HTTPS_PROXY: 'http://proxy:8080', DECISION_BROWSER_INSTALL_INDEX_URL: 'https://mirror.example/simple',
    SSL_CERT_FILE: '/ca.pem', NODE_EXTRA_CA_CERTS: '/corporate-ca.pem',
    DECISION_BROWSER_CONFIG_FILE: '/private profiles/provider.json' });
});

test('valid package runs uv without a shell, from any cwd, and preserves status', async (t) => {
  const root = fixture(t);
  const calls = [];
  const c = capture({ root, cwd: root, env: { PATH: '/bin', OPENAI_API_KEY: 'do-not-forward' },
    run(command, args, options) {
      calls.push({ command, args, options });
      return args[0] === '--version' ? { status: 0, stdout: 'uv 0.13.7\n' } : { status: 7 };
    } });
  assert.equal(await main(['install', 'claude'], c.deps), 7);
  assert.equal(calls.length, 2);
  assert.equal(calls[1].command, 'uv');
  assert.equal(calls[1].options.shell, false);
  assert.deepEqual(calls[1].options.env, { PATH: '/bin' });
  assert.ok(!c.text().includes('do-not-forward'));
});

test('bootstrap failure does not execute installer or leak arbitrary errors', async (t) => {
  const root = fixture(t);
  let calls = 0;
  const c = capture({ root, cwd: root, run() { calls++; assert.fail('installer must not run'); },
    ensureUv: async () => { throw Error('secret detail'); } });
  assert.equal(await main(['install', 'codex'], c.deps), 1);
  assert.equal(calls, 0);
  assert.ok(!c.text().includes('secret detail'));
});

test('managed uv executable and its PATH reach the existing installer', async (t) => {
  const root = fixture(t);
  let call;
  const c = capture({ root, cwd: root,
    ensureUv: async () => ({ command: '/private tools/uv', env: { PATH: '/private tools:/bin' } }),
    run(command, args, options) { call = { command, args, options }; return { status: 0 }; } });
  assert.equal(await main(['install', 'both'], c.deps), 0);
  assert.equal(call.command, '/private tools/uv');
  assert.equal(call.options.env.PATH, '/private tools:/bin');
  assert.ok(call.args.includes('--client'));
});

test('configure and doctor parse their own options without accepting installer flags', () => {
  assert.deepEqual(parseArgs(['configure', 'jev', '--project', 'space project'], '/tmp'), {
    command: 'configure', project: path.resolve('/tmp', 'space project'), provider: 'jev', json: false,
  });
  assert.deepEqual(parseArgs(['configure', 'gemini'], '/tmp'), {
    command: 'configure', project: '/tmp', provider: 'gemini', json: false,
  });
  assert.equal(parseArgs(['doctor', '--json'], '/tmp').json, true);
  for (const args of [['configure', '--upgrade'], ['configure', '--json'], ['doctor', 'codex'],
    ['doctor', '--uninstall'], ['doctor', '--json', '--json'], ['configure', 'unknown']]) {
    assert.throws(() => parseArgs(args, '/tmp'));
  }
});

test('configure terminal preflight and command help need no package or uv', async () => {
  const c = capture({ root: '/missing', cwd: '/missing', stdin: { isTTY: false },
    run() { assert.fail('no subprocess'); } });
  assert.equal(await main(['configure', 'openai'], c.deps), 1);
  assert.ok(c.text().includes('requires a terminal'));
  for (const args of [['configure', '--help'], ['doctor', '--help']]) {
    assert.equal(await main(args, c.deps), 0);
  }
});

test('doctor and configure use package cache, not project manifest commands', async (t) => {
  const root = fixture(t);
  const cache = path.join(fs.realpathSync(root), 'cache');
  fs.mkdirSync(cache, { mode: 0o700 });
  const python = path.join(cache, 'runtimes', 'fixture', 'attempt', 'venv',
    process.platform === 'win32' ? 'Scripts' : 'bin', process.platform === 'win32' ? 'python.exe' : 'python');
  fs.mkdirSync(path.dirname(python), { recursive: true });
  fs.writeFileSync(python, 'fixture, never executed');
  fs.mkdirSync(path.join(root, '.decision-browser-install'));
  fs.writeFileSync(path.join(root, '.decision-browser-install/manifest.json'), JSON.stringify({
    clients: { codex: { config: { command: 'malicious-project-command' } } },
  }));
  for (const [command, provider] of [['doctor'], ['configure', 'openai'], ['configure', 'gemini']]) {
    const calls = [];
    const c = capture({ root, cwd: root, stdin: { isTTY: true },
      env: { PATH: '/bin', DECISION_BROWSER_CACHE_DIR: cache, OPENAI_API_KEY: 'CANARY-SECRET',
        OPENCODE_API_KEY: 'CANARY-OPENCODE',
        DECISION_BROWSER_API_KEY: '', NODE_OPTIONS: 'DO-NOT-FORWARD', PYTHONPATH: 'DO-NOT-FORWARD' },
      run(executable, args, options) {
        calls.push({ executable, args, options });
        if (args[0] === '--version') return { status: 0, stdout: 'uv 0.13.7' };
        if (args.includes('--prepare-runtime')) return { status: 0, stdout: JSON.stringify({ python }) };
        return { status: command === 'configure' ? 130 : 0 };
      } });
    assert.equal(await main([command, ...(command === 'doctor' ? ['--json'] : [provider])], c.deps),
      command === 'configure' ? 130 : 0);
    assert.equal(c.text(), '');
    assert.equal(calls.length, 3);
    assert.ok(calls[1].args.includes('--cache'));
    assert.ok(!JSON.stringify(calls.slice(0, 2)).includes('CANARY-SECRET'));
    assert.ok(!JSON.stringify(calls.slice(0, 2)).includes('CANARY-OPENCODE'));
    assert.equal(calls[1].options.stdio[1], 'pipe');
    assert.equal(calls[2].executable, python);
    assert.deepEqual(calls[2].args, ['-I', '-B', '-m', 'decision_browser.cli', command,
      ...(command === 'doctor' ? ['--json', '--project', root] : [provider])]);
    assert.equal(calls[2].options.env.OPENAI_API_KEY, 'CANARY-SECRET');
    assert.equal(calls[2].options.env.OPENCODE_API_KEY, 'CANARY-OPENCODE');
    assert.equal(calls[2].options.env.DECISION_BROWSER_API_KEY, '');
    assert.equal(calls[2].options.env.NODE_OPTIONS, undefined);
    assert.equal(calls[2].options.env.PYTHONPATH, undefined);
    assert.ok(!JSON.stringify(calls).includes('malicious-project-command'));
  }
});

test('failed or invalid runtime preparation never executes a returned command', async (t) => {
  const root = fixture(t);
  const cache = path.join(fs.realpathSync(root), 'cache');
  for (const result of [{ status: 1, stdout: 'CANARY' }, { status: 0, stdout: 'CANARY' },
    { status: 0, stdout: JSON.stringify({ python: '/bin/sh' }) }]) {
    let calls = 0;
    const c = capture({ root, cwd: root, env: { DECISION_BROWSER_CACHE_DIR: cache },
      run(command, args) {
        calls++;
        assert.ok(calls <= 2);
        return args[0] === '--version' ? { status: 0, stdout: 'uv 0.13.7' } : result;
      } });
    assert.equal(await main(['doctor', '--json'], c.deps), 1);
    assert.ok(!c.text().includes('CANARY'));
  }
});

test('tampered archive or mismatched version fails before any child', async (t) => {
  for (const tamper of ['archive', 'version']) {
    const root = fixture(t);
    if (tamper === 'archive') fs.appendFileSync(path.join(root, 'installer/decision-browser.pyz'), 'bad');
    else {
      const name = path.join(root, 'installer/manifest.json');
      const manifest = JSON.parse(fs.readFileSync(name));
      manifest.version = '9.9.9'; fs.writeFileSync(name, JSON.stringify(manifest));
    }
    const c = capture({ root, cwd: root, run() { assert.fail('no subprocess'); } });
    assert.equal(await main(['install', 'both'], c.deps), 1);
    assert.ok(c.text().includes('integrity'));
  }
});

test('missing package and invalid project fail before downloads', async (t) => {
  const root = fixture(t);
  const c = capture({ root, cwd: root, run() { assert.fail('no subprocess'); } });
  assert.equal(await main(['install', 'codex', '--project', path.join(root, 'missing')], c.deps), 1);
  fs.unlinkSync(path.join(root, 'installer/decision-browser.pyz'));
  assert.equal(await main(['install', 'codex'], c.deps), 1);
});

test('interrupted or failed child cannot report success', async (t) => {
  const root = fixture(t);
  for (const result of [{ status: null, signal: 'SIGINT' }, { status: null, error: Error('private') }]) {
    const c = capture({ root, cwd: root, run(command, args) {
      return args[0] === '--version' ? { status: 0, stdout: 'uv 0.13.7' } : result;
    } });
    assert.equal(await main(['install', 'codex'], c.deps), 1);
    assert.ok(!c.text().includes('private'));
  }
});

test('malformed manifest details are not echoed', async (t) => {
  const root = fixture(t);
  fs.writeFileSync(path.join(root, 'installer/manifest.json'), 'private-marker is not JSON');
  const c = capture({ root, cwd: root, run() { assert.fail('no subprocess'); } });
  assert.equal(await main(['install', 'codex'], c.deps), 1);
  assert.ok(!c.text().includes('private-marker'));
});

test('symlinked archive is rejected before reading or running it', { skip: process.platform === 'win32' }, async (t) => {
  const root = fixture(t);
  const file = path.join(root, 'installer/decision-browser.pyz');
  fs.renameSync(file, `${file}.original`);
  fs.symlinkSync(`${file}.original`, file);
  const c = capture({ root, cwd: root, run() { assert.fail('no subprocess'); } });
  assert.equal(await main(['install', 'codex'], c.deps), 1);
  assert.ok(c.text().includes('regular files'));
});

test('setup accepts an optional client and forwards the interactive mode', async () => {
  for (const client of [undefined, 'codex', 'claude', 'both']) {
    const argv = ['setup', ...(client ? [client] : []), '--project', 'project with spaces', '--upgrade'];
    const options = parseArgs(argv, process.cwd());
    assert.equal(options.client, client);
    assert.equal(options.setup, true);
    const args = uvArgs('/archive with spaces.pyz', options);
    assert.ok(args.includes('--setup'));
    assert.ok(args.includes('--upgrade'));
    assert.equal(args.includes('--client'), Boolean(client));
    assert.ok(!args.includes(undefined));
    assert.equal(args[args.indexOf('--project') + 1], path.resolve('project with spaces'));
  }
  for (const argv of [['setup', 'unknown'], ['setup', '--uninstall'],
    ['setup', 'both', '--uninstall'], ['setup', '--project']]) {
    assert.throws(() => parseArgs(argv, process.cwd()));
  }
});

test('setup without a terminal stops before file reads and subprocesses', async () => {
  for (const args of [['setup'], ['setup', 'codex']]) {
    const c = capture({ stdin: { isTTY: false }, root: '/missing', cwd: '/missing',
      run() { assert.fail('no subprocess'); } });
    assert.equal(await main(args, c.deps), 1);
    assert.ok(c.text().includes('requires a terminal'));
  }
});

test('setup help works without a terminal or package', async () => {
  for (const args of [['setup', '--help'], ['setup', 'codex', '--help']]) {
    const c = capture({ stdin: { isTTY: false }, root: '/missing',
      run() { assert.fail('no subprocess'); } });
    assert.equal(await main(args, c.deps), 0);
    assert.ok(c.text().includes('provider interactively'));
  }
});

test('interactive setup inherits the terminal while excluding keys and preserving cancellation', async (t) => {
  const root = fixture(t);
  const calls = [];
  const c = capture({ root, cwd: root, stdin: { isTTY: true },
    env: { PATH: '/bin', TYPESAFE_API_KEY: 'CANARY-KEY', DECISION_BROWSER_CONFIG_FILE: '/profiles/private.json' },
    run(command, args, options) {
      calls.push({ command, args, options });
      return args[0] === '--version' ? { status: 0, stdout: 'uv 0.13.7\n' } : { status: 130 };
    } });
  assert.equal(await main(['setup'], c.deps), 130);
  assert.equal(calls.length, 2);
  assert.ok(calls[1].args.includes('--setup'));
  assert.ok(!calls[1].args.includes('--client'));
  assert.equal(calls[1].options.stdio, 'inherit');
  assert.equal(calls[1].options.shell, false);
  assert.equal(calls[1].options.env.DECISION_BROWSER_CONFIG_FILE, '/profiles/private.json');
  assert.ok(!JSON.stringify(calls).includes('CANARY-KEY'));
  assert.ok(!c.text().includes('CANARY-KEY'));
});

test('successful setup warns about original provider overrides without forwarding or printing their values', async (t) => {
  for (const status of [0, 1, 130]) {
    const root = fixture(t);
    const calls = [];
    const c = capture({ root, cwd: root, stdin: { isTTY: true },
      env: { PATH: '/bin', DECISION_BROWSER_PROVIDER: 'CANARY-PROVIDER',
        OPENAI_API_KEY: 'CANARY-SECRET', DECISION_BROWSER_API_KEY: '',
        DECISION_BROWSER_CONFIG_FILE: '/CANARY-profile/provider.json',
        DECISION_BROWSER_CACHE_DIR: '/CANARY-cache', PLAYWRIGHT_BROWSERS_PATH: '/CANARY-browser' },
      run(command, args, options) {
        calls.push({ command, args, options });
        return args[0] === '--version' ? { status: 0, stdout: 'uv 0.13.7\n' } : { status };
      } });
    assert.equal(await main(['setup'], c.deps), status);
    assert.equal(calls.length, 2);
    for (const call of calls) {
      for (const name of ['DECISION_BROWSER_PROVIDER', 'OPENAI_API_KEY', 'DECISION_BROWSER_API_KEY']) {
        assert.equal(Object.hasOwn(call.options.env, name), false);
      }
    }
    const output = c.text();
    assert.equal(output.includes('may still take precedence over the saved profile'), status === 0);
    if (status === 0) {
      for (const name of ['DECISION_BROWSER_PROVIDER', 'OPENAI_API_KEY', 'DECISION_BROWSER_API_KEY']) {
        assert.ok(output.includes(name));
      }
      for (const name of ['DECISION_BROWSER_CONFIG_FILE', 'DECISION_BROWSER_CACHE_DIR', 'PLAYWRIGHT_BROWSERS_PATH']) {
        assert.ok(!output.includes(name));
      }
    }
    assert.ok(!output.includes('CANARY'));
    assert.ok(!JSON.stringify(calls).includes('CANARY-SECRET'));
    assert.ok(!JSON.stringify(calls).includes('CANARY-PROVIDER'));
  }
});

test('successful setup does not treat cache and profile locations as provider overrides', async (t) => {
  const root = fixture(t);
  const c = capture({ root, cwd: root, stdin: { isTTY: true },
    env: { PATH: '/bin', DECISION_BROWSER_CONFIG_FILE: '/profiles/provider.json',
      DECISION_BROWSER_CACHE_DIR: '/tool-cache', PLAYWRIGHT_BROWSERS_PATH: '/browsers' },
    run(command, args) {
      return args[0] === '--version' ? { status: 0, stdout: 'uv 0.13.7\n' } : { status: 0 };
    } });
  assert.equal(await main(['setup', 'both'], c.deps), 0);
  assert.ok(!c.text().includes('may still take precedence over the saved profile'));
});
