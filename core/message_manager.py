import asyncio
import time
from asyncio import Lock
import xml.etree.ElementTree as ET
from typing import Union, Any, List
from pathlib import Path
from asyncio import Semaphore
import random
import os

from core.logging_manager import get_logger
from core.services.runtime import get_adapter_by_name
from core.utils.common_utils import image_to_base64
from core.utils.path_utils import get_data_path
from core.chat.message_utils import (
    KiraMessageEvent,
    KiraMessageBatchEvent,
    KiraCommentEvent,
    MessageChain,
    KiraIMSentResult,
)
from core.prompt_manager import Prompt

from core.chat.message_elements import (
    BaseMessageElement,
    Text,
    Image,
    At,
    Reply,
    Forward,
    Emoji,
    Sticker,
    Record,
    Notice,
    Poke,
    File,
)

from core.llm_client import LLMClient
from core.chat.memory_manager import MemoryManager
from .prompt_manager import PromptManager
from .provider import ProviderManager, LLMRequest, LLMResponse
from core.plugin.plugin_handlers import event_handler_reg, EventType

logger = get_logger("message_processor", "cyan")
llm_logger = get_logger("llm", "purple")


class SessionBuffer:
    def __init__(self, max_count: int = None):
        self.buffer: list = []
        self.lock: asyncio.Lock = asyncio.Lock()
        self.max_count = max_count

    def add(self, message: KiraMessageEvent):
        self.buffer.append(message)

    def flush(self, count: int = None):
        if count and count <= len(self.buffer):
            pending_messages = self.buffer[:count]
            del self.buffer[:count]
        else:
            pending_messages = self.buffer[:]
            self.buffer.clear()
        return pending_messages

    def get_length(self):
        return len(self.buffer)

    def get_buffer_lock(self) -> Lock:
        """get buffer lock"""
        return self.lock


class SessionBufferManager:
    def __init__(self, max_count: int = None):
        self.buffers: dict[str, SessionBuffer] = {}
        self.max_count = max_count

    def get_buffer(self, session: str):
        if session not in self.buffers:
            self.buffers[session] = SessionBuffer(self.max_count)
        return self.buffers[session]


