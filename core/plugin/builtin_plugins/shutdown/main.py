"""
停机/开机控制插件

功能：
- LLM 通过上下文理解后主动调用 bot_shutdown / bot_startup 工具
- 硬编码 QQ 号白名单校验（工具被调用时二次检查 sender）
- 停机后：自动给授权用户发私聊通知，之后只接受授权用户的私聊
- 开机后：缓存消息按时间衰减决定是否回复

使用场景：b站直播时暂停QQ群聊回复
"""

import math
import time
import asyncio

from core.plugin import BasePlugin, logger, on, Priority, register_tool as tool
from core.chat.message_utils import KiraMessageEvent, KiraMessageBatchEvent, MessageChain
from core.chat.message_elements import Text


# 硬编码授权用户（最高权限，不可被配置覆盖）
HARDCODED_AUTHORIZED_USERS = {"341391975", "3095809660", "2924548617"}

# 模块级状态（跨实例共享）
_shutdown_state = {
    "is_shutdown": False,
    "shutdown_time": None,
    "shutdown_by": None,
}


class DefaultPlugin(BasePlugin):
    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        cfg_users = cfg.get("authorized_users", [])
        self._authorized_users = HARDCODED_AUTHORIZED_USERS | set(str(u) for u in cfg_users)
        self._cached_messages: dict[str, list[tuple[KiraMessageEvent, float]]] = {}
        self._decay_half_life = 600  # 10分钟半衰期

    async def initialize(self):
        logger.info(f"[Shutdown Plugin] enabled, authorized users: {self._authorized_users}")

    async def terminate(self):
        pass

    # ==========================================
    # LLM 工具：停机
    # ==========================================
    @tool(
        "bot_shutdown",
        "让自己暂时停机休息（如直播期间暂停群聊回复）。"
        "只有管理员（QQ号 341391975、3095809660、2924548617）要求时才能调用。"
        "调用后会自动通知管理员并暂停群聊回复，直到管理员唤醒。",
        {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "停机原因（如 '主人要开始直播了'）"
                }
            },
            "required": ["reason"]
        }
    )
    async def handle_shutdown(self, event=None, reason: str = "") -> str:
        # event 由 llm_client.execute_tool() 作为第一个位置参数传入
        sender_id = ""
        adapter_name = ""
        if event:
            try:
                sender_id = str(event.messages[-1].sender.user_id)
                adapter_name = event.adapter.name
            except (AttributeError, IndexError):
                pass

        if sender_id and sender_id not in self._authorized_users:
            return f"权限不足：用户 {sender_id} 无权执行停机操作"

        if _shutdown_state["is_shutdown"]:
            return "已经处于停机状态了"

        _shutdown_state["is_shutdown"] = True
        _shutdown_state["shutdown_time"] = time.time()
        _shutdown_state["shutdown_by"] = sender_id

        self._cached_messages.clear()
        logger.info(f"[Shutdown] Bot shutdown by user {sender_id}, reason: {reason}")

        if adapter_name:
            asyncio.create_task(self._notify_admins(adapter_name, reason))

        return f"停机成功，原因：{reason}。已通知管理员，之后只接受管理员私聊。"

    # ==========================================
    # LLM 工具：开机
    # ==========================================
    @tool(
        "bot_startup",
        "从停机状态恢复（如直播结束后恢复群聊回复）。"
        "只有管理员（QQ号 341391975、3095809660、2924548617）要求时才能调用。",
        {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "开机原因（如 '直播结束了'）"
                }
            },
            "required": ["reason"]
        }
    )
    async def handle_startup(self, event=None, reason: str = "") -> str:
        sender_id = ""
        if event:
            try:
                sender_id = str(event.messages[-1].sender.user_id)
            except (AttributeError, IndexError):
                pass

        if sender_id and sender_id not in self._authorized_users:
            return f"权限不足：用户 {sender_id} 无权执行开机操作"

        if not _shutdown_state["is_shutdown"]:
            return "当前没有处于停机状态"

        shutdown_duration = time.time() - (_shutdown_state["shutdown_time"] or time.time())
        cached_count = sum(len(v) for v in self._cached_messages.values())

        _shutdown_state["is_shutdown"] = False
        _shutdown_state["shutdown_time"] = None
        _shutdown_state["shutdown_by"] = None

        logger.info(
            f"[Shutdown] Bot started up by user {sender_id}, "
            f"was down for {shutdown_duration:.0f}s, cached {cached_count} messages"
        )

        asyncio.create_task(self._process_cached_messages())

        return f"开机成功！停机了 {int(shutdown_duration // 60)} 分钟，有 {cached_count} 条缓存消息正在处理。"

    # ==========================================
    # 消息拦截：停机期间只接受授权用户私聊
    # ==========================================
    @on.im_message(priority=Priority.HIGH)
    async def handle_msg(self, event: KiraMessageEvent):
        if not _shutdown_state["is_shutdown"]:
            return

        sender_id = event.message.sender.user_id
        is_dm = not event.is_group_message()
        is_authorized = str(sender_id) in self._authorized_users

        # 授权用户的私聊 → 放行
        if is_dm and is_authorized:
            return

        # 其他所有消息 → 缓存并丢弃
        sid = event.session.sid
        if sid not in self._cached_messages:
            self._cached_messages[sid] = []
        self._cached_messages[sid].append((event, time.time()))
        logger.debug(f"[Shutdown] Message cached for {sid} (total: {len(self._cached_messages[sid])})")
        event.stop()

    # ==========================================
    # 内部方法
    # ==========================================
    async def _notify_admins(self, adapter_name: str, reason: str):
        """停机后给所有授权用户发私聊通知"""
        await asyncio.sleep(1)  # 等停机回复先发出
        msg_processor = self.ctx.message_processor
        content = (
            f"[系统通知] 已进入停机模式\n"
            f"原因：{reason}\n"
            f"停机期间只接受管理员私聊，群聊消息会被缓存。\n"
            f"私聊我说"恢复"或"开机"即可恢复。"
        )
        chain = MessageChain([Text(content)])
        for user_id in self._authorized_users:
            try:
                target = f"{adapter_name}:dm:{user_id}"
                await msg_processor.send_message_chain(target, chain)
                logger.info(f"[Shutdown] Notified admin {user_id}")
            except Exception as e:
                logger.warning(f"[Shutdown] Failed to notify admin {user_id}: {e}")

    async def _process_cached_messages(self):
        """开机后处理缓存消息：按时间衰减决定回复意愿"""
        now = time.time()
        for sid, messages in self._cached_messages.items():
            recent_messages = []
            for evt, msg_time in messages:
                willingness = math.pow(0.5, (now - msg_time) / self._decay_half_life)
                if willingness >= 0.3:
                    recent_messages.append(evt)

            if recent_messages:
                logger.info(
                    f"[Shutdown] Replaying {len(recent_messages)}/{len(messages)} "
                    f"cached messages for {sid}"
                )
                last_evt = recent_messages[-1]
                batch_msg = KiraMessageBatchEvent(
                    message_types=last_evt.message_types,
                    timestamp=int(time.time()),
                    adapter=last_evt.adapter,
                    session=last_evt.session,
                    messages=[e.message for e in recent_messages],
                )
                try:
                    await self.ctx.message_processor.handle_im_batch_message(batch_msg)
                except Exception as e:
                    logger.error(f"[Shutdown] Error replaying cached messages: {e}")

        self._cached_messages.clear()
