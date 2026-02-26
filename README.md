# JaredCodingAgent 🤖

Polling daemon that watches GitHub for issues labeled **"Ready"** and automatically runs Claude Code to implement them.

## How it works

```
You label issue → "Ready"
  → daemon polls GitHub every 60s
  → picks up the issue
  → labels it "In Progress"
  → SSHes into Jared-server host
  → runs Claude Code (authenticated via Max subscription)
  → Claude Code implements + opens PR
  → labels it "Review"
```

## Current architecture (temporary)

> ⚠️ **This is a temporary setup.** Claude Code runs on the **host machine** via SSH because it is authenticated through a Max subscription browser session which cannot run headless inside a container.
>
> **TODO:** Migrate to Anthropic API key authentication so Claude Code runs fully inside the container with no SSH dependency. See `ROADMAP.md`.

The container handles only polling and orchestration. The actual Claude Code execution is delegated to the host via SSH.

## Stack

- Python polling daemon (container)
- Claude Code CLI (host, Max subscription)
- SSH for delegation
- Docker container on Jared-server

## Setup

### 1. Allow SSH from container to host

On Jared-server, add the container's SSH key to authorized keys:
```bash
# Generate key inside container (or use existing)
ssh-keygen -t ed25519 -C "jared-coding-agent"
cat ~/.ssh/id_ed25519.pub >> ~/.ssh/authorized_keys
```

### 2. Configure `.env`

```env
GITHUB_TOKEN=your_github_token
GITHUB_REPO=YourJared/WebJared
HOST_USER=piotr
HOST_IP=host.docker.internal
REPO_PATH=/home/piotr/projects/WebJared
POLL_INTERVAL_SECONDS=60
```

### 3. Start

```bash
docker-compose up -d jared-coding-agent
```

### 4. Watch logs

```bash
docker logs -f jared-coding-agent
```

## GitHub labels required

Create these labels in `YourJared/WebJared`:
- `Ready` — you apply this to trigger the agent
- `In Progress` — applied automatically when agent picks up
- `Review` — applied when PR is opened
- `Failed` — applied if Claude Code errors

## Roadmap

See `ROADMAP.md` for the migration path to full API key auth.
