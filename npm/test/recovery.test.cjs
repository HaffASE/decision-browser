'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { main, parseArgs, findRecoveryPython } = require('../bin/decision-browser.cjs');

function fixture(t) {
  const directory = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'decision recovery ')));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const root = path.join(directory, 'package');
  const project = path.join(directory, 'project');
  const tools = path.join(directory, 'tools');
  for (const folder of [path.join(root, 'bin'), project, tools]) fs.mkdirSync(folder, { recursive: true });
  fs.writeFileSync(path.join(root, 'bin/install-recovery.py'), '# fixture, never executed');
  const python = path.join(tools, process.platform === 'win32' ? 'python3.12.exe' : 'python3.12');
  fs.writeFileSync(python, 'fixture, never executed');
  return { directory, root, project, tools, python };
}

function capture(options) {
  let stdout = '', stderr = '';
  return {
    deps: { ...options, out: { write(text) { stdout += text; } }, err: { write(text) { stderr += text; } } },
    stdout: () => stdout,
    stderr: () => stderr,
  };
}

test('installer inspection accepts explicit recovery only on recover-install', () => {
  assert.deepEqual(parseArgs(['install-status', '--project', 'space project', '--json'], '/tmp'), {
    command: 'install-status', project: path.resolve('/tmp', 'space project'), json: true, apply: false,
  });
  assert.deepEqual(parseArgs(['recover-install', '--apply'], '/tmp'), {
    command: 'recover-install', project: '/tmp', json: false, apply: true,
  });
  for (const args of [['install-status', '--apply'], ['recover-install', '--upgrade'],
    ['recover-install', '--uninstall'], ['install-status', '--json', '--json'],
    ['recover-install', '--apply', '--apply'], ['install-status', '--project'],
    ['install-status', 'codex']]) assert.throws(() => parseArgs(args, '/tmp'));
});

test('inspection commands run the shipped helper before archive or runtime preparation', async (t) => {
  const f = fixture(t);
  for (const [command, apply, status] of [['install-status', false, 0], ['recover-install', false, 1],
    ['recover-install', true, 1]]) {
    const calls = [];
    const report = { schema: 1, status: status ? 'blocked' : 'clear', reason: status ? 'reboot_required' : 'none',
      recovery: status ? 'reboot_required' : 'unavailable', changed: apply };
    const c = capture({ root: f.root, cwd: f.project, stdin: { isTTY: false },
      env: { PATH: f.tools, HOME: f.directory, OPENAI_API_KEY: 'CANARY-KEY',
        NODE_OPTIONS: 'CANARY-NODE', PYTHONPATH: 'CANARY-PYTHON', UV_INDEX_URL: 'CANARY-INDEX',
        DECISION_BROWSER_CACHE_DIR: path.join(f.directory, 'never-created-cache') },
      ensureUv() { assert.fail('inspection never prepares uv'); },
      prepareRuntime() { assert.fail('inspection never prepares a package runtime'); },
      run(executable, args, options) {
        calls.push({ executable, args, options });
        if (args.includes('-c')) return { status: 0, stdout: 'decision-browser-stdlib\n' };
        return { status, stdout: JSON.stringify(report), stderr: '' };
      } });
    assert.equal(await main([command, '--json', ...(apply ? ['--apply'] : [])], c.deps), status);
    assert.deepEqual(JSON.parse(c.stdout()), report);
    assert.equal(c.stderr(), '');
    assert.equal(calls.length, 2);
    assert.equal(calls[1].executable, f.python);
    assert.deepEqual(calls[1].args, ['-I', '-S', '-B', path.join(f.root, 'bin/install-recovery.py'),
      command, '--project', f.project, '--json', ...(apply ? ['--apply'] : [])]);
    for (const call of calls) {
      assert.equal(call.options.shell, false);
      assert.notEqual(call.options.cwd, f.project);
      assert.deepEqual(call.options.stdio, ['ignore', 'pipe', 'pipe']);
    }
    assert.ok(!JSON.stringify(calls).includes('CANARY'));
    assert.deepEqual(fs.readdirSync(f.project), []);
    assert.ok(!fs.existsSync(path.join(f.directory, 'never-created-cache')));
  }
});

