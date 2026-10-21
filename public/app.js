/* «Скинулись» — мини-аппа. Ванила JS, один файл. */
'use strict';

const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
const $ = (p) => document.querySelector(p);
const VIEW = $('#view');

const S = {
  user: null,
  bot: 'skinulis_bot',
  demo: false,
  tab: 'home',       // home | collection | form
  coll: null,        // текущий сбор (view с сервера или демо)
  home: [],          // «мои сборы»
  formPool: undefined,// кого можно позвать при создании сбора
  form: null,        // {mode:'create'|'edit', chip, data}
  introDone: localStorage.getItem('skinulis_intro_v2') === '1',
};

/* ─────────── утилиты */

const money = (n, cur = '₽') => n == null ? '' : n.toLocaleString('ru-RU') + ' ' + cur;

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(t._h);
  t._h = setTimeout(() => t.hidden = true, 2200);
}

function haptic(kind = 'light') {
  try { tg.HapticFeedback.impactOccurred(kind); } catch (e) {}
}

function beepOk() {
  try { tg.HapticFeedback.notificationOccurred('success'); } catch (e) {}
}

function askConfirm(text) {
  if (tg && tg.showConfirm) {
    return new Promise(res => {
      try { tg.showConfirm(text, ok => res(!!ok)); } catch (e) { res(window.confirm(text)); }
    });
  }
  return Promise.resolve(window.confirm(text));
}

async function api(path, opts = {}) {
  const r = await fetch(path, Object.assign({
    method: 'GET',
    headers: tg && tg.initData ? { 'Authorization': 'tma ' + tg.initData } : {},
  }, opts));
  if (r.status === 401) throw new Error('auth');
  if (!r.ok) {
    let err = 'ошибка ' + r.status;
    try { err = (await r.json()).error || err; } catch (e) {}
    throw new Error(err);
  }
  return r.json();
}

/* ─────────── демо-данные (если API недоступен: превью в браузере) */

const DEMO = () => ({
  demoUser: { id: 900000, name: 'Ты (демо)', username: 'tester' },
  collections: [
    {
      id: 'c_demo01', title: 'Пицца на пятницу', mode: 'per_person',
      per_person: 400, goal_total: null, currency: '₽',
      deadline: null, deadline_left: 'до 30.10 20:00',
      anonymous: false, pay_hint: 'СБП +7 900 ***-**-45 (Иван)',
      status: 'active', is_organizer: true, me_state: 'waiting',
      organizer: { name: 'Ты', username: 'tester' },
      paid_count: 1, unpaid_count: 1, total: 400,
      my_paid: false, my_amount: null, link: '#',
      roster: [
        { id: '900000', paid: false, me: true, name: 'Ты' },
        { id: '900001', paid: false, name: 'Паша', username: 'pavel' },
        { id: '900002', paid: true, name: 'Лена', username: 'lena', amount: 400 },
        { id: '900003', paid: false, name: 'Митя', username: 'mitya', declined: true },
      ],
    },
    {
      id: 'c_demo02', title: 'Подарок Лене 💅', mode: 'per_person',
      per_person: 300, goal_total: null, currency: '₽',
      deadline: null, deadline_left: 'до 28.12 23:59',
      anonymous: false, pay_hint: 'Перевод на карту мамы 🎁',
      status: 'active', is_organizer: false, me_state: 'none',
      organizer: { name: 'Матильда', username: 'mda' },
      paid_count: 2, unpaid_count: 1, total: 600,
      my_paid: false, my_amount: null, link: '#',
      roster: [
        { id: 'x1', paid: true, name: 'Юля', username: 'ulya', amount: 300 },
        { id: 'x2', paid: true, name: 'Костя', username: 'kostya', amount: 300 },
        { id: '900001', paid: false, name: 'Паша', username: 'pavel' },
      ],
    },
  ],
});

/* ─────────── boot */

async function boot() {
  if (tg) {
    tg.expand();
    tg.setHeaderColor('secondary_bg_color');
  }
  const startParam = (tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param)
    || new URLSearchParams(location.search).get('startapp')
    || 'home';

  try {
    const r = await api('/api/init', {
      method: 'POST',
      body: JSON.stringify({ start_param: startParam }),
      headers: Object.assign(
        tg && tg.initData ? { 'Authorization': 'tma ' + tg.initData } : {},
        { 'Content-Type': 'application/json' }
      ),
    });
    S.user = r.user; S.bot = r.bot_username;
    if (r.collection) { S.tab = 'collection'; S.coll = r.collection; }
  } catch (e) {
    const d = DEMO();
    S.user = d.demoUser; S.demo = true;
    $('#banner').hidden = false;
    if (startParam.startsWith('c_')) {
      S.tab = 'collection';
      S.coll = d.collections.find(c => c.id === startParam) || d.collections[0];
    }
  }

  await loadHome();
  render();
  if (!S.introDone) showIntro();
}

