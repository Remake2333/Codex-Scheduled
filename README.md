# 李荣浩超话每日自动打卡系统

Python 3.12 + Playwright + Chromium + GitHub Actions。目标为签到 1 次、发帖 2 条、点赞 15 条不同帖子、评论 5 条不同帖子、转发 3 条不同帖子。每天北京时间 09:00（UTC 01:00）触发，GitHub 可能延迟或在高负载时丢弃计划任务，不能承诺准点或永不间断。

**交付默认不操作真实账号。** CI 只访问本地模拟页面；业务执行需要开启两个变量、通过内容审批并提供加密会话。尚未完成真实微博写操作验收，网页改版可能需要维护定位规则。遇到验证码、安全验证或访问限制立即停止，不使用 TinyFish、验证码破解、隐身补丁或代理轮换。

## 项目结构

```text
.github/workflows/ci.yml       # 无凭据的本地模拟自测
.github/workflows/daily.yml    # 每日计划和手动运行，真实执行默认关闭
weibo_daily/
  __main__.py                 # keygen / login / run
  browser.py                  # 仅通过浏览器页面操作微博
  runner.py                   # 串行执行、计时、去重、恢复和停止条件
  state.py                    # JSON 原子检查点与本机锁
  github.py                   # GitHub 状态分支及 Issues 通知
  security.py                 # Fernet 加密与安全输出
  content.py                  # 用户审核的文案和上下文匹配
  report.py                   # Markdown 报告
config/content.json           # 未审批的示例文案；approved 默认 false
assets/                       # 仅放拥有使用授权、可公开的图片
tests/fixtures/weibo.html      # 本地模拟微博，不连接真实账号
tests/test_core.py
tests/test_browser.py
scripts/                      # 自测、工作流检查、凭据扫描
SECURITY.md
TEST_REPORT.md
```

