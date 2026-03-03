"""
记忆工具集（重写版）

完全基于 TomlTreeStore + EntityProfileStore，
移除所有 core.txt / core_vector_map.json 遗留逻辑。
支持 user / group / channel / global 四种实体域。

entity_id 自动推断：
当 LLM 未提供 entity_id 时，从 event context 自动推断。
默认策略：关联到当前发言用户（即使在群聊中）。
"""

import asyncio
import time
from typing import Optional, Tuple

from core.utils.tool_utils import BaseTool
from core.logging_manager import get_logger

logger = get_logger("memory_tools", "green")

# 全局引用，由 tool_manager 注入
_memory_manager = None


def set_memory_manager(manager):
    """被外部调用以注入 MemoryManager 引用"""
    global _memory_manager
    _memory_manager = manager


def _resolve_entity_from_event(event) -> Tuple[str, str]:
    """从 event context 推断 entity_id 和 entity_type。

    默认策略：始终关联到当前发言用户。
    即使在群聊中，记录的也是"某用户说了什么"。
    """
    try:
        adapter_name = event.adapter.name
        sender_id = event.messages[-1].sender.user_id
        return f"{adapter_name}:{sender_id}", "user"
    except (AttributeError, IndexError) as e:
        logger.warning(f"Failed to resolve entity from event: {e}")
        return "", "user"


class MemoryAddTool(BaseTool):
    name = "memory_add"
    description = "添加一条记忆到长期记忆系统"
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "要记录的记忆文本"},
            "entity_id": {
                "type": "string",
                "description": "实体ID（可省略，系统自动推断为当前用户）",
            },
            "entity_type": {
                "type": "string",
                "description": "实体类型: user, group, channel（可省略，默认user）",
                "enum": ["user", "group", "channel"],
            },
            "importance": {
                "type": "number",
                "description": "重要性评分 1-10（默认5）",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "标签列表（可选）",
            },
            "memory_type": {
                "type": "string",
                "description": "记忆类型: fact, reflection",
                "enum": ["fact", "reflection"],
            },
        },
        "required": ["text"],
    }

    async def execute(
        self,
        text: str,
        entity_id: str = "",
        entity_type: str = "user",
        importance: int = 5,
        tags: list = None,
        memory_type: str = "fact",
    ) -> str:
        if not _memory_manager or not hasattr(_memory_manager, "tree_store"):
            return "Memory system not available"

        # Auto-resolve entity from event context if not provided
        if not entity_id and self._event_context:
            entity_id, entity_type = _resolve_entity_from_event(self._event_context)
            logger.info(f"Auto-resolved entity: {entity_id} ({entity_type})")

        try:
            importance = max(1, min(10, int(importance)))
        except (TypeError, ValueError):
            importance = 5

        # 映射 memory_type 到 folder
        folder_map = {
            "fact": "facts",
            "reflection": "reflections",
            "episodic": "episodic",
        }
        folder = folder_map.get(memory_type, "facts")

        try:
            entry = await _memory_manager.tree_store.add_memory(
                content_text=text,
                memory_type=memory_type,
                importance=importance,
                tags=tags or [],
                entity_id=entity_id,
                entity_type=entity_type,
                folder=folder,
            )
            return f"Memory added: id={entry.id}, type={memory_type}, entity={entity_id}"
        except Exception as e:
            logger.error(f"MemoryAddTool error: {e}")
            return f"Failed to add memory: {e}"


class MemoryUpdateTool(BaseTool):
    name = "memory_update"
    description = "更新一条已有记忆"
    parameters = {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "记忆ID"},
            "text": {"type": "string", "description": "更新后的记忆文本"},
            "entity_id": {
                "type": "string",
                "description": "实体ID（可省略，系统自动推断为当前用户）",
            },
            "entity_type": {
                "type": "string",
                "description": "实体类型",
                "enum": ["user", "group", "channel"],
            },
            "folder": {
                "type": "string",
                "description": "所在目录: facts, reflections, episodic",
            },
            "importance": {
                "type": "number",
                "description": "新的重要性评分（可选）",
            },
        },
        "required": ["memory_id", "text"],
    }

    async def execute(
        self,
        memory_id: str,
        text: str,
        entity_id: str = "",
        entity_type: str = "user",
        folder: str = "facts",
        importance: int = None,
    ) -> str:
        if not _memory_manager or not hasattr(_memory_manager, "tree_store"):
            return "Memory system not available"

        # Auto-resolve entity from event context if not provided
        if not entity_id and self._event_context:
            entity_id, entity_type = _resolve_entity_from_event(self._event_context)
            logger.info(f"Auto-resolved entity: {entity_id} ({entity_type})")

        memory = await _memory_manager.tree_store.get_memory(
            memory_id=memory_id,
            entity_id=entity_id,
            entity_type=entity_type,
            folder=folder,
        )
        if not memory:
            return f"Memory not found: {memory_id}"

        memory.text = text
        memory.meta["last_accessed"] = time.time()
        if importance is not None:
            try:
                memory.importance = max(1, min(10, int(importance)))
            except (TypeError, ValueError):
                pass

        if await _memory_manager.tree_store.update_memory(memory):
            return f"Memory updated: {memory_id}"
        return f"Failed to update memory: {memory_id}"


