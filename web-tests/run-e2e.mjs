// Local fixture CA is explicitly trusted; production TLS/auth is never disabled.
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import https from 'node:https';
import { tmpdir } from 'node:os';
import { dirname, resolve, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';

const workdir = dirname(fileURLToPath(import.meta.url));
const repo = resolve(workdir, '..');
const scratch = mkdtempSync(join(tmpdir(), 'relaypost-e2e-'));
const certificate = join(scratch, 'cert.pem');
const server = spawn(resolve(repo, '.venv/bin/python'), ['-m', 'tests.e2e_backend'], {
  cwd: repo,
  env: { ...process.env, RELAYPOST_E2E_DIR: scratch },
  stdio: ['ignore', 'inherit', 'inherit'],
});

function healthy() {
  return new Promise((resolveReady) => {
    const request = https.get('https://127.0.0.1:8443/__fixture/health', {
      ca: readFileSync(certificate), timeout: 1000,
    }, (response) => {
      response.resume();
      resolveReady(response.statusCode === 200);
    });
    request.on('error', () => resolveReady(false));
    request.on('timeout', () => request.destroy());
  });
}

try {
  let ready = false;
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (server.exitCode !== null) throw new Error('Fixture backend exited before readiness');
    if (existsSync(certificate) && await healthy()) { ready = true; break; }
    await delay(200);
  }
  if (!ready) throw new Error('Fixture HTTPS readiness deadline exceeded');
  const child = spawn(process.execPath, [resolve(workdir, 'node_modules/cypress/bin/cypress'),
    'run', '--browser', 'electron', ...process.argv.slice(2)], {
    cwd: workdir,
    env: { ...process.env, NODE_EXTRA_CA_CERTS: certificate },
    stdio: 'inherit',
  });
  process.exitCode = await new Promise((resolveCode, reject) => {
    child.once('error', reject);
    child.once('exit', (code) => resolveCode(code ?? 1));
  });
} finally {
  server.kill('SIGTERM');
  for (let attempt = 0; attempt < 30 && server.exitCode === null; attempt += 1) await delay(100);
  if (server.exitCode === null) server.kill('SIGKILL');
  rmSync(scratch, { recursive: true, force: true });
}