async function loadHome() {
  if (S.demo) { S.home = DEMO().collections.map(toTile); return; }
  try {
    const r = await api('/api/my');
    S.home = (r.items || []).map(toTile);
  } catch (e) { S.home = []; }
}

function toTile(c) {
  return {
    id: c.id, title: c.title, deadline_left: c.deadline_left,
    my_paid: c.my_paid, currency: c.currency,
    paid_count: c.paid_count, unpaid_count: c.unpaid_count,
    is_organizer: !!c.is_organizer,
  };
}

/* ─────────── навигация */

function navHome() {
  haptic();
  S.tab = 'home';
  render();
}

function navColl(id) {
  const demo = S.demo && DEMO().collections.find(c => c.id === id);
  if (demo) { S.coll = demo; S.tab = 'collection'; render(); return; }
  openReal(id);
}

async function openReal(id) {
  VIEW.innerHTML = '<div class="hero"><div class="e">⏳</div></div>';
  S.formPool = undefined;
  try {
    S.coll = await api('/api/c/' + id);
    S.tab = 'collection';
    render();
  } catch (e) {
    VIEW.innerHTML = '<div class="hero"><div class="e">🕳</div><p>' + esc(e.message) + '</p></div>';
  }
}

/* ─────────── рендер */

function chipOf(c) {
  if (c.status === 'closed') return '<span class="chip ok">закрыт</span>';
  const left = c.deadline_left || '';
  if (left.includes('прошёл')) return '<span class="chip warn">⏰ время вышло</span>';
  return '<span class="chip">активен</span>';
}

function render() {
  if (S.tab === 'collection' && S.coll) renderCollection(S.coll);
  else if (S.tab === 'form') renderForm();
  else renderHome();
  renderButtons();
}

/* — home — */
function renderHome() {
  const tiles = S.home.length ? S.home.map(t => {
    return '<div class="tile" data-nav="' + esc(t.id) + '">' +
      (t.is_organizer ? '<button class="delsmall" data-del="' + esc(t.id) + '" title="Удалить">✕</button>' : '') +
      '<div class="t">' + (t.my_paid ? '✅ ' : '💸 ') + esc(t.title) + '</div>' +
      '<div class="s">' +
        (t.deadline_left ? '<span>' + esc(t.deadline_left) + '</span>' : '') +
        '<span>👍 ' + t.paid_count + '</span>' +
        (t.unpaid_count ? '<span class="unpaid">💀 ждём: ' + t.unpaid_count + '</span>' : '') +
      '</div>' +
    '</div>';
  }).join('')
  : '<div class="card hero"><div class="e">💸</div>' +
    '<p><b>Сборов пока нет.</b><br>Жми «＋ Создать сбор» — название, сумма, дедлайн.' +
    '<br>Дальше просто: поделись ссылкой, и кто в деле — сам отметится ✅</p></div>';

  VIEW.innerHTML =
    '<div class="top"><h1>🗂 Мои сборы</h1></div>' +
    '<button class="big-btn" id="createBtn">＋ Создать сбор</button>' + tiles +
    '<div class="foot">Скинулись 💸 · честный учёт общих трат</div>';
}

/* — форма (создать / изменить) — */
function openCreate() {
  haptic();
  S.form = { mode: 'create', chip: 'per', data: {} };
  S.formPool = 'loading';
  S.tab = 'form';
  render();
  api('/api/users').then(r => {
    S.formPool = (r.items || []).length ? r.items : 'empty';
    if (S.tab === 'form') render();
  }).catch(() => { S.formPool = 'empty'; if (S.tab === 'form') render(); });
}

function openEdit() {
  const c = S.coll;
  S.form = {
    mode: 'edit',
    chip: c.mode === 'total' ? 'sum' : (c.mode === 'per_person' ? 'per' : 'free'),
    data: { title: c.title, per_person: c.per_person, goal_total: c.goal_total,
            deadline: c.deadline || '', pay_hint: c.pay_hint || '' },
  };
  S.tab = 'form';
  render();
}