test('inspection help and invalid JSON arguments do not prepare anything', async () => {
  const c = capture({ root: '/missing', cwd: '/missing',
    run() { assert.fail('no process'); }, ensureUv() { assert.fail('no uv'); } });
  for (const command of ['install-status', 'recover-install']) {
    assert.equal(await main([command, '--help'], c.deps), 0);
  }
  const bad = capture({ root: '/missing', cwd: '/missing', run() { assert.fail('no process'); } });
  assert.equal(await main(['install-status', '--json', '--apply'], bad.deps), 1);
  assert.equal(JSON.parse(bad.stdout()).status, 'blocked');
  assert.equal(JSON.parse(bad.stdout()).changed, false);
  assert.equal(bad.stderr(), '');
});

test('existing uv-managed Python can be discovered without executing uv or creating caches', (t) => {
  const f = fixture(t);
  const data = path.join(f.directory, 'data');
  const base = process.platform === 'win32' ? path.join(data, 'uv', 'data', 'python')
    : path.join(data, 'uv', 'python');
  const python = path.join(base, 'cpython-3.12.14-platform', process.platform === 'win32' ? 'python.exe' : 'bin/python3');
  fs.mkdirSync(path.dirname(python), { recursive: true });
  fs.writeFileSync(python, 'fixture, never executed');
  const source = { PATH: '.', HOME: f.directory,
    [process.platform === 'win32' ? 'APPDATA' : 'XDG_DATA_HOME']: data };
  const calls = [];
  const result = findRecoveryPython(source, f.project, (command, args) => {
    calls.push(command);
    assert.ok(args.includes('-S'));
    return { status: 0, stdout: 'decision-browser-stdlib\n' };
  });
  assert.equal(result.command, python);
  assert.deepEqual(calls, [python]);
});

test('relative and project PATH entries and project-targeting symlinks are never executed',
  { skip: process.platform === 'win32' }, (t) => {
    const f = fixture(t);
    const projectPython = path.join(f.project, 'python3.12');
    fs.writeFileSync(projectPython, 'project code');
    fs.unlinkSync(f.python);
    fs.symlinkSync(projectPython, f.python);
    assert.throws(() => findRecoveryPython({
      PATH: ['', '.', 'relative-tools', f.project, f.tools].join(path.delimiter),
      HOME: path.join(f.directory, 'absent-home'),
    }, f.project, () => assert.fail('project or relative executable selected')), /existing Python 3.11/);
  });

test('missing Python and unexpected subprocess errors keep JSON diagnostics free of private data', async (t) => {
  const f = fixture(t);
  const cases = [
    { env: { PATH: '/missing', HOME: '/missing' }, run() { assert.fail('missing interpreter'); } },
    { env: { PATH: f.tools, HOME: f.directory }, run(command, args) {
      if (args.includes('-c')) return { status: 0, stdout: 'decision-browser-stdlib' };
      return { status: null, error: Error('https://user:CANARY@private.example?token=CANARY /private/ca.pem'),
        stdout: 'CANARY', stderr: 'CANARY' };
    } },
  ];
  for (const item of cases) {
    const c = capture({ root: f.root, cwd: f.project, ...item });
    assert.equal(await main(['install-status', '--json'], c.deps), 1);
    const report = JSON.parse(c.stdout());
    assert.equal(report.status, 'blocked');
    assert.equal(report.changed, false);
    assert.equal(c.stderr(), '');
    assert.ok(!c.stdout().includes('CANARY'));
    assert.ok(!c.stdout().includes('private.example'));
    assert.ok(!c.stdout().includes('/private/ca.pem'));
  }
});

test('interrupted explicit recovery reports an unknown mutation outcome', async (t) => {
  const f = fixture(t);
  const c = capture({ root: f.root, cwd: f.project, env: { PATH: f.tools, HOME: f.directory },
    run(command, args) {
      if (args.includes('-c')) return { status: 0, stdout: 'decision-browser-stdlib' };
      return { status: null, signal: 'SIGTERM', stdout: '' };
    } });
  assert.equal(await main(['recover-install', '--apply', '--json'], c.deps), 1);
  const report = JSON.parse(c.stdout());
  assert.equal(report.changed, null);
  assert.equal(report.reason, 'recovery_inspection_failed');
  assert.ok(report.next_action.includes('install-status'));
});
