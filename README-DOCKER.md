# Pitching Hub — Docker Deployment Guide

Deploy the full Pitching Hub stack (Node.js server + Python CrewAI pipeline) on any Linux server using Docker.

---

## Prerequisites

| Requirement | Minimum |
|---|---|
| Docker Engine | 20.10+ |
| Docker Compose | v2.0+ (included with Docker Desktop and modern Docker Engine installs) |
| RAM | 4 GB (CrewAI pipeline is memory-intensive) |
| Disk | 5 GB (for Docker image + data) |
| Anthropic API Key | Required for the Scout Agent pipeline and school resolution |

---

## Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/bjkolly/pitching-hub.git
cd pitching-hub

# 2. Create your environment file
cp .env.docker.example .env.docker
nano .env.docker   # <-- edit with your values

# 3. Build and launch
docker compose up -d --build
```

The app is now running at **http://localhost:3001**.

---

## Configuration

Edit `.env.docker` before launching. All variables:

| Variable | Required | Default | Description |
|---|---|---|---|
| `PORT` | No | `3001` | Server port (host and container) |
| `JWT_SECRET` | **Yes** | `change-me...` | Secret for signing JWT tokens. Use a random 64-char string. |
| `ADMIN_USER` | **Yes** | `admin` | Admin username for login |
| `ADMIN_PASS` | **Yes** | `changeme` | Admin password for login |
| `ANTHROPIC_API_KEY` | **Yes**\* | _(empty)_ | Anthropic API key for CrewAI pipeline and school resolution |
| `TRACKMAN_API_KEY` | No | _(empty)_ | Trackman API key (leave blank for demo/mock mode) |
| `TRACKMAN_TEAM_ID` | No | _(empty)_ | Trackman team ID |
| `TRACKMAN_BASE_URL` | No | `https://staging-api.trackmanbaseball.com/v1` | Trackman API base URL |

\* The server runs without `ANTHROPIC_API_KEY`, but the Scout Agent pipeline and school import features will not work.

**Generate a secure JWT secret:**
```bash
openssl rand -hex 32
```

---

## Building

Build the Docker image:
```bash
docker compose build
```

The multi-stage build:
1. **Stage 1** — Installs Python 3.13 packages into a virtual environment
2. **Stage 2** — Installs Node.js production dependencies
3. **Stage 3** — Combines both into a slim runtime image with Node 18 + Python 3.13

First build takes 3-5 minutes. Subsequent builds use Docker layer caching and are much faster.

### Cross-Platform Builds

If building on Apple Silicon (ARM) for an x86 EC2 instance:
```bash
docker buildx build --platform linux/amd64 -t pitching-hub .
```

Or simply build directly on the EC2 instance (recommended).

---

## Running

```bash
# Start in background
docker compose up -d

# View logs
docker compose logs -f

# Stop
docker compose down

# Restart
docker compose restart
```

### Health Check

The container includes an automatic health check. Verify manually:
```bash
curl http://localhost:3001/api/health
```

---

## Data Persistence

All runtime data is stored in a Docker named volume (`pitching-data`) mounted at `/app/data`:

| File | Purpose |
|---|---|
| `users.json` | User accounts (admin + created users) |
| `session.json` | Current pitcher session data |
| `crew_session.json` | CrewAI pipeline output |
| `teams/` | Imported team data and manifest |
| `school_registry.json` | NCAA school lookup data |
| `cache/` | Web scraping cache |

**Data survives** `docker compose down` and `docker compose up`. The volume is only removed if you explicitly delete it.

### Backup

```bash
# Create a backup tarball
docker run --rm -v pitching-hub_pitching-data:/data -v $(pwd):/backup \
  alpine tar czf /backup/pitching-data-backup.tar.gz -C /data .
```

### Restore

```bash
# Restore from backup
docker run --rm -v pitching-hub_pitching-data:/data -v $(pwd):/backup \
  alpine sh -c "cd /data && tar xzf /backup/pitching-data-backup.tar.gz"
```

### Reset Data

To start fresh (removes all user data, sessions, imported teams):
```bash
docker compose down
docker volume rm pitching-hub_pitching-data
docker compose up -d
```

---

## Updating

Pull the latest code and rebuild:
```bash
git pull origin main
docker compose up -d --build
```

Your data volume is preserved. Only application code is updated.

---

