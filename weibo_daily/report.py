import json
from pathlib import Path

from .security import safe_error
from .state import TARGETS, artifact_state, complete, counts


def render(state: dict, mode: str, plans=None) -> str:
    c = counts(state)
    status = "全部完成" if mode == "execute" and complete(state) and state.get("status") == "complete" else (
        "只读检查（未提交）" if mode == "check" else "dry-run（未提交）" if mode == "dry-run" else
        "部分完成" if any(c.values()) else "执行失败")
    prior_sign = any(o["kind"] == "checkin" and o.get("observed_existing") for o in state["operations"])
    sign = "已完成（当天原已签到）" if c["checkin"] and prior_sign else "已完成（本次核实）" if c["checkin"] else "未确认"
    lines = ["# 李荣浩超话每日打卡报告", "", "日期：" + state["date"], "模式：" + mode,
             "环境：" + state.get("environment", "尚未连接"),
             "登录状态：" + state.get("login", "unconfirmed"), "签到：" + sign,
             f"发帖：{c['post']}/2", f"点赞：{c['like']}/15", f"评论：{c['comment']}/5", f"转发：{c['repost']}/3"]
    for label, key in [("签到排名", "rank"), ("积分", "points"), ("经验值", "experience")]:
        value = state.get("metrics", {}).get(key)
        lines.append(label + "：" + (str(value) if value is not None else "未能读取"))
    lines += ["", "## 已确认操作"]
    for op in state["operations"]:
        if op["status"] == "confirmed":
            e = op.get("evidence", {})
            lines.append(f"- {op['kind']} | {op.get('target_id', '')} | {e.get('url', '')} | {op.get('text', '')}")
            if op.get("emoji_fallback"):
                lines.append("  - 表情备用方案，未声称上传图片。")
    lines += ["", "## 失败及未确认操作"]
    for op in state["operations"]:
        if op["status"] != "confirmed":
            lines.append(f"- {op['kind']} | {op['status']} | {op.get('target_id', '')} | {op.get('error', '')}")
    if state.get("reason"):
        lines.append("停止原因：" + state["reason"])
    lines += ["", "剩余任务：" + "，".join(f"{k} {max(0, n-c[k])}" for k, n in TARGETS.items()),
              "最终状态：" + status, "调度配置：本次程序未暂停、删除或重新启用 GitHub Actions。"]
    if plans is not None:
        lines += ["", "## 计划（没有提交）", *[f"- {p['kind']} | {p.get('target_id', '')}" for p in plans]]
    return safe_report("\n".join(lines) + "\n")


def safe_report(text):
    return "\n".join(safe_error(line) for line in text.splitlines()) + "\n"


def write_report(directory: Path, state, mode, plans=None):
    directory.mkdir(parents=True, exist_ok=True)
    report = render(state, mode, plans)
    (directory / "report.md").write_text(report)
    (directory / "state.json").write_text(json.dumps(artifact_state(state), ensure_ascii=False, indent=2))
    return report
