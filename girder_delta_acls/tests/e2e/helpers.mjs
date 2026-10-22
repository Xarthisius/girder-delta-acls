// Shared plumbing for the browser checks.

import { createRequire } from 'module';

const require_ = createRequire(import.meta.url);

/**
 * Playwright is pinned in this directory's package.json (`npm ci` here), not in the
 * plugin's own packages: it is only needed for these browser checks, which are not part
 * of `tox -e pytest`. $PLAYWRIGHT overrides; ~/node_modules is the last resort.
 */
function loadChromium() {
  const candidates = [
    process.env.PLAYWRIGHT,
    'playwright',
    `${process.env.HOME}/node_modules/playwright`
  ].filter(Boolean);
  for (const c of candidates) {
    try { return require_(c).chromium; } catch { /* try the next */ }
  }
  throw new Error('playwright not found; set PLAYWRIGHT=/path/to/node_modules/playwright');
}

const BASE = process.env.GIRDER_URL || 'http://localhost:8751';
const API = `${BASE}/api/v1`;
const SHOT = process.env.SHOT_DIR || new URL('./shots/', import.meta.url).pathname;
// CHROME unset: the system Chrome, so no ms-playwright download is needed locally.
// CHROME='' (empty): playwright's own Chromium, as installed by
// `npx playwright install chromium` -- what CI does.
const CHROME = process.env.CHROME ?? '/usr/bin/google-chrome';

const chromeArgs = CHROME
  ? { executablePath: CHROME, args: ['--no-sandbox'] }
  : { args: ['--no-sandbox'] };

// --------------------------------------------------------------------- assertions

function makeChecker() {
  const state = { pass: 0, fail: 0, failures: [] };
  const check = (name, ok, detail = '') => {
    if (ok) { state.pass++; console.log(`  PASS  ${name}`); } else {
      state.fail++;
      state.failures.push(name + (detail ? ` -- ${detail}` : ''));
      console.log(`  FAIL  ${name} ${detail}`);
    }
  };
  const report = () => {
    console.log(`\n================ ${state.pass} passed, ${state.fail} failed ================`);
    state.failures.forEach((f) => console.log('  - ' + f));
    return state.fail;
  };
  return { check, report, state };
}

// --------------------------------------------------------------------- REST fixtures

async function api(path, { method = 'GET', token, form } = {}) {
  const headers = {};
  if (token) headers['Girder-Token'] = token;
  let body;
  if (form) {
    headers['Content-Type'] = 'application/x-www-form-urlencoded';
    body = new URLSearchParams(form).toString();
  }
  const r = await fetch(`${API}${path}`, { method, headers, body });
  const text = await r.text();
  let json; try { json = JSON.parse(text); } catch { json = text; }
  return { status: r.status, json };
}

// --------------------------------------------------------------------- error noise

/**
 * Reproduced on a stock Girder page, so not this plugin's:
 *  - the google_analytics bundle throws on every page, if that plugin is installed
 *  - core's EventStream opens a websocket with token=null and gets a 403 handshake
 */
const KNOWN_NOISE = [
  /reading 'History'/,
  /notifications\/me\?token=null/,
  /WebSocket connection to/,
  /401 \(Unauthorized\)|You must be logged in/
];

function watch(page, sink) {
  const add = (s) => { if (!KNOWN_NOISE.some((re) => re.test(s))) sink.push(s); };
  page.on('console', (m) => { if (m.type() === 'error') add(`console.error: ${m.text()}`); });
  page.on('pageerror', (e) => add(`pageerror: ${e.message} @ ${String(e.stack).split('\n')[1] || ''}`));
  // Anything thrown from our own bundle is never noise.
  page.on('pageerror', (e) => {
    if (String(e.stack).includes('delta-acls')) sink.push(`PLUGIN pageerror: ${e.message}`);
  });
}

// --------------------------------------------------------------------- pages

/**
 * CSS that removes every transition and animation.
 *
 * Bootstrap 3 fades modals in by animating opacity on `.modal` and a transform on
 * `.modal-dialog`. Screenshots taken during that 300ms land on a half-transparent,
 * still-sliding dialog, and `elementFromPoint` hit-testing can resolve to the backdrop.
 * Killing the transitions makes both deterministic instead of relying on sleeps.
 *
 * Bootstrap still fires `shown.bs.modal`: with no transitionend to listen for it falls
 * back to its own `emulateTransitionEnd` timer.
 */
