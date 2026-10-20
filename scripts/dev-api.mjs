import { spawn } from 'node:child_process';
import { homedir, platform, tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const projectRoot = fileURLToPath(new URL('../', import.meta.url));
const wrangler = fileURLToPath(new URL('../node_modules/wrangler/bin/wrangler.js', import.meta.url));
const configHome = platform() === 'darwin'
  ? join(homedir(), 'Library', 'Application Support', 'Aurum')
  : join(tmpdir(), 'aurum-config');
const child = spawn(process.execPath, [wrangler, 'dev', '--ip', '127.0.0.1', '--port', '8787', ...process.argv.slice(2)], {
  cwd: projectRoot,
  stdio: 'inherit',
  env: {
    ...process.env,
    XDG_CONFIG_HOME: process.env.XDG_CONFIG_HOME || configHome,
    WRANGLER_SEND_METRICS: process.env.WRANGLER_SEND_METRICS ?? 'false',
  },
});

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => {
    if (child.exitCode === null && !child.killed) child.kill(signal);
  });
}
child.on('error', error => {
  console.error(`Pokretanje API-ja nije uspjelo: ${error.message}`);
  process.exitCode = 1;
});
child.on('exit', (code, signal) => {
  process.exitCode = code ?? (signal === 'SIGINT' ? 130 : signal === 'SIGTERM' ? 143 : 1);
});
