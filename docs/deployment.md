# Deploying to a Linux server

A single VPS runs the whole stack: Docker Compose for the app, Caddy in front for HTTPS, and a
firewall that leaves only ports 80 and 443 open. Speech and summarisation are hosted APIs (Deepgram
and Ollama Cloud in this guide), so the server needs no GPU: 2 vCPUs, 4 GB of RAM and enough disk
for the recordings you keep are plenty.

```
Internet ──443──► Caddy (TLS) ──► 127.0.0.1:5000 web ──► redis ──► worker ──► Deepgram, Ollama Cloud
                                                    └──────── app_data volume ────────┘
```

---

## 1. Docker

On Ubuntu 22.04 or 24.04, install Docker Engine with the Compose plugin from
[docs.docker.com/engine/install/ubuntu](https://docs.docker.com/engine/install/ubuntu/), then make
sure it starts at boot:

```bash
sudo systemctl enable --now docker
docker compose version          # v2.x
```

---

## 2. The app

```bash
sudo git clone https://github.com/inboxpraveen/LLM-Minutes-of-Meeting.git /opt/mom-ai
cd /opt/mom-ai
sudo tee .env > /dev/null <<'EOF'
# Only Caddy on this machine may reach the app.
APP_PORT=127.0.0.1:5000
# Trust X-Forwarded-* from Caddy, and send the session cookie over HTTPS only.
BEHIND_PROXY=true
SESSION_COOKIE_SECURE=true
WORKER_CONCURRENCY=2
EOF
sudo chmod 600 .env
sudo docker compose up -d --build --wait
curl -fsS http://127.0.0.1:5000/readyz
```

`APP_PORT` takes Docker's `address:port` form. Binding to `127.0.0.1` matters because ports
published by Docker bypass `ufw`: a bare `5000` would be reachable from the internet whatever the
firewall says. If you use the optional Celery dashboard, bind it the same way with
`FLOWER_PORT=127.0.0.1:5555`. Redis is never published.

**Restart policies.** Every service has `restart: unless-stopped`, and Docker starts at boot, so the
stack comes back after a crash or a reboot without a systemd unit of its own. `docker compose stop`
keeps it stopped until you start it again.

### Create the administrator before anyone else can

The first person to open the app becomes its administrator. Do it before the site is public, over
an SSH tunnel from your own computer:

```bash
ssh -N -L 5000:127.0.0.1:5000 you@your-server     # then open http://localhost:5000
```

Then go to **Settings**:

| Tab | Field | Value |
| --- | --- | --- |
| LLM provider | Preset | **Ollama Cloud** (`https://ollama.com/v1`, model `gemma4:31b`) |
| LLM provider | API key | The whole key from [ollama.com/settings/keys](https://ollama.com/settings/keys) |
| Speech provider | Active provider | Deepgram, with your key from [console.deepgram.com](https://console.deepgram.com/) |
| Speech provider | Speaker diarization | On |

Press **Test** on both tabs, then **Save**. Keys are stored encrypted in the `app_data` volume.

---

## 3. HTTPS with Caddy

Point a DNS `A` record (say `minutes.example.com`) at the server, then install Caddy from its
[official package repository](https://caddyserver.com/docs/install#debian-ubuntu-raspbian) and
replace `/etc/caddy/Caddyfile` with:

```caddyfile
minutes.example.com {
	encode gzip

	# Monitoring endpoints stay internal: scrape them on 127.0.0.1:5000.
	@internal path /metrics /readyz
	respond @internal 404

	# Keep at or above Settings → Storage → Maximum upload size.
	request_body {
		max_size 2GB
	}

	reverse_proxy 127.0.0.1:5000
}
```

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Caddy obtains and renews the certificate itself and redirects HTTP to HTTPS.

### Or nginx

With certbot for the certificate (`sudo certbot --nginx -d minutes.example.com`):

```nginx
server {
    listen 443 ssl;
    server_name minutes.example.com;
    # ssl_certificate lines are added by certbot

    location ~ ^/(metrics|readyz)$ { return 404; }

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        client_max_body_size 2048m;
        proxy_request_buffering off;
        proxy_read_timeout   3600s;
        proxy_send_timeout   3600s;
    }
}

server {
    listen 80;
    server_name minutes.example.com;
    return 301 https://$host$request_uri;
}
```

---

## 4. Firewall

Allow SSH first, or you will lock yourself out:

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status verbose
```

Outgoing HTTPS has to stay open: the worker calls `api.deepgram.com` and `ollama.com`. From a
machine elsewhere, `nc -zv your-server 5000` and `nc -zv your-server 6379` should both be refused.

---

## 5. Backups

Everything the app stores is in the `app_data` volume: the SQLite database, `instance.key` (which
decrypts the stored API keys), uploaded recordings and logs. The script below takes a consistent
copy of the database while the app keeps running, then archives the volume.

`/usr/local/bin/mom-ai-backup`:

```bash
#!/usr/bin/env bash
# Nightly backup of the app_data volume. Keeps 14 days.
set -euo pipefail
cd /opt/mom-ai
dest=/var/backups/mom-ai
mkdir -p "$dest" && chmod 700 "$dest"
stamp=$(date +%Y%m%d-%H%M%S)

# sqlite3's online backup: a consistent copy even while a job is writing.
docker compose exec -T web python -c \
  "import sqlite3; s = sqlite3.connect('/data/mom.db'); d = sqlite3.connect('/data/mom-backup.db'); s.backup(d); d.close()"

# The live database files are left out; mom-backup.db is the consistent copy.
docker run --rm -v mom-ai_app_data:/data:ro -v "$dest":/backup alpine:3.20 \
  tar czf "/backup/mom-ai-$stamp.tar.gz" -C /data --exclude=./mom.db --exclude=./mom.db-wal \
  --exclude=./mom.db-shm .
docker compose exec -T web rm -f /data/mom-backup.db

chmod 600 "$dest/mom-ai-$stamp.tar.gz"
find "$dest" -name 'mom-ai-*.tar.gz' -mtime +14 -delete
```

```bash
sudo chmod 700 /usr/local/bin/mom-ai-backup
echo '30 2 * * * root /usr/local/bin/mom-ai-backup' | sudo tee /etc/cron.d/mom-ai-backup
```

A backup contains `instance.key` next to the keys it decrypts: treat it as a secret, and copy it
off the server encrypted (for example with `restic` or `age`). Add `--exclude=./uploads` to the `tar`
line if you only need the minutes and transcripts, which are in the database.

To restore, stop the app, empty the volume and unpack the archive into it, renaming the database
copy back:

```bash
cd /opt/mom-ai && sudo docker compose down
sudo docker run --rm -v mom-ai_app_data:/data -v /var/backups/mom-ai:/backup:ro alpine:3.20 sh -c \
  'rm -rf /data/* && tar xzf /backup/mom-ai-YYYYMMDD-HHMMSS.tar.gz -C /data \
   && mv /data/mom-backup.db /data/mom.db && chown -R 999:999 /data'
sudo docker compose up -d --wait
```

The `chown` matches the `app` user inside the image; check it with
`docker compose exec web id -u` if you have changed the Dockerfile.

---

## 6. Secrets

Nothing secret belongs in git, and nothing secret has to go there:

- API keys are entered in **Settings** and stored encrypted in the database, never in a file in the
  repository.
- `.env` holds only ports and switches, and `.gitignore` excludes it along with `.env.*`, `data/`,
  `instance/`, `*.db` and `*.log`.
- `instance.key` and the database live in the `app_data` volume, outside the checkout.
- Before every commit on a server checkout, `git status` should show no `.env`, database or backup
  file. `git check-ignore -v .env` confirms which rule covers it.

To rotate a key, paste the new one in **Settings** and press **Test**; the next job uses it. A key
that has been pasted into a chat, a ticket or a log should be treated as public and rotated.

---

## 7. Operating it

```bash
cd /opt/mom-ai
sudo docker compose ps
sudo docker compose logs -f web worker
sudo mom-ai-backup && sudo git pull && sudo docker compose up -d --build --wait   # upgrade
```

`/healthz` answers whenever the web process is up; `/readyz` also checks the database, the queue and
ffmpeg. **Admin → System** shows the same checks, job counts and the end of the log. More in
[operations.md](operations.md).
