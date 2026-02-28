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
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
HOST_USER = os.environ.get("HOST_USER", "juchas")
HOST_IP = os.environ.get("HOST_IP", "172.22.0.1")

# Status option IDs (shared across all projects)
STATUS_READY = "61e4505c"
STATUS_IN_PROGRESS = "47fc9ee4"
STATUS_IN_REVIEW = "df73e18b"

# Projects to watch — each has its own project board, repo, and local path
PROJECTS = [
    {
        "name": "WebJared",
        "project_id": "PVT_kwDOC5s1AM4BO9Pf",
        "status_field_id": "PVTSSF_lADOC5s1AM4BO9Pfzg9gcHw",
        "github_repo": "YourJared/WebJared",
        "repo_path": "/opt/jared/repos/JaredAPIs/WebJared",
    },
    {
        "name": "Jared Backend",
        "project_id": "PVT_kwDOC5s1AM4BO9XS",
        "status_field_id": "PVTSSF_lADOC5s1AM4BO9XSzg9ghr8",
        "github_repo": "YourJared/JaredAPIs",
        "repo_path": "/opt/jared/repos/JaredAPIs",
    },
]

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
    """Poll all watched projects for items with Status=Ready."""
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
    ready = []
    for project in PROJECTS:
        try:
            data = graphql(query, {"projectId": project["project_id"]})
            items = data["node"]["items"]["nodes"]

            for item in items:
                for fv in item["fieldValues"]["nodes"]:
                    if (fv.get("field", {}).get("id") == project["status_field_id"]
                            and fv.get("optionId") == STATUS_READY):
                        content = item.get("content")
                        if content and "number" in content:
                            ready.append({
                                "item_id": item["id"],
                                "issue_number": content["number"],
                                "title": content["title"],
                                "body": content.get("body") or "",
                                "project": project,
                            })
        except Exception as e:
            log.error(f"Error polling project {project['name']}: {e}")

    return ready


def set_status(item_id, option_id, project):
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
        "projectId": project["project_id"],
        "itemId": item_id,
        "fieldId": project["status_field_id"],
        "optionId": option_id
    })


def add_comment(issue_number, message, github_repo):
    owner, repo = github_repo.split("/")
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/comments"
    requests.post(
        url,
        headers={"Authorization": f"token {GITHUB_TOKEN}"},
        json={"body": message}
    )


def run_claude_code_on_host(issue_number, title, body, project):
    github_repo = project["github_repo"]
    repo_path = project["repo_path"]

    prompt = f"""You are working on the {github_repo} repository.
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
        f"cd {repo_path} && git checkout main && git pull --rebase origin main && /home/{HOST_USER}/.local/bin/claude --print --permission-mode bypassPermissions '{escaped}'"
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


def get_pr_body(pr_number, github_repo):
    """Fetch PR body from GitHub REST API."""
    owner, repo = github_repo.split("/")
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    resp = requests.get(url, headers={"Authorization": f"token {GITHUB_TOKEN}"})
    resp.raise_for_status()
    return resp.json().get("body", "")


def append_test_plan(pr_number, pr_title, test_items, github_repo):
    """Append test plan items to TEST_PLAN.md in the repo via GitHub API."""
    owner, repo = github_repo.split("/")
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
    project_names = ", ".join(p["name"] for p in PROJECTS)
    log.info(f"JaredCodingAgent started — watching [{project_names}] every {POLL_INTERVAL}s")
    log.info(f"SSH delegation → {HOST_USER}@{HOST_IP}")

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
                project = item["project"]
                github_repo = project["github_repo"]

                if item_id in processed:
                    log.info(f"Skipping #{number} — already processed this session")
                    continue

                log.info(f"Picking up [{project['name']}] #{number}: {title}")
                processed.add(item_id)

                set_status(item_id, STATUS_IN_PROGRESS, project)
                add_comment(number, "🤖 **Jared Coding Agent** picked up this issue and is working on it...", github_repo)

                success, pr_url = run_claude_code_on_host(number, title, body, project)

                if success:
                    set_status(item_id, STATUS_IN_REVIEW, project)
                    if pr_url:
                        pr_num = pr_url.rstrip("/").split("/")[-1]
                        add_comment(number, f"✅ **Jared Coding Agent** completed implementation.\n🔗 PR #{pr_num}: {pr_url}", github_repo)
                        # Collect test plan items into TEST_PLAN.md
                        try:
                            pr_body = get_pr_body(pr_num, github_repo)
                            test_items = extract_test_plan(pr_body)
                            if test_items:
                                append_test_plan(int(pr_num), title, test_items, github_repo)
                            else:
                                log.info(f"No test plan items found in PR #{pr_num}")
                        except Exception as e:
                            log.warning(f"Failed to collect test plan from PR #{pr_num}: {e}")
                    else:
                        add_comment(number, "✅ **Jared Coding Agent** completed implementation. PR opened for review.", github_repo)
                else:
                    add_comment(number, "❌ **Jared Coding Agent** encountered an error. Check container logs.", github_repo)

        except Exception as e:
            log.error(f"Poll #{cycle} error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.error(f"Fatal error in main(): {e}", exc_info=True)
        raise