function renderForm() {
  const f = S.form;
  const d = f.data;
  const chip = f.chip;

  const chips =
    [['per', '💵 По N с каждого'], ['sum', '💰 Общая сумма'], ['free', '🎲 Кто сколько хочет']]
      .map(k => '<button class="chip' + (chip === k[0] ? ' on' : '') + '" data-chip="' + k[0] + '">' + k[1] + '</button>').join('');

  let sumHtml;
  if (chip === 'per') {
    sumHtml = '<label class="fl">С каждого по</label>' +
      '<input id="fPer" inputmode="numeric" placeholder="400" value="' + (d.per_person || '') + '">';
  } else if (chip === 'sum') {
    sumHtml = '<label class="fl">Собираем всего</label>' +
      '<input id="fGoal" inputmode="numeric" placeholder="5000" value="' + (d.goal_total || '') + '">';
  } else {
    sumHtml = '<div class="sub" style="margin:2px 0 0">Каждый введёт свою сумму сам 🎲</div>';
  }

  const [dy, dt] = d.deadline && d.deadline.includes(' ') ? d.deadline.split(' ') : ['', ''];
  VIEW.innerHTML =
    '<div class="top"><button class="back on" id="backBtn">‹</button>' +
      '<h1>' + (f.mode === 'create' ? 'Новый сбор' : 'Изменить сбор') + '</h1></div>' +
    '<div class="card">' +
      '<label class="fl">Название</label>' +
      '<input id="fTitle" maxlength="64" placeholder="Пицца на пятницу" value="' + esc(d.title || '') + '">' +
      '<label class="fl">Как считаем</label>' +
      '<div class="chips" id="fModeChips">' + chips + '</div>' +
      sumHtml +
      '<label class="fl">Дедлайн (не обязательно)</label>' +
      '<div class="dl-row">' +
        '<input type="date" id="fDate" value="' + esc(dy || '') + '">' +
        '<input type="time" id="fTime" value="' + esc(dt || '') + '">' +
      '</div>' +
      '<label class="fl">Куда скидывать (реквизиты, увидят участники)</label>' +
      '<input id="fHint" placeholder="СБП, карта, ссылка…" value="' + esc(d.pay_hint || '') + '">' +
      (f.mode === 'create'
        ? '<label class="fl">Пригласить сразу (кто откликнётся — в списке)</label>' + formPool()
        : '') +
      '<button class="mini-btn go" id="fSave">' + (f.mode === 'create' ? 'Создать сбор 💸' : 'Сохранить') + '</button>' +
    '</div>' +
    (f.mode === 'create'
      ? '<div class="foot">Сбор — твой: кидай ссылку друзьям 📤<br>' +
        'Для карточки в чате: /skor Пицца по 400 до пт</div>'
      : '');
}

/* — пул «пригласить сразу» в форме создания — */
function formPool() {
  if (S.formPool === 'loading') return '<div class="sub">смотрю, кто зарегистрирован…</div>';
  if (S.formPool === 'empty') return '<div class="sub">Пока некого вписать — после создания поделишься ссылкой 📤</div>';
  if (Array.isArray(S.formPool) && S.formPool.length) {
    return '<div class="addpool">' +
      S.formPool.map(u =>
        '<label class="urow"><input type="checkbox" data-user="' + esc(u.code) + '">' +
        '<span>' + esc(u.name) + (u.username ? ' <i>@' + esc(u.username) + '</i>' : '') + '</span></label>'
      ).join('') + '</div>';
  }
  return '<div class="sub">Некого вписать — ок, пригласишь потом 📤</div>';
}

/* — collection — */
function meStateOf(c) {
  if (c.me_state) return c.me_state;
  if (c.my_paid) return 'paid';
  return 'none';
}