const NO_MOTION_CSS = `
  *, *::before, *::after {
    transition: none !important;
    animation: none !important;
    transition-duration: 0s !important;
    animation-duration: 0s !important;
  }
  .modal.fade, .modal.fade .modal-dialog, .modal-backdrop.fade { transition: none !important; }
  .modal.fade { opacity: 1; }
  .modal.fade .modal-dialog { transform: none !important; }
`;

/**
 * A page with animations disabled from the first paint. `addInitScript` runs before any
 * page script on every navigation, so this survives reloads and hash routing, which an
 * `addStyleTag` after load would not.
 */
async function newPage(browser, opts = {}) {
  const ctx = await browser.newContext({
    viewport: { width: 1400, height: 950 },
    reducedMotion: 'reduce',
    ...opts
  });
  await ctx.addInitScript((css) => {
    // Init scripts also run on about:blank and before the parser has built <head>, where
    // both document.head and document.documentElement can still be null.
    const inject = () => {
      const root = document.head || document.documentElement;
      if (!root) return false;
      if (!document.getElementById('e2e-no-motion')) {
        const el = document.createElement('style');
        el.id = 'e2e-no-motion';
        el.textContent = css;
        root.appendChild(el);
      }
      return true;
    };
    if (!inject()) {
      document.addEventListener('DOMContentLoaded', inject, { once: true });
    }
  }, NO_MOTION_CSS);
  const page = await ctx.newPage();
  return { ctx, page };
}

/**
 * Click something that opens a Bootstrap modal and return once the modal is genuinely
 * settled: `shown.bs.modal` has fired, the dialog is at full opacity, and its geometry has
 * stopped moving between animation frames.
 *
 * The listener is installed *before* the click, otherwise the event can fire first and the
 * wait never resolves.
 */
async function openModal(page, clickSelector, settledSelector) {
  await page.evaluate(() => {
    /* global girder -- evaluated in the page, where core has set window.girder */
    window.__e2eModalShown = 0;
    // girder.$ is the same jQuery instance Bootstrap's plugins are attached to.
    girder.$(document).on('shown.bs.modal', () => { window.__e2eModalShown += 1; });
  });

  await page.click(clickSelector);
  if (settledSelector) await page.waitForSelector(settledSelector, { timeout: 15000 });
  await page.waitForFunction(() => window.__e2eModalShown > 0, { timeout: 15000 });
  await waitForStable(page, '.modal.in .modal-dialog');
}

/** Resolve once an element's opacity is 1 and its box has stopped changing. */
async function waitForStable(page, selector, { frames = 3 } = {}) {
  await page.waitForFunction(async ({ selector, frames }) => {
    const el = document.querySelector(selector);
    if (!el) return false;
    const raf = () => new Promise((resolve) => requestAnimationFrame(resolve));
    const sample = () => {
      const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      const modal = el.closest('.modal') || el;
      return [
        Math.round(r.top), Math.round(r.left), Math.round(r.height),
        cs.opacity, cs.transform, getComputedStyle(modal).opacity
      ].join('|');
    };
    let prev = sample();
    for (let i = 0; i < frames; i++) {
      await raf();
      const now = sample();
      if (now !== prev) return false;
      prev = now;
    }
    // Fully opaque, and no residual transform on the dialog.
    const cs = getComputedStyle(el);
    const modalOpacity = getComputedStyle(el.closest('.modal') || el).opacity;
    return parseFloat(cs.opacity) === 1 && parseFloat(modalOpacity) === 1 &&
      (cs.transform === 'none' || cs.transform === 'matrix(1, 0, 0, 1, 0, 0)');
  }, { selector, frames }, { timeout: 15000 });
}

/** Sign a page in as an existing user by seeding the token core reads at boot. */
async function signIn(page, token) {
  await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded' });
  await page.evaluate((t) => window.localStorage.setItem('girderToken', t), token);
}

/** Basic-auth login, returning a token. */
async function login(loginName, password = 'password123') {
  const auth = Buffer.from(`${loginName}:${password}`).toString('base64');
  const r = await fetch(`${API}/user/authentication`, {
    headers: { Authorization: `Basic ${auth}` }
  });
  return (await r.json()).authToken.token;
}

export {
  loadChromium,
  BASE,
  API,
  SHOT,
  CHROME,
  chromeArgs,
  makeChecker,
  api,
  KNOWN_NOISE,
  watch,
  newPage,
  openModal,
  waitForStable,
  signIn,
  login
};
