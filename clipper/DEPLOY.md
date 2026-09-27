# Deploying Clipper on a Hostinger VPS

End result: `https://clips.yourdomain.com`, password-protected, HTTPS, restarts
on its own after crashes and reboots.

**Sizing:** 2 vCPU / 8 GB RAM (Hostinger KVM 2) handles it; expect a 10-minute
video to take several minutes. On 1 vCPU set `CLIP_WHISPER_MODEL=base`. The image
is ~2.5 GB, and uploads + clips live on disk for `CLIP_KEEP_HOURS` (default 24).

## 1. Point a subdomain at the VPS

hPanel → **Domains** → your domain → **DNS / Nameservers** → add a record:

| Type | Name | Points to | TTL |
| --- | --- | --- | --- |
| A | `clips` | your VPS IP (hPanel → VPS → Overview) | 300 |

Check it before moving on (can take a few minutes):

```bash
ping clips.yourdomain.com     # should show your VPS IP
```

## 2. SSH in and get the code

hPanel → **VPS** → **Overview** shows the SSH command and root password.

```bash
ssh root@YOUR_VPS_IP

# Docker is already there on Hostinger's Docker/n8n templates. If `docker --version` fails:
curl -fsSL https://get.docker.com | sh

git clone -b claude/opusclip-replica-30sec-ylkjf0 https://github.com/connerquarles/loganlynch.git
cd loganlynch/clipper
```

If the repo is private, `git clone` asks for a username and password: use your
GitHub username and a [personal access token](https://github.com/settings/tokens)
(fine-grained, read-only "Contents" on this repo) as the password.

## 3. Fill in the settings

```bash
cp .env.example .env
nano .env        # set CLIP_DOMAIN, CLIP_PASSWORD, ANTHROPIC_API_KEY; Ctrl+O, Enter, Ctrl+X
chmod 600 .env
```

## 4. Start it — pick the one that matches your VPS

First check whether something already owns ports 80/443 (your n8n setup, usually):

```bash
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Ports}}' | grep -E ':80->|:443->'
```

### A. Nothing printed → fresh VPS

```bash
docker compose up -d --build
```

Caddy gets the HTTPS certificate automatically on first visit.

### B. A `traefik` container printed → Hostinger's n8n template (or any Traefik setup)

Don't use option A: its Caddy would fight Traefik for ports 80/443 and knock n8n
offline. Instead, plug Clipper into your existing Traefik:

```bash
# Which network is traefik on? Put that name in .env as TRAEFIK_NETWORK
docker inspect traefik --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}'

# Which cert resolver does it use? Put it in .env as TRAEFIK_CERTRESOLVER
docker inspect traefik --format '{{join .Args "\n"}}' | grep -o 'certificatesresolvers\.[^.]*' | sort -u
# (and the https entrypoint name, usually "websecure", as TRAEFIK_ENTRYPOINT)
docker inspect traefik --format '{{join .Args "\n"}}' | grep -o 'entrypoints\.[^.]*' | sort -u

docker compose -f docker-compose.traefik.yml up -d --build
```

If your traefik container has a different name, swap it into those commands
(`docker ps` shows it).

The first build takes 5–15 minutes (it downloads the speech model into the image).

## 5. Open it

Go to `https://clips.yourdomain.com`. The browser asks for a login: **any
username**, and the `CLIP_PASSWORD` from `.env`.

## Day-to-day

```bash
cd ~/loganlynch/clipper
docker compose logs -f clipper            # watch it work / debug errors
docker compose restart clipper            # after changing .env
git pull && docker compose up -d --build  # update to the latest code
```

(Add `-f docker-compose.traefik.yml` after `docker compose` if you used option B.)

## Troubleshooting

- **Browser says "not secure" / certificate error for a few minutes:** the
  certificate is issued on first visit; DNS has to point at the VPS first.
  `docker compose logs caddy` (or `docker logs traefik`) shows why.
- **404 from Traefik (option B):** `TRAEFIK_NETWORK` is wrong, so Traefik can't
  reach the container. Re-check step 4B.
- **Upload fails on big files:** raise `CLIP_MAX_MB` in `.env` and restart.
- **Scores say "audio heuristic":** `ANTHROPIC_API_KEY` is missing or invalid;
  `docker compose logs clipper | grep Claude` shows the error.
- **Out of disk:** lower `CLIP_KEEP_HOURS`, then `docker system prune` to drop
  old image layers from previous builds.
