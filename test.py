# Smoke test against a running server: python test.py [url]. Random suffix, so it works on a non-empty DB too.
# Telegram checks run when the server was started with
#   BOT_TOKEN=test TG_API=http://127.0.0.1:8099 TZ=UTC REMIND_EVERY=1   (fake Bot API below, fast reminder checks)
import json, random, re, string, sys, threading, time, urllib.error, urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

URL = sys.argv[1] if len(sys.argv) > 1 else 'http://localhost:8080'
S = ''.join(random.choices(string.ascii_lowercase + string.digits, k=5))


def client():
    cookie = ''
    def call(path, body=None, method=None):
        nonlocal cookie
        req = urllib.request.Request(URL + path, method=method or ('GET' if body is None else 'POST'),
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={'cookie': cookie, 'content-type': 'application/json'})
        try: r = urllib.request.urlopen(req)
        except urllib.error.HTTPError as e: r = e
        cookie = (r.headers.get('set-cookie') or '').split(';')[0] or cookie
        text = r.read().decode()
        return r.status, json.loads(text) if 'json' in (r.headers.get('content-type') or '') else text
    return call


for _ in range(50):  # wait for the server to come up
    try: urllib.request.urlopen(URL); break
    except OSError: time.sleep(0.2)

a, b = client(), client()
bob_pass = 'secret1'
a('/api/register', {'name': 'admin' + S, 'pass': 'secret1'})
is_admin = a('/api/state')[1]['me']['admin']
assert b('/api/register', {'name': 'bob' + S, 'pass': 'secret1'})[0] == 200
assert b('/api/register', {'name': 'BOB' + S, 'pass': 'secret1'})[0] == 409, 'names are case-insensitive'
assert b('/api/register', {'name': 'x' + S, 'pass': '123'})[0] == 400, 'short password'
assert b('/api/state')[1]['me']['admin'] is False
assert b('/api/activities', {'date': '2030-01-01', 'title': 'own' + S})[0] == 200, 'anyone can add an activity'
own = next(x for x in b('/api/state')[1]['acts'] if x['title'] == 'own' + S)
assert own['author'] == 'bob' + S and a('/api/state')[1]['acts'], 'everyone sees it, with its author'
edit = {'date': '2030-01-02', 'time': '18:30', 'title': 'own2' + S, 'place': 'p', 'note': 'n'}
assert b('/api/activities/' + own['id'], edit)[0] == 200, 'author can edit'
got = next(x for x in b('/api/state')[1]['acts'] if x['id'] == own['id'])
assert (got['date'], got['time'], got['title'], got['place']) == ('2030-01-02', '18:30', 'own2' + S, 'p')
stranger = client()
stranger('/api/register', {'name': 'str' + S, 'pass': 'secret1'})
assert stranger('/api/activities/' + own['id'], edit)[0] == 403, "can't edit someone else's activity"
assert b('/api/activities/' + own['id'], {**edit, 'date': '2030-02-30'})[0] == 400, 'invalid date'
assert b('/api/activities/' + own['id'], {**edit, 'time': '25:00'})[0] == 400, 'invalid time'
assert b('/api/activities', {**edit, 'date': '2030-13-01'})[0] == 400, 'invalid date on create too'
if is_admin: assert a('/api/activities/' + own['id'], {**edit, 'title': 'adm' + S})[0] == 200, 'admin can edit any activity'
assert b('/api/activities/' + own['id'], {}, 'DELETE')[0] == 200, 'author can delete their own'
assert client()('/api/state')[0] == 401, 'not logged in'
assert b('/api/login', {'name': 'bob' + S, 'pass': 'wrong11'})[0] == 401

