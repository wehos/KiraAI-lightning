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

# 全局引用，由 lifecycle 注入
_memory_manager = None
_llm_client = None


def set_memory_manager(manager):
    """被外部调用以注入 MemoryManager 引用"""
    global _memory_manager
    _memory_manager = manager


def set_llm_client(client):
    """被外部调用以注入 LLMClient 引用（用于 fast_llm entity 提取）"""
    global _llm_client
    _llm_client = client


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


def _looks_like_entity_id(entity_id: str) -> bool:
    """判断是否为合法 entity_id 格式（adapter:numeric_id）"""
    if not entity_id:
        return False
    if ":" in entity_id:
        parts = entity_id.split(":", 1)
        # adapter:id 格式
        return len(parts) == 2 and len(parts[1]) > 0
    return False


async def _resolve_entity_id_by_name(
    entity_id: str, entity_type: str, event=None
) -> Tuple[str, str]:
    """如果 entity_id 看起来像昵称而非标准格式，尝试反查。

    返回 (resolved_entity_id, entity_type)。
    """
    if not entity_id or _looks_like_entity_id(entity_id):
        return entity_id, entity_type

    # entity_id 不是标准格式，当作昵称尝试 resolve
    if _memory_manager and hasattr(_memory_manager, "profile_store"):
        resolved = await _memory_manager.profile_store.resolve_entity_by_name(
            entity_id, entity_type
        )
        if resolved:
            logger.info(
                f"Nickname resolved: '{entity_id}' → {resolved} ({entity_type})"
            )
            return resolved, entity_type

    logger.warning(f"Could not resolve nickname '{entity_id}', using as-is")
    return entity_id, entity_type


async def _get_known_users_hint() -> str:
    """获取已知用户列表，作为 fast_llm 提取 entity 的参考"""
    if not _memory_manager or not hasattr(_memory_manager, "profile_store"):
        return ""
    try:
        from core.chat.memory_paths import list_all_entities
        users = []
        for eid, etype in list_all_entities("user"):
            try:
                profile = await _memory_manager.profile_store.get_profile(eid, etype)
            except Exception:
                continue
            names = []
            if profile.name:
                names.append(profile.name)
            if profile.nickname and profile.nickname != profile.name:
                names.append(profile.nickname)
            for a in profile.aliases:
                if a and a not in names:
                    names.append(a)
            if names:
                users.append(f"  {eid} → {'/'.join(names)}")
        if users:
            return "\n已知用户：\n" + "\n".join(users)
    except Exception:
        pass
    return ""


async def _extract_entities_from_context(
    query: str, context: str = ""
) -> list[str]:
    """用 fast_llm 从 query + 对话上下文中提取涉及的 entity 标识列表。

    返回昵称/QQ号列表，如 ["小明", "341391975"]。
    SELF 表示当前发言者自己，NONE 表示无法确定。
    """
    if not _llm_client or not query:
        return []

    known_hint = await _get_known_users_hint()

    prompt = (
        "从以下查询和对话上下文中, 提取所有被提及的人物标识(昵称或QQ号).\n"
        "规则:\n"
        '- 如果查询是关于当前发言者自己的(如"我喜欢...", "记住我..."), 返回 SELF\n'
        "- 如果涉及其他用户, 返回他们的昵称或QQ号, 每行一个\n"
        "- 如果无法确定具体人物, 返回 NONE\n"
        "- 不要输出任何解释, 只输出标识\n"
        f"{known_hint}\n\n"
        f"查询: {query}\n"
        f"对话上下文: {context[-800:] if context else 'N/A'}\n\n"
        "提取的人物标识(每行一个):"
    )

    try:
        resp = await _llm_client.chat_fast([{"role": "user", "content": prompt}])
        raw = resp.text_response.strip()
        lines = [l.strip() for l in raw.split("\n") if l.strip()]
        # 过滤特殊标记和空行
        entities = [l for l in lines if l not in ("SELF", "NONE", "UNKNOWN", "无", "")]
        logger.info(f"Entity extraction: query='{query[:30]}' → {entities}")
        return entities
    except Exception as e:
        logger.warning(f"Entity extraction via fast_llm failed: {e}")
        return []