function renderCollection(c) {
  const unpaid = c.roster.filter(r => !r.paid && !r.declined).length;
  const declinedN = c.roster.filter(r => r.declined).length;

  /* цель */
  let target;
  if (c.mode === 'per_person') {
    target = '<div class="big">' + money(c.per_person, c.currency) + '</div>' +
             '<div class="sub">с каждого· скинулись ' + c.paid_count + ' · ждём ' + unpaid + '</div>';
  } else if (c.mode === 'total') {
    const pct = c.goal_total ? Math.min(100, Math.round(c.total / c.goal_total * 100)) : 0;
    target = '<div class="big">' + money(c.total, c.currency) +
             '<span style="font-size:16px;color:var(--muted)"> из ' + money(c.goal_total, c.currency) + '</span></div>' +
             '<div class="sub">скинулись ' + c.paid_count + ' · ждём ' + unpaid + '</div>' +
             '<div class="bar"><i style="width:' + pct + '%"></i></div>';
  } else {
    target = '<div class="big">' + money(c.total, c.currency) + '</div>' +
             '<div class="sub">кто сколько хочет · скинулись ' + c.paid_count + ' · ждём ' + unpaid + '</div>';
  }

  /* мой блок — зависит от того, участвуют ли */
  const st = meStateOf(c);
  let mine;
  if (st === 'paid') {
    mine = '<div class="card">' +
      '<div class="mystatus"><span class="emoji">✅</span>' +
      '<div><div class="big" style="font-size:22px">' + money(c.my_amount, c.currency) + '</div>' +
      '<div class="sub">Ты скинулся. Красавчик 💪</div></div></div>' +
      '<div class="org-actions single"><button class="mini-btn ghost" id="unpayMe">↩️ Убрать галочку</button></div>' +
      '</div>';
  } else if (st === 'declined') {
    mine = '<div class="card"><div class="mystatus"><span class="emoji">🚫</span>' +
      '<div><div class="big" style="font-size:20px">Ты не участвуешь</div>' +
      '<div class="sub">Список и статусы смотреть можно. Если передумал — кнопка ниже. Никто не осудит 🙂</div></div></div>' +
      '<div class="org-actions single"><button class="mini-btn ghost" id="joinMe">↩️ Вернуться в сбор</button></div></div>';
  } else if (st === 'none' && c.status === 'active') {
    mine = '<div class="card"><h2>Ты участвуешь?</h2>' +
      '<div class="sub">Нажми «В деле» — попадёшь в список. Скинешься потом, кнопка будет внизу экрана</div>' +
      '<div class="org-actions"><button class="mini-btn go" id="joinMe">⭕ В деле</button>' +
      '<button class="mini-btn ghost" id="declineMe">🚫 Не участвую</button></div></div>';
  } else if (c.status === 'active') {
    mine = '<div class="card">' +
      '<div class="mystatus"><span class="emoji">💸</span>' +
      '<div><div class="big" style="font-size:22px">' +
        (c.mode === 'per_person' ? money(c.per_person, c.currency) : 'сколько не жалко') + '</div>' +
      '<div class="sub">' + (c.mode === 'per_person'
        ? 'С тебя столько. Жми «Я скинулся» внизу экрана'
        : 'Введи сумму и жми «Я скинулся» внизу экрана') + '</div></div></div>' +
      (c.mode !== 'per_person' ? '<input class="free-input" id="amount" inputmode="numeric" placeholder="300">' : '') +
      '<div class="org-actions single"><button class="mini-btn ghost" id="declineMe">🚫 Не участвую</button></div>' +
      '</div>';
  } else {
    mine = '<div class="card"><div class="mystatus"><span class="emoji">🏁</span>' +
      '<div><div class="big" style="font-size:20px">Сбор закрыт</div>' +
      '<div class="sub">Опоздал 💀 В следующий раз — вовремя.</div></div></div></div>';
  }

  /* реквизиты */
  const hint = c.pay_hint && !c.my_paid && c.status === 'active'
    ? '<div class="hint-box"><code>' + esc(c.pay_hint) + '</code>' +
      '<button class="mini-btn" id="copyHint">копировать</button></div>'
    : '';

  /* участники: ✅ скинулся · ⭕ в деле · 🚫 отказался */
  const canKick = c.is_organizer && c.status === 'active';
  const rows = c.roster.map(r => {
    const nm = r.me ? 'Ты' : esc(r.name || '???');
    const amt = r.paid && r.amount != null ? '<span class="amt">' + esc(r.amount) + ' ' + esc(c.currency) + '</span>' : '';
    const mark = r.declined ? '🚫' : (r.paid ? '✅' : '⭕');
    const kick = (canKick && !r.me && !r.paid)
      ? '<button class="delsmall kick" data-kick="' + esc(r.id) + '" title="Убрать из сбора" style="margin-top:10px">✕</button>' : '';
    return '<li' + (r.declined ? ' class="declined"' : '') + '>' +
      '<span class="st">' + mark + '</span>' +
      '<span class="nm' + (r.me ? ' me' : '') + '">' + nm + '</span>' + amt + kick + '</li>';
  }).join('');
  const roster = '<div class="card"><h2>Участники</h2>' +
    '<div class="roster-legend">✅ скинулся · ⭕ в деле · 🚫 отказался</div>' +
    '<ul class="who">' + rows + '</ul>' +
    '<div class="org-actions single"><button class="mini-btn ghost" data-share="1">📤 Поделиться ссылкой</button></div>' +
    '</div>';

  /* организаторская панель */
  let org = '';
  if (c.is_organizer && c.status === 'active') {
    org = '<div class="card org-panel"><h2>Организатору</h2>' +
      '<input id="hintInput" placeholder="Куда скидывать: СБП, карта…" value="' + esc(c.pay_hint) + '">' +
      '<div class="org-actions">' +
        '<button class="mini-btn ghost" id="saveHint">Сохранить реквизиты</button>' +
        '<button class="mini-btn danger" id="closeColl">Завершить сбор</button>' +
      '</div>' +
      '<div class="org-actions single"><button class="mini-btn ghost" id="editColl">✏️ Изменить название, сумму, дедлайн</button></div>' +
      '<div class="sub">✕ возле имени — убрать человека из сбора. Приглашать удобно ссылкой 📤</div>' +
      '</div>';
  }
  const delRow = c.is_organizer
    ? '<div class="delrow"><button class="mini-btn danger" id="deleteColl">Удалить сбор</button></div>'
    : '';

  VIEW.innerHTML =
    '<div class="top">' +
      '<button class="back' + (S.home.length ? ' on' : '') + '" id="backBtn">‹</button>' +
      '<h1>' + esc(c.title) + '</h1>' + chipOf(c) +
    '</div>' +
    '<div class="card">' + target + '</div>' +
    (c.deadline_left ? '<div class="card" style="text-align:center">⏳ <b>' + esc(c.deadline_left) + '</b></div>' : '') +
    mine + hint + roster + org + delRow +
    '<div class="foot">Скинулись 💸 · честный учёт, видно кто и сколько скинул</div>';
}

