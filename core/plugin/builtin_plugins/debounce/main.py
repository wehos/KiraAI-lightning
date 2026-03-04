"""
消息去抖 + 唤醒/休眠 状态机插件

群聊消息流模型：
- SLEEPING（默认）：只有唤醒条件能触发处理
  - @机器人 / 引用机器人的消息 → 唤醒，且该消息必定回复
  - 关键词匹配 → 唤醒，且该消息必定回复
  - 特定用户（waking_users）的任何消息 → 唤醒，回复意愿由 LLM 判断
- AWAKE：所有消息都会被缓冲处理
  - 回复意愿由 LLM 自行判断（@/引用 = 必须回复，其他 = 自由判断）
  - 收不到新消息超过 idle_timeout → 休眠

私聊：所有消息直接处理，不走状态机。

去抖逻辑保持不变：消息先 buffer，debounce_interval 内无新消息或 buffer 满则 flush。
"""

import time
import asyncio

from core.plugin import BasePlugin, logger, on, Priority
from core.chat.message_utils import KiraMessageEvent, KiraMessageBatchEvent


# 默认唤醒用户（可被配置覆盖）
DEFAULT_WAKING_USERS = {"341391975", "3095809660", "2924548617"}


class DefaultPlugin(BasePlugin):
    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        bot_cfg = ctx.config["bot_config"].get("bot", {})
        self.debounce_interval = float(bot_cfg.get("max_message_interval", 1.5))
        self.max_buffer_messages = int(bot_cfg.get("max_buffer_messages", 3))
        self.idle_timeout = float(cfg.get("idle_timeout", 10.0))

        # 唤醒用户列表
        cfg_users = cfg.get("waking_users", [])
        self._waking_users = DEFAULT_WAKING_USERS | set(str(u) for u in cfg_users)

        # 去抖状态
        self.session_events: dict[str, asyncio.Event] = {}
        self.session_tasks: dict[str, asyncio.Task] = {}

        # 唤醒/休眠状态机（仅群聊）
        self._awake_sessions: dict[str, float] = {}   # sid → last_message_time
        self._idle_tasks: dict[str, asyncio.Task] = {}

    async def initialize(self):
        logger.info(
            f"[Debounce Plugin] enabled, idle_timeout={self.idle_timeout}s, "
            f"waking_users={self._waking_users}"
        )

    async def terminate(self):
        for task in self._idle_tasks.values():
            task.cancel()
        for task in self.session_tasks.values():
            task.cancel()

    # ==========================================
    # 核心消息处理
    # ==========================================

    @on.im_message(priority=Priority.HIGH)
    async def handle_msg(self, event: KiraMessageEvent):
        sid = event.session.sid
        is_group = event.is_group_message()

        # 私聊：直接放行，保持原有逻辑
        if not is_group:
            event.buffer()
            self._kick_debounce(sid)
            return

        # === 群聊：唤醒/休眠状态机 ===
        is_awake = sid in self._awake_sessions
        sender_id = str(event.message.sender.user_id)

        # 判断唤醒条件
        is_direct_trigger = event.is_mentioned  # @、引用、关键词（已在 adapter 层设置）
        is_waking_user = sender_id in self._waking_users

        if not is_awake:
            # SLEEPING → 只有唤醒条件能激活
            if not is_direct_trigger and not is_waking_user:
                event.stop()
                return
            # WAKE UP
            self._awake_sessions[sid] = time.time()
            logger.info(
                f"[Wake] Session {sid} woke up "
                f"(trigger={'mention' if is_direct_trigger else 'waking_user'}, "
                f"sender={sender_id})"
            )

        # AWAKE — 更新活跃时间
        self._awake_sessions[sid] = time.time()

        # 缓冲消息
        event.buffer()

        # 重置空闲计时器
        self._reset_idle_timer(sid)

        # 去抖逻辑
        buffer_len = self.ctx.message_processor.get_session_buffer_length(sid)
        if buffer_len + 1 >= self.max_buffer_messages:
            event.flush()
            return

        self._kick_debounce(sid)

    # ==========================================
    # 去抖循环
    # ==========================================

    def _kick_debounce(self, sid: str):
        """触发去抖计时器"""
        if sid not in self.session_events:
            self.session_events[sid] = asyncio.Event()
        if sid not in self.session_tasks:
            self.session_tasks[sid] = asyncio.create_task(self._debounce_loop(sid))
        self.session_events[sid].set()

    async def _debounce_loop(self, sid: str):
        event = self.session_events[sid]
        while True:
            await event.wait()
            event.clear()
            try:
                await asyncio.sleep(self.debounce_interval)
            except asyncio.CancelledError:
                break
            if event.is_set():
                continue
            buffer_len = self.ctx.message_processor.get_session_buffer_length(sid)
            if buffer_len == 0:
                continue
            await self.ctx.message_processor.flush_session_messages(sid)

    # ==========================================
    # 空闲休眠
    # ==========================================

    def _reset_idle_timer(self, sid: str):
        """重置空闲计时器，idle_timeout 后进入休眠"""
        if sid in self._idle_tasks:
            self._idle_tasks[sid].cancel()
        self._idle_tasks[sid] = asyncio.create_task(self._idle_sleep(sid))

    async def _idle_sleep(self, sid: str):
        """空闲超时 → 休眠，flush 剩余 buffer"""
        try:
            await asyncio.sleep(self.idle_timeout)
        except asyncio.CancelledError:
            return

        # 超时 → 休眠
        self._awake_sessions.pop(sid, None)
        logger.info(f"[Sleep] Session {sid} went to sleep (idle {self.idle_timeout}s)")

        # flush 剩余缓冲
        buffer_len = self.ctx.message_processor.get_session_buffer_length(sid)
        if buffer_len > 0:
            await self.ctx.message_processor.flush_session_messages(sid)

    # ==========================================
    # Batch 事件（保留 hook 点）
    # ==========================================

    @on.im_batch_message(priority=Priority.MEDIUM)
    async def handle_batch_event(self, event: KiraMessageBatchEvent):
        pass
