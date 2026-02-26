# Roadmap

## Current state: SSH delegation (temporary)

Claude Code runs on the host machine via SSH because it authenticates through a
Max subscription browser session that cannot run headless in a container.

## Target state: Full API key auth

Replace SSH delegation with direct Claude Code execution inside the container:

1. Get an Anthropic API key from console.anthropic.com
2. Set a monthly spend limit
3. Add `ANTHROPIC_API_KEY` to `.env`
4. Remove SSH logic from `agent.py` — run `claude --print` directly
5. Remove SSH volume mounts from `docker-compose.yml`
6. Remove host repo volume mount — clone fresh inside container instead

The `agent.py` already has the direct execution path commented out and ready to restore.