/* ─────────── кнопки Telegram (MainButton / BackButton) */

function renderButtons() {
  if (!tg) return;

  if (S.tab === 'home') {
    tg.BackButton.hide();
    tg.MainButton.hide();
    return;
  }
  if (S.tab === 'form') {
    tg.BackButton.show();
    tg.MainButton.hide();
    return;
  }
  const c = S.coll;
  tg.BackButton.show();
  if (c.status === 'active' && meStateOf(c) === 'waiting') {
    const sum = c.mode === 'per_person' && c.per_person ? ' ' + c.per_person + ' ' + c.currency : '';
    tg.MainButton.setText('Я скинулся ✅' + (sum ? sum : ''));
    tg.MainButton.show();
    tg.MainButton.onClick(markPaid);
  } else {
    tg.MainButton.hide();
  }
}

async function markPaid() {
  let c = S.coll;
  let amount = c.my_amount;
  if (S.demo) {
    amount = c.mode === 'per_person' ? c.per_person : (parseInt(($('#amount') || {}).value, 10) || 300);
    c.my_paid = true; c.my_amount = amount; c.paid_count++; c.total = (c.total || 0) + amount;
    beepOk(); toast('Демо: засчитали ' + money(amount, c.currency) + ' 💰'); render(); return;
  }
  if (c.mode !== 'per_person') {
    const raw = $('#amount') ? $('#amount').value : '';
    amount = parseInt(raw, 10);
    if (!amount || amount <= 0) { toast('Введи сумму'); return; }
  }
  try {
    const r = await api('/api/c/' + c.id + '/paid', {
      method: 'POST',
      body: JSON.stringify({ amount: amount }),
      headers: Object.assign({}, { 'Content-Type': 'application/json' },
        tg && tg.initData ? { 'Authorization': 'tma ' + tg.initData } : {}),
    });
    c = S.coll = r.view;
    beepOk(); toast('Зачет! 💰');
    render();
    loadHome();
  } catch (e) {
    toast(e.message);
  }
}

/* ─────────── действия */

/* — helpers удаления и формы — */

function authHeaders() {
  return Object.assign({ 'Content-Type': 'application/json' },
    tg && tg.initData ? { 'Authorization': 'tma ' + tg.initData } : {});
}

async function deleteById(id) {
  await api('/api/c/' + id + '/delete', { method: 'POST', body: '{}', headers: authHeaders() });
}

function nameOf(id) {
  const t = S.home.find(x => x.id === id);
  return t ? t.title : '';
}

