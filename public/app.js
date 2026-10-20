/* «Скинулись» — мини-аппа. Ванила JS, один файл. */
'use strict';

const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
const $ = (p) => document.querySelector(p);
const VIEW = $('#view');

const S = {
  user: null,
  bot: 'skinulis_bot',
  demo: false,
  tab: 'home',       // home | collection
  coll: null,        // текущий сбор (view с сервера или демо)
  home: [],          // «мои сборы»
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
      status: 'active', is_organizer: true,
      organizer: { name: 'Ты', username: 'tester' },
      paid_count: 1, unpaid_count: 2, total: 400,
      my_paid: false, my_amount: null, link: '#',
      roster: [
        { id: '900000', paid: false, me: true, name: 'Ты' },
        { id: '900001', paid: false, name: 'Паша', username: 'pavel' },
        { id: '900002', paid: true, name: 'Лена', username: 'lena', amount: 400 },
      ],
    },
    {
      id: 'c_demo02', title: 'Подарок Лене 💅', mode: 'per_person',
      per_person: 300, goal_total: null, currency: '₽',
      deadline: null, deadline_left: 'до 28.12 23:59',
      anonymous: true, pay_hint: '',
      status: 'active', is_organizer: false,
      organizer: { name: 'Матильда', username: 'mda' },
      paid_count: 2, unpaid_count: 1, total: 600,
      my_paid: false, my_amount: null, link: '#',
      roster: [
        { id: '900000', paid: false, me: true, name: 'Ты' },
        { id: 'x1', paid: true, name: null },
        { id: 'x2', paid: true, name: null },
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
  else renderHome();
  renderButtons();
}

/* — home — */
function renderHome() {
  const tiles = S.home.length ? S.home.map(t =>
    '<button class="tile" data-nav="' + esc(t.id) + '">' +
      '<div class="t">' + (t.my_paid ? '✅ ' : '💸 ') + esc(t.title) + '</div>' +
      '<div class="s">' +
        (t.deadline_left ? '<span>' + esc(t.deadline_left) + '</span>' : '') +
        '<span>👍 ' + t.paid_count + '</span>' +
        (t.unpaid_count ? '<span class="unpaid">💀 ждём: ' + t.unpaid_count + '</span>' : '') +
      '</div>' +
    '</button>').join('')
    : '<div class="card hero"><div class="e">💸</div>' +
      '<p>Своих сборов нет.<br>Создай в любом чате командой<br><code>/skor Пицца по 400 до 30.10</code></p></div>';

  VIEW.innerHTML =
    '<div class="top"><h1>🗂 Мои сборы</h1></div>' + tiles +
    '<div class="foot">Скинулись 💸 · честный учёт и дожиматель</div>';
}

/* — collection — */
function renderCollection(c) {
  const unpaid = c.roster.filter(r => !r.paid).length;

  /* цель */
  let target;
  if (c.mode === 'per_person') {
    target = '<div class="big">' + money(c.per_person, c.currency) + '</div>' +
             '<div class="sub">с каждого· скинулись ' + c.paid_count + ' ' + (!c.anonymous ? '· ждём ' + unpaid : '· есть тихони') + '</div>';
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

  /* мой блок */
  let mine;
  if (c.my_paid) {
    mine = '<div class="card">' +
      '<div class="mystatus"><span class="emoji">✅</span>' +
      '<div><div class="big" style="font-size:22px">' + money(c.my_amount, c.currency) + '</div>' +
      '<div class="sub">Ты скинулся. Красавчик 💪</div></div></div></div>';
  } else if (c.status === 'active') {
    mine = '<div class="card">' +
      '<div class="mystatus"><span class="emoji">💸</span>' +
      '<div><div class="big" style="font-size:22px">' +
        (c.mode === 'per_person' ? money(c.per_person, c.currency) : 'сколько не жалко') + '</div>' +
      '<div class="sub">' + (c.mode === 'per_person'
        ? 'С тебя столько. Жми кнопку «Я скинулся» внизу'
        : 'Введи сумму и жми кнопку внизу') + '</div></div></div>' +
      (c.mode !== 'per_person' ? '<input class="free-input" id="amount" inputmode="numeric" placeholder="300">' : '') +
      (tg ? '' : '<div style="margin-top:10px"><button class="mini-btn go" id="payFallback">Я скинулся ✅' +
        (c.mode === 'per_person' && c.per_person ? ' ' + esc(c.per_person) + ' ' + esc(c.currency) : '') +
        '</button><div class="sub" style="margin-top:6px">Кнопка внизу появится в Telegram</div></div>') +
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

  /* участники */
  const rows = c.roster.map(r => {
    const nm = r.me ? 'Ты' : (r.name ? esc(r.name) : '<i>скрыт</i>');
    const amt = r.paid && r.amount != null ? '<span class="amt">' + esc(r.amount) + ' ' + esc(c.currency) + '</span>' : '';
    const masked = (!r.me && !r.name) ? ' class="masked"' : '';
    return '<li' + masked + '>' +
      '<span class="st">' + (r.paid ? '✅' : '⭕') + '</span>' +
      '<span class="nm' + (r.me ? ' me' : '') + '">' + nm + '</span>' + amt + '</li>';
  }).join('');
  const roster = '<div class="card"><h2>Учёт</h2><ul class="who">' + rows + '</ul></div>';

  /* организаторская панель */
  let org = '';
  if (c.is_organizer && c.status === 'active') {
    org = '<div class="card org-panel"><h2>Организатору</h2>' +
      '<input id="hintInput" placeholder="Куда скидывать: СБП, карта…" value="' + esc(c.pay_hint) + '">' +
      '<div class="org-actions">' +
        '<button class="mini-btn ghost" id="saveHint">Сохранить реквизиты</button>' +
        '<button class="mini-btn danger" id="closeColl">Завершить сбор</button>' +
      '</div></div>';
  }

  VIEW.innerHTML =
    '<div class="top">' +
      '<button class="back' + (S.home.length ? ' on' : '') + '" id="backBtn">‹</button>' +
      '<h1>' + esc(c.title) + '</h1>' + chipOf(c) +
    '</div>' +
    '<div class="card">' + target + '</div>' +
    (c.deadline_left ? '<div class="card" style="text-align:center">⏳ <b>' + esc(c.deadline_left) + '</b></div>' : '') +
    mine + hint + roster + org +
    '<div class="foot">Надёжный учёт без раскрытия чужих сумм' +
      (c.anonymous ? ' · режим анонимности' : '') + '</div>';
}

/* ─────────── кнопки Telegram (MainButton / BackButton) */

function renderButtons() {
  if (!tg) return;

  if (S.tab === 'home') {
    tg.BackButton.hide();
    tg.MainButton.hide();
    return;
  }
  const c = S.coll;
  tg.BackButton.show();
  if (c.status === 'active' && !c.my_paid) {
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

document.addEventListener('click', async (ev) => {
  if (ev.target.id === 'payFallback') return markPaid();

  const nav = ev.target.closest('[data-nav]');
  if (nav) { haptic(); return navColl(nav.dataset.nav); }

  if (ev.target.id === 'backBtn') return navHome();
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
  /* поделиться */
  const shareB = ev.target.closest('[data-share]');
  if (shareB) {
    const c = S.coll;
    const url = 'https://t.me/share/url?url=' + encodeURIComponent('https://t.me/' + S.bot + '?startapp=' + c.id) +
      '&text=' + encodeURIComponent('Скидываемся: ' + c.title + ' 💸');
    if (tg) tg.openTelegramLink(url);
    else { try { await navigator.clipboard.writeText(url); toast('Ссылка скопирована'); } catch (e) {} }
  }
});

if (tg) tg.BackButton.onClick(() => navHome());

/* ─────────── онбординг (3 шага, один раз) */

const INTRO = [
  { e: '💸', h: 'Скинулись', p: 'Честный учёт общих сборов прямо в Telegram-чате.' },
  { e: '✅', h: 'Один клик', p: 'Реквизиты перед глазами, скинулся — подтвердил.' },
  { e: '💀', h: 'Дожиматель', p: 'Дедлайны и напоминания тем, кто тянет резину.' },
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
