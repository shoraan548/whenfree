import hashlib, hmac, json, os, re, secrets, sqlite3, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

db = sqlite3.connect(os.environ.get('DB', 'app.db'), check_same_thread=False, isolation_level=None)
db.row_factory = sqlite3.Row
db.executescript('''
  CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, hash TEXT NOT NULL, admin INTEGER NOT NULL DEFAULT 0);
  CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL);
  CREATE TABLE IF NOT EXISTS activities(id INTEGER PRIMARY KEY, date TEXT NOT NULL, time TEXT, title TEXT NOT NULL, place TEXT, note TEXT);
  CREATE TABLE IF NOT EXISTS going(activity_id INTEGER, user_id INTEGER, PRIMARY KEY(activity_id, user_id));
  CREATE TABLE IF NOT EXISTS votes(date TEXT, user_id INTEGER, choice TEXT NOT NULL, PRIMARY KEY(date, user_id));
  CREATE TABLE IF NOT EXISTS reminders(activity_id INTEGER, user_id INTEGER, hours INTEGER, PRIMARY KEY(activity_id, user_id, hours));
  CREATE TABLE IF NOT EXISTS outbox(chat_id INTEGER, activity_id INTEGER, kind TEXT, text TEXT);  -- held during quiet hours
''')
# migrations for existing DBs
if 'tg_id' not in [c[1] for c in db.execute('PRAGMA table_info(users)')]:
    db.execute('ALTER TABLE users ADD COLUMN tg_id INTEGER')
if 'author_id' not in [c[1] for c in db.execute('PRAGMA table_info(activities)')]:
    db.execute('ALTER TABLE activities ADD COLUMN author_id INTEGER')  # NULL for old activities: only admins delete those
db.execute('CREATE UNIQUE INDEX IF NOT EXISTS users_tg ON users(tg_id)')
lock = threading.Lock()  # ponytail: one lock for the whole DB, plenty for a group of friends
page = (Path(__file__).parent / 'index.html').read_bytes()
fails = {}  # ponytail: in memory, reset on restart — enough to stop brute force
WINDOW = 15 * 60

# Telegram bot: login + notifications. Without BOT_TOKEN these features are simply off.
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
TG_API = os.environ.get('TG_API', 'https://api.telegram.org')  # overridable for tests
SITE_URL = os.environ.get('SITE_URL', '')  # optional link appended to notifications
bot = {'username': None}
pending = {}  # nonce -> {'at', 'link_to', 'user'}; ponytail: in memory, a restart just means "log in again"
TG_TTL = 10 * 60
TZ = ZoneInfo(os.environ.get('TZ') or 'Europe/Moscow')  # activity times are in this timezone
REMIND_HOURS = (24, 6, 1)
REMIND_EVERY = float(os.environ.get('REMIND_EVERY', 60))  # seconds between checks; tests make it short
_quiet = os.environ.get('QUIET_HOURS', '0-7')
QUIET = tuple(map(int, _quiet.split('-'))) if _quiet else None  # no messages in these hours, e.g. 0-7 or 23-8; empty = off


def quiet(now):
    if not QUIET: return False
    start, end = QUIET
    return start <= now.hour < end if start <= end else now.hour >= start or now.hour < end


def q(sql, *args): return db.execute(sql, args)
def one(sql, *args): r = q(sql, *args).fetchone(); return dict(r) if r else None
def rows(sql, *args): return [dict(r) for r in q(sql, *args)]
def raw(v): return '' if v is None else str(v)
def s(v, mx): return raw(v).strip()[:mx]