async function submitForm() {
  const f = S.form;
  const title = $('#fTitle').value.trim();
  if (!title) { toast('Название надо придумать 🙂'); haptic('rigid'); return; }
  let per = null, goal = null;
  if (f.chip === 'per') {
    per = parseInt($('#fPer') ? $('#fPer').value : '', 10);
    if (!per || per <= 0) { toast('Сколько с каждого? Например: 400'); haptic('rigid'); return; }
  } else if (f.chip === 'sum') {
    goal = parseInt($('#fGoal') ? $('#fGoal').value : '', 10);
    if (!goal || goal <= 0) { toast('Сколько собираем всего? Например: 5000'); haptic('rigid'); return; }
  }
  const dy = $('#fDate') && $('#fDate').value ? $('#fDate').value : '';
  const dt = $('#fTime') && $('#fTime').value ? $('#fTime').value : '';
  const deadline = dy && dt ? dy + ' ' + dt : '';
  const hint = $('#fHint') ? $('#fHint').value.trim() : '';
  const data = { title: title, per_person: per, goal_total: goal, deadline: deadline, pay_hint: hint };
  data.codes = (f.mode === 'create' && Array.isArray(S.formPool))
    ? [...VIEW.querySelectorAll('.addpool input[type=checkbox]:checked')].map(i => i.dataset.user)
    : [];

  const prettyDl = deadline ? 'до ' + dy.split('-').reverse().join('.') + ' ' + dt : '';

  if (S.demo) {
    const id = 'c_demo' + Date.now().toString(36);
    const view = {
      id: id, title: title,
      mode: f.chip === 'per' ? 'per_person' : (f.chip === 'sum' ? 'total' : 'free'),
      per_person: per, goal_total: goal, currency: '₽',
      deadline: deadline || null, deadline_left: prettyDl, anonymous: false, pay_hint: hint,
      status: 'active', is_organizer: true, organizer: { name: S.user ? S.user.name : 'Ты' },
      paid_count: 0, unpaid_count: 0, total: 0, my_paid: false, my_amount: null,
      roster: [{ id: '900000', paid: false, me: true, name: 'Ты' }], link: '#',
    };
    (Array.isArray(S.formPool) ? S.formPool.filter(u => data.codes.includes(u.code)) : [])
      .forEach(u => view.roster.push({ id: u.code, paid: false, name: u.name }));
    view.unpaid_count = view.roster.length;
    if (f.mode === 'create') {
      S.home.unshift({ id: id, title: title, deadline_left: prettyDl, my_paid: false,
        currency: '₽', paid_count: 0, unpaid_count: view.unpaid_count, is_organizer: true });
      S.coll = view;
      beepOk(); toast('Демо: сбор создан 💸');
    } else {
      Object.assign(S.coll, view, { id: S.coll.id, status: S.coll.status,
        roster: S.coll.roster, paid_count: S.coll.paid_count, unpaid_count: S.coll.unpaid_count,
        total: S.coll.total, my_paid: S.coll.my_paid, my_amount: S.coll.my_amount });
      beepOk(); toast('Демо: сохранено ✅');
    }
    S.form = null; S.tab = 'collection';
    return render();
  }

  try {
    const r = f.mode === 'create'
      ? await api('/api/create', { method: 'POST', body: JSON.stringify(data), headers: authHeaders() })
      : await api('/api/c/' + S.coll.id + '/edit', { method: 'POST', body: JSON.stringify(data), headers: authHeaders() });
    S.coll = r.view;
    S.form = null;
    S.tab = 'collection';
    beepOk();
    toast(f.mode === 'create' ? 'Сбор создан 💸' : 'Сохранено ✅');
    render();
    loadHome();
  } catch (e) {
    toast(e.message);
    haptic('rigid');
  }
}