if is_admin:  # only on a fresh DB is the first user admin
    date = f'2030-01-0{random.randint(1, 9)}'
    a('/api/activities', {'date': date, 'title': 'Кино', 'time': '19:00'})
    a('/api/activities', {'date': date, 'title': 'Боулинг'})
    ids = [x['id'] for x in b('/api/state')[1]['acts'] if x['date'] == date]
    b('/api/going/' + ids[0], {})
    b('/api/vote', {'date': date, 'choice': 'all'})
    a('/api/vote', {'date': date, 'choice': ids[1]})
    assert b('/api/vote', {'date': date, 'choice': '999999'})[0] == 400
    st = b('/api/state')[1]
    assert len(next(x for x in st['acts'] if x['id'] == ids[0])['going']) == 1
    assert sorted(st['days'][date]['votes'].values()) == sorted([ids[1], 'all'])
    b('/api/going/' + ids[0], {})  # second click un-joins
    assert b('/api/activities/' + ids[1], {}, 'DELETE')[0] == 403, "can't delete someone else's activity"
    a('/api/activities/' + ids[1], {}, 'DELETE')
    b('/api/activities', {'date': date, 'title': 'bobs' + S})
    bobs = next(x['id'] for x in a('/api/state')[1]['acts'] if x['title'] == 'bobs' + S)
    assert a('/api/activities/' + bobs, {}, 'DELETE')[0] == 200, 'admin can delete any activity'
    st = b('/api/state')[1]
    assert len(next(x for x in st['acts'] if x['id'] == ids[0])['going']) == 0
    assert list(st['days'][date]['votes'].values()) == ['all'], 'votes for a deleted activity are cleared'
    a('/api/activities/' + ids[0], {}, 'DELETE')

    # admins and password reset
    bob_id, admin_id = b('/api/state')[1]['me']['id'], a('/api/state')[1]['me']['id']
    assert 'users' not in b('/api/state')[1], 'user list is admin-only'
    assert b(f'/api/users/{admin_id}/admin', {'admin': False})[0] == 403
    assert a(f'/api/users/{admin_id}/admin', {'admin': False})[0] == 400, 'cannot demote yourself'
    a(f'/api/users/{bob_id}/admin', {'admin': True})
    assert b('/api/state')[1]['me']['admin'] is True
    a(f'/api/users/{bob_id}/admin', {'admin': False})
    assert b('/api/state')[1]['me']['admin'] is False
    tmp = a(f'/api/users/{bob_id}/reset', {})[1]
    assert b('/api/state')[0] == 401, 'reset logs the user out'
    assert b('/api/login', {'name': 'bob' + S, 'pass': 'secret1'})[0] == 401, 'old password no longer works'
    assert b('/api/login', {'name': 'bob' + S, 'pass': tmp})[0] == 200
    bob_pass = tmp

# changing your own password
other = client()
assert b('/api/password', {'old': 'nope', 'pass': 'newpass1'})[0] == 401
assert b('/api/password', {'old': bob_pass, 'pass': '123'})[0] == 400
other('/api/login', {'name': 'bob' + S, 'pass': bob_pass})
assert b('/api/password', {'old': bob_pass, 'pass': 'newpass1'})[0] == 200
assert other('/api/state')[0] == 401, 'other sessions are closed'
assert b('/api/state')[0] == 200, 'current session survives'
b('/api/logout', {})
assert b('/api/state')[0] == 401, 'after logout'
print('OK', '(full)' if is_admin else '(admin checks skipped: DB not fresh)')

# brute force is blocked after 10 failures
c, victim = client(), 'lock' + S
c('/api/register', {'name': victim, 'pass': 'secret1'})
for i in range(10): c('/api/login', {'name': victim, 'pass': f'wrong{i}'})
assert c('/api/login', {'name': victim, 'pass': 'secret1'})[0] == 429, 'blocked even with the right password'
print('OK lockout')

# Telegram: fake Bot API that queues updates for the server and records what the bot sends
updates, sent = [], []


class FakeTG(BaseHTTPRequestHandler):
    def do_POST(self):
        method = self.path.rsplit('/', 1)[1]
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
        if method == 'getMe': result = {'username': 'test_bot'}
        elif method == 'getUpdates':
            time.sleep(0.2)
            result = [u for u in updates if u['update_id'] >= body.get('offset', 0)]
        else: sent.append((method, body)); result = True
        out = json.dumps({'ok': True, 'result': result}).encode()
        self.send_response(200); self.send_header('Content-Length', str(len(out))); self.end_headers(); self.wfile.write(out)

    def log_message(self, *args): pass


def push(update): updates.append({'update_id': len(updates) + 1, **update})


def wait(cond, timeout=6):
    end = time.time() + timeout
    while time.time() < end:
        if cond(): return True
        time.sleep(0.2)
    return False


def confirm(nonce, tg_id, name='x'):
    push({'callback_query': {'id': f'cq{len(updates)}', 'data': 'ok:' + nonce, 'from': {'id': tg_id, 'first_name': name}}})


def bot_said(chat, part): return any(m == 'sendMessage' and p['chat_id'] == chat and part in p['text'] for m, p in sent)


threading.Thread(target=ThreadingHTTPServer(('127.0.0.1', 8099), FakeTG).serve_forever, daemon=True).start()
if not wait(lambda: client()('/api/config')[1]['tg'], 8):
    print('telegram checks skipped (server not started with the fake TG_API)')
    sys.exit()

