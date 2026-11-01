const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../public/app.js'), 'utf8');

function app(options = {}) {
  const nodes = new Map();
  const node = selector => {
    if (!nodes.has(selector)) nodes.set(selector, {value: '', innerHTML: '', hidden: true,
      textContent: '', classList: {add() {}, toggle() {}}, querySelectorAll() { return []; }});
    return nodes.get(selector);
  };
  const responses = [];
  const document = {querySelector: node, addEventListener() {}, body: {classList: {add() {}}, insertAdjacentHTML() {}}};
  const context = {document, URLSearchParams, location: {hash: '', search: '', href: 'http://localhost/app'},
    navigator: {userAgent: 'test'}, localStorage: {getItem() { return '1'; }, setItem() {}},
    fetch: async (url, opts) => { responses.push({url, opts}); return {ok: true, status: 200,
      json: async () => ({user: {id: '101'}, items: [], bot_username: 'test_bot'})}; },
    setTimeout, clearTimeout, console, ...options};
  context.window = {setTimeout, Telegram: options.Telegram || null, confirm: () => true,
    open() {}, alert() {}, ...(options.window || {})};
  vm.createContext(context);
  vm.runInContext(source.replace(/\nboot\(\);\s*$/, '') + '\nthis.test = {S, parseAmount, captureForm, renderForm, renderCollection, markPaid, submitForm, boot, tg, buildTelegramShim};', context);
  return {...context.test, context, nodes, node, responses};
}

const collection = () => ({id: 'c_test', title: 'Pizza', currency: '₽', mode: 'per_person',
  per_person: 400, status: 'active', me_state: 'waiting', roster: [{id: '101', me: true, paid: false}],
  paid_count: 0, unpaid_count: 1, total: 0, is_organizer: true});

async function run() {
  let checks = 0;
  const check = (name, fn) => { fn(); checks++; console.log('ok - ' + name); };
  const a = app();
  check('strict amount input and thousands separators', () => {
    for (const raw of ['-5', '0', '12.50', '12,50', '500abc', '10000001', '', '1e3']) assert.equal(a.parseAmount(raw), null);
    assert.equal(a.parseAmount('1 500'), 1500);
    assert.equal(a.parseAmount('10000000'), 10000000);
  });
  check('form draft survives mode switch, escapes numeric fields', () => {
    a.S.form = {mode: 'create', chip: 'per', data: {}};
    for (const [id, value] of Object.entries({fTitle: 'Gift', fPer: '400', fHint: 'SBP', fDate: '2030-12-28', fTime: '20:00'})) a.node('#' + id).value = value;
    a.captureForm(); a.S.form.chip = 'sum'; a.renderForm();
    assert.equal(a.S.form.data.title, 'Gift');
    assert.equal(a.S.form.data.per_person, '400');
    assert.equal(a.S.form.data.deadline, '2030-12-28 20:00');
    a.S.form.chip = 'per'; a.S.form.data.per_person = '" onfocus="alert(1)'; a.renderForm();
    assert.ok(!a.node('#view').innerHTML.includes('value="" onfocus='));
  });
  check('payment button is present without Telegram bridge', () => {
    a.renderCollection(collection());
    assert.ok(a.node('#view').innerHTML.includes('id="payInline"'));
  });
  check('closed collections do not offer mutations', () => {
    for (const state of ['paid', 'declined', 'waiting']) {
      a.renderCollection({...collection(), me_state: state, status: 'closed'});
      for (const id of ['unpayMe', 'joinMe', 'declineMe', 'payInline']) assert.ok(!a.node('#view').innerHTML.includes('id="' + id + '"'));
    }
  });
  const auth = app({Telegram: {WebApp: {initData: 'auth', initDataUnsafe: {}, ready() {}, expand() {}, setHeaderColor() {}}},
    fetch: async () => ({status: 401, ok: false})});
  await auth.boot();
  check('auth failure never substitutes a fake paid collection', () => {
    assert.equal(auth.S.demo, false);
    assert.equal(auth.S.coll, null);
    assert.ok(auth.node('#banner').textContent.includes('Сервер не принял'));
  });
  const fresh = app({location: {hash: '', search: '?startapp=new', href: 'http://localhost/app?startapp=new'}});
  await fresh.boot();
  check('new deep link opens the creation form', () => assert.equal(fresh.S.tab, 'form'));
  check('blocked localStorage does not crash startup', () => {
    const storage = app({localStorage: {getItem() { throw Error('blocked'); }}});
    assert.equal(storage.S.introDone, false);
  });
  let finish;
  let payments = 0;
  const concurrent = app({fetch: async url => {
    if (url.endsWith('/paid')) {
      payments++;
      await new Promise(resolve => { finish = resolve; });
      return {ok: true, status: 200, json: async () => ({view: {...collection(), me_state: 'paid'}})};
    }
    return {ok: true, status: 200, json: async () => ({items: []})};
  }});
  concurrent.S.coll = collection();
  const one = concurrent.markPaid();
  const two = concurrent.markPaid();
  finish(); await Promise.all([one, two]);
  check('double tap submits only one payment', () => assert.equal(payments, 1));
  let submitted;
  const date = app({fetch: async (url, opts) => {
    if (url === '/api/create') submitted = JSON.parse(opts.body);
    return {ok: true, status: 200, json: async () => ({view: collection(), items: []})};
  }});
  date.S.form = {mode: 'create', chip: 'per', data: {}};
  date.node('#fTitle').value = 'Gift'; date.node('#fPer').value = '1 500';
  date.node('#fDate').value = '2030-12-28';
  await date.submitForm();
  check('date-only deadline is preserved and uses end of day', () => {
    assert.equal(submitted.per_person, 1500);
    assert.ok(submitted.deadline.startsWith('2030-12-28 23:59 '));
  });
  console.log(`${checks} frontend scenarios passed`);
}
run().catch(error => { console.error(error); process.exitCode = 1; });
