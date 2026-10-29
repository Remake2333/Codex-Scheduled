"""Only GitHub control-plane API calls. Weibo operations always use Playwright UI."""
import base64
import json
import urllib.error
import urllib.request
from datetime import date, timedelta
from urllib.parse import quote

from .security import StopRun
from .state import new_state, validate_state


class GitHub:
    def __init__(self, repository: str, token: str, branch="weibo-state"):
        parts = repository.split("/")
        if len(parts) != 2 or not all(p and all(c.isalnum() or c in "-_." for c in p) for p in parts):
            raise StopRun("invalid_github_repository")
        if not token or branch != "weibo-state":
            raise StopRun("github_checkpoint_configuration_required")
        self.repository, self.token, self.branch = repository, token, branch

    def request(self, method: str, path: str, data=None, missing_ok=False):
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request("https://api.github.com" + path, data=body, method=method,
            headers={"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "weibo-daily", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404 and missing_ok:
                return None
            raise StopRun("github_api_http_" + str(e.code)) from None
        except (OSError, ValueError):
            # A failed checkpoint MUST prevent the subsequent browser submit.
            raise StopRun("github_api_unavailable") from None

    def ensure_state_branch(self):
        root = "/repos/" + self.repository
        if self.request("GET", root + "/git/ref/heads/" + self.branch, missing_ok=True):
            return
        repo = self.request("GET", root)
        ref = self.request("GET", root + "/git/ref/heads/" + quote(repo["default_branch"], safe=""))
        self.request("POST", root + "/git/refs", {"ref": "refs/heads/" + self.branch, "sha": ref["object"]["sha"]})

    def notify(self, day: str, report: str):
        root = "/repos/" + self.repository + "/issues"
        title = "李荣浩超话每日打卡报告 " + day
        existing = next((i for i in self.request("GET", root + "?state=all&per_page=100")
                         if i.get("title") == title and "pull_request" not in i), None)
        if existing:
            return self.request("PATCH", root + "/" + str(existing["number"]), {"body": report})["html_url"]
        return self.request("POST", root, {"title": title, "body": report})["html_url"]


class GitHubStore:
    """CAS checkpoint before every submit; artifacts are supplementary, not the durable journal."""
    def __init__(self, client: GitHub, account: str):
        if not account.isdigit():
            raise StopRun("numeric_account_id_required")
        self.client, self.account, self.sha = client, account, None

    def path(self, day):
        return "/repos/" + self.client.repository + "/contents/daily/" + self.account + "/" + day + ".json"

    def read(self, day):
        data = self.client.request("GET", self.path(day) + "?ref=" + self.client.branch, missing_ok=True)
        if not data:
            return None, None
        try:
            state = validate_state(json.loads(base64.b64decode(data["content"])), day)
            return state, data["sha"]
        except (KeyError, ValueError, TypeError):
            raise StopRun("invalid_remote_checkpoint") from None

    def load(self, day):
        value, self.sha = self.read(day)
        if value:
            return value
        yesterday = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
        prior, _ = self.read(yesterday)
        return new_state(day, prior.get("last_action_time") if prior else None,
                         prior.get("next_action_not_before", 0) if prior else 0)

    def save(self, value):
        validate_state(value, value["date"])
        self.client.ensure_state_branch()
        body = {"message": "checkpoint " + value["date"], "branch": self.client.branch,
                "content": base64.b64encode(json.dumps(value, ensure_ascii=False).encode()).decode()}
        if self.sha:
            body["sha"] = self.sha
        result = self.client.request("PUT", self.path(value["date"]), body)
        self.sha = result["content"]["sha"]
