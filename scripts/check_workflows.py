"""Supplement actionlint with project-specific scheduling/security invariants."""
import re
from pathlib import Path
import yaml

root = Path(__file__).resolve().parents[1]
for path in (root / ".github/workflows").glob("*.yml"):
    workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    assert workflow["jobs"] and workflow["on"], path
    assert "pull_request_target" not in workflow["on"], path
    for job in workflow["jobs"].values():
        assert int(job["timeout-minutes"]) <= 40
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", step["uses"]), step["uses"]
            if "upload-artifact" in step.get("uses", ""):
                paths = step["with"]["path"].splitlines()
                assert all(p in {"out/report.md", "out/state.json", "out/tests.txt"} for p in paths)
daily = yaml.load((root / ".github/workflows/daily.yml").read_text(), Loader=yaml.BaseLoader)
assert daily["on"]["schedule"][0]["cron"] == "0 1 * * *"
assert daily["on"]["workflow_dispatch"]["inputs"]["mode"]["default"] == "dry-run"
assert daily["concurrency"]["cancel-in-progress"] == "false"
assert "default_branch" in daily["jobs"]["daily"]["if"]
assert "WEIBO_AUTOMATION_ENABLED" in daily["jobs"]["daily"]["if"]
print("Workflow syntax and safety invariants passed (also run actionlint).")