## EC2 Deployment (Step by Step)

### 1. Launch an EC2 Instance

- **AMI:** Amazon Linux 2023 or Ubuntu 22.04
- **Instance type:** `t3.medium` (2 vCPU, 4 GB RAM) minimum
- **Storage:** 20 GB gp3
- **Security group:** Allow inbound TCP on port 3001 (or 80/443 if using a reverse proxy)

### 2. Connect and Install Docker

**Amazon Linux 2023:**
```bash
sudo dnf update -y
sudo dnf install -y docker git
sudo systemctl enable docker && sudo systemctl start docker
sudo usermod -aG docker $USER

# Install Docker Compose plugin
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m) \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# Re-login to pick up docker group
exit
```

**Ubuntu 22.04:**
```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git
sudo systemctl enable docker && sudo systemctl start docker
sudo usermod -aG docker $USER
exit
```

### 3. Clone and Configure

```bash
# SSH back in
ssh -i your-key.pem ec2-user@<your-ec2-ip>

git clone https://github.com/bjkolly/pitching-hub.git
cd pitching-hub

cp .env.docker.example .env.docker
nano .env.docker
```

Set at minimum:
- `JWT_SECRET` — run `openssl rand -hex 32` to generate
- `ADMIN_USER` / `ADMIN_PASS` — your login credentials
- `ANTHROPIC_API_KEY` — your Anthropic API key

### 4. Build and Launch

```bash
docker compose up -d --build
```

First build takes 3-5 minutes. Watch progress:
```bash
docker compose logs -f
```

### 5. Verify

```bash
# Check container health
docker compose ps

# Test the endpoint
curl http://localhost:3001/api/health
```

Open in browser: `http://<your-ec2-ip>:3001`

### 6. (Optional) Set Up HTTPS with Nginx

For production, put Nginx in front with SSL:

```bash
sudo dnf install -y nginx certbot python3-certbot-nginx  # Amazon Linux
# or
sudo apt install -y nginx certbot python3-certbot-nginx   # Ubuntu
```

Nginx config (`/etc/nginx/conf.d/pitching-hub.conf`):
```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:3001;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;  # Long timeout for SSE streaming
    }
}
```

Then enable HTTPS:
```bash
sudo certbot --nginx -d your-domain.com
```

---

## Troubleshooting

### Container won't start
```bash
docker compose logs
```
Common causes:
- Missing `.env.docker` file — copy from `.env.docker.example`
- Port 3001 already in use — change `PORT` in `.env.docker`

### Scout pipeline fails
- Verify `ANTHROPIC_API_KEY` is set in `.env.docker`
- Check the container has outbound HTTPS access (API calls to anthropic.com, Baseball Cube, NCAA sites)
- Monitor memory: `docker stats pitching-hub` (pipeline needs ~2-3 GB)

### Out of memory during pipeline
- Upgrade to a larger instance (t3.large = 8 GB RAM)
- Or increase the memory limit in `docker-compose.yml` under `deploy.resources.limits.memory`

### Data not persisting
- Ensure you're using `docker compose down` (not `docker compose down -v` which removes volumes)
- Check volume exists: `docker volume ls | grep pitching`

### Permission denied on data files
```bash
docker compose exec pitching-hub ls -la /app/data/
```
The container runs as root by default, so this should not be an issue.

---

## Security Notes

Before exposing the server publicly:

1. **Change all default credentials** — `JWT_SECRET`, `ADMIN_USER`, `ADMIN_PASS`
2. **Use HTTPS** — Set up Nginx + Let's Encrypt (see above)
3. **Firewall** — Only expose port 80/443 (via Nginx), not 3001 directly
4. **Keep `.env.docker` private** — It is in `.gitignore` and should never be committed
5. **Rotate JWT secret** — Changing `JWT_SECRET` invalidates all active sessions

---

## Architecture

```
                    Docker Container
                    ┌────────────────────────────┐
  Port 3001 ───────►│  Node.js 18 (Express)      │
                    │    ├── REST API             │
                    │    ├── Static files (client/)│
                    │    └── SSE streaming        │
                    │                              │
                    │  Python 3.13 (CrewAI)       │
                    │    └── Spawned on-demand     │
                    │       by Node child_process  │
                    │                              │
                    │  /app/data/ ◄──── Volume ────┼── pitching-data
                    └────────────────────────────┘
```
