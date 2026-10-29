import copy
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .security import StopRun

TZ = ZoneInfo("Asia/Shanghai")
TARGETS = {"checkin": 1, "post": 2, "like": 15, "comment": 5, "repost": 3}


def china_date(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, TZ).date().isoformat()


def new_state(day: str, last_action_time=None, next_action_not_before=0) -> dict:
    return {"version": 1, "date": day, "login": "unconfirmed", "status": "pending",
            "operations": [], "last_action_time": last_action_time,
            "next_action_not_before": next_action_not_before, "reason": None,
            "metrics": {"rank": None, "points": None, "experience": None}}


def validate_state(value: dict, day: str) -> dict:
    if value.get("version") != 1 or value.get("date") != day or not isinstance(value.get("operations"), list):
        raise StopRun("invalid_state_file")
    seen = set()
    for op in value["operations"]:
        if op.get("kind") not in TARGETS or op.get("status") not in {"pending", "confirmed", "uncertain", "skipped"}:
            raise StopRun("invalid_operation_record")
        if not isinstance(op.get("target_id"), str) or op.get("key") != op["kind"] + ":" + op["target_id"]:
            raise StopRun("invalid_operation_identity")
        if op.get("key") in seen or not isinstance(op.get("key"), str):
            raise StopRun("duplicate_operation_key")
        seen.add(op["key"])
        if op["status"] == "confirmed" and not all(op.get("evidence", {}).get(k) for k in ["verified", "id", "url"]):
            raise StopRun("unverified_confirmation_in_state")
    return value


def counts(state: dict) -> dict:
    return {kind: len({o["target_id"] for o in state["operations"] if o["kind"] == kind and o["status"] == "confirmed"})
            for kind in TARGETS}


def complete(state: dict) -> bool:
    return state.get("login") == "authenticated" and all(counts(state)[k] >= n for k, n in TARGETS.items())


class JsonStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self, day: str) -> dict:
        if not self.path.exists():
            if self.path.name == day + ".json":
                yesterday = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
                prior_path = self.path.parent / (yesterday + ".json")
                if prior_path.exists():
                    prior = JsonStore(prior_path).load(yesterday)
                    return new_state(day, prior.get("last_action_time"), prior.get("next_action_not_before", 0))
            return new_state(day)
        try:
            return validate_state(json.loads(self.path.read_text()), day)
        except (ValueError, TypeError, KeyError):
            raise StopRun("invalid_state_file") from None

    def save(self, value: dict) -> None:
        validate_state(value, value["date"])
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".checkpoint-")
        try:
            with os.fdopen(fd, "w") as f:
                os.fchmod(f.fileno(), 0o600)
                json.dump(value, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)


@contextmanager
def account_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StopRun("another_local_run_is_active") from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def artifact_state(state: dict) -> dict:
    # Execution records contain only public IDs/text and safe error codes, never storage_state.
    return copy.deepcopy(state)