## 安装与安全自测

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m playwright install chromium
python -m compileall -q weibo_daily tests scripts
python scripts/check_workflows.py
python scripts/security_check.py
python scripts/selftest.py
```

Linux 缺少系统依赖时使用官方命令 `python -m playwright install --with-deps chromium`。若已有兼容系统浏览器，可仅在测试时设置 `PLAYWRIGHT_CHROMIUM_EXECUTABLE=/usr/bin/chromium`。CI 使用 Playwright 配套浏览器。

自测不需要微博或 GitHub Secrets。模拟页面通过回环 HTTP 服务运行，并在浏览器网络层拒绝所有外部请求。测试中的“完整执行”只是模拟页面的 26 项操作，不代表访问过真实微博。`out/tests.txt` 是当次测试结果，退出码会保留失败状态；零测试或跳过测试不会算通过。

工作流还使用固定版本 actionlint：

```bash
go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.7
```

本地没有 Go 时可以从 actionlint 官方 Releases 获取对应平台文件，先核对官方 SHA-256 校验和再运行；不要禁用 TLS、包签名或校验。

## 首次授权与加密会话

每日运行完全在 GitHub 托管 runner 上，不需要用户电脑开机。首次登录是一次性人工步骤：在你信任的、有图形浏览器的 Linux/macOS 设备或授权云桌面上操作；不是让 GitHub Actions 绕过扫码。

1. 确认是自己的账号，取得个人主页中的数字账号 ID。
2. 在项目目录生成密钥，并通过官方网页手动登录：

```bash
export WEIBO_ACCOUNT_ID=你的数字账号ID
python -m weibo_daily keygen
python -m weibo_daily login
```

`keygen` 不覆盖已有密钥，不打印密钥。`login` 打开可见的 Chromium，你自行扫码或完成网站要求的正常验证，完成后在终端按回车。程序检查实际账号及登录后控件，仅在核实后使用 Playwright 官方 `storage_state(indexed_db=True)` 获取状态，直接在内存中加密。

生成 `.private/session.key` 和 `.private/session.enc`，权限 0600，均已被 Git 忽略。不生成明文会话 JSON。密文绑定授权账号 ID，错误账号、错误密钥或篡改数据会被拒绝。密文和密钥分别保存在 GitHub Secrets，绝不放入仓库、状态分支或 Artifacts。密文超过单个 Secret 允许的大小时停止，不截断。

3. GitHub 仓库 Settings → Environments 新建 `weibo`，只允许默认分支部署。在该环境中添加两个 Secret：

| Secret | 内容 |
| --- | --- |
| `WEIBO_SESSION_KEY` | `.private/session.key` 文件内容 |
| `WEIBO_SESSION_ENCRYPTED` | `.private/session.enc` 文件内容 |

可以通过 GitHub 安全设置页面填写，或在已授权的 GitHub CLI 中使用标准输入，不把值写进命令参数或聊天：

```bash
gh secret set WEIBO_SESSION_KEY --env weibo < .private/session.key
gh secret set WEIBO_SESSION_ENCRYPTED --env weibo < .private/session.enc
gh variable set WEIBO_ACCOUNT_ID --body 你的数字账号ID
gh variable set WEIBO_AUTOMATION_ENABLED --body false
gh variable set WEIBO_ALLOW_LIVE --body false
```

两个开关使用**仓库级 Variables**，不要仅放在 Environment 中，否则作业入口条件可能无法读取。GitHub 托管 runner 使用临时内存中的解密状态，不把明文状态写到 Artifacts。会话可能过期或被微博要求再次验证，届时停止并通知；需要重新执行人工授权并更新 Secrets，不能保证永久免登录。

## 上线前验收与启用

代码可以先推送，**不要立即开启真实互动**。先完成本地模拟自测和 ChatGPT 协助验收。

1. Actions → `Weibo daily (disabled until authorized)` → Run workflow，选择 `check`。它检查真实页面和登录，不提交操作。
2. 再选择 `dry-run`。它读取页面并列出可执行计划，但不点击签到、发送、点赞、评论或转发，也不修改远程执行记录或发布 Issues。
3. 审阅 `config/content.json`。示例文案不等于已经得到你的内容审批。确认后才将 `approved` 改为 `true` 并提交。评论只在正文匹配已审批的上下文规则时生成，没有合适内容就少做并报告，绝不用无意义回复凑数。
4. 可将明确获得使用授权且可以公开的图片放到 `assets/`，在帖子对象的 `image` 填相对路径。仅允许图片目录中的已验证图片文件；无图、上传失败或无法确认上传完成时，重新打开干净的编辑框并使用微博表情，报告会标注备用方案。`.private/media/` 仅适合可信本机使用，不会自动上传到 GitHub。
5. **验收通过并明确批准真实运行后**，才设置：

```bash
gh variable set WEIBO_AUTOMATION_ENABLED --body true
gh variable set WEIBO_ALLOW_LIVE --body true
```

手动 `execute` 和定时运行都会检查两个开关、内容审批、账号、会话及页面状态；缺一即停止。CLI 还要求显式 `--confirm-live`。本次开发交付不会替你开启这些设置或执行真实互动。

GitHub Actions 中两个工作流有区别：CI 可以自动运行安全测试；每日业务作业在未开启 `WEIBO_AUTOMATION_ENABLED` 时被跳过。手动 `check` / `dry-run` 仍可用于验收。全局 concurrency group 串行化所有日期和手动运行，`cancel-in-progress: false` 不会为了新任务中断正在提交的旧任务。

## 状态保存与失败恢复

真实 GitHub 执行使用独立的 `weibo-state` 分支，按 `daily/<账号ID>/YYYY-MM-DD.json` 保存 JSON。第一次真实运行才按需从默认分支建立状态分支；不要在该分支配置额外推送触发器。CI 仅对 `main` 的推送和 PR 运行，不会被每日状态提交反复触发。

每次操作流程：

1. 通过页面准备操作，并排除已有点赞或作者权限限制。
2. 等到已持久化的随机间隔结束；每次独立取 5–15 秒，跨轮次、重跑和午夜也保留等待边界。
3. 在远程 JSON 中写入 `pending` 和下一次最早执行时间。远程保存失败就不点击提交。
4. 串行提交，记录服务端回执中的公开操作 ID（若有）。
5. 再通过实际页面验证，只有核实后才写入 `confirmed`。

因此 runner 被强制终止、进程崩溃或 Artifacts 上传失败，不会使已经提交的操作直接丢失后被盲目重发。重跑会先处理 `pending/uncertain`：能通过页面确认就恢复，否则保留未确认状态并停止，不直接重发。已有点赞永远不再次点击取消；作者禁止评论或转发则跳过，不为此自动关注。

状态分支不保存会话、Cookie 或密钥，只保存已计划发布的文案、公开帖子 ID、时间和安全错误码。公开仓库的该分支也是公开的，请不要在自定义文案中放入个人秘密。只有受信任的维护者应有修改该分支的权限。

本机执行使用 `.state/YYYY-MM-DD.json`、原子替换和文件锁；正式长期云端运行应使用 GitHub 远程检查点。GitHub 状态保存使用 compare-and-swap SHA，发生冲突、权限问题或网络错误时停止，绝不在检查点不可用时继续提交。

程序最多使用 30 分钟执行预算，单次普通加载最多尝试两次；轮次之间自动补缺，连续三轮没有新增已确认操作则停止。跨北京时间日期停止，下一次使用新日期记录。403/418/429、验证或账号限制会停止整个运行，并在当天状态中保留阻止标记，不能用重跑绕过。暂停一次运行不会自动关闭每日调度。

## 报告、暂停与撤销授权

每次运行生成 `out/report.md` 和 `out/state.json`，写入 Actions Job Summary，并以 Artifacts 保留 7 天。只有执行模式且签到、两帖、十五赞、五评论、三转发都得到页面核实时才报告“全部完成”。只读和 dry-run 永不报告真实任务全部完成。签到人数不会被误当作个人排名，未显示的积分写“未能读取”。

真实执行通过当前仓库 `GITHUB_TOKEN` 创建或更新当天的 GitHub Issue。需要开启仓库 Issues，以及允许工作流申请 `contents: write` 和 `issues: write`；否则远程检查点或通知会明确失败。Issue 通知失败会使工作流失败，Artifact 仍尝试保留。是否收到邮件/手机推送，取决于你在 GitHub 的 Watch 和通知设置，程序不保证邮件必达。

暂停未来真实执行：将 `WEIBO_AUTOMATION_ENABLED=false`、`WEIBO_ALLOW_LIVE=false`；也可在 Actions 页面 Disable workflow。停止当前运行：Actions → Cancel workflow。本机可创建 `.state/STOP` 或发送 SIGTERM/SIGINT；已进入提交中的操作可能无法撤回，但提交前的检查点可以避免盲目重复。

撤销授权：先暂停，取消正在运行的作业，删除 `weibo` 环境的两个会话 Secrets，在微博官方账号安全设置中退出相应设备/撤销会话，再删除可信设备上的 `.private/` 文件。仅删除本机文件不能替代服务器端会话撤销。不要在日志或 Issue 中提交任何凭据。

## 免费额度与资源估算

- GitHub 托管的标准 Linux runner 对符合政策的公开仓库通常免费；本仓库为公开仓库。平台条款、自动化用途限制及账户政策仍适用，不保证永久免费。
- 私有仓库要看账户计划。以每次 15–35 分钟、每天一次估计，每月约 450–1050 Linux runner 分钟，外加 CI。若你的计划包含 2000 分钟/月，通常可容纳这一估计；当前实际额度必须以账户 Billing 为准。
- 业务作业有 40 分钟工作流上限，程序本身默认 30 分钟。页面慢、依赖安装、网络限制会增加时间或导致部分完成，不能为赶数量绕过停止条件。
- 不保留视频、浏览器录像、页面快照或原始会话，报告一般为几十 KB，7 天保留通常远小于 MB 级；远程状态分支的 Git 历史会长期累积，需定期查看仓库大小。
- 超出免费额度可能计费，应设置账单预算或禁用超额付费。此开发环境读取 GitHub 最新计费文档被网络策略拒绝，因此以上为预算示例，不是对当前套餐额度的核价保证。

官方参考：
- https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule
- https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency
- https://docs.github.com/en/actions/security-for-github-actions/security-guides/using-secrets-in-github-actions
- https://playwright.dev/python/docs/auth
- https://cryptography.io/en/latest/fernet/

## 已知限制

微博页面没有为本项目承诺稳定的 DOM 接口。本次仅以本地模拟页面验证各路径，没有执行真实写操作；实际登录导航、评论 ID、点赞选中状态或图片完成标记变化时，会停止或进入未确认，而不是猜测点击。特别是恢复时无法从 DOM 找到评论 ID，就必须人工核验，不能保证自动补齐。

示例文案采用本地、可审阅的上下文规则，不调用付费模型；它不是无限创作系统，长期使用需要维护文案和匹配规则。匹配不足、帖子不足、会话过期、GitHub 计划任务延迟/停用、微博对云 IP 的限制，都可能使某天部分完成。公开仓库长期无活动时，GitHub 也可能停用计划任务，用户需检查平台提示。

完整操作记录可帮助恢复，但它不保证突破平台时间、权限或风控限制。开发完成后先进行测试验收；未获得进一步确认，不执行真实微博互动。
