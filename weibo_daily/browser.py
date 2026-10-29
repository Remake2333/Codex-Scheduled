"""Fail-closed DOM automation. No stealth flags, private Weibo API calls or CAPTCHA solving."""
import re
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

from playwright.sync_api import Error as PlaywrightError

from .content import plain
from .security import StopRun, public_url

TOPIC_PATH = "/p/10080868ed174b2d302045692b38756ee47f21"


class AuthorDenied(StopRun):
    pass


class AlreadyLiked(StopRun):
    pass


class WeiboBrowser:
    def __init__(self, context, account: str, base="https://weibo.com", fixture=False):
        origin = urlsplit(base)
        if (not fixture and base != "https://weibo.com") or (fixture and origin.hostname not in {"127.0.0.1", "localhost"}):
            raise StopRun("invalid_browser_origin")
        self.context, self.account, self.base, self.fixture = context, account, base.rstrip("/"), fixture
        self.page = context.new_page()
        self.gate = None
        self.receipt = None
        self.context.on("response", self._response)

    def _response(self, response):
        p = urlsplit(response.url)
        own_host = p.hostname == urlsplit(self.base).hostname
        is_write = response.request.method == "POST" and p.path.startswith("/ajax/") and not any(x in p.path for x in ["upload", "/image"])
        if own_host and response.status in {403, 418, 429} and (is_write or response.request.is_navigation_request()):
            self.gate = "weibo_http_" + str(response.status)
        if own_host and is_write:
            try:
                value = response.json()
                if not isinstance(value, dict):
                    return
                message = str(value.get("msg") or value.get("message") or "")
                if "对方的设置" in message or "禁止评论" in message or "禁止转发" in message:
                    self.receipt = {"denied": True}
                elif value.get("ok") != 1 and re.search("验证|频繁|限制|安全|登录", message):
                    self.gate = "weibo_verification_or_restriction"
                elif any(s in p.path for s in ["comments/create", "statuses/update", "repost"]):
                    d = value.get("data", {})
                    self.receipt = {"ok": value.get("ok"), "id": str(d.get("idstr") or d.get("id") or ""),
                                    "mblogid": d.get("mblogid")} if isinstance(d, dict) else None
            except (ValueError, PlaywrightError):
                pass

    def guard(self):
        if self.gate:
            raise StopRun(self.gate)
        for text in ["请完成安全验证", "操作太频繁", "访问频次过高", "账号异常", "请先登录", "请重新登录"]:
            if self.page.get_by_text(text, exact=True).first.is_visible():
                raise StopRun("weibo_verification_or_restriction")
        for frame in self.page.locator('iframe[src*="captcha"],iframe[src*="geetest"],iframe[src*="verify"]').all():
            if frame.is_visible():
                raise StopRun("visible_verification_frame")

    def navigate(self, url):
        url = public_url(url)
        if urlsplit(url).netloc != urlsplit(self.base).netloc:
            raise StopRun("navigation_outside_authorized_origin")
        self.guard()
        for attempt in range(2):
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=25000)
                self.page.wait_for_timeout(80 if self.fixture else 1200)
                self.guard()
                return
            except PlaywrightError:
                self.guard()
                if attempt:
                    raise StopRun("page_load_failed") from None

    def login_status(self):
        self.navigate(self.base + "/")
        for attempt in range(2):
            self.guard()
            account_links = self.page.locator(f'a[href="/u/{self.account}"],a[href^="/{self.account}/"],a[href="https://weibo.com/u/{self.account}"]')
            composer = self.page.locator('textarea,[contenteditable="true"],button[aria-label="发布微博"]')
            outside_posts = account_links.evaluate_all("els=>els.some(e=>!e.closest('article'))")
            if outside_posts and composer.count():
                return "authenticated"
            if self.page.get_by_text("登录/注册", exact=True).first.is_visible() or self.page.get_by_text("扫描二维码登录", exact=True).first.is_visible():
                return "expired"
            if not attempt:
                self.navigate(self.base + "/")
        return "unconfirmed"

    def topic(self):
        self.navigate(self.base + TOPIC_PATH)
        body = self.page.locator("body").inner_text()
        if "李荣浩" not in body or not self.page.get_by_text("发帖", exact=True).count():
            raise StopRun("target_topic_not_verified")

    def checkin_status(self):
        self.topic()
        if self.page.get_by_text("已签到", exact=True).first.is_visible():
            return True
        if self.page.get_by_text("签到", exact=True).first.is_visible():
            return False
        return None

    def metrics(self):
        # Read explicit labels only. "今日签到人数" is deliberately never parsed as a rank.
        result = {"rank": None, "points": None, "experience": None}
        for key, label in [("rank", "我的签到排名"), ("points", "我的积分"), ("experience", "我的经验值")]:
            locator = self.page.get_by_text(re.compile("^" + label + r"[：:]\s*\d+$"))
            if locator.count() == 1:
                result[key] = int(re.search(r"\d+$", locator.inner_text())[0])
        return result

    def candidates(self, round_number=0):
        self.topic()
        tab = ["最新", "热门", "精华"][round_number % 3]
        if self.page.get_by_text(tab, exact=True).count() == 1:
            self.page.get_by_text(tab, exact=True).click()
            self.page.wait_for_timeout(50 if self.fixture else 1000)
        if tab == "最新" and self.page.get_by_text("最新发帖", exact=True).count() == 1:
            self.page.get_by_text("最新发帖", exact=True).click()
        found = {}
        for _ in range(4):
            self.guard()
            rows = self.page.locator("article").evaluate_all("""els => els.map(e => ({
              text: e.innerText.slice(0,1600), id: e.getAttribute('data-id'),
              links: Array.from(e.querySelectorAll('a[href]')).map(a=>a.href)
            }))""")
            for row in rows:
                for link in row["links"]:
                    p = urlsplit(link)
                    match = re.fullmatch(r"/(\d+)/([A-Za-z0-9]+)", p.path)
                    if p.netloc == urlsplit(self.base).netloc and match and match[1] != self.account:
                        target_id = match[2]  # Stable across feed variants, regardless of data-id availability.
                        if "李荣浩" in row["text"]:
                            found[target_id] = {"target_id": target_id, "native_id": row["id"], "url": public_url(link), "caption": row["text"]}
                        break
            if len(found) >= 60 or self.fixture:
                break
            self.page.mouse.wheel(0, 1000)
            self.page.wait_for_timeout(1000)
        return list(found.values())

    def _target(self, op):
        self.navigate(op["url"])
        article = self.page.locator("article").first
        if not article.count() or "李荣浩" not in article.inner_text():
            raise StopRun("post_topic_membership_unconfirmed")
        return article

    def _like(self, article):
        control = article.locator('[aria-label="赞"][aria-pressed],.woo-like-main').last
        if not control.count():
            raise StopRun("like_control_not_identified")
        pressed = control.get_attribute("aria-pressed")
        if pressed in {"true", "false"}:
            return control, pressed == "true"
        classes = control.get_attribute("class") or ""
        if "woo-like-main" in classes:
            return control, "woo-like-liked" in classes
        raise StopRun("like_state_unconfirmed")

    def _checkbox_off(self, label):
        text = self.page.get_by_text(label, exact=True)
        if text.count() != 1:
            raise StopRun("sharing_checkbox_not_identified")
        control = text.locator("..").locator('input[type="checkbox"]')
        if control.count() != 1:
            raise StopRun("sharing_checkbox_not_identified")
        if control.is_checked():
            control.set_checked(False)

    def _composer(self, text):
        self.topic()
        self.page.get_by_text("发帖", exact=True).click()
        editor = self.page.get_by_placeholder("掐指一算，此贴必火", exact=True)
        editor.wait_for(state="visible", timeout=10000)
        self.page.wait_for_timeout(30 if self.fixture else 500)
        if "发布到李荣浩" not in plain(self.page.locator("body").inner_text()):
            raise StopRun("topic_composer_not_verified")
        editor.fill(text)
        if editor.input_value() != text:
            raise StopRun("composer_text_not_retained")

    def prepare(self, op):
        self.guard()
        kind = op["kind"]
        if kind == "checkin":
            self.topic()
            if self.page.get_by_text("已签到", exact=True).first.is_visible():
                return
            self.page.get_by_text("签到", exact=True).wait_for(state="visible")
        elif kind == "post":
            self._composer(op["text"])
            uploaded = False
            image = op.get("image")
            if image:
                path = Path(image).resolve()
                roots = [Path(".private/media").resolve(), Path("assets").resolve()]
                if any(path.is_relative_to(root) for root in roots) and path.is_file() and path.stat().st_size < 10_000_000 and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
                    magic = path.read_bytes()[:12]
                    if magic.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF8")) or magic.startswith(b"RIFF") and magic[8:12] == b"WEBP":
                        editor = self.page.get_by_placeholder("掐指一算，此贴必火", exact=True)
                        dialog = editor.locator("xpath=ancestor::*[@role='dialog' or contains(@class,'woo-dialog-main') or contains(@class,'woo-modal-main')][1]")
                        if dialog.count() == 1:
                            field = dialog.locator('input[type="file"][accept*=".jpg"]').last
                            try:
                                field.set_input_files(str(path))
                                dialog.locator('[data-upload-status="complete"],[data-pid]').first.wait_for(state="visible", timeout=3000)
                                uploaded = True
                            except PlaywrightError:
                                self.guard()
            if not uploaded:
                # Reopen a clean draft so failed/in-flight media cannot be submitted accidentally.
                self._composer(op["text"] + "[心]")
                op["emoji_fallback"] = True
        elif kind == "like":
            _, liked = self._like(self._target(op))
            if liked:
                raise AlreadyLiked("already_liked_before_run")
        elif kind == "comment":
            self._target(op)
            self._checkbox_off("同时转发")
            self.page.get_by_placeholder("发布你的评论", exact=True).fill(op["text"])
        elif kind == "repost":
            article = self._target(op)
            article.get_by_title("转发", exact=True).last.click()
            self._checkbox_off("同时评论")
            self.page.get_by_placeholder("说说分享心得", exact=True).fill(op["text"])
        self.guard()

    def submit(self, op):
        self.guard()
        self.receipt = None
        if op["kind"] == "checkin":
            self.page.get_by_text("签到", exact=True).click()
        elif op["kind"] == "like":
            control, liked = self._like(self.page.locator("article").first)
            if liked:
                raise AlreadyLiked("like_changed_before_submit")
            control.click()
        else:
            label = {"post": "发送", "comment": "评论", "repost": "转发"}[op["kind"]]
            self.page.get_by_role("button", name=label, exact=True).click()
        self.page.wait_for_timeout(100 if self.fixture else 2500)
        self.guard()
        if self.receipt and self.receipt.get("denied"):
            raise AuthorDenied("author_permission_denied")
        return dict(self.receipt or {})

    def _own_post(self, op):
        hint = op.get("proof_hint", {})
        mid = hint.get("mblogid")
        if mid and re.fullmatch(r"[A-Za-z0-9]+", mid):
            self.navigate(self.base + "/" + self.account + "/" + mid)
        else:
            self.navigate(self.base + "/u/" + self.account)
        for article in self.page.locator("article").all():
            if plain(op["text"]) not in plain(article.inner_text()):
                continue
            links = article.locator("a[href]").evaluate_all("els=>els.map(e=>e.href)")
            if op["kind"] == "repost":
                source_ids = {op["url"].rstrip("/").rsplit("/", 1)[1], op["target_id"], op.get("native_id")}
                if not any(urlsplit(x).netloc == urlsplit(self.base).netloc and urlsplit(x).path.rstrip("/").rsplit("/", 1)[1] in source_ids for x in links):
                    continue
            for link in links:
                p = urlsplit(link)
                if p.netloc == urlsplit(self.base).netloc and re.fullmatch("/" + self.account + r"/[A-Za-z0-9]+", p.path):
                    return {"verified": True, "url": public_url(link), "id": hint.get("id") or p.path.rsplit("/", 1)[1]}
        return None

    def verify(self, op):
        self.guard()
        if op["kind"] == "checkin":
            if self.checkin_status() is True:
                return {"verified": True, "url": self.base + TOPIC_PATH, "id": op["target_id"]}
        elif op["kind"] == "like":
            _, liked = self._like(self._target(op))
            if liked:
                return {"verified": True, "url": op["url"], "id": op["target_id"]}
        elif op["kind"] == "comment":
            self._target(op)
            # Recover from a crash using a DOM permalink/ID when available; never resubmit on ambiguity.
            nodes = self.page.locator('[data-comment-id], [class*="comment"]')
            for node in nodes.all():
                if plain(op["text"]) not in plain(node.inner_text()):
                    continue
                own = node.locator(f'a[href$="/u/{self.account}"],a[href^="/{self.account}/"]')
                if own.count() != 1 or len(node.inner_text()) > len(op["text"]) + 350:
                    continue
                cid = node.get_attribute("data-comment-id")
                if not cid:
                    for link in node.locator("a[href]").evaluate_all("els=>els.map(e=>e.href)"):
                        cid = parse_qs(urlsplit(link).query).get("cid", [None])[0]
                        if cid:
                            break
                cid = cid or op.get("proof_hint", {}).get("id")
                if cid and re.fullmatch(r"[A-Za-z0-9]+", str(cid)):
                    return {"verified": True, "url": op["url"], "id": str(cid)}
        else:
            return self._own_post(op)
        return None
