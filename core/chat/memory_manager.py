"""
双脑架构记忆管理器（重写版）

快系统（Fast Loop）：对话时检索记忆、维护短期对话历史
慢系统（Slow Loop）：后台海马体 — 提取事实、去重合并、升维反思、更新画像

完全基于 TomlTreeStore + EntityProfileStore，遵循 Memory Agent Charter。
"""

import asyncio
import json
import os
import time
from typing import Dict, Optional, List
from threading import Lock
from asyncio import Lock as AsyncLock

from core.logging_manager import get_logger
from core.config import KiraConfig

from .session import Session
from .memory_index import MemoryIndex
from .toml_tree_store import TomlTreeStore, Memory
from .entity_profile import EntityProfileStore, EntityProfile
from .memory_extractor import MemoryExtractor
from .memory_router import MemoryRouter
from .memory_paths import (
    ensure_directory_structure,
    list_all_entities,
    ENTITIES_DIR,
    ENTITY_USER,
    ENTITY_GROUP,
)

logger = get_logger("memory_manager", "green")

CHAT_MEMORY_PATH: str = "data/memory/chat_memory.json"


class MemoryManager:
    """双脑架构记忆管理器

    快系统：对话检索 + 短期对话历史
    慢系统：海马体后台处理（提取 → 去重 → 合并 → 升维 → 画像更新 → 遗忘）
    """

    def __init__(self, kira_config: KiraConfig, llm_client=None):
        self.kira_config = kira_config
        self.max_memory_length = int(
            kira_config["bot_config"].get("bot").get("max_memory_length")
        )
        self.chat_memory_path = CHAT_MEMORY_PATH

        self.memory_lock = AsyncLock()

        # === 初始化目录结构 ===
        ensure_directory_structure()

        # === 短期记忆 ===
        self.chat_memory = self._load_memory(self.chat_memory_path)
        self._ensure_memory_format()

        # === SQLite 持久化索引 ===
        self.memory_index = MemoryIndex()

        # === 长期记忆（TOML Tree Store + SQLite Index） ===
        self.tree_store = TomlTreeStore(index=self.memory_index)

        # === 实体画像 ===
        self.profile_store = EntityProfileStore()

        # === 海马体 ===
        self._llm_client = llm_client
        self.extractor = MemoryExtractor(self.tree_store, llm_client)
        self.router = MemoryRouter()

        # === 后台任务管理 ===
        self._pending_conversations: Dict[str, list] = {}
        self._hippocampus_threshold = 3
        self._hippocampus_lock = Lock()
        self._background_tasks: set = set()
        self._background_tasks_lock = Lock()

        logger.info("MemoryManager initialized (dual-brain, JSON-based architecture)")

    def set_llm_client(self, llm_client):
        """延迟设置 LLM 客户端"""
        self._llm_client = llm_client
        self.extractor.set_llm_client(llm_client)

    # ==========================================
    # 短期记忆（对话历史 — 滑动窗口）
    # ==========================================

    @staticmethod
    def _load_memory(path: str) -> Dict[str, dict]:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                    return json.loads(content) if content.strip() else {}
            except Exception as e:
                logger.error(f"Error loading memory from {path}: {e}")
                return {}
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            return {}

    def _ensure_memory_format(self):
        for session in self.chat_memory:
            session_content = self.chat_memory[session]
            if isinstance(session_content, dict):
                continue
            if isinstance(session_content, list):
                self.chat_memory[session] = {
                    "title": "",
                    "description": "",
                    "memory": session_content,
                }
        self._sync_save_memory(self.chat_memory, self.chat_memory_path)

    def _sync_save_memory(self, memory: Dict[str, dict] = None, path: str = None):
        if not memory:
            memory = self.chat_memory
        if not path:
            path = self.chat_memory_path
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(memory, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error saving memory to {path}: {e}")

    async def _save_memory(self, memory: Dict[str, dict] = None, path: str = None):
        if not memory:
            memory = self.chat_memory
        if not path:
            path = self.chat_memory_path
        await asyncio.to_thread(self._sync_save_memory, memory, path)

    def get_session_info(self, session: str):
        parts = session.split(":", maxsplit=2)
        if len(parts) != 3:
            raise ValueError("Invalid session ID")
        if session not in self.chat_memory:
            self.chat_memory[session] = {"title": "", "description": "", "memory": []}
        return Session(
            adapter_name=parts[0],
            session_type=parts[1],
            session_id=parts[2],
            session_title=self.chat_memory[session]["title"],
            session_description=self.chat_memory[session]["description"],
        )

    async def update_session_info(
        self, session: str, title: str = None, description: str = None
    ):
        async with self.memory_lock:
            if session not in self.chat_memory:
                self.chat_memory[session] = {"title": "", "description": "", "memory": []}
            if title:
                self.chat_memory[session]["title"] = title
            if description:
                self.chat_memory[session]["description"] = description
            await self._save_memory()

    def get_memory_count(self, session: str) -> int:
        if session not in self.chat_memory:
            return 0
        return len(self.chat_memory[session].get("memory", []))

    def fetch_memory(self, session: str):
        if session not in self.chat_memory:
            self.chat_memory[session] = {"title": "", "description": "", "memory": []}
            return []
        mem_list = self.chat_memory[session].get("memory", [])
        messages = []
        for chunk in mem_list:
            for message in chunk:
                messages.append(message)
        return messages

    def read_memory(self, session: str):
        if session not in self.chat_memory:
            self.chat_memory[session] = {"title": "", "description": "", "memory": []}
            return []
        return self.chat_memory[session].get("memory", [])

    async def write_memory(self, session: str, memory: list[list[dict]]):
        async with self.memory_lock:
            self.chat_memory[session]["memory"] = memory
            await self._save_memory()
        logger.info(f"Memory written for {session}")

    async def update_memory(self, session: str, new_chunk):
        async with self.memory_lock:
            if session not in self.chat_memory:
                self.chat_memory[session] = {"title": "", "description": "", "memory": []}
            self.chat_memory[session]["memory"].append(new_chunk)
            if len(self.chat_memory[session]["memory"]) > self.max_memory_length:
                self.chat_memory[session]["memory"] = self.chat_memory[session]["memory"][1:]
            await self._save_memory()
        logger.info(f"Memory updated for {session}")

        # 触发海马体
        self._buffer_for_hippocampus(session, new_chunk)

    async def delete_session(self, session: str):
        async with self.memory_lock:
            self.chat_memory.pop(session, None)
            await self._save_memory()
        logger.info(f"Memory deleted for {session}")

    # ==========================================
    # 长期记忆检索（快系统）
    # ==========================================

    async def recall(
        self,
        query: str,
        entity_id: str = "",
        entity_type: str = "user",
        k: int = 5,
    ) -> List[Memory]:
        """检索相关长期记忆（对话前调用）

        跨 facts + reflections 联合检索，按综合分数排序。
        遵循宪章 §7：不在检索时 +1 access_count。
        """
        try:
            k = max(1, int(k))
        except (TypeError, ValueError):
            k = 5

        if not entity_id:
            return []

        try:
            return await self.tree_store.search_across_folders(
                query=query,
                entity_id=entity_id,
                entity_type=entity_type,
                folders=["facts", "reflections"],
                k=k,
            )
        except Exception as e:
            logger.error(f"Recall error: {e}")
            return []

    def format_recalled_memories(self, memories: List[Memory]) -> str:
        """格式化检索到的记忆为 Prompt 文本"""
        if not memories:
            return "暂无相关长期记忆"

        parts = []
        type_labels = {
            "fact": "事实",
            "reflection": "洞察",
            "episodic": "事件",
            "skill": "技能",
            "summary": "摘要",
        }
        for mem in memories:
            label = type_labels.get(mem.type, mem.type)
            tags_str = f" [{', '.join(mem.tags)}]" if mem.tags else ""
            parts.append(f"[{label}]{tags_str} {mem.raw_text}")
        return "\n".join(parts)

    async def confirm_memory_usage(self, memory_ids: list[str]):
        """确认记忆被实质使用，执行严格的 access_count +1

        宪章 §7.4：只有在记忆被实质引用时才 +1。
        由消息处理器在生成回复后调用。
        """
        # TODO: 需要根据具体 entity 信息定位记忆
        # 暂时按全局搜索处理
        pass

    # ==========================================
    # 实体画像
    # ==========================================

    async def get_profile(
        self, entity_id: str, entity_type: str = "user"
    ) -> EntityProfile:
        return await self.profile_store.get_profile(entity_id, entity_type)

    async def get_profile_prompt(
        self, entity_id: str, entity_type: str = "user"
    ) -> str:
        return await self.profile_store.get_profile_prompt(entity_id, entity_type)

    async def update_user_interaction(
        self, user_id: str, platform: str = "", nickname: str = ""
    ):
        """更新用户交互信息"""
        updates = {}
        if platform:
            updates["platform"] = platform
        if nickname:
            updates["nickname"] = nickname
        await self.profile_store.increment_interaction(
            user_id, ENTITY_USER, **updates
        )

    # ==========================================
    # 海马体（慢系统 — 后台处理）
    # ==========================================

    def _buffer_for_hippocampus(self, session: str, new_chunk):
        """将新对话缓冲到待处理队列"""
        chunks_to_process = None
        with self._hippocampus_lock:
            if session not in self._pending_conversations:
                self._pending_conversations[session] = []
            self._pending_conversations[session].append(new_chunk)

            if len(self._pending_conversations[session]) >= self._hippocampus_threshold:
                chunks_to_process = self._pending_conversations[session][:]
                self._pending_conversations[session] = []

        if chunks_to_process is not None:
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(
                    self._hippocampus_process(session, chunks_to_process)
                )
                with self._background_tasks_lock:
                    self._background_tasks.add(task)
                task.add_done_callback(self._on_background_task_done)
            except RuntimeError:
                with self._hippocampus_lock:
                    if session in self._pending_conversations:
                        self._pending_conversations[session] = (
                            chunks_to_process + self._pending_conversations[session]
                        )
                    else:
                        self._pending_conversations[session] = chunks_to_process
                logger.debug("No running event loop, skipping hippocampus processing")

    def _on_background_task_done(self, task: asyncio.Task):
        with self._background_tasks_lock:
            self._background_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(
                "Background hippocampus task failed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    async def _hippocampus_process(self, session: str, chunks: list):
        """海马体后台处理：提取事实 → 去重存储 → 升维反思 → 更新画像"""
        if not self._llm_client:
            logger.debug("LLM client not set, skipping hippocampus processing")
            return

        try:
            # 1. 提取事实
            conversation_text = self._chunks_to_text(chunks)
            facts = await self.extractor.extract_facts(conversation_text)

            if not facts:
                return

            # 2. 解析实体信息
            entity_id, entity_type = self._parse_entity_from_session(session)

            # 3. 去重 & 存储（铁律 #1）
            for fact in facts:
                await self.extractor.deduplicate_and_store(
                    fact, entity_id, entity_type
                )

            # 4. 检查升维触发（铁律 #2）
            if await self.extractor.check_elevation_trigger(entity_id, entity_type):
                await self.extractor.generate_reflections(entity_id, entity_type)

            # 5. 更新画像（铁律 #3 — 优先级分层）
            await self._update_profile_from_facts(entity_id, entity_type, facts)

            logger.info(f"Hippocampus completed for session {session}")
        except Exception as e:
            logger.error(f"Hippocampus processing error: {e}")

    async def _update_profile_from_facts(
        self, entity_id: str, entity_type: str, facts: list[dict]
    ):
        """从提取的事实更新实体画像

        宪章 §4.3 优先级分层：importance >= 7 的事实写入 profile
        """
        for fact in facts:
            content = fact.get("content", "")
            importance = fact.get("importance", 5)
            if importance >= 7 and content:
                await self.profile_store.add_fact(entity_id, content, entity_type)

    # ==========================================
    # 遗忘周期（由外部调度器定时调用）
    # ==========================================

    async def run_forgetting_cycle(self):
        """执行遗忘周期：清理低价值记忆

        遗留接口，实际遗忘逻辑在 memory_decay.py 中实现。
        这里作为入口调用。
        """
        from .memory_decay import MemoryDecayEngine

        engine = MemoryDecayEngine(self.tree_store)
        removed, downgraded = await engine.run_full_cycle()

        if removed > 0 or downgraded > 0:
            logger.info(
                f"Forgetting cycle: removed={removed}, downgraded={downgraded}"
            )

    # ==========================================
    # 工具方法
    # ==========================================

    @staticmethod
    def _chunks_to_text(chunks: list) -> str:
        lines = []
        for chunk in chunks:
            for msg in chunk:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                if role == "user":
                    lines.append(f"User: {content}")
                elif role == "assistant":
                    lines.append(f"Bot: {content}")
        return "\n".join(lines)

    @staticmethod
    def _parse_entity_from_session(session: str) -> tuple[str, str]:
        """从 session ID 解析实体信息

        session 格式: adapter:type:id
        - 私聊: adapter:pm:user_id → (adapter:user_id, "user")
        - 群聊: adapter:gm:group_id → (adapter:group_id, "group")
        """
        parts = session.split(":", maxsplit=2)
        if len(parts) != 3:
            raise ValueError(f"Invalid session ID: {session}")

        adapter = parts[0]
        session_type = parts[1]
        session_id = parts[2]

        if session_type == "gm":
            return f"{adapter}:{session_id}", ENTITY_GROUP
        return f"{adapter}:{session_id}", ENTITY_USER
