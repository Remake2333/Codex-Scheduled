# 安全边界与验收

- 默认是 dry-run，真实模式需要 CLI 明确确认、两个仓库开关和内容审批，且 workflow 仅允许默认分支。
- 不使用 TinyFish，不解决验证码，不加隐身补丁，不轮换代理，不绕过平台权限。
- 会话通过 Playwright 官方 storage_state 获取，在内存中使用 cryptography Fernet 加密；密钥、密文分别进入 GitHub Environment Secrets，不进入仓库、状态分支或 Artifacts。
- 确认授权数字账号 ID，拒绝密文账号与执行账号不一致。建议为 weibo 环境设置默认分支限制，并在人工验收期间设置 required reviewers。
- GitHub Actions 仅使用已核实的官方 action commit SHA；CI 不使用真实 Secrets，不使用 pull_request_target。
- .private、.state、out、会话 JSON 与密文均被 Git 忽略。Artifact 上传采用具体文件白名单，不打包整个工作区。
- 每次外部提交前先持久化 pending。未知结果不直接重发，已确认结果不重复，喜欢状态无法判断时不点击。
- 同一天遇到验证码或 HTTP 403/418/429 后保留阻止状态，禁止通过工作流重跑绕过。
- 对单个作者的评论权限拒绝只跳过该帖，不自动关注；评论和转发的附带分享复选框会关闭。
- 不将任意原始 Playwright/HTTP 异常、HTML、浏览器录像或截图写入业务日志。异常仅输出预定义原因或异常类型；公开 URL 去掉认证查询参数。
- 控制状态分支的写权限很重要：仓库维护者能修改程序和记录，不能把未受信任 PR 或状态文件视为可信授权。

安全扫描不是完整渗透测试。交付包含会话篡改/错误密钥/账号不符、检查点失败、崩溃恢复、失效登录、访问拒绝和只读零写入测试。真实微博 DOM 适配和真实账号运行仍需用户批准后的验收。

撤销授权方法、暂停/停止方法以及公开仓库状态记录的可见性见 README。
