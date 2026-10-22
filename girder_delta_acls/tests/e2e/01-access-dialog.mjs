// The access dialog in a real browser: nested recursion choices, the live preview, the
// replace-mode confirm, what actually gets sent, the restricted mode and the config page.
import {
  SHOT, api, chromeArgs, loadChromium, login, makeChecker, newPage, openModal, signIn, watch
} from './helpers.mjs';

const chromium = loadChromium();
const { check, report } = makeChecker();
const PASSWORD = 'password123';

async function createUser(loginName, firstName) {
  const r = await api('/user', {
    method: 'POST',
    form: {
      login: loginName,
      password: PASSWORD,
      firstName,
      lastName: 'Tester',
      email: `${loginName}@example.com`
    }
  });
  if (r.status !== 200) throw new Error(`user ${loginName}: ${JSON.stringify(r.json)}`);
  return { id: r.json._id, token: r.json.authToken.token };
}

/** The ACL as GET .../access returns it, reduced to what PUT .../access accepts. */
async function getAcl(kind, id, token) {
  const r = await api(`/${kind}/${id}/access`, { token });
  const strip = (e) => ({ id: e.id, level: e.level, flags: e.flags || [] });
  return { users: r.json.users.map(strip), groups: r.json.groups.map(strip) };
}

async function levelOf(kind, id, token, userId) {
  const acl = await getAcl(kind, id, token);
  const e = acl.users.find((u) => u.id === userId);
  return e ? e.level : null;
}

async function waitFor(fn, timeout = 10000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (await fn()) return true;
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  return false;
}

async function setup() {
  const admin = await createUser('deltaadmin', 'Site'); // first user: site admin
  const alice = await createUser('alice', 'Alice');
  const bob = await createUser('bobby', 'Bobby');
  const carol = await createUser('carol', 'Carol');

  let r = await api('/collection', {
    method: 'POST',
    token: admin.token,
    form: { name: 'Shared Study', description: '', public: 'false' }
  });
  const C = r.json._id;
  await api(`/collection/${C}/access`, {
    method: 'PUT',
    token: admin.token,
    form: {
      access: JSON.stringify({
        users: [{ id: admin.id, level: 2 }, { id: alice.id, level: 2 }],
        groups: []
      }),
      public: 'false'
    }
  });

  const folder = async (parentType, parentId, name) => (await api('/folder', {
    method: 'POST',
    token: alice.token,
    form: { parentType, parentId, name }
  })).json._id;
  const A = await folder('collection', C, 'A');
  const S = await folder('collection', C, 'S');
  const S1 = await folder('folder', S, 'S1');

  // Bob gets WRITE on S alone: exactly what a replace-mode save would destroy.
  const sAcl = await getAcl('folder', S, alice.token);
  sAcl.users.push({ id: bob.id, level: 1, flags: [] });
  r = await api(`/folder/${S}/access`, {
    method: 'PUT', token: alice.token, form: { access: JSON.stringify(sAcl), public: 'false' }
  });
  if (r.status !== 200) throw new Error('setting Bob on S failed: ' + JSON.stringify(r.json));

  return { admin, alice, bob, carol, C, A, S, S1 };
}

const summaryText = (page) => page.$eval('.g-delta-acls-summary', (el) => el.innerText.trim());

async function waitForSummary(page, needle) {
  try {
    await page.waitForFunction(
      (n) => {
        const el = document.querySelector('.g-delta-acls-summary');
        return el && el.innerText.includes(n);
      }, needle, { timeout: 10000 });
    return true;
  } catch {
    return false;
  }
}

async function openCollectionAccess(page, base, C) {
  await page.goto(`${base}/#collections`);
  await page.waitForSelector('.g-collection-list-entry, .g-collections-search-container', {
    timeout: 15000
  }).catch(() => {});
  await page.goto(`${base}/#collection/${C}`);
  await page.waitForSelector('.g-collection-actions-button', { timeout: 15000 });
  await page.click('.g-collection-actions-button');
  await openModal(page, '.g-collection-access-control', '#g-apply-recursive');
  await page.waitForSelector('.g-delta-acls-mode', { state: 'attached' });
}

const dialogOpen = (page) => page.evaluate(() => !!document.querySelector('#g-dialog-container.in'));

async function waitDialogClosed(page) {
  try {
    await page.waitForFunction(() => !document.querySelector('#g-dialog-container.in'),
      null, { timeout: 15000 });
    return true;
  } catch {
    return false;
  }
}

