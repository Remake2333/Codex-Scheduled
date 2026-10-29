import copy
import random
import time

from playwright.sync_api import Error as PlaywrightError

from .browser import AlreadyLiked, AuthorDenied
from .content import fingerprint
from .security import StopRun
from .state import TARGETS, china_date, complete, counts


class Runner:
    def __init__(self, browser, store, content, mode="dry-run", clock=time.time, sleep=time.sleep,
                 rng=None, max_seconds=1800, max_rounds=100, stopped=lambda: False):
        if mode not in {"check", "dry-run", "execute"}:
            raise ValueError("invalid mode")
        self.browser, self.store, self.content, self.mode = browser, store, content, mode
        self.clock, self.sleep, self.rng = clock, sleep, rng or random.SystemRandom()
        self.deadline, self.max_rounds, self.stopped = clock() + max_seconds, max_rounds, stopped
        self.day = china_date(clock())
        self.state = store.load(self.day)
        self.state["environment"] = "local-fixture" if browser.fixture else "weibo"
        self.plans = []

    def boundary(self):
        if self.stopped():
            raise StopRun("paused_or_cancelled")
        if self.clock() >= self.deadline:
            raise StopRun("run_time_budget_exhausted")
        if china_date(self.clock()) != self.day:
            raise StopRun("china_date_changed")
        self.browser.guard()

    def checkpoint(self):
        if self.mode == "execute":
            self.store.save(self.state)

    def operation(self, kind, target, text="", image=None):
        target = {k: v for k, v in target.items() if k in {"target_id", "url", "native_id"}}
        return {"kind": kind, "key": kind + ":" + target["target_id"], **target, "text": text,
                "image": image, "fingerprint": fingerprint(text), "status": "pending"}

    def existing(self, op):
        return any(o["key"] == op["key"] for o in self.state["operations"])

    def perform(self, op):
        self.boundary()
        if self.existing(op):
            return
        try:
            self.browser.prepare(op)
        except (AlreadyLiked, AuthorDenied) as error:
            op.update(status="skipped", error=str(error))
            self.state["operations"].append(op)
            self.checkpoint()
            return
        while self.clock() < self.state.get("next_action_not_before", 0):
            self.boundary()
            self.sleep(min(1, self.state["next_action_not_before"] - self.clock()))
        self.boundary()
        op["time"] = self.clock()
        self.state["last_action_time"] = op["time"]
        op["delay_seconds"] = self.rng.uniform(5, 15)
        self.state["next_action_not_before"] = op["time"] + op["delay_seconds"]
        self.state["operations"].append(op)
        # Must succeed remotely before any externally visible submit.
        self.checkpoint()
        try:
            self.boundary()
            op["proof_hint"] = self.browser.submit(op)
            # A slow durable checkpoint/submit must not consume the next action's cooldown.
            self.state["last_action_time"] = self.clock()
            self.state["next_action_not_before"] = self.clock() + op["delay_seconds"]
            self.checkpoint()
            evidence = self.browser.verify(op)
            if not evidence or not evidence.get("verified") or not evidence.get("id"):
                raise StopRun("operation_result_uncertain")
            op.update(status="confirmed", evidence=evidence)
            self.checkpoint()
        except AuthorDenied:
            op.update(status="skipped", error="author_permission_denied")
            self.checkpoint()
        except AlreadyLiked:
            # State changed concurrently: never click again / unlike it.
            op.update(status="skipped", error="like_changed_before_submit")
            self.checkpoint()
        except (StopRun, PlaywrightError):
            op.update(status="uncertain", error="submission_or_verification_interrupted")
            self.checkpoint()
            raise

    def reconcile(self):
        for op in self.state["operations"]:
            if op["status"] in {"pending", "uncertain"}:
                self.boundary()
                evidence = self.browser.verify(op)
                if not evidence or not evidence.get("verified"):
                    raise StopRun("unresolved_previous_operation_no_resubmit")
                op.update(status="confirmed", evidence=evidence)
                self.state["last_action_time"] = max(self.state.get("last_action_time") or 0, self.clock())
                self.state["next_action_not_before"] = max(self.state.get("next_action_not_before", 0), self.clock() + op.get("delay_seconds", 15))
                self.checkpoint()

    def run(self):
        try:
            self.boundary()
            self.state["login"] = self.browser.login_status()
            if self.state["login"] != "authenticated":
                raise StopRun("login_" + self.state["login"])
            if self.mode != "execute":
                self.state["metrics"] = self.browser.metrics() if self.browser.checkin_status() is not None else self.state["metrics"]
                if self.mode == "dry-run":
                    self.plan()
                return self.state
            if not self.content.approved:
                raise StopRun("content_not_approved")
            if self.state["status"] == "blocked":
                raise StopRun("same_day_stop_requires_manual_review")
            self.reconcile()
            self.state.update(reason=None, status="running")
            if complete(self.state):
                self.state["status"] = "complete"
                return self.state
            sign = self.browser.checkin_status()
            if sign is None:
                raise StopRun("checkin_state_unconfirmed")
            op = self.operation("checkin", {"target_id": self.day, "url": self.browser.base + "/p/10080868ed174b2d302045692b38756ee47f21"})
            if not self.existing(op):
                if sign:
                    op.update(status="confirmed", evidence={"verified": True, "id": self.day, "url": op["url"]}, observed_existing=True)
                    self.state["operations"].append(op)
                    self.checkpoint()
                else:
                    self.perform(op)
            self.state["metrics"] = self.browser.metrics()
            for slot in range(2):
                item = self.content.post(slot, self.day)
                self.perform(self.operation("post", {"target_id": str(slot), "url": self.browser.base}, item["text"], item.get("image")))
            idle = 0
            for round_number in range(self.max_rounds):
                self.boundary()
                before = sum(counts(self.state).values())
                candidates = self.browser.candidates(round_number)
                for kind in sorted(["like", "comment", "repost"], key=lambda k: TARGETS[k] - counts(self.state)[k], reverse=True):
                    for candidate in candidates:
                        if counts(self.state)[kind] >= TARGETS[kind]:
                            break
                        text = ""
                        if kind == "comment":
                            text = self.content.comment(candidate["caption"], self.state)
                            if not text:
                                continue
                        elif kind == "repost":
                            text = self.content.data["repost_text"]
                        self.perform(self.operation(kind, candidate, text))
                self.state["rounds"] = round_number + 1
                if complete(self.state):
                    self.state.update(status="complete", reason=None)
                    self.checkpoint()
                    return self.state
                idle = idle + 1 if before == sum(counts(self.state).values()) else 0
                if idle >= 3:
                    raise StopRun("three_rounds_without_progress")
            raise StopRun("round_budget_exhausted")
        except (StopRun, PlaywrightError) as error:
            reason = str(error) if isinstance(error, StopRun) else "browser_operation_failed"
            gated = any(x in reason for x in ["weibo_http_", "verification", "visible_verification", "login_expired", "same_day_stop"])
            self.state.update(status="blocked" if gated else "stopped", reason=reason)
            try:
                self.checkpoint()
            except StopRun:
                self.state["reason"] = "durable_checkpoint_failed"
            return self.state

    def plan(self):
        sign = self.browser.checkin_status()
        if sign is False:
            self.plans.append({"kind": "checkin", "target_id": self.day})
        for slot in range(2):
            op = self.operation("post", {"target_id": str(slot)}, self.content.post(slot, self.day)["text"])
            if not self.existing(op):
                self.plans.append(op)
        candidates = self.browser.candidates()
        for kind in ["like", "comment", "repost"]:
            remaining = max(0, TARGETS[kind] - counts(self.state)[kind])
            planned = copy.deepcopy(self.state)
            for candidate in candidates:
                if remaining <= 0:
                    break
                text = self.content.comment(candidate["caption"], planned) if kind == "comment" else ""
                if kind == "comment" and not text:
                    continue
                op = self.operation(kind, candidate, text or "")
                if not self.existing(op):
                    self.plans.append(op)
                    planned["operations"].append(op)
                    remaining -= 1
