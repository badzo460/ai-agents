import { defineConfig } from '@playwright/test';
import { existsSync } from 'node:fs';

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: false,
  workers: 1,
  timeout: 30_000,
  use: {
    baseURL: 'http://127.0.0.1:8787',
    browserName: 'chromium',
    launchOptions: { executablePath: process.env.CHROMIUM_PATH || (existsSync('/usr/bin/chromium') ? '/usr/bin/chromium' : undefined), args: ['--no-sandbox'] },
    viewport: { width: 1512, height: 1050 },
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'npm run dev:api',
    url: 'http://127.0.0.1:8787/api/state',
    reuseExistingServer: true,
    timeout: 60_000,
  },
});
