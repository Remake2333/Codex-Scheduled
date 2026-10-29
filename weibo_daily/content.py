import hashlib
import json
import re
from pathlib import Path

from .security import StopRun


def plain(text: str) -> str:
    text = re.sub(r"#([^#]*?)\[超话\]#", r"\1超话", text)
    text = re.sub(r"\[[^\]]+\]", "", text).replace("#", "")
    return re.sub(r"[\s\u200b-\u200f\ufe0f\ufeff\U0001f000-\U0001faff\u2600-\u27bf]", "", text)


class Content:
    def __init__(self, path: Path):
        try:
            self.data = json.loads(path.read_text())
            assert isinstance(self.data["posts"], list) and len(self.data["posts"]) >= 2
            assert isinstance(self.data["comments"], list)
            for p in self.data["posts"]:
                assert isinstance(p["text"], str) and "#李荣浩超话#" in p["text"]
            assert len({plain(p["text"]) for p in self.data["posts"][:2]}) == 2
            for rule in self.data["comments"]:
                assert rule["terms"] and rule["texts"]
        except (OSError, ValueError, KeyError, AssertionError, TypeError):
            raise StopRun("invalid_content_configuration") from None

    @property
    def approved(self):
        return self.data.get("approved") is True

    def post(self, slot: int, day: str):
        p = dict(self.data["posts"][slot])
        p["text"] = p["text"].replace("{date}", day)
        return p

    def comment(self, caption: str, state: dict):
        used = {plain(o.get("text", "")) for o in state["operations"] if o["kind"] == "comment"}
        for rule in self.data["comments"]:
            if any(term in caption for term in rule["terms"]):
                return next((t for t in rule["texts"] if plain(t) not in used), None)
        # No meaningless generic fallback: insufficient approved content is a partial run.
        return None


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