# salt:hex format matches the old Node version, so existing passwords keep working
def make_hash(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    return salt + ':' + hashlib.scrypt(pw.encode(), salt=salt.encode(), n=16384, r=8, p=1, dklen=32).hex()

def verify(pw, stored):  # stored == '' means a Telegram-only account without a password
    return bool(stored) and hmac.compare_digest(make_hash(pw, stored.split(':')[0]), stored)


def tg(method, **params):
    req = urllib.request.Request(f'{TG_API}/bot{BOT_TOKEN}/{method}', json.dumps(params).encode(), {'content-type': 'application/json'})
    with urllib.request.urlopen(req, timeout=70) as r: return json.load(r)['result']


def activity_fields(b):
    """Validated activity fields from a request body, or None."""
    f = {'date': s(b.get('date'), 10), 'time': s(b.get('time'), 5), 'title': s(b.get('title'), 100),
         'place': s(b.get('place'), 100), 'note': s(b.get('note'), 1000)}
    try:
        datetime.strptime(f['date'], '%Y-%m-%d')  # a real date: bad ones would break reminders
        if f['time']: datetime.strptime(f['time'], '%H:%M')
    except ValueError: return None
    if not re.fullmatch(r'\d{4}-\d\d-\d\d', f['date']) or (f['time'] and not re.fullmatch(r'\d\d:\d\d', f['time'])): return None
    return f if f['title'] else None


def summary(f): return ' '.join(filter(None, [f"{f['date'][8:]}.{f['date'][5:7]}", f['time'], f['title']])) + (f" · {f['place']}" if f['place'] else '')


def start_of(a): return datetime.fromisoformat(f"{a['date']}T{a['time'] or '09:00'}").replace(tzinfo=TZ)  # no time given = 09:00


def activity_text(a, kind):
    text = ('Новое: ' if kind == 'new' else 'Изменено: ') + summary(a)
    if kind == 'new' and one('SELECT COUNT(*) n FROM activities WHERE date = ?', a['date'])['n'] > 1:
        text += '\nНа этот день уже есть другие планы, голосуйте на сайте.'
    return text


def send_all(msgs):
    """Send (chat_id, text) pairs one by one."""
    for chat, text in msgs:
        try: tg('sendMessage', chat_id=chat, text=text + ('\n' + SITE_URL if SITE_URL else ''))
        except Exception as e: print('telegram send failed:', e, flush=True)  # e.g. the user blocked the bot
        time.sleep(0.05)  # stay well under Telegram's 30 msg/s


def notify(text, exclude=None, going_of=None, activity_id=None, kind=None):
    """Message users with Telegram linked (only those going, if going_of is set): in the background now,
    or queued until morning during quiet hours. activity_id + kind ('new'/'edit') let the queue merge them."""
    sql, args = 'SELECT tg_id FROM users WHERE tg_id IS NOT NULL AND id IS NOT ?', [exclude]
    if going_of is not None:
        sql += ' AND id IN (SELECT user_id FROM going WHERE activity_id = ?)'
        args.append(going_of)
    ids = [r['tg_id'] for r in rows(sql, *args)]
    if not BOT_TOKEN or not ids: return len(ids)
    if quiet(datetime.now(TZ)):
        for chat in ids: q('INSERT INTO outbox VALUES (?, ?, ?, ?)', chat, activity_id, kind, text)
    else:
        threading.Thread(target=send_all, args=([(chat, text) for chat in ids],), daemon=True).start()
    return len(ids)


def take_outbox(now):
    """Under the lock: messages held overnight, one per person and activity, rebuilt from the activity's
    current state ('new' wins over 'edit'). Activities deleted or already over by now are dropped."""
    out, merged = [], {}
    for r in rows('SELECT * FROM outbox ORDER BY rowid'):
        if r['activity_id'] is None: out.append((r['chat_id'], r['text']))  # e.g. an admin broadcast
        else:
            key = (r['chat_id'], r['activity_id'])
            merged[key] = 'new' if 'new' in (merged.get(key), r['kind']) else 'edit'
    for (chat, aid), kind in merged.items():
        a = one('SELECT * FROM activities WHERE id = ?', aid)
        if a and start_of(a) > now: out.append((chat, activity_text(a, kind)))
    q('DELETE FROM outbox')
    return out


def tg_confirm(p, who):
    """Runs under the lock when someone presses "Yes" in the bot. Returns the bot's reply."""
    tid, owner = who['id'], one('SELECT id FROM users WHERE tg_id = ?', who['id'])
    if p['link_to']:
        if owner and owner['id'] != p['link_to']: return 'Этот Telegram уже привязан к другому аккаунту.'
        q('UPDATE users SET tg_id = ? WHERE id = ?', tid, p['link_to'])
        p['user'] = p['link_to']
        return 'Telegram привязан. Сюда будут приходить уведомления.'
    if not owner:
        base = s(' '.join(filter(None, [who.get('first_name'), who.get('last_name')])), 26)
        base = base if len(base) >= 2 else f'tg{tid}'[:26]
        name, i = base, 1
        while one('SELECT 1 FROM users WHERE name = ?', name): i += 1; name = f'{base} {i}'
        first = not one('SELECT 1 FROM users')
        owner = {'id': q('INSERT INTO users(name, hash, admin, tg_id) VALUES (?, ?, ?, ?)', name, '', int(first), tid).lastrowid}
    p['user'] = owner['id']
    return 'Готово! Вернитесь в браузер. Сюда будут приходить уведомления.'


def on_update(upd):
    if m := upd.get('message'):
        chat, text = m['chat']['id'], m.get('text') or ''
        nonce = text[7:].strip() if text.startswith('/start ') else ''
        with lock: p = pending.get(nonce)
        if not p or time.time() - p['at'] > TG_TTL:
            return tg('sendMessage', chat_id=chat, text='Чтобы войти, нажмите «Войти через Telegram» на сайте.')
        # Explicit confirmation, so a link someone else sent you can't silently log them in as you
        tg('sendMessage', chat_id=chat, text='Войти на сайт «WhenFree»? Нажимайте, только если вы сами сейчас входите.',
           reply_markup={'inline_keyboard': [[{'text': 'Да, это я', 'callback_data': 'ok:' + nonce}]]})
    elif cq := upd.get('callback_query'):
        nonce = (cq.get('data') or '')[3:]
        with lock:
            p = pending.get(nonce)
            ok = p and not p['user'] and time.time() - p['at'] <= TG_TTL
            reply = tg_confirm(p, cq['from']) if ok else 'Ссылка устарела, начните вход заново.'
        tg('answerCallbackQuery', callback_query_id=cq['id'])
        tg('sendMessage', chat_id=cq['from']['id'], text=reply)


def due_reminders(now):
    """Under the lock: (activity, time left as text, chat id) for reminders due now; marks them sent.
    Tracked per user, so someone who joins late still gets the current reminder. Only the closest due
    threshold is sent, so a late check or a last-minute activity doesn't produce stale "in 24 h" messages."""
    out = []
    for a in rows('SELECT * FROM activities WHERE date BETWEEN ? AND ?', now.date().isoformat(), (now + timedelta(days=2)).date().isoformat()):
        start = start_of(a)
        due = [h for h in REMIND_HOURS if start - timedelta(hours=h) <= now]
        if start <= now or not due: continue
        mins = round((start - now).total_seconds() / 60)
        left = f'{mins} мин' if mins < 90 else f'{round(mins / 60)} ч'
        for g in rows('SELECT u.id, u.tg_id FROM going g JOIN users u ON u.id = g.user_id WHERE g.activity_id = ? AND u.tg_id IS NOT NULL', a['id']):
            if one('SELECT 1 FROM reminders WHERE activity_id = ? AND user_id = ? AND hours = ?', a['id'], g['id'], min(due)): continue
            for h in due: q('INSERT OR IGNORE INTO reminders VALUES (?, ?, ?)', a['id'], g['id'], h)
            out.append((a, left, g['tg_id']))
    return out


def tick(now):
    """Under the lock: everything to send at this moment. Nothing during quiet hours; at the end of them the
    night's queue goes out, and reminders skipped overnight collapse into the closest one (see due_reminders)."""
    if quiet(now): return []
    msgs = take_outbox(now)
    for a, left, chat in due_reminders(now):
        msgs.append((chat, f"Напоминание: через {left} {a['title']}" + (f" · {a['place']}" if a['place'] else '')))
    return msgs


def reminder_loop():
    while True:
        try:
            with lock: msgs = tick(datetime.now(TZ))
            send_all(msgs)
        except Exception as e:
            print('reminders:', repr(e), flush=True)
        time.sleep(REMIND_EVERY)


def bot_loop():
    offset = 0
    while True:
        try:
            if not bot['username']: bot['username'] = tg('getMe')['username']
            for upd in tg('getUpdates', offset=offset, timeout=50, allowed_updates=['message', 'callback_query']):
                offset = upd['update_id'] + 1
                on_update(upd)
        except Exception as e:
            print('telegram:', repr(e), flush=True)
            time.sleep(3)


def login(res, user_id):
    t = secrets.token_hex(24)
    q('INSERT INTO sessions VALUES (?, ?)', t, user_id)
    secure = '; Secure' if res.headers.get('X-Forwarded-Proto') == 'https' else ''  # set by Caddy / Tailscale
    res.cookie = f'sid={t}; HttpOnly; SameSite=Lax; Path=/; Max-Age=31536000{secure}'
    return 200, None


def state(u):
    acts = rows('SELECT a.*, u.name author FROM activities a LEFT JOIN users u ON u.id = a.author_id')
    going = rows('SELECT g.activity_id, u.id, u.name FROM going g JOIN users u ON u.id = g.user_id')
    for a in acts:
        a['going'] = {g['id']: g['name'] for g in going if g['activity_id'] == a['id']}
        a['id'] = str(a['id'])
    days = {}
    for v in rows('SELECT * FROM votes'):
        days.setdefault(v['date'], {'votes': {}})['votes'][v['user_id']] = v['choice']
    data = {'me': {'id': u['id'], 'name': u['name'], 'admin': bool(u['admin']), 'tg': bool(u['tg_id']), 'pw': bool(u['hash'])}, 'acts': acts, 'days': days}
    if u['admin']: data['users'] = rows('SELECT id, name, admin FROM users ORDER BY name')
    return 200, data


def handle(method, path, b, u, res):
    if method == 'POST' and path == '/api/register':
        name, pw = s(b.get('name'), 100), raw(b.get('pass'))
        if not 2 <= len(name) <= 30 or len(pw) < 6: return 400, 'Имя 2–30 символов, пароль от 6'
        if one('SELECT 1 FROM users WHERE name = ?', name): return 409, 'Имя занято'
        first = not one('SELECT 1 FROM users')  # the first user to register becomes admin
        return login(res, q('INSERT INTO users(name, hash, admin) VALUES (?, ?, ?)', name, make_hash(pw), int(first)).lastrowid)
    if method == 'POST' and path == '/api/login':
        name = s(b.get('name'), 100)
        key, now = name.lower(), time.time()
        f = fails.get(key)
        fresh = f and now - f['at'] < WINDOW
        if fresh and f['n'] >= 10: return 429, 'Слишком много попыток, подождите 15 минут'
        row = one('SELECT * FROM users WHERE name = ?', name)
        if not row or not verify(raw(b.get('pass')), row['hash']):
            fails[key] = {'n': (f['n'] if fresh else 0) + 1, 'at': now}
            return 401, 'Неверное имя или пароль'
        fails.pop(key, None)
        return login(res, row['id'])
    if method == 'GET' and path == '/api/config': return 200, {'tg': bool(bot['username'])}
    if method == 'POST' and path == '/api/tg/start':  # login, or linking Telegram to the current account
        if not bot['username']: return 404, 'Вход через Telegram не настроен'
        now = time.time()
        for k in [k for k, p in pending.items() if now - p['at'] > TG_TTL]: del pending[k]
        if len(pending) > 1000: return 429, 'Слишком много попыток, попробуйте позже'
        nonce = secrets.token_urlsafe(16)
        pending[nonce] = {'at': now, 'link_to': u['id'] if u else None, 'user': None}
        return 200, {'nonce': nonce, 'link': f"https://t.me/{bot['username']}?start={nonce}"}
    if method == 'POST' and path == '/api/tg/check':
        nonce = s(b.get('nonce'), 64)
        p = pending.get(nonce)
        if not p: return 404, 'Вход устарел, начните заново'
        if not p['user']: return 202, 'wait'
        del pending[nonce]
        return (200, None) if p['link_to'] else login(res, p['user'])
    if not u: return 401, 'Нужно войти'
    if method == 'GET' and path == '/api/state': return state(u)
    if method == 'POST' and path == '/api/logout':
        q('DELETE FROM sessions WHERE token = ?', u['token'])
        res.cookie = 'sid=; Path=/; Max-Age=0'
        return 200, None
    if method == 'POST' and path == '/api/vote':
        date, choice = s(b.get('date'), 10), s(b.get('choice'), 20)
        if choice != 'all' and not one('SELECT 1 FROM activities WHERE id = ? AND date = ?', choice, date): return 400, 'Нет такого варианта'
        q('INSERT OR REPLACE INTO votes VALUES (?, ?, ?)', date, u['id'], choice)
        return 200, None
    if method == 'POST' and path == '/api/password':
        pw = raw(b.get('pass'))
        if u['hash'] and not verify(raw(b.get('old')), u['hash']): return 401, 'Старый пароль неверный'  # Telegram-only accounts set a first password
        if len(pw) < 6: return 400, 'Пароль от 6 символов'
        q('UPDATE users SET hash = ? WHERE id = ?', make_hash(pw), u['id'])
        q('DELETE FROM sessions WHERE user_id = ? AND token != ?', u['id'], u['token'])  # log out other devices
        return 200, None
    m = re.fullmatch(r'/api/going/(\d+)', path)
    if method == 'POST' and m:
        aid = int(m[1])
        if not one('SELECT 1 FROM activities WHERE id = ?', aid): return 404, 'Нет такой активности'
        if not q('DELETE FROM going WHERE activity_id = ? AND user_id = ?', aid, u['id']).rowcount:
            q('INSERT INTO going VALUES (?, ?)', aid, u['id'])
        return 200, None
    if method == 'POST' and path == '/api/activities':
        f = activity_fields(b)
        if not f: return 400, 'Нужны название и правильные дата и время'
        aid = q('INSERT INTO activities(date, time, title, place, note, author_id) VALUES (?, ?, ?, ?, ?, ?)',
                f['date'], f['time'], f['title'], f['place'], f['note'], u['id']).lastrowid
        notify(activity_text(f, 'new'), exclude=u['id'], activity_id=aid, kind='new')
        return 200, None
    m = re.fullmatch(r'/api/activities/(\d+)', path)
    if m and method in ('POST', 'DELETE'):  # POST = edit
        aid = int(m[1])
        a = one('SELECT * FROM activities WHERE id = ?', aid)
        if not a: return 404, 'Нет такой активности'
        if not u['admin'] and a['author_id'] != u['id']: return 403, 'Изменить или удалить можно только свою активность'
        if method == 'DELETE':
            for sql in ('DELETE FROM activities WHERE id = ?', 'DELETE FROM going WHERE activity_id = ?', 'DELETE FROM reminders WHERE activity_id = ?'):
                q(sql, aid)
            q('DELETE FROM votes WHERE choice = ?', m[1])
            return 200, None
        f = activity_fields(b)
        if not f: return 400, 'Нужны название и правильные дата и время'
        q('UPDATE activities SET date = ?, time = ?, title = ?, place = ?, note = ? WHERE id = ?',
          f['date'], f['time'], f['title'], f['place'], f['note'], aid)
        if (f['date'], f['time']) != (a['date'], a['time']):
            q('DELETE FROM reminders WHERE activity_id = ?', aid)  # reschedule reminders
            if f['date'] != a['date']: q('DELETE FROM votes WHERE choice = ?', m[1])  # those votes were for the old day
            notify(activity_text(f, 'edit'), exclude=u['id'], going_of=aid, activity_id=aid, kind='edit')
        return 200, None
    if not u['admin']: return 403, 'Только для админа'
    if method == 'POST' and path == '/api/notify':
        text = s(b.get('text'), 1000)
        if not text: return 400, 'Пустое сообщение'
        return 200, str(notify(f"{u['name']}: {text}", exclude=u['id']))
    m = re.fullmatch(r'/api/users/(\d+)/(admin|reset)', path)
    if method == 'POST' and m:
        uid = int(m[1])
        if not one('SELECT 1 FROM users WHERE id = ?', uid): return 404, 'Нет такого пользователя'
        if m[2] == 'admin':
            if uid == u['id']: return 400, 'Себя менять нельзя'  # so there is always at least one admin
            q('UPDATE users SET admin = ? WHERE id = ?', int(bool(b.get('admin'))), uid)
            return 200, None
        pw = secrets.token_hex(4)  # temporary; the admin hands it over themselves
        q('UPDATE users SET hash = ? WHERE id = ?', make_hash(pw), uid)
        q('DELETE FROM sessions WHERE user_id = ?', uid)
        return 200, pw
    return 404, 'Not found'


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split('?')[0]
        if not path.startswith('/api/'): return self.reply(200, page, 'text/html; charset=utf-8')
        self.cookie = None
        try:
            n = int(self.headers.get('Content-Length') or 0)
            if n > 10000: raise ValueError('body too large')
            body = json.loads(self.rfile.read(n) or b'{}')
            if not isinstance(body, dict): raise ValueError('body must be an object')
            m = re.search(r'(?:^|; )sid=([0-9a-f]+)', self.headers.get('Cookie') or '')
            with lock:
                u = m and one('SELECT u.*, s.token FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?', m[1])
                status, data = handle(self.command, path, body, u, self)
        except Exception as e:
            self.log_error('%r', e)
            status, data = 400, 'Ошибка запроса'
        if isinstance(data, dict): self.reply(status, json.dumps(data, ensure_ascii=False).encode(), 'application/json')
        else: self.reply(status, (data or 'ok').encode(), 'text/plain; charset=utf-8')

    do_POST = do_DELETE = do_GET

    def reply(self, status, body, ctype):
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        if getattr(self, 'cookie', None): self.send_header('Set-Cookie', self.cookie)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args): pass  # no access logs with friends' IPs; errors go through log_error

    def log_error(self, fmt, *args): print(fmt % args, flush=True)


if __name__ == '__main__':
    if BOT_TOKEN:
        threading.Thread(target=bot_loop, daemon=True).start()
        threading.Thread(target=reminder_loop, daemon=True).start()
    ThreadingHTTPServer(('', int(os.environ.get('PORT', 8080))), Handler).serve_forever()
