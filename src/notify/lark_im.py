"""飞书私聊通知 —— 直接给人发消息（不是往群里推）

用途
----
触达撞上验证码（短信/拖拽图形）时，账号需要人工验证。
这时候人不在电脑前是常态，所以必须**主动叫他**：
飞书私聊在手机上有推送，比等网页上的日志强得多。

为什么走 lark-cli
----------------
多维表格那边已经用 lark-cli 写表了，认证和 token 续期都交给它管，
不用再自己维护一套飞书应用凭证。bot 身份发消息的权限已经有了。

用法::

    n = LarkIMNotifier(open_id="ou_xxx")
    ok, err = await n.send_text("触达撞到验证码了")
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from typing import Optional

from ..utils.logger import get_logger

log = get_logger("lark_im")

DEFAULT_CLI_PATH = "/Users/jack/.npm-global/bin/lark-cli"


class LarkIMNotifier:
    """通过 lark-cli 给指定用户发飞书私聊"""

    def __init__(
        self,
        open_id: str = "",
        cli_path: str = "",
        identity: str = "bot",
        timeout: int = 20,
    ):
        self.open_id = (open_id or "").strip()
        self.cli_path = cli_path or DEFAULT_CLI_PATH
        self.identity = (identity or "bot").strip() or "bot"
        self.timeout = timeout

    @property
    def is_configured(self) -> bool:
        return bool(self.open_id)

    # ------------------------------------------------------------------
    def _run_sync(self, args: list[str]) -> dict:
        cmd = [self.cli_path, *args, "--as", self.identity, "--format", "json"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        out = (proc.stdout or "").strip()
        if not out:
            raise RuntimeError(f"lark-cli 无输出: {(proc.stderr or '')[:300]}")
        data = json.loads(out)
        if not data.get("ok", True):
            err = data.get("error") or data
            raise RuntimeError(json.dumps(err, ensure_ascii=False)[:400])
        return data

    async def _run(self, args: list[str]) -> dict:
        return await asyncio.to_thread(self._run_sync, args)

    # ------------------------------------------------------------------
    async def send_text(self, text: str) -> tuple[bool, str]:
        """发一条纯文本私聊"""
        if not self.is_configured:
            return False, "没配 lark_im_open_id"
        try:
            await self._run([
                "im", "+messages-send",
                "--user-id", self.open_id,
                "--text", text,
            ])
            return True, ""
        except Exception as e:
            log.warning("[飞书私聊] 发送失败: %s", e)
            return False, str(e)

    async def send_markdown(self, text: str) -> tuple[bool, str]:
        """发一条富文本私聊（带加粗/换行）"""
        if not self.is_configured:
            return False, "没配 lark_im_open_id"
        try:
            await self._run([
                "im", "+messages-send",
                "--user-id", self.open_id,
                "--markdown", text,
            ])
            return True, ""
        except Exception as e:
            log.warning("[飞书私聊] 发送失败: %s", e)
            return False, str(e)


def get_im_notifier(config=None) -> LarkIMNotifier:
    """按配置构造私聊通知器"""
    if config is None:
        from ..config import get_config

        config = get_config()
    n = config.notify
    return LarkIMNotifier(
        open_id=n.lark_im_open_id,
        identity=n.lark_im_identity,
        cli_path=getattr(n, "bitable_lark_cli_path", "") or DEFAULT_CLI_PATH,
    )
