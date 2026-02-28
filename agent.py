#!/usr/bin/env python3
"""
JaredCodingAgent - Polling daemon
Watches GitHub Projects V2 for issues with Status="Ready",
then delegates to Claude Code running on the host via SSH.

NOTE: Temporary SSH delegation because Claude Code authenticates via
Max subscription browser session. TODO: migrate to API key auth.
See ROADMAP.md.
"""

import os
import re
import sys
import time
import base64
import subprocess
import logging
from datetime import datetime, timezone, timedelta
import requests

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

# Config
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO = os.environ.get("GITHUB_REPO", "YourJared/WebJared")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
HOST_USER = os.environ.get("HOST_USER", "juchas")
HOST_IP = os.environ.get("HOST_IP", "172.22.0.1")
REPO_PATH = os.environ.get("REPO_PATH", "/opt/jared/repos/JaredAPIs/WebJared")

# WebJared Project IDs (from GraphQL introspection)
PROJECT_ID = "PVT_kwDOC5s1AM4BO9Pf"
STATUS_FIELD_ID = "PVTSSF_lADOC5s1AM4BO9Pfzg9gcHw"
STATUS_READY = "61e4505c"
STATUS_IN_PROGRESS = "47fc9ee4"
STATUS_IN_REVIEW = "df73e18b"

GRAPHQL_URL = "https://api.github.com/graphql"
HEADERS = {
    "Authorization": f"bearer {GITHUB_TOKEN}",
    "Content-Type": "application/json"
}

processed = set()


def graphql(query, variables=None):
    resp = requests.post(
        GRAPHQL_URL,
        headers=HEADERS,
        json={"query": query, "variables": variables or {}}
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise Exception(f"GraphQL errors: {data['errors']}")
    return data["data"]


def get_ready_items():
    query = """
    query($projectId: ID!) {
      node(id: $projectId) {
        ... on ProjectV2 {
          items(first: 50) {
            nodes {
              id
              fieldValues(first: 10) {
                nodes {
                  ... on ProjectV2ItemFieldSingleSelectValue {
                    field { ... on ProjectV2SingleSelectField { id name } }
                    optionId
                    name
                  }
                }
              }
              content {
                ... on Issue {
                  number
                  title
                  body
                }
              }
            }
          }
        }
      }
    }
    """
    data = graphql(query, {"projectId": PROJECT_ID})
    items = data["node"]["items"]["nodes"]

    ready = []
    for item in items:
        for fv in item["fieldValues"]["nodes"]:
            if fv.get("field", {}).get("id") == STATUS_FIELD_ID and fv.get("optionId") == STATUS_READY:
                content = item.get("content")
                if content and "number" in content:
                    ready.append({
                        "item_id": item["id"],
                        "issue_number": content["number"],
                        "title": content["title"],
                        "body": content.get("body") or ""
                    })
    return ready


def set_status(item_id, option_id):
    mutation = """
    mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
      updateProjectV2ItemFieldValue(input: {
        projectId: $projectId
        itemId: $itemId
        fieldId: $fieldId
        value: { singleSelectOptionId: $optionId }
      }) {
        projectV2Item { id }
      }
    }
    """
    graphql(mutation, {
        "projectId": PROJECT_ID,
        "itemId": item_id,
        "fieldId": STATUS_FIELD_ID,
        "optionId": option_id
    })


def add_comment(issue_number, message):
    owner, repo = GITHUB_REPO.split("/")
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/comments"
    requests.post(
        url,
        headers={"Authorization": f"token {GITHUB_TOKEN}"},
        json={"body": message}
    )


def run_claude_code_on_host(issue_number, title, body):
    prompt = f"""You are working on the {GITHUB_REPO} repository.
Implement the following GitHub issue completely, then create a pull request.

Issue #{issue_number}: {title}

{body}

Instructions:
- Make all necessary code changes
- Follow existing code patterns and conventions
- Create a PR with a clear description referencing issue #{issue_number}
- Branch name: fix/issue-{issue_number}
"""
    escaped = prompt.replace("'", "'\\''")
    ssh_cmd = [
        "ssh", "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        f"{HOST_USER}@{HOST_IP}",
        f"cd {REPO_PATH} && git checkout main && git pull --rebase origin main && /home/{HOST_USER}/.local/bin/claude --print --permission-mode bypassPermissions '{escaped}'"
    ]

    log.info(f"SSHing to host to run Claude Code for issue #{issue_number}")
    result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=600)

    if result.returncode != 0:
        log.error(f"Claude Code failed (exit {result.returncode}):\n{result.stderr}")
        if result.stdout:
            log.error(f"stdout:\n{result.stdout[-2000:]}")
        return False, None

    # Extract PR URL from output
    pr_url = None
    if result.stdout:
        match = re.search(r'https://github\.com/[^\s)]+/pull/\d+', result.stdout)
        if match:
            pr_url = match.group(0)

    log.info(f"Claude Code completed for #{issue_number}, PR: {pr_url}")
    if result.stdout:
        output_tail = result.stdout[-2000:]
        log.info(f"Claude output (tail):\n{output_tail}")
    return True, pr_url


