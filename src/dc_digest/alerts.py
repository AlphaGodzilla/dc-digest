"""运行失败与降级时的通知内容。

把异常翻译成给人看的短消息：失败根因、影响窗口、可执行的下一步。
只负责排版，发送由 cli 调用 FeishuSender 完成。
"""

from __future__ import annotations

from .discord_api import DiscordError
from .feishu import FeishuError
from .summarize import SummarizeError
from .timewin import TimeWindowError

STATUS_HINTS = {
    401: "token 失效、账号被强制重登或已被封禁：重新登录 Discord 取新 token 并更新 Keychain 条目",
    403: "权限或账号受限：确认该账号对该频道的「查看频道」「读取历史消息」权限，并检查账号是否被限制",
    404: "频道已被删除或 ID 已失效：核对 config.toml 的 channel_id",
}


def alert_hint(exc: BaseException) -> str:
    if isinstance(exc, DiscordError):
        if exc.status in STATUS_HINTS:
            return STATUS_HINTS[exc.status]
        if "网络请求失败" in str(exc):
            return "网络或代理问题：检查代理是否放行 discord.com 与 cdn.discordapp.com，或临时关掉代理直连"
        return "Discord 接口返回异常：看下方原始错误，必要时降低单轮请求数后再试"
    if isinstance(exc, FeishuError):
        return "飞书侧失败：检查应用凭证与机器人是否仍在目标会话内"
    if isinstance(exc, SummarizeError):
        return "摘要子进程失败：手动执行 `[summarize] args` 里的命令确认模型与凭证可用"
    if isinstance(exc, TimeWindowError):
        return "时间参数不合法：检查 --from / --to 的写法"
    return "未归类的异常：看下方原始错误并检查运行环境"


def format_failure(
    *,
    command: str,
    window_text: str | None,
    exc: BaseException,
    now_text: str,
) -> str:
    lines = [
        f"**命令**：{command}",
        f"**时间**：{now_text}",
    ]
    if window_text:
        lines.append(f"**窗口**：{window_text}")
    lines.append(f"**错误类型**：{type(exc).__name__}")
    lines.append(f"**原始错误**：{str(exc).strip()}")
    lines.append(f"**建议**：{alert_hint(exc)}")
    return "\n".join(lines)


def format_degraded(
    *,
    command: str,
    window_text: str,
    reason: str,
    incomplete: list[str],
    now_text: str,
) -> str:
    lines = [
        f"**命令**：{command}",
        f"**时间**：{now_text}",
        f"**窗口**：{window_text}",
        f"**降级原因**：{reason}",
    ]
    if incomplete:
        lines.append("**未抓齐的频道**：" + "、".join(incomplete))
    lines.append("**建议**：等本轮退避结束后重跑同一条命令，游标会从断点续传")
    return "\n".join(lines)