async def _resolve_single_entity_from_context(
    query: str, context: str, event=None
) -> Tuple[str, str]:
    """单 entity 解析：从上下文提取第一个 entity 并 resolve。

    提取失败或为 SELF 时回退到 event 的当前发言者。
    """
    extracted = await _extract_entities_from_context(query, context)

    if extracted:
        # 取第一个
        resolved_id, resolved_type = await _resolve_entity_id_by_name(
            extracted[0], "user"
        )
        if resolved_id and _looks_like_entity_id(resolved_id):
            return resolved_id, resolved_type

    # 回退：当前发言者
    if event:
        return _resolve_entity_from_event(event)
    return "", "user"


class MemoryAddTool(BaseTool):
    name = "memory_add"
    description = "添加一条记忆到长期记忆系统"
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "要记录的记忆文本"},
            "context": {
                "type": "string",
                "description": "相关的对话上下文片段，帮助系统理解记忆涉及的人物。建议传入最近几轮对话。",
            },
            "entity_id": {
                "type": "string",
                "description": "想要操作的用户：昵称或QQ号。省略则系统自动从上下文推断。",
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
        context: str = "",
        entity_id: str = "",
        entity_type: str = "user",
        importance: int = 5,
        tags: list = None,
        memory_type: str = "fact",
    ) -> str:
        if not _memory_manager or not hasattr(_memory_manager, "tree_store"):
            return "Memory system not available"

        # 智能 entity 解析：LLM 传了 entity_id 走 resolve，没传走 fast_llm 提取
        if entity_id:
            entity_id, entity_type = await _resolve_entity_id_by_name(
                entity_id, entity_type, self._event_context
            )
        elif context and _llm_client:
            entity_id, entity_type = await _resolve_single_entity_from_context(
                text, context, self._event_context
            )
        elif self._event_context:
            entity_id, entity_type = _resolve_entity_from_event(self._event_context)
            logger.info(f"Auto-resolved entity (fallback): {entity_id}")

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
            # 走去重管线（与海马体一致）：SHA-256 精确 → FTS5 + LLM 语义判断
            if hasattr(_memory_manager, "extractor") and _memory_manager.extractor:
                decision, matched = await _memory_manager.extractor.deduplicate(
                    text, entity_id, entity_type, folder
                )
                if decision == "duplicate":
                    logger.info(f"MemoryAddTool: duplicate skipped: {text[:50]}...")
                    return f"Memory already exists (duplicate detected), skipped"
                if decision == "update" and matched:
                    # 合并后更新旧记忆
                    merged_text = await _memory_manager.extractor.merge_facts(
                        matched.text, text
                    )
                    matched.text = merged_text
                    matched.importance = max(matched.importance, importance)
                    await _memory_manager.tree_store.update_memory(matched)
                    logger.info(
                        f"MemoryAddTool: merged into existing: {matched.id}"
                    )
                    return (
                        f"Memory merged into existing: id={matched.id}, "
                        f"entity={entity_id}"
                    )

            # decision == "new" 或无 extractor 兜底：直接写入
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
            "context": {
                "type": "string",
                "description": "相关的对话上下文片段，帮助系统理解涉及的人物。",
            },
            "entity_id": {
                "type": "string",
                "description": "想要操作的用户：昵称或QQ号。省略则系统自动从上下文推断。",
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
        context: str = "",
        entity_id: str = "",
        entity_type: str = "user",
        folder: str = "facts",
        importance: int = None,
    ) -> str:
        if not _memory_manager or not hasattr(_memory_manager, "tree_store"):
            return "Memory system not available"

        if entity_id:
            entity_id, entity_type = await _resolve_entity_id_by_name(
                entity_id, entity_type, self._event_context
            )
        elif context and _llm_client:
            entity_id, entity_type = await _resolve_single_entity_from_context(
                text, context, self._event_context
            )
        elif self._event_context:
            entity_id, entity_type = _resolve_entity_from_event(self._event_context)

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
            "context": {
                "type": "string",
                "description": "相关的对话上下文片段，帮助系统理解涉及的人物。",
            },
            "entity_id": {
                "type": "string",
                "description": "想要操作的用户：昵称或QQ号。省略则系统自动从上下文推断。",
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
        context: str = "",
        entity_id: str = "",
        entity_type: str = "user",
        folder: str = "facts",
    ) -> str:
        if not _memory_manager or not hasattr(_memory_manager, "tree_store"):
            return "Memory system not available"

        if entity_id:
            entity_id, entity_type = await _resolve_entity_id_by_name(
                entity_id, entity_type, self._event_context
            )
        elif self._event_context:
            entity_id, entity_type = _resolve_entity_from_event(self._event_context)

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
    description = "搜索长期记忆，通过语义相似度检索相关记忆。支持自动从对话上下文推断涉及的用户，支持多用户并行搜索。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索查询文本"},
            "context": {
                "type": "string",
                "description": "相关的对话上下文片段，帮助系统理解查询意图和涉及的人物。建议传入最近几轮对话。",
            },
            "entity_id": {
                "type": "string",
                "description": "想要查询的用户：昵称或QQ号。省略则系统自动从上下文推断涉及的用户（可能是多个）。",
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

    TYPE_LABELS = {
        "fact": "事实",
        "reflection": "洞察",
        "episodic": "事件",
        "summary": "摘要",
    }

    @staticmethod
    def _format_memories(memories, entity_id: str = "") -> str:
        """格式化记忆列表为文本"""
        if not memories:
            return ""
        lines = []
        for mem in memories:
            label = MemorySearchTool.TYPE_LABELS.get(mem.type, mem.type)
            tags = f" [{', '.join(mem.tags)}]" if mem.tags else ""
            prefix = f"[{entity_id}] " if entity_id else ""
            lines.append(f"{prefix}[{label}]{tags} {mem.raw_text}")
        return "\n".join(lines)

    async def execute(
        self,
        query: str,
        context: str = "",
        entity_id: str = "",
        entity_type: str = "user",
        k: int = 5,
    ) -> str:
        if not _memory_manager or not hasattr(_memory_manager, "recall"):
            return "Memory system not available"

        try:
            k = max(1, int(k))
        except (TypeError, ValueError):
            k = 5

        # === 路径 1：LLM 明确传了 entity_id → 单 entity 搜索 ===
        if entity_id:
            entity_id, entity_type = await _resolve_entity_id_by_name(
                entity_id, entity_type, self._event_context
            )
            memories = await _memory_manager.recall(
                query, entity_id=entity_id, entity_type=entity_type, k=k
            )
            return self._format_memories(memories) or "No relevant memories found"

        # === 路径 2：没传 entity_id → fast_llm 从上下文提取 ===
        extracted = []
        if context and _llm_client:
            extracted = await _extract_entities_from_context(query, context)

        if not extracted:
            # 提取失败 → 回退当前发言者
            if self._event_context:
                entity_id, entity_type = _resolve_entity_from_event(self._event_context)
            memories = await _memory_manager.recall(
                query, entity_id=entity_id, entity_type=entity_type, k=k
            )
            return self._format_memories(memories) or "No relevant memories found"

        # resolve 每个提取出的 entity
        resolved = []
        for name in extracted:
            rid, rtype = await _resolve_entity_id_by_name(name, "user")
            if rid and _looks_like_entity_id(rid):
                resolved.append((rid, rtype))

        if not resolved:
            # resolve 全部失败 → 回退当前发言者
            if self._event_context:
                entity_id, entity_type = _resolve_entity_from_event(self._event_context)
            memories = await _memory_manager.recall(
                query, entity_id=entity_id, entity_type=entity_type, k=k
            )
            return self._format_memories(memories) or "No relevant memories found"

        # === 单 entity：直接搜索 ===
        if len(resolved) == 1:
            eid, etype = resolved[0]
            memories = await _memory_manager.recall(
                query, entity_id=eid, entity_type=etype, k=k
            )
            return self._format_memories(memories) or "No relevant memories found"

        # === 多 entity：并行搜索 ===
        search_tasks = [
            _memory_manager.recall(query, entity_id=eid, entity_type=etype, k=k)
            for eid, etype in resolved
        ]
        all_results = await asyncio.gather(*search_tasks, return_exceptions=True)

        # 合并结果，标注来源
        merged_parts = []
        for i, result in enumerate(all_results):
            if isinstance(result, Exception):
                logger.warning(f"Search failed for {resolved[i][0]}: {result}")
                continue
            eid = resolved[i][0]
            formatted = self._format_memories(result, entity_id=eid)
            if formatted:
                merged_parts.append(formatted)

        if not merged_parts:
            return "No relevant memories found"

        merged_text = "\n".join(merged_parts)

        # fast_llm 筛选汇总
        return await self._summarize_multi_entity(query, merged_text)

    async def _summarize_multi_entity(self, query: str, merged_text: str) -> str:
        """用 fast_llm 从多 entity 搜索结果中筛选相关内容"""
        if not _llm_client:
            return merged_text

        prompt = (
            "从以下多个用户的记忆搜索结果中，筛选出与查询最相关的内容。\n"
            "去除完全不相关的条目，保留原始格式（包括来源用户标注），按相关性排序。\n"
            "不要添加任何解释，直接输出筛选后的结果。\n\n"
            f"查询：{query}\n\n"
            f"搜索结果：\n{merged_text}\n\n"
            "筛选后："
        )

        try:
            resp = await _llm_client.chat_fast([{"role": "user", "content": prompt}])
            result = resp.text_response.strip()
            return result if result else merged_text
        except Exception as e:
            logger.warning(f"Multi-entity summarization failed: {e}")
            return merged_text


class ProfileViewTool(BaseTool):
    name = "profile_view"
    description = "查看实体画像信息"
    parameters = {
        "type": "object",
        "properties": {
            "context": {
                "type": "string",
                "description": "相关的对话上下文片段，帮助系统理解涉及的人物。",
            },
            "entity_id": {
                "type": "string",
                "description": "想要查询的用户：昵称或QQ号。省略则系统自动从上下文推断。",
            },
            "entity_type": {
                "type": "string",
                "description": "实体类型: user, group, channel",
                "enum": ["user", "group", "channel"],
            },
        },
        "required": [],
    }

    async def execute(
        self, context: str = "", entity_id: str = "", entity_type: str = "user"
    ) -> str:
        if not _memory_manager or not hasattr(_memory_manager, "profile_store"):
            return "Profile system not available"

        if entity_id:
            entity_id, entity_type = await _resolve_entity_id_by_name(
                entity_id, entity_type, self._event_context
            )
        elif context and _llm_client:
            entity_id, entity_type = await _resolve_single_entity_from_context(
                "查看用户画像", context, self._event_context
            )
        elif self._event_context:
            entity_id, entity_type = _resolve_entity_from_event(self._event_context)

        return await _memory_manager.profile_store.get_profile_prompt(
            entity_id, entity_type
        )


class ProfileUpdateTool(BaseTool):
    name = "profile_update"
    description = "更新实体画像的特征标签、事实或关系"
    parameters = {
        "type": "object",
        "properties": {
            "context": {
                "type": "string",
                "description": "相关的对话上下文片段，帮助系统理解涉及的人物。",
            },
            "entity_id": {
                "type": "string",
                "description": "想要操作的用户：昵称或QQ号。省略则系统自动从上下文推断。",
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
        context: str = "",
        entity_id: str = "",
        entity_type: str = "user",
        target: str = "",
    ) -> str:
        if not _memory_manager or not hasattr(_memory_manager, "profile_store"):
            return "Profile system not available"

        if entity_id:
            entity_id, entity_type = await _resolve_entity_id_by_name(
                entity_id, entity_type, self._event_context
            )
        elif context and _llm_client:
            entity_id, entity_type = await _resolve_single_entity_from_context(
                value, context, self._event_context
            )
        elif self._event_context:
            entity_id, entity_type = _resolve_entity_from_event(self._event_context)

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