async function main() {
  const fx = await setup();
  const base = process.env.GIRDER_URL || 'http://localhost:8751';
  const browser = await chromium.launch(chromeArgs);
  const errors = [];

  try {
    // ------------------------------------------------------------ alice, collection dialog
    const { page } = await newPage(browser);
    watch(page, errors);
    const sent = [];
    page.on('request', (req) => {
      if (req.method() === 'PUT' && /\/access(\?|$)/.test(req.url())) {
        sent.push(new URLSearchParams(req.postData() || ''));
      }
    });

    await signIn(page, fx.alice.token);
    await openCollectionAccess(page, base, fx.C);

    check('nested choices injected but hidden while non-recursive',
      await page.$eval('.g-delta-acls-mode', (el) => el.classList.contains('hide')));
    const recursiveLabel = await page.$eval('#g-apply-recursive', (el) => el.parentElement.innerText.trim());
    check('core recursive radio relabelled', recursiveLabel === 'Also apply to subfolders', recursiveLabel);

    await page.click('#g-apply-recursive');
    check('nested choices shown when recursive',
      await page.$eval('.g-delta-acls-mode', (el) => !el.classList.contains('hide')));
    check('delta mode is the default', await page.isChecked('#g-delta-acls-mode-delta'));
    check('empty delta says so', await waitForSummary(page, 'No permission changes to apply'),
      await summaryText(page));
    await page.screenshot({ path: `${SHOT}/01-recursive-empty.png` });

    // Add Carol through the core search widget.
    await page.fill('.modal .g-search-field', 'carol');
    const result = page.locator('.g-search-result>a', { hasText: 'carol' }).first();
    await result.waitFor({ timeout: 10000 });
    await result.click();
    await page.waitForSelector(`.g-user-access-entry[resourceid="${fx.carol.id}"]`, { timeout: 10000 });

    check('preview lists the added user', await waitForSummary(page, '+ Carol Tester (Read)'),
      await summaryText(page));
    const text = await summaryText(page);
    check('preview counts administered subfolders', text.includes('Will apply to 3 subfolders'), text);
    await page.screenshot({ path: `${SHOT}/02-delta-preview.png` });

    // Replace mode: warning, then the inline confirm on save.
    await page.click('#g-delta-acls-mode-replace');
    check('replace mode warns about divergent subfolders',
      await waitForSummary(page, '1 subfolder has its own permissions'), await summaryText(page));
    check('replace warning is an alert-warning',
      await page.$eval('.g-delta-acls-summary .alert-warning', () => true).catch(() => false));
    await page.screenshot({ path: `${SHOT}/03-replace-warning.png` });

    const entriesBefore = await page.$$eval('.g-user-access-entry', (els) => els.length);
    await page.click('button.g-save-access-list');
    await page.waitForSelector('.g-delta-acls-confirm', { timeout: 10000 });
    check('replace save asks for confirmation inside the dialog', await dialogOpen(page));
    check('confirm keeps the dialog contents',
      (await page.$$eval('.g-user-access-entry', (els) => els.length)) === entriesBefore);
    check('save button re-enabled while confirming',
      await page.$eval('button.g-save-access-list', (el) => !el.disabled && !el.classList.contains('disabled')));
    check('nothing sent before confirming', sent.length === 0, `${sent.length} sent`);
    await page.screenshot({ path: `${SHOT}/04-replace-confirm.png` });

    await page.click('.g-delta-acls-cancel-replace');
    check('cancel dismisses the confirm',
      (await page.$('.g-delta-acls-confirm')) === null && await dialogOpen(page));

    // Back to delta and save.
    await page.click('#g-delta-acls-mode-delta');
    await waitForSummary(page, '+ Carol Tester (Read)');
    await page.click('button.g-save-access-list');
    check('delta save closes the dialog', await waitDialogClosed(page));
    check('delta request carries recurse and recurseMode',
      sent.length === 1 && sent[0].get('recurse') === 'true' && sent[0].get('recurseMode') === 'delta',
      sent.map((p) => p.toString()).join(' | '));
    check('Carol reached S1',
      await waitFor(async () => (await levelOf('folder', fx.S1, fx.alice.token, fx.carol.id)) === 0));
    check('Bob kept WRITE on S', (await levelOf('folder', fx.S, fx.alice.token, fx.bob.id)) === 1);
    check('success alert shown', await page.waitForSelector('.alert-success', { timeout: 5000 })
      .then(() => true, () => false));

    // Replace, confirmed this time.
    await openCollectionAccess(page, base, fx.C);
    await page.click('#g-apply-recursive');
    await page.click('#g-delta-acls-mode-replace');
    await waitForSummary(page, 'own permissions');
    await page.click('button.g-save-access-list');
    await page.waitForSelector('.g-delta-acls-confirm-replace', { timeout: 10000 });
    await page.click('.g-delta-acls-confirm-replace');
    check('confirmed replace closes the dialog', await waitDialogClosed(page));
    const last = sent[sent.length - 1];
    check('replace request carries recurseMode=replace',
      sent.length === 2 && last.get('recurseMode') === 'replace', last && last.toString());
    check('replace removed Bob from S',
      await waitFor(async () => (await levelOf('folder', fx.S, fx.alice.token, fx.bob.id)) === null));

    // Non-recursive saves send no mode.
    await openCollectionAccess(page, base, fx.C);
    await page.click('#g-apply-recursive');
    await page.click('#g-apply-nonrecursive');
    check('nested choices hidden again', await page.$eval('.g-delta-acls-mode',
      (el) => el.classList.contains('hide')));
    await page.click('button.g-save-access-list');
    await waitDialogClosed(page);
    const plain = sent[sent.length - 1];
    check('non-recursive save sends no recurseMode',
      sent.length === 3 && plain.get('recurse') === 'false' && !plain.has('recurseMode'),
      plain && plain.toString());

    // Folder dialog from the hierarchy widget.
    await page.goto(`${base}/#collection/${fx.C}/folder/${fx.S}`);
    await page.waitForSelector('.g-folder-access-button', { timeout: 15000 });
    await openModal(page, '.g-folder-access-button', '#g-apply-recursive');
    await page.waitForSelector('.g-delta-acls-mode', { state: 'attached' });
    await page.click('#g-apply-recursive');
    check('folder dialog gets the same choices', await waitForSummary(page, 'No permission changes'),
      await summaryText(page));
    await page.click('.modal-footer a[data-dismiss="modal"]');
    await waitDialogClosed(page);

    // ------------------------------------------------------------ restricted replace
    const r = await api('/system/setting', {
      method: 'PUT',
      token: fx.admin.token,
      form: { key: 'delta_acls.replace_requires_site_admin', value: 'true' }
    });
    check('admin can set replace_requires_site_admin', r.status === 200, JSON.stringify(r.json));

    await openCollectionAccess(page, base, fx.C);
    await page.click('#g-apply-recursive');
    await waitForSummary(page, 'No permission changes');
    check('replace disabled for non-admins when restricted',
      await page.$eval('#g-delta-acls-mode-replace', (el) => el.disabled));
    check('restriction explained',
      await page.$eval('.g-delta-acls-restricted', (el) => !el.classList.contains('hide')));
    await page.screenshot({ path: `${SHOT}/05-replace-restricted.png` });

    // ------------------------------------------------------------ admin config page
    const { page: adminPage } = await newPage(browser);
    watch(adminPage, errors);
    await signIn(adminPage, await login('deltaadmin', PASSWORD));
    await adminPage.goto(`${base}/#plugins/delta_acls/config`);
    await adminPage.waitForSelector('#g-delta-acls-default-mode', { timeout: 15000 });
    await adminPage.waitForFunction(
      () => document.querySelector('#g-delta-acls-replace-admin-only').checked, null,
      { timeout: 10000 });
    check('config page shows current values',
      (await adminPage.$eval('#g-delta-acls-default-mode', (el) => el.value)) === 'replace');
    await adminPage.screenshot({ path: `${SHOT}/06-config.png` });
    await adminPage.selectOption('#g-delta-acls-default-mode', 'delta');
    await adminPage.uncheck('#g-delta-acls-replace-admin-only');
    await adminPage.click('#g-delta-acls-config-form input[type="submit"]');
    check('config saved', await waitFor(async () => {
      const s = await api('/system/setting?list=' + encodeURIComponent(JSON.stringify([
        'delta_acls.default_recurse_mode', 'delta_acls.replace_requires_site_admin'
      ])), { token: fx.admin.token });
      return s.json['delta_acls.default_recurse_mode'] === 'delta' &&
        s.json['delta_acls.replace_requires_site_admin'] === false;
    }));

    check('no unexpected JS errors', errors.length === 0, errors.join('\n'));
  } finally {
    await browser.close();
  }
}

main().then(() => process.exit(report() ? 1 : 0), (e) => {
  console.error(e);
  process.exit(1);
});
