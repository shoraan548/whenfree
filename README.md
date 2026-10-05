# Plany — plans with friends

A small self-hosted calendar for planning activities with friends:

- everyone adds activities and signs up for them ("Я иду"); you can delete only your own, admins can delete any;
- when several activities land on the same day, friends vote: one of them, or "both, we'll split up";
- month and week views, works well on phones;
- optional Telegram bot: log in with Telegram, get notified about new activities, reminders 24 h / 6 h / 1 h before the start;
- accounts with passwords, admins can grant admin rights and reset passwords.

Python 3 (standard library only) + SQLite in one small container. The UI is in Russian.

## Quick start

You need Docker with the Compose plugin.

```bash
git clone <this repo> plany && cd plany
cp .env.example .env    # then edit .env, see below
docker compose up -d --build
```

Open http://localhost:8090 (or `http://<server-ip>:8090`). **The first user to register becomes admin.**

Update to a newer version: `git pull && docker compose up -d --build`. Your data is kept.

## Configuration

Everything is set in `.env`; [.env.example](.env.example) has every option with comments. All are optional.

| Setting | What it does | Default |
|---|---|---|
| `COMPOSE_PROFILES` | Extras to run: `tailscale`, `domain` (comma-separated) | none: app only |
| `HTTP_PORT` | Port the app listens on. Use `127.0.0.1:8090` on a public server | `8090` |
| `TZ` | Timezone of activity times, used for reminders | `Europe/Moscow` |
| `BOT_TOKEN` | Telegram bot token, turns on Telegram login, notifications and reminders | off |
| `SITE_URL` | Public address, added as a link to Telegram messages | none |
| `DOMAIN` | Your domain (profile `domain`) | — |
| `TS_AUTHKEY`, `TS_HOSTNAME` | Tailscale key and link name (profile `tailscale`) | —, `plany` |

After changing `.env`, run `docker compose up -d --build` again.

## Making it reachable from the internet

Pick one. Without either, the app is available on your local network only.

### Option A: your own domain (server or cloud with a public IP)

1. Point the domain's DNS `A` (and/or `AAAA`) record to the server's IP.
2. Open ports 80 and 443 on the server / cloud firewall.
3. In `.env`:
   ```
   COMPOSE_PROFILES=domain
   DOMAIN=plans.example.com
   SITE_URL=https://plans.example.com
   HTTP_PORT=127.0.0.1:8090
   ```
4. `docker compose up -d --build`. [Caddy](https://caddyserver.com) gets and renews a free Let's Encrypt certificate automatically.

### Option B: Tailscale Funnel (no domain, no open ports, e.g. a home server)

Free permanent link `https://<TS_HOSTNAME>.<tailnet>.ts.net`; visitors don't need Tailscale.

1. Sign up at https://tailscale.com (free Personal plan).
2. Admin console → **DNS** → enable **HTTPS Certificates**.
3. **Settings → Keys → Generate auth key**.
4. In `.env`: `COMPOSE_PROFILES=tailscale` and `TS_AUTHKEY=<key>` (needed on first start only); optionally `TS_HOSTNAME`.
5. `docker compose up -d --build`
6. Admin console → **Machines** → your machine → **Disable key expiry** (otherwise it drops off after 180 days).
7. Your link: `docker compose exec tailscale tailscale funnel status`. Put it into `SITE_URL`.

If Funnel isn't allowed in your tailnet, add this to `nodeAttrs` in **Access controls**: `{"target": ["autogroup:member"], "attr": ["funnel"]}`.

## Telegram bot (optional, free)

One bot handles login, notifications and reminders, the same way on Android, iOS and any browser.

1. In Telegram, message [@BotFather](https://t.me/BotFather) → `/newbot` → pick a name → copy the token.
2. In `.env`: `BOT_TOKEN=<token>`, and `SITE_URL` so messages link to your site.
3. `docker compose up -d --build`. A "Войти через Telegram" button appears on the login screen.

How it works:
- **Login:** the site gives a one-time link → the bot asks "Войти на сайт?" → the user taps "Да, это я" → the browser is logged in. The confirmation step stops someone from logging in as you by sending you their link. An account is created on first login; it can get a password later in the ☰ menu.
- **Linking:** existing users link Telegram in the ☰ menu.
- **Notifications:** when someone adds an activity, everyone else with Telegram linked gets a message (with a nudge to vote if the day already has plans). Admins can message everyone from the ☰ menu.
- **Reminders:** 24 h, 6 h and 1 h before an activity, to everyone who tapped "Я иду". If one was missed (server down, or the person joined late), only the closest is sent, with the real time left. An activity without a time counts as 09:00, in the `TZ` timezone.
- The bot polls Telegram itself (no webhook), so it works behind NAT and on localhost.
- Users without Telegram use the app as before, just without notifications.

## Data and backups

The database is a single SQLite file in the docker volume `plany_data`.

```bash
docker compose cp app:/data/app.db ./backup.db     # backup
docker compose cp ./backup.db app:/data/app.db && docker compose restart app   # restore
```

`docker compose down -v` deletes all data.

## Development

- Without Docker: `python server.py` → http://localhost:8080 (data in `app.db`; on Windows, if it fails with a timezone error: `pip install tzdata`).
- Code: [server.py](server.py) (HTTP API, SQLite, Telegram bot, reminders) and [index.html](index.html) (the whole UI).
- Tests run in a container with a temporary DB; Telegram is checked against a fake Bot API built into [test.py](test.py):

```bash
docker build -t plany-test . && docker run --rm -e DB=/tmp/t.db -e BOT_TOKEN=test -e TG_API=http://127.0.0.1:8099 -e TZ=UTC -e REMIND_EVERY=1 -v "$PWD/test.py:/app/test.py:ro" plany-test sh -c 'python server.py & python test.py'
```

## License

MIT, see [LICENSE](LICENSE).
