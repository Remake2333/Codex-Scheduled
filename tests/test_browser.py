import copy
import base64
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

from weibo_daily.browser import WeiboBrowser
from weibo_daily.content import Content
from weibo_daily.report import render
from weibo_daily.runner import Runner
from weibo_daily.security import StopRun
from weibo_daily.state import JsonStore, complete, counts, new_state

ROOT = Path(__file__).resolve().parents[1]


class Handler(BaseHTTPRequestHandler):
    requests = []
    blocked = None

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write((ROOT / "tests/fixtures/weibo.html").read_bytes())

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Handler.requests.append(data)
        if Handler.blocked:
            self.send_response(Handler.blocked)
            self.end_headers()
            return
        value = {"ok": 0, "message": "由于对方的设置，你不能评论哦！"} if data.get("deny") else {
            "ok": 1, "data": {"idstr": str(100000 + len(Handler.requests)), "mblogid": "p" + str(100000 + len(Handler.requests))}}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(value).encode())


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 29, 1, tzinfo=timezone.utc).timestamp()
        self.waits = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:" + str(cls.server.server_port)
        cls.pw = sync_playwright().start()
        executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        cls.browser = cls.pw.chromium.launch(headless=True, **({"executable_path": executable} if executable else {}))

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.external = []

        def route(r):
            if r.request.url.startswith(self.base + "/"):
                r.continue_()
            else:
                self.external.append(r.request.url)
                r.abort()

        self.context.route("**/*", route)
        self.adapter = WeiboBrowser(self.context, "42", self.base, fixture=True)
        self.adapter.navigate(self.base + "/")
        Handler.requests, Handler.blocked = [], None
        self.clock = Clock()
        self.store = JsonStore(Path(self.tmp.name) / "state.json")
        self.content = Content(ROOT / "config/content.json")
        self.content.data["approved"] = True

    def tearDown(self):
        self.assertEqual(self.external, [], "Fixture attempted an external request")

    def config(self, **values):
        self.adapter.page.evaluate("v=>{let d=JSON.parse(localStorage.simulation||'null'); if(!d){render();d=JSON.parse(localStorage.simulation||'null')||initial;} Object.assign(d,v);localStorage.simulation=JSON.stringify(d)}", values)

    def runner(self, mode="execute", **kwargs):
        return Runner(self.adapter, self.store, self.content, mode, clock=self.clock.time, sleep=self.clock.sleep, **kwargs)

    def effects(self):
        return self.adapter.page.evaluate("JSON.parse(localStorage.simulation||'null')?.effects||initial.effects")

    def test_readonly_has_no_submissions_and_no_journal(self):
        result = self.runner("check").run()
        self.assertEqual(result["login"], "authenticated")
        self.assertEqual(Handler.requests, [])
        self.assertFalse(self.store.path.exists())

    def test_dry_run_plans_without_submissions(self):
        runner = self.runner("dry-run")
        result = runner.run()
        self.assertTrue(runner.plans)
        self.assertEqual(Handler.requests, [])
        self.assertFalse(self.store.path.exists())
        self.assertNotIn("最终状态：全部完成", render(result, "dry-run", runner.plans))

    def test_full_fixture_workflow_and_evidence(self):
        result = self.runner().run()
        self.assertTrue(complete(result), result.get("reason"))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(counts(result), {"checkin": 1, "post": 2, "like": 15, "comment": 5, "repost": 3})
        self.assertEqual(len(Handler.requests), 26)
        self.assertIn("最终状态：全部完成", render(result, "execute"))
        self.assertEqual(result["metrics"]["rank"], None)
        self.assertEqual(result["metrics"]["points"], 123)
        comments = [r for r in Handler.requests if r["kind"] == "comment"]
        self.assertEqual(len({r["text"] for r in comments}), 5)
        self.assertTrue(all(r["forward"] is False for r in comments))
        self.assertTrue(all(r["comment"] is False for r in Handler.requests if r["kind"] == "repost"))
        times = [o["time"] for o in result["operations"]]
        self.assertTrue(all(5 <= b-a <= 15.01 for a,b in zip(times,times[1:])))

    def test_second_run_does_not_repeat_success(self):
        first = self.runner().run()
        submitted = len(Handler.requests)
        result = self.runner().run()
        self.assertTrue(complete(first) and complete(result))
        self.assertEqual(len(Handler.requests), submitted)

    def test_expired_login_stops_without_submitting(self):
        self.config(logged=False)
        result = self.runner().run()
        self.assertEqual(result["login"], "expired")
        self.assertEqual(Handler.requests, [])
        self.assertEqual(result["status"], "blocked")

    def test_existing_checkin_is_not_clicked(self):
        self.config(checkin=True)
        result = self.runner().run()
        self.assertTrue(complete(result))
        self.assertFalse(any(r["kind"] == "checkin" for r in Handler.requests))

    def test_existing_like_is_never_unliked(self):
        self.config(liked=["src000"])
        result = self.runner().run()
        self.assertEqual(counts(result)["like"], 15)
        self.assertFalse(any(r["kind"] == "like" and r["target"] == "src000" for r in Handler.requests))
        self.assertIn("src000", self.adapter.page.evaluate("JSON.parse(localStorage.simulation).liked"))

    def test_http_restrictions_stop_without_new_round(self):
        for code in [403, 418, 429]:
            with self.subTest(code=code):
                Handler.requests, Handler.blocked = [], code
                self.adapter.gate = None  # Independent fixture scenario; not a production recovery path.
                self.store = JsonStore(Path(self.tmp.name) / (str(code)+".json"))
                result = self.runner().run()
                self.assertEqual(result["reason"], "weibo_http_"+str(code))
                self.assertEqual(len(Handler.requests), 1)
                self.assertFalse(complete(result))

    def test_author_denial_skips_without_following(self):
        self.config(denied=["src000"])
        result = self.runner().run()
        self.assertTrue(complete(result), result.get("reason"))
        self.assertTrue(any(o.get("error") == "author_permission_denied" for o in result["operations"]))
        self.assertFalse(any(r["kind"] == "follow" for r in Handler.requests))

    def test_checkpoint_failure_prevents_submit(self):
        self.config(checkin=True)
        original = self.store.save

        def fail(value):
            if any(o["status"] == "pending" for o in value["operations"]):
                raise StopRun("checkpoint_unavailable")
            original(value)

        self.store.save = fail
        result = self.runner().run()
        self.assertEqual(Handler.requests, [])
        self.assertFalse(complete(result))

    def test_ambiguous_success_never_resubmits(self):
        self.config(checkin=True, noEffect=True)
        result = self.runner().run()
        self.assertTrue(any(o["status"] == "uncertain" for o in result["operations"]))
        count = len(Handler.requests)
        again = self.runner().run()
        self.assertEqual(len(Handler.requests), count)
        self.assertEqual(again["reason"], "unresolved_previous_operation_no_resubmit")

    def test_crash_after_post_recovers_without_duplicate(self):
        self.config(checkin=True)
        original = self.adapter.verify

        class Crash(BaseException):
            pass

        def crash(op):
            if op["kind"] == "post":
                raise Crash()
            return original(op)

        self.adapter.verify = crash
        with self.assertRaises(Crash):
            self.runner().run()
        self.adapter.verify = original
        result = self.runner().run()
        self.assertTrue(complete(result), result.get("reason"))
        self.assertEqual(sum(r["kind"] == "post" for r in Handler.requests), 2)

    def test_pause_flag_prevents_every_action(self):
        result = self.runner(stopped=lambda: True).run()
        self.assertEqual(result["reason"], "paused_or_cancelled")
        self.assertEqual(Handler.requests, [])

    def test_content_approval_required(self):
        self.content.data["approved"] = False
        result = self.runner().run()
        self.assertEqual(result["reason"], "content_not_approved")
        self.assertEqual(Handler.requests, [])

    def test_wrong_account_is_not_authenticated(self):
        self.adapter.account = "99"
        result = self.runner().run()
        self.assertEqual(result["login"], "unconfirmed")
        self.assertEqual(Handler.requests, [])

    def image_operation(self):
        fd, filename = tempfile.mkstemp(dir=ROOT / "assets", suffix=".png")
        with os.fdopen(fd, "wb") as f:
            f.write(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a32kAAAAASUVORK5CYII="))
        self.addCleanup(lambda: Path(filename).unlink(missing_ok=True))
        runner = self.runner()
        return runner.operation("post", {"target_id": "0", "url": self.base}, self.content.post(0, runner.day)["text"], filename)

    def test_authorized_image_upload_has_scoped_confirmation(self):
        op = self.image_operation()
        self.adapter.prepare(op)
        self.assertFalse(op.get("emoji_fallback", False))
        self.assertEqual(Handler.requests, [])

    def test_failed_upload_does_not_use_feed_image_as_confirmation(self):
        self.config(failUpload=True)
        op = self.image_operation()
        self.adapter.prepare(op)
        self.assertTrue(op["emoji_fallback"])
        self.assertEqual(self.adapter.page.locator('input[type="file"]').evaluate_all("els=>els.reduce((n,e)=>n+e.files.length,0)"), 0)
        self.assertEqual(Handler.requests, [])

    def test_three_empty_rounds_do_not_report_complete(self):
        self.config(checkin=True)
        self.adapter.candidates = lambda _: []
        result = self.runner().run()
        self.assertEqual(result["reason"], "three_rounds_without_progress")
        self.assertEqual(result["rounds"], 3)
        self.assertEqual(counts(result)["like"], 0)

    def test_date_change_during_wait_stops(self):
        self.config(checkin=True)
        self.clock.now = datetime(2026,10,29,15,59,59,tzinfo=timezone.utc).timestamp()
        s = new_state("2026-10-29", self.clock.now, self.clock.now+10)
        self.store.save(s)
        result = self.runner().run()
        self.assertEqual(result["reason"], "china_date_changed")
        self.assertEqual(Handler.requests, [])

    def test_storage_record_does_not_contain_session(self):
        result = self.runner("dry-run").run()
        text = json.dumps(result)
        for field in ["cookies", "origins", "storage_state", "WEIBO_SESSION_KEY"]:
            self.assertNotIn(field, text)

    def test_cancel_during_remote_checkpoint_prevents_click(self):
        self.config(checkin=True)
        cancelled = False
        original = self.store.save

        def checkpoint(value):
            nonlocal cancelled
            original(value)
            if any(o["status"] == "pending" for o in value["operations"]):
                cancelled = True

        self.store.save = checkpoint
        result = self.runner(stopped=lambda: cancelled).run()
        self.assertEqual(Handler.requests, [])
        self.assertEqual(result["reason"], "paused_or_cancelled")

    def test_slow_checkpoint_still_keeps_interaction_spacing(self):
        self.config(checkin=True)
        original = self.store.save

        def slow(value):
            self.clock.now += 1
            original(value)

        self.store.save = slow
        result = self.runner().run()
        self.assertTrue(complete(result))
        times = [o["time"] for o in result["operations"] if "time" in o]
        self.assertTrue(all(b-a >= 5 for a,b in zip(times,times[1:])))

    def test_verification_frame_stops_without_submitting(self):
        original = self.adapter.login_status

        def with_gate():
            status = original()
            self.adapter.page.evaluate("()=>{let f=document.createElement('iframe'); f.src='about:blank#captcha'; f.style.width='200px';f.style.height='100px';document.body.append(f)}")
            self.adapter.guard()
            return status

        self.adapter.login_status = with_gate
        result = self.runner().run()
        self.assertEqual(result["reason"], "visible_verification_frame")
        self.assertEqual(Handler.requests, [])


if __name__ == "__main__":
    unittest.main()
