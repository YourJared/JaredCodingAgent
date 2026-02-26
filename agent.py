#!/usr/bin/env python3
"""
JaredCodingAgent - Polling daemon
Watches GitHub for issues labeled "Ready", runs Claude Code to implement them.
"""

import os
import time
import subprocess
import logging
import requests

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
log = logging.getLogger(__name__)

# Config from environment
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GITHUB_REPO = os.environ.get("GITHUB_REPO", "YourJared/WebJared")
WORKSPACE = os.environ.get("WORKSPACE", "/workspace")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))

HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json"
}

REPO_DIR = f"{WORKSPACE}/{GITHUB_REPO.split('/')[-1]}"


def get_ready_issues():
    url = f"https://api.github.com/repos/{GITHUB_REPO}/issues"
    resp = requests.get(url, headers=HEADERS, params={"labels": "Ready", "state": "open"})
    resp.raise_for_status()
    return resp.json()


def set_label(issue_number, remove_label, add_label):
    base = f"https://api.github.com/repos/{GITHUB_REPO}/issues/{issue_number}"

    # Remove old label
    requests.delete(f"{base}/labels/{remove_label}", headers=HEADERS)

    # Add new label
    requests.post(f"{base}/labels", headers=HEADERS, json={"labels": [add_label]})


def run_claude_code(issue):
    number = issue["number"]
    title = issue["title"]
    body = issue["body"] or ""

    prompt = f"""You are working on the {GITHUB_REPO} repository.
Implement the following GitHub issue completely, then create a pull request.

Issue #{number}: {title}

{body}

Instructions:
- Make all necessary code changes
- Follow existing code patterns and conventions
- Create a PR with a clear description referencing issue #{number}
- Branch name: fix/issue-{number}
"""

    log.info(f"Running Claude Code for issue #{number}: {title}")

    result = subprocess.run(
        ["claude", "--print", prompt],
        cwd=REPO_DIR,
        env={**os.environ, "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY},
        capture_output=True,
        text=True,
        timeout=600  # 10 min max per issue
    )

    if result.returncode != 0:
        log.error(f"Claude Code failed for #{number}:\n{result.stderr}")
        return False

    log.info(f"Claude Code completed for #{number}")
    return True


def add_comment(issue_number, message):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/issues/{issue_number}/comments"
    requests.post(url, headers=HEADERS, json={"body": message})


def ensure_repo_up_to_date():
    subprocess.run(["git", "pull", "origin", "main"], cwd=REPO_DIR, check=True)


def main():
    log.info(f"JaredCodingAgent started. Watching {GITHUB_REPO} every {POLL_INTERVAL}s")
    log.info(f"Workspace: {REPO_DIR}")

    while True:
        try:
            issues = get_ready_issues()

            if issues:
                log.info(f"Found {len(issues)} Ready issue(s)")

            for issue in issues:
                number = issue["number"]
                title = issue["title"]

                log.info(f"Picking up issue #{number}: {title}")

                # Mark as In Progress immediately
                set_label(number, "Ready", "In Progress")
                add_comment(number, "🤖 **Jared Coding Agent** picked up this issue and is working on it...")

                # Pull latest before making changes
                ensure_repo_up_to_date()

                # Run Claude Code
                success = run_claude_code(issue)

                if success:
                    set_label(number, "In Progress", "Review")
                    add_comment(number, "✅ **Jared Coding Agent** completed implementation. PR opened for review.")
                else:
                    set_label(number, "In Progress", "Failed")
                    add_comment(number, "❌ **Jared Coding Agent** encountered an error. Check container logs.")

        except Exception as e:
            log.error(f"Poll cycle error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