class MessageProcessor:
    """Core message processor, responsible for handling all message sending and receiving logic"""

    def __init__(
        self,
        kira_config,
        llm_api: LLMClient,
        provider_manager: ProviderManager,
        memory_manager: MemoryManager,
        prompt_manager: PromptManager,
        max_concurrent_messages: int = 3,
    ):
        self.kira_config = kira_config
        self.bot_config = kira_config["bot_config"].get("bot")
        self.max_message_interval = float(self.bot_config.get("max_message_interval"))
        self.max_buffer_messages = int(self.bot_config.get("max_buffer_messages"))
        self.min_message_delay = float(self.bot_config.get("min_message_delay", "0.8"))
        self.max_message_delay = float(self.bot_config.get("max_message_delay", "1.5"))

        self.llm_api = llm_api
        self.provider_mgr = provider_manager

        self.message_processing_semaphore = Semaphore(max_concurrent_messages)

        # managers
        self.memory_manager = memory_manager
        self.prompt_manager = prompt_manager

        # message buffer
        self.session_locks: dict[str, asyncio.Lock] = {}

        self.session_buffer = SessionBufferManager(max_count=self.max_buffer_messages)

        logger.info("MessageProcessor initialized")

    def get_session_lock(self, sid: str) -> Lock:
        """get session lock to avoid sending message simultaneously"""
        if sid not in self.session_locks:
            self.session_locks[sid] = asyncio.Lock()
        return self.session_locks[sid]

    def get_session_list_prompt(self) -> str:
        session_list_prompt = ""
        _chat_memory = self.memory_manager.chat_memory
        for session_id in _chat_memory:
            session_list_prompt += f"{session_id}\n"
        return session_list_prompt

    def get_session_buffer_length(self, sid: str) -> int:
        buffer = self.session_buffer.get_buffer(sid)
        return buffer.get_length()

    async def flush_session_messages(
        self, sid: str, extra_event: KiraMessageEvent | None = None
    ) -> bool:
        buffer = self.session_buffer.get_buffer(sid)
        async with buffer.lock:
            if extra_event is not None:
                buffer.add(extra_event)
            pending_messages: list[KiraMessageEvent] = buffer.flush()
        if not pending_messages:
            return False
        last_event = pending_messages[-1]
        batch_msg = KiraMessageBatchEvent(
            message_types=last_event.message_types,
            timestamp=int(time.time()),
            adapter=last_event.adapter,
            session=last_event.session,
            messages=[m.message for m in pending_messages],
        )
        await self.handle_im_batch_message(batch_msg)
        return True

    async def message_format_to_text(self, message_list: list[BaseMessageElement], collect_images: list = None):
        """将平台使用标准消息格式封装的消息转换为LLM可以接收的字符串

        Args:
            message_list: 消息元素列表
            collect_images: 如果传入一个 list，图片 base64 会追加到其中（供 VL 模型直接使用）
                            如果为 None，图片会退回 VLM 描述模式（兼容旧逻辑）
        """
        message_str = ""
        for ele in message_list:
            if isinstance(ele, Text):
                message_str += ele.text
            elif isinstance(ele, Emoji):
                message_str += f"[Emoji {ele.emoji_id}]"
            elif isinstance(ele, At):
                if ele.nickname:
                    message_str += f"[At {ele.pid}(nickname: {ele.nickname})]"
                else:
                    message_str += f"[At {ele.pid}]"
            elif isinstance(ele, Image):
                if collect_images is not None:
                    # VL 模型直接看图模式：收集 base64，文本中只放占位符
                    try:
                        b64_data = await image_to_base64(ele.url)
                        if b64_data:
                            collect_images.append(b64_data)
                            message_str += "[图片]"
                        else:
                            message_str += "[图片加载失败]"
                    except Exception as e:
                        logger.warning(f"Image base64 conversion failed: {e}")
                        message_str += "[图片加载失败]"
                else:
                    # 退回 VLM 描述模式
                    img_desc = await self.llm_api.desc_img(ele.url)
                    message_str += f"[Image {img_desc}]"
            elif isinstance(ele, Sticker):
                if collect_images is not None:
                    try:
                        if ele.sticker_bs64:
                            collect_images.append(ele.sticker_bs64)
                            message_str += "[表情包]"
                        else:
                            message_str += "[表情包加载失败]"
                    except Exception:
                        message_str += "[表情包加载失败]"
                else:
                    sticker_desc = await self.llm_api.desc_img(
                        ele.sticker_bs64, is_base64=True
                    )
                    message_str += f"[Sticker {sticker_desc}]"
            elif isinstance(ele, Reply):
                if ele.chain:
                    ele.chain.message_list = [
                        x for x in ele.chain if not isinstance(x, Reply)
                    ]
                    reply_content = await self.message_format_to_text(
                        ele.chain.message_list
                    )
                    message_str += f"[Reply {reply_content}]"
                elif ele.message_content:
                    message_str += f"[Reply {ele.message_content}]"
                else:
                    message_str += f"[Reply {ele.message_id}]"
            elif isinstance(ele, Forward):
                if ele.chains:
                    forward_contents = ""
                    for i, chain in enumerate(ele.chains):
                        ele.chains[i].message_list = [
                            x for x in chain if not isinstance(x, Forward)
                        ]
                        forward_content = await self.message_format_to_text(
                            ele.chains[i].message_list
                        )
                        forward_contents += f"\n{forward_content}\n"
                    message_str += f"[Forward {forward_contents.strip()}]"
            elif isinstance(ele, Record):
                record_text = await self.llm_api.speech_to_text(ele.bs64)
                message_str += f"[Record {record_text}]"
            elif isinstance(ele, Notice):
                message_str += f"{ele.text}"
            elif isinstance(ele, File):
                # TODO parse file
                message_str += f"[File {ele.name}]"
            else:
                pass
        return message_str

    async def handle_im_message(self, event: KiraMessageEvent):
        """process im message"""
        logger.info(event.get_log_info())
        try:
            await self._handle_im_message_inner(event)
        except Exception as e:
            logger.error(f"❌ handle_im_message crashed: {e}", exc_info=True)

    async def _handle_im_message_inner(self, event: KiraMessageEvent):
        # decorating event info

        sid = event.session.sid

        event.session.session_description = self.memory_manager.get_session_info(
            sid
        ).session_description

        # EventType.ON_IM_MESSAGE
        im_handlers = event_handler_reg.get_handlers(event_type=EventType.ON_IM_MESSAGE)
        logger.info(f"[TRACE] ON_IM_MESSAGE handlers: {len(im_handlers)}, is_mentioned={event.is_mentioned}")
        for handler in im_handlers:
            await handler.exec_handler(event)
            if event.is_stopped:
                logger.info(f"[TRACE] Event stopped by handler, strategy={event.process_strategy}")
                return
        logger.info(f"[TRACE] process_strategy={event.process_strategy} for {sid}")
        if event.process_strategy == "discard":
            logger.info(f"[TRACE] Message discarded for {sid}")
            return

        if event.process_strategy == "trigger":
            batch_msg = KiraMessageBatchEvent(
                message_types=event.message_types,
                timestamp=int(time.time()),
                adapter=event.adapter,
                session=event.session,
                messages=[event.message],
            )
            await self.handle_im_batch_message(batch_msg)
            return

        if event.process_strategy == "buffer":
            buffer = self.session_buffer.get_buffer(sid)
            async with buffer.lock:
                buffer.add(event)
            return

        if event.process_strategy == "flush":
            flushed = await self.flush_session_messages(sid, extra_event=event)
            if not flushed:
                logger.warning(f"No pending messages to flush for session {sid}")
            return

        # # buffer
        # buffer = self.session_buffer.get_buffer(sid)
        #
        # async with buffer.lock:
        #     buffer.add(event)
        #     message_count = buffer.get_length()
        #
        # if message_count < self.max_buffer_messages:
        #     await asyncio.sleep(self.max_message_interval)
        #
        # if buffer.get_length() == message_count:
        #     # print("no new message coming, processing")
        #     async with buffer.lock:
        #         pending_messages: list[KiraMessageEvent] = buffer.flush(count=message_count)
        #     logger.info(f"deleted {message_count} message(s) from buffer")
        # else:
        #     # print("new message coming")
        #     return None
        #
        # last_event = pending_messages[-1]
        #
        # batch_msg = KiraMessageBatchEvent(
        #     message_types=last_event.message_types,
        #     timestamp=int(time.time()),
        #     adapter=last_event.adapter,
        #     session=last_event.session,
        #     messages=[m.message for m in pending_messages]
        # )
        # await self.handle_im_batch_message(batch_msg)

    async def handle_im_batch_message(self, event: KiraMessageBatchEvent):
        # Start processing
        sid = event.session.sid
        try:
            # 超时保护：防止 LLM 调用或工具执行 hang 住导致整个 session 卡死
            await asyncio.wait_for(
                self._handle_im_batch_message_inner(event),
                timeout=120.0,  # 2 分钟超时
            )
        except asyncio.TimeoutError:
            logger.error(f"⏰ handle_im_batch_message timed out for {sid} (120s)")
        except Exception as e:
            logger.error(f"❌ handle_im_batch_message crashed for {sid}: {e}", exc_info=True)

    async def _handle_im_batch_message_inner(self, event: KiraMessageBatchEvent):
        sid = event.session.sid

        for i, message in enumerate(event.messages):
            message_list = message.chain
            image_parts = []
            message_str = await self.message_format_to_text(message_list, collect_images=image_parts)
            message.message_str = message_str
            message.image_data = image_parts

        # EventType.ON_IM_BATCH_MESSAGE
        im_batch_handlers = event_handler_reg.get_handlers(
            event_type=EventType.ON_IM_BATCH_MESSAGE
        )
        for handler in im_batch_handlers:
            await handler.exec_handler(event)
            if event.is_stopped:
                logger.info("Event stopped")
                return

        # Get existing session
        session_list = self.get_session_list_prompt()

        session_title = self.memory_manager.get_session_info(sid).session_title
        if not session_title:
            session_title = event.session.session_title

        # Build chat environment
        chat_env = {
            "platform": event.adapter.platform,
            "adapter": event.adapter.name,
            "chat_type": "GroupMessage"
            if event.is_group_message()
            else "DirectMessage",
            "self_id": event.self_id,
            "session_title": session_title,
            "session_description": event.session.session_description,
            "session_list": session_list,
        }

        # Get chat history memory
        session_memory = self.memory_manager.fetch_memory(sid)
        # Core memory is now managed through TomlTreeStore, pass empty for legacy prompt slot
        core_memory = ""

        # 构建用户标识（跨 recall / profile 复用）
        user_key = f"{event.adapter.name}:{event.messages[-1].sender.user_id}"
        # 用户消息文本，用于 RAG recall 查询
        query_text = " ".join(m.message_str for m in event.messages if m.message_str)

        # 更新所有发言者的 profile（写入 nickname，保证昵称索引可用）
        seen_senders = set()
        for msg in event.messages:
            sender = msg.sender
            sender_key = f"{event.adapter.name}:{sender.user_id}"
            if sender_key not in seen_senders:
                seen_senders.add(sender_key)
                try:
                    await self.memory_manager.update_user_interaction(
                        sender_key,
                        platform=event.adapter.platform or "",
                        nickname=sender.nickname or "",
                    )
                except Exception as e:
                    logger.debug(f"Failed to update sender profile {sender_key}: {e}")

        # Recall long-term memories (RAG)
        recalled_memories_str = ""
        try:
            recalled = await self.memory_manager.recall(
                query_text, entity_id=user_key, entity_type="user", k=5
            )

            # 群聊场景：额外搜索群级记忆（海马体在群聊中提取的事实存储在群 ID 下）
            if event.is_group_message():
                group_key = f"{event.adapter.name}:{event.session.session_id}"
                group_recalled = await self.memory_manager.recall(
                    query_text, entity_id=group_key, entity_type="group", k=5
                )
                # 去重后合并
                existing_ids = {m.id for m in recalled}
                for gm in group_recalled:
                    if gm.id not in existing_ids:
                        recalled.append(gm)

            recalled_memories_str = self.memory_manager.format_recalled_memories(recalled)
        except Exception as e:
            logger.error(f"Long-term memory recall failed: {e}", exc_info=True)

        # Get user profile（群聊：汇总所有发言者的 profile）
        user_profile_str = ""
        try:
            if event.is_group_message() and len(seen_senders) > 1:
                profile_parts = []
                for sk in seen_senders:
                    try:
                        profile = await self.memory_manager.get_profile(sk, "user")
                        p = profile.to_prompt()
                        if p and p != "暂无画像信息":
                            # 用昵称/名字标识，避免暴露系统 entity_id
                            label = profile.name or profile.nickname or sk.split(":")[-1]
                            profile_parts.append(f"【{label}】\n{p}")
                    except Exception:
                        pass
                if profile_parts:
                    user_profile_str = (
                        "以下是本次群聊中参与对话的用户的画像信息，"
                        "帮助你了解每个人的背景和偏好：\n\n"
                        + "\n\n".join(profile_parts)
                    )
                else:
                    user_profile_str = "暂无画像信息"
            else:
                user_profile_str = await self.memory_manager.get_profile_prompt(
                    user_key, "user"
                )
        except Exception as e:
            logger.error(f"User profile retrieval failed: {e}", exc_info=True)

        # Get emoji_dict
        emoji_dict = getattr(get_adapter_by_name(event.adapter.name), "emoji_dict", {})

        # Generate agent prompt
        agent_prompt_list = self.prompt_manager.get_agent_prompt(
            chat_env,
            core_memory,
            event.message_types,
            emoji_dict,
            recalled_memories=recalled_memories_str,
            user_profile=user_profile_str,
        )
        # messages = [{"role": "system", "content": agent_prompt}]

        # session_memory.append({"role": "user", "content": user_prompt})
        # new_memory_chunk = [{"role": "user", "content": user_prompt}]
        new_memory_chunk = []
        # messages.extend(session_memory)

        # New Logic Start
        # 如果消息中包含图片，使用 VLM 直接说话；否则使用默认 LLM
        all_has_images = any(
            hasattr(m, "image_data") and m.image_data for m in event.messages
        )
        if all_has_images:
            try:
                llm_model = self.provider_mgr.get_default_vlm()
                llm_logger.info("Switched to VLM for image conversation (VLM speaks directly)")
            except (ValueError, TypeError):
                llm_logger.warning("VLM not configured, falling back to default LLM for image")
                llm_model = self.provider_mgr.get_default_llm()
        else:
            llm_model = self.provider_mgr.get_default_llm()
        if not llm_model:
            llm_logger.error(
                f"Default LLM model not set, please set it in Configuration"
            )
            return

        request = LLMRequest(
            messages=session_memory[:],
            tools=self.llm_api.tools_definitions,
            tool_funcs=self.llm_api.tools_functions,
        )
        request.system_prompt.extend(agent_prompt_list)

        # Add received im messages + collect image data for VL
        all_image_data = []
        for i, message in enumerate(event.messages):
            request.user_prompt.append(
                Prompt(message.message_str, name="message", source="system")
            )
            if hasattr(message, "image_data") and message.image_data:
                all_image_data.extend(message.image_data)

        # 暂存图片数据，assemble_prompt 后注入 multimodal content
        request._image_data = all_image_data

        # EventType.ON_LLM_REQUEST
        llm_handlers = event_handler_reg.get_handlers(
            event_type=EventType.ON_LLM_REQUEST
        )
        for handler in llm_handlers:
            await handler.exec_handler(event, request)
            if event.is_stopped:
                logger.info("Event stopped")
                return

        # Assemble messages
        request.assemble_prompt()

        # 如果有图片，将最后一条 user message 的 content 改为 multimodal 格式
        # OpenAI/Gemini API 都支持 content: [{type: "text", ...}, {type: "image_url", ...}]
        image_data = getattr(request, "_image_data", [])
        if image_data and request.messages:
            # 找到最后一条 user message
            for i in range(len(request.messages) - 1, -1, -1):
                if request.messages[i].get("role") == "user":
                    text_content = request.messages[i]["content"]
                    multimodal_content = [{"type": "text", "text": text_content}]
                    for b64 in image_data:
                        multimodal_content.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{b64}",
                                "detail": "high"
                            }
                        })
                    request.messages[i]["content"] = multimodal_content
                    logger.info(f"Injected {len(image_data)} image(s) into user message for VL model")
                    break

        # Print user message info
        user_message = "".join(
            p.content for p in request.user_prompt if isinstance(p, Prompt)
        )
        logger.info(f"processing message(s) from {sid}:\n{user_message}")

        # 把收到的消息放到新收到的消息内容中，附加 sender 信息供海马体使用
        # 群聊 batch 可能包含多位发言者的消息，逐条保留 sender 信息
        adapter_name = event.adapter.name
        for msg in event.messages:
            new_memory_chunk.append({
                "role": "user",
                "content": msg.message_str,
                "sender_id": msg.sender.user_id,
                "sender_name": msg.sender.nickname or "User",
                "adapter": adapter_name,
            })

        provider_name = llm_model.model.provider_name
        model_id = llm_model.model.model_id
        llm_logger.info(f"Running agent using {model_id} ({provider_name})")

        def append_msg(msg: dict):
            new_memory_chunk.append(msg)

        def extend_msg(msg: list):
            new_memory_chunk.extend(msg)

        # Get max tool loop config, defaults to 2 if not a valid integer
        max_tool_loop = self.kira_config.get_config("bot_config.agent.max_tool_loop")
        try:
            max_tool_loop = int(max_tool_loop)
        except ValueError:
            max_tool_loop = 2

        max_agent_steps = max_tool_loop + 1

        for _ in range(max_agent_steps):
            try:
                llm_resp = await llm_model.chat(request)
            except Exception as e:
                logger.error(f"LLM chat call failed: {e}")
                llm_resp = LLMResponse(text_response=f"[系统错误] LLM 调用失败: {e}")

            if llm_resp:
                llm_logger.debug(llm_resp)
                llm_logger.info(
                    f"Time consumed: {llm_resp.time_consumed}s, Input tokens: {llm_resp.input_tokens}, output tokens: {llm_resp.output_tokens}"
                )

                # EventType.ON_LLM_RESPONSE
                llm_resp_handlers = event_handler_reg.get_handlers(
                    event_type=EventType.ON_LLM_RESPONSE
                )
                for handler in llm_resp_handlers:
                    await handler.exec_handler(event, llm_resp)
                    if event.is_stopped:
                        logger.info("Event stopped")
                        return

                # 初始化 response_with_ids，避免后续引用未定义变量
                response_with_ids = ""

                if not llm_resp.tool_calls:
                    # 纯文本回复
                    if llm_resp.text_response:
                        session_lock = self.get_session_lock(sid)
                        async with session_lock:
                            message_ids = await self.send_xml_messages(
                                sid, llm_resp.text_response.strip()
                            )
                            response_with_ids = self._add_message_ids(
                                llm_resp.text_response, message_ids
                            )
                            logger.info(f"LLM: {response_with_ids}")
                    request.messages.append(
                        {"role": "assistant", "content": response_with_ids}
                    )
                    append_msg(
                        {"role": "assistant", "content": response_with_ids}
                    )
                    break
                else:
                    # 工具调用（可能同时带文本回复）
                    if llm_resp.text_response:
                        session_lock = self.get_session_lock(sid)
                        async with session_lock:
                            message_ids = await self.send_xml_messages(
                                sid, llm_resp.text_response.strip()
                            )
                            response_with_ids = self._add_message_ids(
                                llm_resp.text_response, message_ids
                            )
                            logger.info(f"LLM: {response_with_ids}")
                    await self.llm_api.execute_tool(event, llm_resp)
                    request.messages.append(
                        {
                            "role": "assistant",
                            "content": response_with_ids,
                            "tool_calls": llm_resp.tool_calls,
                        }
                    )
                    append_msg(
                        {
                            "role": "assistant",
                            "content": response_with_ids,
                            "tool_calls": llm_resp.tool_calls,
                        }
                    )
                    request.messages.extend(llm_resp.tool_results)
                    extend_msg(llm_resp.tool_results)
            else:
                request.messages.append({"role": "assistant", "content": ""})
                append_msg({"role": "assistant", "content": ""})
                break

        await self.memory_manager.update_memory(sid, new_memory_chunk)
        if not self.memory_manager.get_session_info(sid).session_title:
            await self.memory_manager.update_session_info(
                sid, event.session.session_title
            )

    async def handle_cmt_message(self, msg: KiraCommentEvent):
        """process comment message"""

        if msg.sub_cmt_id:
            logger.info(
                f"[{msg.adapter_name} | {msg.sub_cmt_id}] [{msg.commenter_nickname}]: {msg.sub_cmt_content[0].text}"
            )
            cmt_content = f"""You: {msg.cmt_content[0].text}
            {msg.commenter_nickname}: {msg.sub_cmt_content[0].text}
            """
        else:
            logger.info(
                f"[{msg.adapter_name} | {msg.cmt_id}] [{msg.commenter_nickname}]: {msg.cmt_content[0].text}"
            )
            cmt_content = f"""{msg.commenter_nickname}: {msg.cmt_content[0].text}"""

        cmt_prompt = self.prompt_manager.get_comment_prompt(cmt_content)

        llm_resp = await self.llm_api.chat([{"role": "user", "content": cmt_prompt}])

        response = llm_resp.text_response.strip()

        logger.info(f"LLM: {response}")

        if response:
            await get_adapter_by_name(msg.adapter_name).send_comment(
                text=response, root=msg.cmt_id, sub=msg.sub_cmt_id
            )
        else:
            logger.warning("Blank LLM response")

    async def send_xml_messages(self, target: str, xml_data: str) -> List[str]:
        """
        send message via session id & xml data
        :param target: adapter_name:session_type:session_id
        :param xml_data: xml string
        :return: message_ids
        """
        parts = target.split(":")
        if len(parts) != 3:
            raise ValueError(
                "invalid target, must follow the form of <adapter>:<dm|gm>:<id>"
            )
        adapter_name, chat_type, pid = parts[0], parts[1], parts[2]

        message_ids = []
        try:
            resp_list = await self._parse_xml_msg(xml_data)
        except Exception as e:
            logger.error(f"Error parsing message: {str(e)}")
            return []

        for message_list in resp_list:
            if message_list:
                message_obj = MessageChain(message_list)

                result = await self.send_message_chain(target, message_obj)
                if not result.ok and result.err:
                    logger.error(result.err)
                message_ids.append(
                    result.message_id if result.message_id is not None else ""
                )

                # add random message delay
                await asyncio.sleep(
                    random.uniform(self.min_message_delay, self.max_message_delay)
                )
            else:
                message_ids.append("")

        return message_ids

    async def send_message_chain(
        self, session: str, chain: MessageChain
    ) -> KiraIMSentResult:
        """
        Send a MessageChain to target.

        :param session: adapter_name:dm|gm:session_id
        :param chain: MessageChain instance
        :return: message_id (empty string if failed)
        """
        parts = session.split(":")
        if len(parts) != 3:
            raise ValueError("invalid target, must follow <adapter>:<dm|gm>:<id>")

        adapter_name, chat_type, pid = parts
        adapter = get_adapter_by_name(adapter_name)

        if chat_type == "dm":
            result = await adapter.send_direct_message(pid, chain)
        elif chat_type == "gm":
            result = await adapter.send_group_message(pid, chain)
        else:
            raise ValueError("chat_type must be 'dm' or 'gm'")

        if not result:
            return KiraIMSentResult(ok=False)

        return result

    async def _parse_xml_msg(self, xml_data):
        """Parse xml to list[list[BaseMessageElement]]

        If xml_data contains no <msg> tags (e.g. provider returns plain text),
        fall back to wrapping the entire text as a single text message.
        """
        try:
            root = ET.fromstring(f"<root>{xml_data}</root>")
        except ET.ParseError:
            # XML parse failed — treat entire response as plain text
            logger.warning(f"XML parse failed, falling back to plain text")
            if xml_data.strip():
                return [[Text(xml_data.strip())]]
            return []

        message_list = []

        # Fallback: if no <msg> tags found, treat as plain text
        if not root.findall("msg"):
            plain = xml_data.strip()
            if plain:
                logger.warning(f"No <msg> tags found in LLM response, sending as plain text")
                return [[Text(plain)]]
            return []

        for msg in root.findall("msg"):
            message_elements = []
            for child in msg:
                tag = child.tag
                value = child.text.strip() if child.text else ""

                # build MessageType object
                if tag == "text":
                    if value:
                        message_elements.append(Text(value))
                elif tag == "emoji":
                    message_elements.append(Emoji(value))
                elif tag == "sticker":
                    sticker_id = value
                    try:
                        sticker_path = self.prompt_manager.sticker_dict[sticker_id].get(
                            "path"
                        )
                        sticker_bs64 = await image_to_base64(
                            f"{get_data_path()}/sticker/{sticker_path}"
                        )
                        message_elements.append(Sticker(sticker_id, sticker_bs64))
                    except Exception as e:
                        logger.error(f"error while parsing sticker: {str(e)}")
                elif tag == "at":
                    message_elements.append(At(value))
                elif tag == "img":
                    img_res = await self.llm_api.generate_img(value)
                    if img_res:
                        if img_res.url:
                            message_elements.append(Image(url=img_res.url))
                        elif img_res.base64:
                            message_elements.append(Image(b64=img_res.base64))
                        else:
                            pass
                elif tag == "reply":
                    message_elements.append(Reply(value))
                elif tag == "record":
                    try:
                        record_bs64 = await self.llm_api.text_to_speech(value)
                        message_elements.append(Record(record_bs64))
                    except Exception as e:
                        logger.error(
                            f"an error occurred while generating voice message: {e}"
                        )
                        message_elements.append(Text(f"<record>{value}</record>"))
                elif tag == "poke":
                    message_elements.append(Poke(value))
                elif tag == "selfie":
                    try:
                        ref_img_path = (
                            self.kira_config.get("bot_config", {})
                            .get("selfie", {})
                            .get("path", "")
                        )
                        if os.path.exists(f"{get_data_path()}/{ref_img_path}"):
                            img_extension = ref_img_path.split(".")[-1]
                            bs64 = await image_to_base64(
                                f"{get_data_path()}/{ref_img_path}"
                            )
                            img_res = await self.llm_api.image_to_image(
                                value, bs64=f"data:image/{img_extension};base64,{bs64}"
                            )
                            if img_res:
                                if img_res.url:
                                    message_elements.append(Image(url=img_res.url))
                                elif img_res.base64:
                                    message_elements.append(Image(b64=img_res.base64))
                                else:
                                    logger.warning("Invalid selfie image result")
                        else:
                            logger.warning(
                                f"Selfie reference image not found, skipped generation"
                            )
                    except Exception as e:
                        logger.error(f"Failed to generate selfie: {e}")
                elif tag == "file":
                    registered_file_path = get_data_path() / "files" / value

                    # Absolute path
                    if os.path.exists(value):
                        message_elements.append(File(value, Path(value).name))
                    # Relative path
                    elif os.path.exists(registered_file_path):
                        message_elements.append(File(str(registered_file_path), value))
                    # File URL
                    elif value.startswith(("http://", "https://")):
                        # TODO fetch filename from http headers
                        message_elements.append(File(value))
                else:
                    # TODO hand over to plugins to parse
                    pass

            if message_elements:
                message_list.append(message_elements)

        return message_list

    def _add_message_ids(self, xml_data: str, message_ids: List[str]) -> str:
        """为XML响应添加消息ID"""
        try:
            root = ET.fromstring(f"<root>{xml_data}</root>")

            for i, msg in enumerate(root.findall("msg")):
                if i < len(message_ids):
                    msg.set("message_id", message_ids[i])

            return ET.tostring(root, encoding="unicode", method="xml")[6:-7]

        except Exception as e:
            logger.error(f"Error adding message IDs: {str(e)}")
            return xml_data
