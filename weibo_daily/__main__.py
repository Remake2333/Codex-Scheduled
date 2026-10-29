import argparse
import json
import os
import signal
from pathlib import Path

from cryptography.fernet import Fernet
from playwright.sync_api import sync_playwright

from .browser import WeiboBrowser
from .content import Content
from .github import GitHub, GitHubStore
from .report import write_report
from .runner import Runner
from .security import StopRun, decrypt_session, encrypt_session, private_write
from .state import JsonStore, account_lock, china_date, complete, new_state
import time


def parser():
    p = argparse.ArgumentParser(description="Weibo automation; defaults to no submissions")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("keygen")
    sub.add_parser("login")
    run = sub.add_parser("run")
    run.add_argument("--mode", choices=["check", "dry-run", "execute"], default="dry-run")
    run.add_argument("--confirm-live", action="store_true")
    run.add_argument("--content", type=Path, default=Path("config/content.json"))
    run.add_argument("--state-dir", type=Path, default=Path(".state"))
    run.add_argument("--output", type=Path, default=Path("out"))
    run.add_argument("--notify", action="store_true")
    run.add_argument("--max-seconds", type=int, default=1800)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "keygen":
        path = Path(".private/session.key")
        if path.exists():
            print("Existing key preserved; explicit revocation/rotation is required.")
            return 2
        private_write(path, Fernet.generate_key())
        print("Key saved to ignored .private/session.key (value not printed).")
        return 0
    account = os.environ.get("WEIBO_ACCOUNT_ID", "")
    if not account.isdigit():
        if args.command == "run":
            invalid = new_state(china_date(time.time()))
            invalid.update(status="stopped", reason="numeric_account_id_required")
            print(write_report(args.output, invalid, args.mode))
        else:
            print("WEIBO_ACCOUNT_ID must be the authorized numeric account ID.")
        return 2
    if args.command == "login":
        try:
            key = Path(".private/session.key").read_bytes()
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=False)
                context = browser.new_context()
                adapter = WeiboBrowser(context, account)
                adapter.navigate("https://weibo.com/")
                input("Complete official login yourself, then press Enter. Do not paste credentials here: ")
                if adapter.login_status() != "authenticated":
                    raise StopRun("login_not_verified")
                private_write(Path(".private/session.enc"), encrypt_session(context.storage_state(indexed_db=True), key, account))
                browser.close()
            print("Verified session encrypted to .private/session.enc; no plaintext export created.")
            return 0
        except (Exception, KeyboardInterrupt):
            print("Login/session setup failed; no credential details logged.")
            return 2
    state = new_state(china_date(time.time()))
    runner = None
    client = None
    cancelled = False

    def cancel(*_):
        nonlocal cancelled
        cancelled = True

    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    try:
        if args.max_seconds <= 0 or args.max_seconds > 2100:
            raise StopRun("invalid_time_budget")
        if args.mode == "execute" and not (args.confirm_live and os.environ.get("WEIBO_ALLOW_LIVE") == "true" and os.environ.get("WEIBO_AUTOMATION_ENABLED") == "true"):
            raise StopRun("live_execution_not_enabled")
        key = os.environ.get("WEIBO_SESSION_KEY", "").encode()
        blob = os.environ.get("WEIBO_SESSION_ENCRYPTED", "").encode()
        if not key or not blob:
            raise StopRun("encrypted_session_secrets_missing")
        storage = decrypt_session(blob, key, account)
        content = Content(args.content)
        repository = os.environ.get("GITHUB_REPOSITORY")
        if repository:
            client = GitHub(repository, os.environ.get("GH_TOKEN", ""))
            store = GitHubStore(client, account)
        else:
            store = JsonStore(args.state_dir / (state["date"] + ".json"))
        with account_lock(args.state_dir / "account.lock"):
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                context = browser.new_context(storage_state=storage, locale="zh-CN")
                adapter = WeiboBrowser(context, account)
                runner = Runner(adapter, store, content, mode=args.mode, max_seconds=args.max_seconds,
                                stopped=lambda: cancelled or (args.state_dir / "STOP").exists())
                state = runner.run()
                browser.close()
    except StopRun as error:
        if runner:
            state = runner.state
        state.update(status="stopped", reason=str(error))
    except Exception as error:
        # Playwright/HTTP exceptions may contain cookie-bearing URLs. Never log repr/tracebacks.
        if runner:
            state = runner.state
        state.update(status="stopped", reason="unexpected_" + type(error).__name__)
    report = write_report(args.output, state, args.mode, runner.plans if runner and args.mode == "dry-run" else None)
    print(report)
    notification_failed = False
    if args.notify:
        if not client or args.mode != "execute":
            print("Issue notification skipped: only explicit execute runs can write GitHub Issues.")
        else:
            try:
                print("Report issue:", client.notify(state["date"], report))
            except StopRun:
                notification_failed = True
                print("Issue notification failed; local report/artifact retained.")
    if args.mode == "execute":
        return 0 if complete(state) and state.get("status") == "complete" and not notification_failed else 1
    return 0 if state.get("login") == "authenticated" and not state.get("reason") else 1


if __name__ == "__main__":
    raise SystemExit(main())