document.addEventListener('click', async (ev) => {
  if (ev.target.id === 'payFallback' || ev.target.id === 'payInline') return markPaid();

  /* крестик удаления на плитке: раньше навигации — он окружён плиткой */
  const delBtn = ev.target.closest('[data-del]');
  if (delBtn) {
    const id = delBtn.dataset.del;
    const t = S.home.find(x => x.id === id);
    const ok = await askConfirm('Удалить «' + (t ? t.title : 'сбор') + '»?\nКарточка в чате тоже снесётся. Это навсегда.');
    if (!ok) return;
    if (S.demo) {
      S.home = S.home.filter(x => x.id !== id);
      haptic(); toast('Демо: удалено 🗑'); return render();
    }
    try {
      await deleteById(id);
      haptic(); beepOk(); toast('Сбор удалён 🗑');
      await loadHome(); render();
    } catch (e) { toast(e.message); }
    return;
  }

  const nav = ev.target.closest('[data-nav]');
  if (nav) { haptic(); return navColl(nav.dataset.nav); }

  if (ev.target.id === 'backBtn') {
    if (S.tab === 'form') { S.tab = S.coll ? 'collection' : 'home'; S.form = null; return render(); }
    return navHome();
  }
  if (ev.target.id === 'copyHint') {
    const text = S.coll && S.coll.pay_hint || '';
    try { await navigator.clipboard.writeText(text); } catch (e) {}
    haptic(); return toast('Скопировано 📋');
  }
  if (ev.target.id === 'saveHint' && S.coll) {
    try {
      const r = await api('/api/c/' + S.coll.id + '/hint', {
        method: 'POST', body: JSON.stringify({ hint: $('#hintInput').value }),
        headers: Object.assign({}, { 'Content-Type': 'application/json' },
          tg && tg.initData ? { 'Authorization': 'tma ' + tg.initData } : {}),
      });
      S.coll = r.view; haptic(); toast('Реквизиты сохранены ✅'); render();
    } catch (e) { toast(e.message); }
    return;
  }
  if (ev.target.id === 'closeColl' && S.coll) {
    if (!confirm('Завершить сбор?')) return;
    try {
      const r = await api('/api/c/' + S.coll.id + '/close', {
        method: 'POST', body: '{}',
        headers: Object.assign({}, { 'Content-Type': 'application/json' },
          tg && tg.initData ? { 'Authorization': 'tma ' + tg.initData } : {}),
      });
      S.coll = r.view; haptic(); toast('Сбор закрыт 🏁'); render();
    } catch (e) { toast(e.message); }
    return;
  }
  /* — home: создать — */
  if (ev.target.closest('#createBtn')) return openCreate();

  /* — участие: в деле / отказ — */
  if (ev.target.id === 'joinMe' && S.coll) return membershipToggle('join');
  if (ev.target.id === 'declineMe' && S.coll) return membershipToggle('decline');

  /* — отменить «я скинулся» — */
  if (ev.target.id === 'unpayMe' && S.coll) return unpay();

  /* — организатор убирает человека из сбора — */
  const kickB = ev.target.closest('[data-kick]');
  if (kickB && S.coll) return kickMember(kickB.dataset.kick);

  /* — форма (создать / изменить) — */
  const chipBtn = ev.target.closest('#fModeChips [data-chip]');
  if (chipBtn && S.form) {
    S.form.chip = chipBtn.dataset.chip;
    haptic();
    return renderForm();
  }
  if (ev.target.id === 'fSave' && S.form) return submitForm();

  /* — организатор: изменить — */
  if (ev.target.id === 'editColl' && S.coll) { haptic(); return openEdit(); }

  /* — удаление внутри сбора — */
  if (ev.target.id === 'deleteColl' && S.coll) {
    const c0 = S.coll;
    const ok = await askConfirm('Удалить «' + c0.title + '»?\nКарточку в чате тоже снесём. Это навсегда.');
    if (!ok) return;
    if (S.demo) {
      S.home = S.home.filter(t => t.id !== c0.id);
      S.coll = null; S.tab = 'home';
      haptic(); toast('Демо: сбор удалён 🗑'); render(); return;
    }
    try {
      await deleteById(c0.id);
      haptic(); beepOk(); toast('Сбор удалён 🗑');
      S.coll = null; S.tab = 'home';
      await loadHome(); render();
    } catch (e) { toast(e.message); }
    return;
  }

  /* поделиться — с живым текстом приглашения */
  const shareB = ev.target.closest('[data-share]');
  if (shareB) {
    const c = S.coll;
    const msg = 'Вас пригласили в сбор «' + c.title + '» 💸 Загляни — и скажи, ты в деле или нет.';
    const url = 'https://t.me/share/url?url=' + encodeURIComponent('https://t.me/' + S.bot + '?startapp=' + c.id) +
      '&text=' + encodeURIComponent(msg);
    if (tg) tg.openTelegramLink(url);
    else {
      const full = 'https://t.me/' + S.bot + '?startapp=' + c.id;
      try { await navigator.clipboard.writeText(full); toast('Ссылка скопирована'); } catch (e) {}
    }
  }
});

if (tg) tg.BackButton.onClick(() => {
  if (S.tab === 'form') { S.tab = S.coll ? 'collection' : 'home'; S.form = null; render(); return; }
  navHome();
});

