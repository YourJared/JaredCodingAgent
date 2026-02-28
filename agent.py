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
import time
import subprocess
import logging
import requests

import sys

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
        f"cd {REPO_PATH} && git pull origin main && /home/{HOST_USER}/.local/bin/claude --print '{escaped}'"
    ]

    log.info(f"SSHing to host to run Claude Code for issue #{issue_number}")
    result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=600)

    if result.returncode != 0:
        log.error(f"Claude Code failed:\n{result.stderr}")
        return False

    log.info(f"Claude Code completed for #{issue_number}")
    return True


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

                success = run_claude_code_on_host(number, title, body)

                if success:
                    set_status(item_id, STATUS_IN_REVIEW)
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
