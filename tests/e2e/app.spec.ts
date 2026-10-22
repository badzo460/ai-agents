import { test, expect } from '@playwright/test';
import { createDemoSnapshot } from '../../shared/demo';

const origin = 'http://127.0.0.1:8787';
test.describe.configure({ mode: 'serial' });

test.beforeEach(async ({ request }) => {
  await request.post('/api/connection/disconnect', { headers: { Origin: origin }, data: {} });
  await request.post('/api/demo/reset', { headers: { Origin: origin }, data: {} });
});

test('dashboard renders six agents, details and paper order controls', async ({ page, request }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Tvoje zlato. Tvoji agenti.' })).toBeVisible();
  await expect(page.getByRole('img', { name: /Kontrolna soba sa šest/ })).toBeVisible();
  for (const name of ['SCOUT', 'NEWS', 'SENTIMENT', 'CHARTS', 'RISK', 'DECISION']) {
    await expect(page.getByRole('button', { name: `Otvori agenta ${name}` })).toBeAttached();
  }
  await page.getByRole('button', { name: 'Otvori agenta RISK' }).click();
  await expect(page.getByText('Veličina naloga i zaštitne granice', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Zatvori', exact: true }).click();
  await page.getByRole('switch', { name: 'Automatsko trgovanje' }).click();
  await expect(page.getByRole('switch', { name: 'Automatsko trgovanje' })).toHaveAttribute('aria-checked', 'true');
  const tick = await request.post('/api/demo/step', { headers: { Origin: origin }, data: {} });
  expect(tick.ok()).toBeTruthy();
  const state = await tick.json();
  expect(state.mode).toBe('demo');
  expect(state.snapshot.positions).toHaveLength(1);
  expect(state.trades[0].source).toBe('demo');
  await page.reload();
  await expect(page.locator('tbody tr')).toHaveCount(1);
  await page.getByRole('button', { name: /Zaustavi nove naloge/ }).click();
  await expect(page.getByRole('switch', { name: 'Automatsko trgovanje' })).toHaveAttribute('aria-checked', 'false');
  expect(errors).toEqual([]);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: 'test-results/aurum-desktop.png', fullPage: true });
});

test('mobile dashboard has no horizontal page overflow and pairing stays explicit', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Tvoje zlato. Tvoji agenti.' })).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
  expect(overflow).toBe(false);
  await page.getByRole('button', { name: /Poveži MT5/ }).first().click();
  await expect(page.getByRole('dialog', { name: 'Poveži svoj MT5 račun' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Generiraj ključ' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Generiraj ključ' })).toBeDisabled();
  await expect(page.getByText('Prijavi se u PU Prime MT5', { exact: true })).toBeVisible();
  await expect(page.getByRole('textbox', { name: 'Naziv simbola u MT5' })).toHaveValue('XAUUSD.s');
  const brokerOffset = page.getByRole('spinbutton', { name: 'UTC pomak vremena ticka (sati)' });
  await expect(brokerOffset).toHaveValue('0');
  await brokerOffset.fill('3');
  await expect(brokerOffset).toHaveValue('3');
  await page.getByRole('button', { name: 'Zatvori', exact: true }).click();
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: 'test-results/aurum-mobile.png', fullPage: true });
});

test('paired bridge queues only gold and honors pause, revocation and account binding', async ({ request }) => {
  const mutation = { Origin: origin };
  const pairResponse = await request.post('/api/connection/pair', { headers: mutation, data: {} });
  expect(pairResponse.ok()).toBeTruthy();
  const pair = await pairResponse.json();
  expect(pair.token.length).toBeGreaterThanOrEqual(32);
  const auth = { Authorization: `Bearer ${pair.token}` };
  const initial = await request.get('/api/bridge/command', { headers: auth });
  expect((await initial.json()).command).toBeNull();
  const bad = { ...createDemoSnapshot(), symbol: 'EURUSD' };
  expect((await request.post('/api/bridge/snapshot', { headers: auth, data: bad })).status()).toBe(400);
  const snapshot = { ...createDemoSnapshot(), accountId: 'PU-DEMO-TEST' };
  expect((await request.post('/api/bridge/snapshot', { headers: auth, data: snapshot })).ok()).toBeTruthy();
  expect((await request.post('/api/settings', { headers: mutation, data: { enabled: true } })).ok()).toBeTruthy();
  expect((await request.post('/api/bridge/snapshot', { headers: auth, data: { ...snapshot, timestamp: Date.now() } })).ok()).toBeTruthy();
  const queued = await (await request.get('/api/bridge/command', { headers: auth })).json();
  expect(queued.command.symbol).toBe('XAUUSD');
  expect(queued.command.accountId).toBe('PU-DEMO-TEST');
  await request.post('/api/emergency-stop', { headers: mutation, data: {} });
  expect((await (await request.get('/api/bridge/command', { headers: auth })).json()).command).toBeNull();
  expect((await request.post('/api/bridge/snapshot', { headers: auth, data: { ...snapshot, accountId: 'OTHER', timestamp: Date.now() } })).status()).toBe(409);
  await request.post('/api/connection/disconnect', { headers: mutation, data: {} });
  expect((await request.get('/api/bridge/command', { headers: auth })).status()).toBe(401);
});

test('cross-origin mutations and real-account activation without confirmation fail', async ({ request }) => {
  expect((await request.post('/api/settings', { headers: { Origin: 'https://attacker.invalid' }, data: { enabled: true } })).status()).toBe(403);
  expect((await request.post('/api/settings', { headers: { Origin: origin }, data: { allowLive: true } })).status()).toBe(400);
});

test('connected MT5 account remains visible while stale or future quotes block orders', async ({ page, request }) => {
  const mutation = { Origin: origin };
  const pair = await (await request.post('/api/connection/pair', { headers: mutation, data: {} })).json();
  const auth = { Authorization: `Bearer ${pair.token}` };
  const snapshot = { ...createDemoSnapshot(), accountId: 'PU-DEMO-CLOCK', balance: 12345.67, equity: 12345.67, dailyPnl: 0, dailyPnlConservative: true, dailyPnlUpperBound: 0, timestamp: Date.now() - 120_000 };
  expect((await request.post('/api/bridge/snapshot', { headers: auth, data: snapshot })).ok()).toBeTruthy();
  expect((await request.post('/api/settings', { headers: mutation, data: { enabled: true } })).ok()).toBeTruthy();
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'MT5 povezan', exact: true })).toBeVisible();
  await expect(page.getByText('$12,345.67', { exact: true }).first()).toBeVisible();
  await expect(page.getByRole('status')).toContainText('MT5 račun je povezan.');
  await expect(page.getByRole('status')).toContainText('Cijena je starija od 30 sekundi.');
  await expect(page.getByTestId('quote-age')).toContainText('+');
  await expect(page.getByText('KONZERVATIVNI DNEVNI P&L', { exact: true })).toBeVisible();
  await page.getByText('Objašnjenje procjene', { exact: true }).click();
  await expect(page.getByText(/UTC granica dana u povijesti poslova/)).toBeVisible();
  expect((await (await request.get('/api/bridge/command', { headers: auth })).json()).command).toBeNull();

  expect((await request.post('/api/bridge/snapshot', { headers: auth, data: { ...snapshot, timestamp: Date.now() + 60_000 } })).ok()).toBeTruthy();
  await page.reload();
  await expect(page.getByRole('status')).toContainText('Vrijeme cijene je u budućnosti.');
  await expect(page.getByTestId('quote-age')).toContainText('-');
  expect((await (await request.get('/api/bridge/command', { headers: auth })).json()).command).toBeNull();
});