class MemoryRemoveTool(BaseTool):
    name = "memory_remove"
    description = "删除一条记忆（移入归档）"
    parameters = {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "记忆ID"},
            "entity_id": {
                "type": "string",
                "description": "实体ID（可省略，系统自动推断为当前用户）",
            },
            "entity_type": {
                "type": "string",
                "description": "实体类型",
                "enum": ["user", "group", "channel"],
            },
            "folder": {
                "type": "string",
                "description": "所在目录: facts, reflections, episodic",
            },
        },
        "required": ["memory_id"],
    }

    async def execute(
        self,
        memory_id: str,
        entity_id: str = "",
        entity_type: str = "user",
        folder: str = "facts",
    ) -> str:
        if not _memory_manager or not hasattr(_memory_manager, "tree_store"):
            return "Memory system not available"

        # Auto-resolve entity from event context if not provided
        if not entity_id and self._event_context:
            entity_id, entity_type = _resolve_entity_from_event(self._event_context)
            logger.info(f"Auto-resolved entity: {entity_id} ({entity_type})")

        if await _memory_manager.tree_store.archive_memory(
            memory_id=memory_id,
            entity_id=entity_id,
            entity_type=entity_type,
            folder=folder,
        ):
            return f"Memory archived: {memory_id}"
        return f"Failed to archive memory: {memory_id}"


class MemorySearchTool(BaseTool):
    name = "memory_search"
    description = "搜索长期记忆，通过语义相似度检索相关记忆"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索查询文本"},
            "entity_id": {
                "type": "string",
                "description": "实体ID（可省略，系统自动推断为当前用户）",
            },
            "entity_type": {
                "type": "string",
                "description": "实体类型",
                "enum": ["user", "group", "channel"],
            },
            "k": {"type": "number", "description": "返回结果数量（默认5）"},
        },
        "required": ["query"],
    }

    async def execute(
        self,
        query: str,
        entity_id: str = "",
        entity_type: str = "user",
        k: int = 5,
    ) -> str:
        if not _memory_manager or not hasattr(_memory_manager, "recall"):
            return "Memory system not available"

        # Auto-resolve entity from event context if not provided
        if not entity_id and self._event_context:
            entity_id, entity_type = _resolve_entity_from_event(self._event_context)
            logger.info(f"Auto-resolved entity: {entity_id} ({entity_type})")

        try:
            k = max(1, int(k))
        except (TypeError, ValueError):
            k = 5

        memories = await _memory_manager.recall(
            query, entity_id=entity_id, entity_type=entity_type, k=k
        )
        if not memories:
            return "No relevant memories found"

        type_labels = {
            "fact": "事实",
            "reflection": "洞察",
            "episodic": "事件",
            "summary": "摘要",
        }
        lines = []
        for mem in memories:
            label = type_labels.get(mem.type, mem.type)
            tags = f" [{', '.join(mem.tags)}]" if mem.tags else ""
            lines.append(f"[{label}]{tags} {mem.raw_text}")
        return "\n".join(lines)


class ProfileViewTool(BaseTool):
    name = "profile_view"
    description = "查看实体画像信息"
    parameters = {
        "type": "object",
        "properties": {
            "entity_id": {
                "type": "string",
                "description": "实体ID（可省略，系统自动推断为当前用户）",
            },
            "entity_type": {
                "type": "string",
                "description": "实体类型: user, group, channel",
                "enum": ["user", "group", "channel"],
            },
        },
        "required": [],
    }

    async def execute(self, entity_id: str = "", entity_type: str = "user") -> str:
        if not _memory_manager or not hasattr(_memory_manager, "profile_store"):
            return "Profile system not available"

        # Auto-resolve entity from event context if not provided
        if not entity_id and self._event_context:
            entity_id, entity_type = _resolve_entity_from_event(self._event_context)
            logger.info(f"Auto-resolved entity: {entity_id} ({entity_type})")

        return await _memory_manager.profile_store.get_profile_prompt(
            entity_id, entity_type
        )


class ProfileUpdateTool(BaseTool):
    name = "profile_update"
    description = "更新实体画像的特征标签、事实或关系"
    parameters = {
        "type": "object",
        "properties": {
            "entity_id": {
                "type": "string",
                "description": "实体ID（可省略，系统自动推断为当前用户）",
            },
            "entity_type": {
                "type": "string",
                "description": "实体类型",
                "enum": ["user", "group", "channel"],
            },
            "action": {
                "type": "string",
                "description": "操作类型",
                "enum": [
                    "add_trait",
                    "remove_trait",
                    "add_fact",
                    "set_name",
                    "set_relationship",
                ],
            },
            "value": {"type": "string", "description": "操作值"},
            "target": {
                "type": "string",
                "description": "关系目标（action=set_relationship 时必填）",
            },
        },
        "required": ["action", "value"],
    }

    async def execute(
        self,
        action: str,
        value: str,
        entity_id: str = "",
        entity_type: str = "user",
        target: str = "",
    ) -> str:
        if not _memory_manager or not hasattr(_memory_manager, "profile_store"):
            return "Profile system not available"

        # Auto-resolve entity from event context if not provided
        if not entity_id and self._event_context:
            entity_id, entity_type = _resolve_entity_from_event(self._event_context)
            logger.info(f"Auto-resolved entity: {entity_id} ({entity_type})")

        store = _memory_manager.profile_store

        if action == "add_trait":
            await store.add_trait(entity_id, value, entity_type)
            return f"Added trait '{value}'"
        elif action == "remove_trait":
            await store.remove_trait(entity_id, value, entity_type)
            return f"Removed trait '{value}'"
        elif action == "add_fact":
            await store.add_fact(entity_id, value, entity_type)
            return f"Added fact"
        elif action == "set_name":
            await store.update_profile(entity_id, entity_type, name=value)
            return f"Set name '{value}'"
        elif action == "set_relationship":
            if not target:
                return "target is required for set_relationship"
            await store.set_relationship(entity_id, target, value, entity_type)
            return f"Set relationship '{value}' with '{target}'"

        return f"Unknown action: {action}"
