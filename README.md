# JaredCodingAgent 🤖

Polling daemon that watches GitHub for issues labeled **"Ready"** and automatically runs Claude Code to implement them.

## How it works

```
You label issue → "Ready"
  → daemon polls GitHub every 60s
  → picks up the issue
  → labels it "In Progress"
  → runs Claude Code with the issue body
  → Claude Code implements + opens PR
  → labels it "Review"
```

## Stack

- Python polling daemon
- Claude Code CLI
- Docker container (runs on Jared-server)

## Setup

```bash
docker-compose up -d jared-coding-agent
```

## Environment variables

```
GITHUB_TOKEN=
ANTHROPIC_API_KEY=
GITHUB_REPO=YourJared/WebJared
POLL_INTERVAL_SECONDS=60
```