/* — участие: в дело / не участвую — */
async function membershipToggle(action) {
  const c = S.coll;
  if (S.demo) {
    const meRow = c.roster.find(r => r.me);
    if (action === 'join') {
      c.me_state = 'waiting';
      if (meRow) meRow.declined = false;
      else c.roster.unshift({ id: '900000', paid: false, me: true, name: 'Ты' });
      haptic(); toast('Ты в деле ✅');
    } else {
      c.me_state = 'declined';
      if (meRow) meRow.declined = true;
      else c.roster.push({ id: '900000', paid: false, me: true, name: 'Ты', declined: true });
      haptic(); toast('Ок, не участвуешь 🚫');
    }
    c.paid_count = c.roster.filter(r => r.paid).length;
    c.unpaid_count = c.roster.filter(r => !r.paid && !r.declined).length;
    if (action === 'decline') c.unpaid_count = Math.max(0, c.unpaid_count);
    return render();
  }
  try {
    const r = await api('/api/c/' + c.id + '/' + action, {
      method: 'POST', body: '{}', headers: authHeaders(),
    });
    S.coll = r.view;
    haptic();
    toast(action === 'join' ? 'Ты в деле ✅' : 'Не участвуешь 🚫');
    render();
    loadHome();
  } catch (e) { toast(e.message); }
}

/* — отменить «я скинулся» — */
async function unpay() {
  const c = S.coll;
  if (S.demo) {
    const meRow = c.roster.find(r => r.me);
    if (meRow) { meRow.paid = false; delete meRow.amount; }
    c.my_paid = false; c.my_amount = null; c.me_state = 'waiting';
    c.paid_count = c.roster.filter(r => r.paid).length;
    c.unpaid_count = c.roster.filter(r => !r.paid && !r.declined).length;
    c.total = c.roster.reduce((s, r) => s + (r.paid ? (r.amount || 0) : 0), 0);
    haptic(); toast('Галочку убрал — снова ждём ⭕');
    return render();
  }
  try {
    const r = await api('/api/c/' + c.id + '/unpay', {
      method: 'POST', body: '{}', headers: authHeaders(),
    });
    S.coll = r.view;
    haptic(); toast('Галочку убрал — снова ждём ⭕');
    render(); loadHome();
  } catch (e) { toast(e.message); }
}

/* — организатор убирает человека из сбора — */
async function kickMember(code) {
  const c = S.coll;
  const row = (c.roster || []).find(r => String(r.id) === String(code));
  const nm = row ? (row.name || 'этого человека') : 'этого человека';
  const ok = await askConfirm('Убрать ' + nm + ' из сбора?\nЧем меньше шум — тем лучше, придётся позвать ещё раз 📤');
  if (!ok) return;
  if (S.demo) {
    c.roster = c.roster.filter(r => r.id !== code);
    c.paid_count = c.roster.filter(r => r.paid).length;
    c.unpaid_count = c.roster.filter(r => !r.paid && !r.declined).length;
    haptic(); toast('Демо: убрано ✕');
    return render();
  }
  try {
    const r = await api('/api/c/' + c.id + '/remove', {
      method: 'POST', body: JSON.stringify({ code }), headers: authHeaders(),
    });
    S.coll = r.view;
    haptic(); toast('Убрал из сбора ✕');
    render(); loadHome();
  } catch (e) { toast(e.message); }
}

/* ─────────── онбординг (3 шага, один раз) */

const INTRO = [
  { e: '💸', h: 'Скинулись', p: 'Честный учёт общих сборов прямо в Telegram— без переводов боту и обид.' },
  { e: '✅', h: 'Один клик', p: 'Реквизиты перед глазами, скинулся — подтвердил, все видят кто в деле.' },
  { e: '⏳', h: 'Дедлайны', p: 'Кто тянет — бот аккуратно напомнит в личку. Дружно, без позора.' },
];

function showIntro() {
  let step = 0;
  const el = document.createElement('div');
  el.className = 'intro';
  el.innerHTML = '<div class="sheet">' +
    '<div class="e" id="iE"></div><h3 id="iT"></h3><p id="iP"></p>' +
    '<div class="dots" id="iDots">' + INTRO.map((_, i) => '<i' + (i === 0 ? ' class="on"' : '') + '></i>').join('') + '</div>' +
    '<button class="mini-btn go" id="iGo">Далее</button></div>';
  document.body.appendChild(el);
  const paint = () => {
    $('#iE').textContent = INTRO[step].e;
    $('#iT').textContent = INTRO[step].h;
    $('#iP').textContent = INTRO[step].p;
    el.querySelectorAll('.dots i').forEach((d, i) => d.classList.toggle('on', i === step));
    $('#iGo').textContent = step === INTRO.length - 1 ? 'Поехали 💸' : 'Далее';
  };
  el.querySelector('#iGo').onclick = () => {
    haptic();
    if (step < INTRO.length - 1) { step++; paint(); return; }
    el.remove();
    localStorage.setItem('skinulis_intro_v2', '1');
    S.introDone = true;
  };
  paint();
}

boot();