# login via Telegram creates an account
t = client()
d = t('/api/tg/start', {})[1]
nonce = d['nonce']
assert d['link'] == 'https://t.me/test_bot?start=' + nonce
push({'message': {'chat': {'id': 111}, 'from': {'id': 111}, 'text': '/start ' + nonce}})
assert wait(lambda: any('reply_markup' in p for m, p in sent if m == 'sendMessage')), 'bot asks for confirmation'
assert t('/api/tg/check', {'nonce': nonce})[0] == 202, 'not logged in before confirming'
confirm(nonce, 111, 'Тест' + S)
assert wait(lambda: t('/api/tg/check', {'nonce': nonce})[0] == 200)
me = t('/api/state')[1]['me']
assert me['tg'] and not me['pw'] and me['name'] == 'Тест' + S, me
assert t('/api/tg/check', {'nonce': nonce})[0] == 404, 'nonce is single-use'
assert t('/api/login', {'name': 'Тест' + S, 'pass': ''})[0] == 401, 'no password means no password login'
assert t('/api/password', {'pass': 'tgpass1'})[0] == 200, 'Telegram-only user sets a first password'
assert client()('/api/login', {'name': 'Тест' + S, 'pass': 'tgpass1'})[0] == 200

# linking Telegram to an existing account
l = client()
l('/api/register', {'name': 'link' + S, 'pass': 'secret1'})
nonce = l('/api/tg/start', {})[1]['nonce']
confirm(nonce, 111)
assert wait(lambda: bot_said(111, 'уже привязан')), 'one Telegram per account'
assert l('/api/tg/check', {'nonce': nonce})[0] == 202
confirm(nonce, 222)
assert wait(lambda: l('/api/tg/check', {'nonce': nonce})[0] == 200)
assert l('/api/state')[1]['me']['tg'] is True

# notifications
if is_admin:
    a('/api/activities', {'date': '2031-02-03', 'time': '18:00', 'title': 'Каток' + S})
    assert wait(lambda: bot_said(111, 'Каток' + S) and bot_said(222, 'Каток' + S)), 'new activity is announced'
    assert int(a('/api/notify', {'text': 'привет' + S})[1]) >= 2
    assert wait(lambda: bot_said(222, 'привет' + S)), 'admin broadcast'
    assert l('/api/notify', {'text': 'x'})[0] == 403, 'broadcast is admin-only'
    aid = next(x['id'] for x in a('/api/state')[1]['acts'] if x['title'] == 'Каток' + S)
    a('/api/activities/' + aid, {}, 'DELETE')

# reminders go to people who joined; times are UTC because the server runs with TZ=UTC
def remind_case(hours_ahead):
    when = datetime.now(timezone.utc) + timedelta(hours=hours_ahead)
    title = f'rem{hours_ahead}{S}'
    t('/api/activities', {'date': when.date().isoformat(), 'time': when.strftime('%H:%M'), 'title': title})
    t('/api/going/' + next(x['id'] for x in t('/api/state')[1]['acts'] if x['title'] == title), {})
    return title


def reminders(title): return [p['text'] for m, p in sent if m == 'sendMessage' and p['chat_id'] == 111 and title in p['text'] and 'Напоминание' in p['text']]


soon, later, far = remind_case(0.5), remind_case(5), remind_case(30)
assert wait(lambda: reminders(soon) and reminders(later)), 'due reminders are sent'
time.sleep(2.5)  # a few more reminder checks
m = re.search(r'через (\d+) мин', reminders(soon)[0])
assert len(reminders(soon)) == 1 and m and 25 <= int(m[1]) <= 30, reminders(soon)
assert len(reminders(later)) == 1 and 'через 5 ч' in reminders(later)[0], 'only the closest threshold, real time left'
assert not reminders(far), 'nothing 30 h ahead'

# moving an activity: people going are told, and reminders follow the new time
far_act = next(x for x in t('/api/state')[1]['acts'] if x['title'] == far)
l('/api/going/' + far_act['id'], {})
soon_at = datetime.now(timezone.utc) + timedelta(minutes=40)
t('/api/activities/' + far_act['id'], {**far_act, 'date': soon_at.date().isoformat(), 'time': soon_at.strftime('%H:%M')})
assert wait(lambda: bot_said(222, 'Изменено') and bot_said(222, far)), 'people going are told about the change'
assert not bot_said(111, 'Изменено'), 'the editor is not notified'
assert wait(lambda: len(reminders(far)) == 1), 'reminder rescheduled to the new time'
print('OK telegram')
