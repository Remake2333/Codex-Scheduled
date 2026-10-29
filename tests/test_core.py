import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import yaml
from cryptography.fernet import Fernet

from weibo_daily.__main__ import main
from weibo_daily.content import Content, plain
from weibo_daily.github import GitHub, GitHubStore
from weibo_daily.report import render
from weibo_daily.security import StopRun, decrypt_session, encrypt_session, private_write, public_url, safe_error
from weibo_daily.state import TARGETS, JsonStore, account_lock, china_date, complete, counts, new_state

ROOT = Path(__file__).resolve().parents[1]


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)

    def test_encrypted_session_roundtrip(self):
        key = Fernet.generate_key()
        storage = {"cookies": [{"name": "fake", "value": "fake-session-value"}], "origins": []}
        blob = encrypt_session(storage, key, "42")
        self.assertNotIn(b"fake-session-value", blob)
        self.assertEqual(decrypt_session(blob, key, "42"), storage)

    def test_session_wrong_account(self):
        key = Fernet.generate_key()
        blob = encrypt_session({"cookies": [], "origins": []}, key, "42")
        with self.assertRaises(StopRun):
            decrypt_session(blob, key, "99")

    def test_session_tamper_rejected(self):
        key = Fernet.generate_key()
        blob = encrypt_session({"cookies": [], "origins": []}, key, "42")
        with self.assertRaises(StopRun):
            decrypt_session(blob[:-5] + b"wrong", key, "42")

    def test_session_wrong_key(self):
        blob = encrypt_session({"cookies": [], "origins": []}, Fernet.generate_key(), "42")
        with self.assertRaises(StopRun):
            decrypt_session(blob, Fernet.generate_key(), "42")

    def test_secret_size_limit(self):
        with self.assertRaises(StopRun):
            encrypt_session({"cookies": [], "origins": ["x" * 50000]}, Fernet.generate_key(), "42")

    def test_private_file_mode(self):
        p = self.path / "key"
        private_write(p, b"dummy")
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)

    def test_urls_remove_ticket_and_fragment(self):
        self.assertEqual(public_url("https://weibo.com/p/x?ticket=hidden#hidden"), "https://weibo.com/p/x")

    def test_url_credentials_rejected(self):
        with self.assertRaises(StopRun):
            public_url("https://user:secret@weibo.com/")

    def test_log_redaction(self):
        text = safe_error("Cookie: hidden https://weibo.com/a?ticket=hidden")
        self.assertNotIn("hidden", text)

    def test_atomic_json_roundtrip(self):
        store = JsonStore(self.path / "state.json")
        state = new_state("2026-10-29")
        store.save(state)
        self.assertEqual(store.load("2026-10-29"), state)
        self.assertFalse(list(self.path.glob(".checkpoint-*")))

    def test_corrupt_state_fails_closed(self):
        p = self.path / "state.json"
        p.write_text("broken")
        with self.assertRaises(StopRun):
            JsonStore(p).load("2026-10-29")

    def test_duplicate_journal_keys_rejected(self):
        state = new_state("2026-10-29")
        state["operations"] = [{"key": "like:a", "target_id": "a", "kind": "like", "status": "skipped"}] * 2
        with self.assertRaises(StopRun):
            JsonStore(self.path / "state.json").save(state)

    def test_unverified_success_rejected(self):
        state = new_state("2026-10-29")
        state["operations"] = [{"key": "like:a", "target_id": "a", "kind": "like", "status": "confirmed"}]
        with self.assertRaises(StopRun):
            JsonStore(self.path / "state.json").save(state)

    def test_local_concurrency_lock(self):
        with account_lock(self.path / "lock"):
            with self.assertRaises(StopRun):
                with account_lock(self.path / "lock"):
                    pass

    def test_china_date_boundary(self):
        epoch = datetime(2026, 10, 28, 16, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(china_date(epoch), "2026-10-29")
        self.assertEqual(china_date(epoch - 1), "2026-10-28")

    def test_all_complete_requires_every_target(self):
        s = new_state("2026-10-29")
        s["login"] = "authenticated"
        for kind, target in TARGETS.items():
            s["operations"] += [{"key": f"{kind}:{i}", "target_id": str(i), "kind": kind, "status": "confirmed", "evidence": {"verified": True,"id":str(i),"url":"https://weibo.com/42/p1"}}
                                for i in range(target)]
        self.assertTrue(complete(s))
        s["operations"].pop()
        self.assertFalse(complete(s))

    def test_report_never_claims_completion_for_dry_run(self):
        s = new_state("2026-10-29")
        self.assertNotIn("最终状态：全部完成", render(s, "dry-run"))
        self.assertIn("未能读取", render(s, "dry-run"))

    def test_report_pending_not_success(self):
        s = new_state("2026-10-29")
        s["operations"] = [{"key": "post:0", "target_id": "0", "kind": "post", "status": "uncertain", "text": "example"}]
        self.assertIn("发帖：0/2", render(s, "execute"))
        self.assertIn("uncertain", render(s, "execute"))

    def test_remote_read_does_not_create_branch(self):
        client = Mock(repository="owner/repo", branch="weibo-state")
        client.request.return_value = None
        store = GitHubStore(client, "42")
        state = store.load("2026-10-29")
        self.assertEqual(counts(state)["post"], 0)
        self.assertTrue(all(c.args[0] == "GET" for c in client.request.call_args_list))

    def test_remote_checkpoint_cas_sha(self):
        client = Mock(repository="owner/repo", branch="weibo-state")
        client.request.return_value = {"content": {"sha": "next"}}
        store = GitHubStore(client, "42")
        store.sha = "prior"
        store.save(new_state("2026-10-29"))
        self.assertEqual(client.request.call_args.args[2]["sha"], "prior")
        self.assertEqual(store.sha, "next")

    def test_content_needs_approval(self):
        self.assertFalse(Content(ROOT / "config/content.json").approved)

    def test_comments_are_contextual_and_unique(self):
        content = Content(ROOT / "config/content.json")
        s = new_state("2026-10-29")
        text = content.comment("李荣浩 天津", s)
        self.assertIn("天津", text)
        s["operations"].append({"kind": "comment", "text": text})
        self.assertIsNone(content.comment("李荣浩 天津", s))
        self.assertIsNone(content.comment("unmatched content", s))

    def test_native_topic_tag_normalization(self):
        self.assertEqual(plain("#李荣浩[超话]# #李荣浩超话# 内容🎧[心]"), plain("李荣浩超话#李荣浩超话# 内容"))

    def test_local_midnight_keeps_cooldown(self):
        yesterday = new_state("2026-10-28", 100, 115)
        JsonStore(self.path / "2026-10-28.json").save(yesterday)
        today = JsonStore(self.path / "2026-10-29.json").load("2026-10-29")
        self.assertEqual(today["next_action_not_before"], 115)
        self.assertEqual(today["operations"], [])

    def test_notification_create_and_update(self):
        github = GitHub("owner/repo", "dummy-test-token")
        with patch.object(github, "request", side_effect=[[], {"html_url": "https://github.com/owner/repo/issues/1"}]) as request:
            self.assertTrue(github.notify("2026-10-29", "fixture report").endswith("/1"))
            self.assertEqual(request.call_args.args[0], "POST")
        with patch.object(github, "request", side_effect=[[{"title": "李荣浩超话每日打卡报告 2026-10-29", "number": 1}], {"html_url": "https://github.com/owner/repo/issues/1"}]) as request:
            github.notify("2026-10-29", "updated fixture report")
            self.assertEqual(request.call_args.args[0], "PATCH")

    def test_notification_failure_is_explicit(self):
        github = GitHub("owner/repo", "dummy-test-token")
        with patch.object(github, "request", side_effect=StopRun("github_api_http_403")):
            with self.assertRaises(StopRun):
                github.notify("2026-10-29", "fixture report")

    def test_cli_real_actions_are_locked_by_default(self):
        with patch.dict(os.environ, {"WEIBO_ACCOUNT_ID": "42"}, clear=True):
            result = main(["run", "--mode", "execute", "--output", str(self.path)])
        self.assertEqual(result, 1)
        self.assertIn("live_execution_not_enabled", (self.path / "report.md").read_text())

    def test_unconfigured_run_still_produces_report(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(main(["run", "--output", str(self.path)]), 2)
        self.assertIn("numeric_account_id_required", (self.path / "report.md").read_text())

    def test_duplicate_post_content_is_rejected(self):
        value = json.loads((ROOT / "config/content.json").read_text())
        value["posts"][1]["text"] = value["posts"][0]["text"]
        path = self.path / "content.json"
        path.write_text(json.dumps(value))
        with self.assertRaises(StopRun):
            Content(path)

    def test_workflows_safe_schedule_and_artifacts(self):
        workflow = yaml.load((ROOT / ".github/workflows/daily.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(workflow["on"]["schedule"][0]["cron"], "0 1 * * *")
        self.assertEqual(workflow["on"]["workflow_dispatch"]["inputs"]["mode"]["default"], "dry-run")
        self.assertEqual(workflow["concurrency"]["cancel-in-progress"], "false")
        self.assertIn("WEIBO_AUTOMATION_ENABLED", workflow["jobs"]["daily"]["if"])
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            data = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
            self.assertNotIn("pull_request_target", data["on"])
            for job in data["jobs"].values():
                for step in job["steps"]:
                    if "uses" in step:
                        self.assertRegex(step["uses"], r"@[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
