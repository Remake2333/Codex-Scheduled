"""Scan project deliverables only; deliberately never read the user's private browser/session files."""
import re
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
ignored = {".git", ".private", ".venv", ".state", "out", "__pycache__"}
bad = []
patterns = [r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", r"gh[pousr]_[A-Za-z0-9]{30,}"]
for path in root.rglob("*"):
    rel = path.relative_to(root)
    if any(part in ignored for part in rel.parts) or not path.is_file():
        continue
    if path.suffix == ".enc" or path.name in {"cookies.json", "session.json", "storage_state.json"}:
        bad.append(str(rel))
        continue
    text = path.read_text(errors="replace")
    if any(re.search(pattern, text) for pattern in patterns):
        bad.append(str(rel))
assert not bad, "Potential credential files/patterns: " + ", ".join(bad)
for path in [".private/session.key", ".private/session.enc", ".state/2026-10-29.json", "out/report.md"]:
    assert subprocess.run(["git", "check-ignore", "-q", path], cwd=root).returncode == 0, path
print("Credential file scan and ignore rules passed. No private session files were inspected.")
