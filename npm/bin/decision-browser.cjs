#!/usr/bin/env node
'use strict';

const { createHash } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

function run(args, root = path.resolve(__dirname, '..')) {
  try {
    const manifestPath = path.join(root, 'native', 'manifest.json');
    const manifestStat = fs.lstatSync(manifestPath);
    if (!manifestStat.isFile() || manifestStat.isSymbolicLink() || manifestStat.size > 4096) {
      throw new Error('invalid bundled manifest');
    }
    const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
    const expectedFile = process.platform === 'win32' ? 'decision-browser.exe' : 'decision-browser';
    if (manifest.format !== 1 || manifest.platform !== process.platform ||
        manifest.arch !== process.arch || manifest.file !== expectedFile ||
        typeof manifest.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(manifest.sha256)) {
      throw new Error('bundle does not match this platform or manifest format');
    }
    const executable = path.join(root, 'native', manifest.file);
    const executableStat = fs.lstatSync(executable);
    if (!executableStat.isFile() || executableStat.isSymbolicLink()) {
      throw new Error('bundled executable is missing or invalid');
    }
    const digest = createHash('sha256').update(fs.readFileSync(executable)).digest('hex');
    if (digest !== manifest.sha256) {
      throw new Error('bundled executable integrity check failed');
    }
    const result = spawnSync(executable, args, { stdio: 'inherit', env: process.env, shell: false });
    if (result.error) throw new Error('could not start bundled executable');
    if (result.signal) {
      process.kill(process.pid, result.signal);
      return 1;
    }
    return Number.isInteger(result.status) ? result.status : 1;
  } catch (error) {
    process.stderr.write(`Decision Browser native launcher: ${error.message}\n`);
    return 1;
  }
}

module.exports = { run };
if (require.main === module) process.exitCode = run(process.argv.slice(2));