def extract_test_plan(pr_body):
    """Extract test plan checklist items from a PR body."""
    if not pr_body:
        return []
    # Find the ## Test plan section
    match = re.search(r'##\s+Test\s+plan\s*\n(.*?)(?=\n##\s|\Z)', pr_body, re.DOTALL | re.IGNORECASE)
    if not match:
        return []
    section = match.group(1)
    # Extract checklist items (- [ ] ... or - [x] ...)
    items = re.findall(r'^[ \t]*-\s+\[[ xX]\]\s+(.+)$', section, re.MULTILINE)
    return items


def get_pr_body(pr_number):
    """Fetch PR body from GitHub REST API."""
    owner, repo = GITHUB_REPO.split("/")
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    resp = requests.get(url, headers={"Authorization": f"token {GITHUB_TOKEN}"})
    resp.raise_for_status()
    return resp.json().get("body", "")


def append_test_plan(pr_number, pr_title, test_items):
    """Append test plan items to TEST_PLAN.md in the WebJared repo via GitHub API."""
    owner, repo = GITHUB_REPO.split("/")
    api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/TEST_PLAN.md"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}

    # Build the new section
    cet = timezone(timedelta(hours=1))
    date_str = datetime.now(cet).strftime("%Y-%m-%d")
    lines = [f"\n### PR #{pr_number} — {pr_title} ({date_str})\n"]
    for item in test_items:
        lines.append(f"- [ ] {item}\n")
    new_section = "".join(lines)

    # Try to fetch existing file
    sha = None
    existing_content = ""
    resp = requests.get(api_url, headers=headers)
    if resp.status_code == 200:
        file_data = resp.json()
        sha = file_data["sha"]
        existing_content = base64.b64decode(file_data["content"]).decode("utf-8")
    elif resp.status_code != 404:
        resp.raise_for_status()

    # If file doesn't exist yet, add a header
    if not existing_content:
        existing_content = "# TEST_PLAN.md\n\nCollected test plan items from coding agent PRs.\n"

    updated_content = existing_content.rstrip("\n") + "\n" + new_section

    # Commit via GitHub API
    put_body = {
        "message": f"test-plan: collect items from PR #{pr_number}",
        "content": base64.b64encode(updated_content.encode("utf-8")).decode("ascii"),
        "branch": "main",
    }
    if sha:
        put_body["sha"] = sha

    resp = requests.put(api_url, headers=headers, json=put_body)
    resp.raise_for_status()
    log.info(f"Appended {len(test_items)} test plan item(s) to TEST_PLAN.md from PR #{pr_number}")


def main():
    log.info(f"JaredCodingAgent started — watching WebJared Project every {POLL_INTERVAL}s")
    log.info(f"SSH delegation → {HOST_USER}@{HOST_IP}:{REPO_PATH}")

    cycle = 0
    while True:
        cycle += 1
        try:
            log.info(f"Poll #{cycle} — checking for Ready items...")
            items = get_ready_items()
            log.info(f"Poll #{cycle} — found {len(items)} Ready item(s)")

            for item in items:
                item_id = item["item_id"]
                number = item["issue_number"]
                title = item["title"]
                body = item["body"]

                if item_id in processed:
                    log.info(f"Skipping #{number} — already processed this session")
                    continue

                log.info(f"Picking up #{number}: {title}")
                processed.add(item_id)

                set_status(item_id, STATUS_IN_PROGRESS)
                add_comment(number, "🤖 **Jared Coding Agent** picked up this issue and is working on it...")

                success, pr_url = run_claude_code_on_host(number, title, body)

                if success:
                    set_status(item_id, STATUS_IN_REVIEW)
                    if pr_url:
                        pr_num = pr_url.rstrip("/").split("/")[-1]
                        add_comment(number, f"✅ **Jared Coding Agent** completed implementation.\n🔗 PR #{pr_num}: {pr_url}")
                        # Collect test plan items into TEST_PLAN.md
                        try:
                            pr_body = get_pr_body(pr_num)
                            test_items = extract_test_plan(pr_body)
                            if test_items:
                                append_test_plan(int(pr_num), title, test_items)
                            else:
                                log.info(f"No test plan items found in PR #{pr_num}")
                        except Exception as e:
                            log.warning(f"Failed to collect test plan from PR #{pr_num}: {e}")
                    else:
                        add_comment(number, "✅ **Jared Coding Agent** completed implementation. PR opened for review.")
                else:
                    add_comment(number, "❌ **Jared Coding Agent** encountered an error. Check container logs.")

        except Exception as e:
            log.error(f"Poll #{cycle} error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.error(f"Fatal error in main(): {e}", exc_info=True)
        raise
