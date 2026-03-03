"""
新人进群问候插件

当检测到 [System 用户xxx加入了群聊] 的 notice 消息时，
修改消息内容，引导 LLM 以自然的方式欢迎新人。

支持配置固定欢迎图片，在 LLM 问候语之后自动追加发送。

实现方式：拦截 ON_IM_BATCH_MESSAGE，检测 notice 消息中的进群通知，
注入欢迎提示词到消息内容中，让 LLM 自由发挥问候方式。
"""

import re
import asyncio

from core.plugin import BasePlugin, logger, on, Priority
from core.chat.message_utils import KiraMessageBatchEvent, MessageChain
from core.chat.message_elements import Image


# 匹配 "[System 用户xxx加入了群聊]"
JOIN_PATTERN = re.compile(r"\[System 用户(\d+)加入了群聊\]")


class DefaultPlugin(BasePlugin):
    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        enabled_groups = cfg.get("enabled_groups", [])
        self._enabled_groups = set(str(g) for g in enabled_groups) if enabled_groups else None
        self._welcome_image = (cfg.get("welcome_image") or "").strip()

    async def initialize(self):
        scope = "所有群" if self._enabled_groups is None else f"群: {self._enabled_groups}"
        img_info = f", image: {self._welcome_image}" if self._welcome_image else ", no image"
        logger.info(f"[Welcome Plugin] enabled for {scope}{img_info}")

    async def terminate(self):
        pass

    @on.im_batch_message(priority=Priority.HIGH)
    async def handle_batch_event(self, event: KiraMessageBatchEvent):
        # 只处理群聊
        if not event.is_group_message():
            return

        # 检查群是否启用
        group_id = event.session.session_id
        if self._enabled_groups is not None and group_id not in self._enabled_groups:
            return

        # 检查是否有进群通知
        for message in event.messages:
            if not message.is_notice:
                continue
            text = message.message_str or ""
            match = JOIN_PATTERN.search(text)
            if match:
                new_user_id = match.group(1)
                # 用更自然的引导替换原始 notice 文本
                message.message_str = (
                    f"[System 新人 {new_user_id} 加入了群聊！"
                    f"请以你的风格热情地欢迎ta。回复中请用 <at>{new_user_id}</at> 来@这位新人。"
                    f"欢迎内容需要包含以下要点（用你自己的语气自然地表达，不要生硬复制）：\n"
                    f"1. 热情打招呼，像朋友一样\n"
                    f"2. 告诉ta：为了帮助大家快速融入这个「人机共育」家庭，"
                    f"Neon精心设计了一套新人激活任务，完成任意1项即可获得「活跃舰长」认证～\n"
                    f"3. 可以简短介绍一下自己和这个群的氛围\n"
                    f"请保持你一贯的可爱风格，不要太正式]"
                )
                logger.info(f"[Welcome] New member {new_user_id} joined group {group_id}")

                # 如果配置了固定图片，在 LLM 问候语发出后追加发送
                if self._welcome_image:
                    adapter_name = event.adapter.name
                    target = f"{adapter_name}:gm:{group_id}"
                    asyncio.create_task(
                        self._send_welcome_image(target, new_user_id)
                    )

    async def _send_welcome_image(self, target: str, new_user_id: str):
        """等 LLM 问候语发出后，追加发送固定欢迎图片"""
        await asyncio.sleep(8)  # 等待 LLM 回复先到达
        try:
            chain = MessageChain([Image(image=self._welcome_image)])
            msg_processor = self.ctx.message_processor
            await msg_processor.send_message_chain(target, chain)
            logger.info(f"[Welcome] Sent welcome image to {target} for new member {new_user_id}")
        except Exception as e:
            logger.warning(f"[Welcome] Failed to send welcome image: {e}")
