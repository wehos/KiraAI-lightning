import os
import math
import uuid
import time
import asyncio
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field

import yaml
import jieba
from rank_bm25 import BM25Okapi

from core.logging_manager import get_logger

logger = get_logger("tree_store", "green")

ENTITIES_DIR = "data/memory/entities"


@dataclass
class MarkdownMemory:
    """一条基于 Markdown 文件的记忆/画像实体"""

    id: str
    user_id: str
    folder: str  # e.g., "facts", "reflections", "skills", or "" for root profile
    content: str
    meta: dict = field(default_factory=dict)

    @property
    def file_path(self) -> str:
        """获取该实体的对应绝对相对路径"""
        base_path = os.path.join(ENTITIES_DIR, f"user_{self.user_id}")
        if self.folder:
            return os.path.join(base_path, self.folder, f"{self.id}.md")
        else:
            # 如果没有 folder 通常是 profile.md
            return os.path.join(base_path, f"{self.id}.md")


class MarkdownTreeStore:
    """基于文件树与 YAML Frontmatter 的纯文本记忆管理系统 (替代 VectorStore & UserProfileStore)"""

    def __init__(self):
        os.makedirs(ENTITIES_DIR, exist_ok=True)
        # 缓存 BM25 实例，避免频繁重建 (user_id -> folder -> BM25 data)
        self._bm25_cache: Dict[str, Dict[str, Dict[str, Any]]] = {}
        # 文管锁
        self._io_lock = asyncio.Lock()
        logger.info("MarkdownTreeStore initialized (Tree-Based YAML + MD architecture)")

    @staticmethod
    def _split_frontmatter(file_content: str) -> tuple[dict, str]:
        """解析带有 YAML Frontmatter 的 Markdown 文本"""
        if file_content.startswith("---"):
            parts = file_content.split("---", 2)
            if len(parts) >= 3:
                try:
                    meta = yaml.safe_load(parts[1]) or {}
                    content = parts[2].strip()
                    return meta, content
                except Exception as e:
                    logger.warning(f"Failed to parse YAML frontmatter: {e}")
        return {}, file_content.strip()

    @staticmethod
    def _build_frontmatter(meta: dict, content: str) -> str:
        """将元数据和文本组装为含有 YAML Frontmatter 的内容"""
        if not meta:
            return content
        try:
            yaml_str = yaml.dump(
                meta, default_flow_style=False, allow_unicode=True
            ).strip()
            return f"---\n{yaml_str}\n---\n{content}\n"
        except Exception as e:
            logger.error(f"Failed to dump YAML frontmatter: {e}")
            return content

    def _get_entity_dir(self, user_id: str, folder: str = "") -> str:
        d = os.path.join(ENTITIES_DIR, f"user_{user_id}")
        if folder:
            d = os.path.join(d, folder)
        return d

    async def add_memory(
        self,
        user_id: str,
        folder: str,
        content: str,
        meta: Optional[dict] = None,
        explicit_id: str = "",
    ) -> MarkdownMemory:
        """保存一条新的事实、技能或反思为 MD 文件"""
        meta = meta or {}
        # 补齐默认元数据
        if "timestamp" not in meta:
            meta["timestamp"] = time.time()
        if "access_count" not in meta:
            meta["access_count"] = 0

        mem_id = explicit_id if explicit_id else uuid.uuid4().hex[:12]
        memory = MarkdownMemory(
            id=mem_id, user_id=user_id, folder=folder, content=content, meta=meta
        )

        async with self._io_lock:
            await asyncio.to_thread(self._sync_save_memory, memory)
            self._invalidate_cache(user_id, folder)

        logger.debug(f"Memory added: user={user_id}, folder={folder}, id={mem_id}")
        return memory

    def _sync_save_memory(self, memory: MarkdownMemory):
        """(同步) 写入 MD 文件"""
        d = os.path.dirname(memory.file_path)
        os.makedirs(d, exist_ok=True)
        full_text = self._build_frontmatter(memory.meta, memory.content)
        with open(memory.file_path, "w", encoding="utf-8") as f:
            f.write(full_text)

    async def update_memory(self, memory: MarkdownMemory) -> bool:
        """更新现有的记忆文件，覆盖元数据和内容"""
        async with self._io_lock:
            try:
                await asyncio.to_thread(self._sync_save_memory, memory)
                self._invalidate_cache(memory.user_id, memory.folder)
                return True
            except Exception as e:
                logger.error(f"Failed to update memory {memory.id}: {e}")
                return False

    async def get_memory(
        self, user_id: str, folder: str, memory_id: str
    ) -> Optional[MarkdownMemory]:
        """精确获取一条记忆"""
        mem_path = os.path.join(
            self._get_entity_dir(user_id, folder), f"{memory_id}.md"
        )
        if not os.path.exists(mem_path):
            return None

        async with self._io_lock:
            try:

                def read_file():
                    with open(mem_path, "r", encoding="utf-8") as f:
                        return f.read()

                raw = await asyncio.to_thread(read_file)
                meta, content = self._split_frontmatter(raw)
                return MarkdownMemory(
                    id=memory_id,
                    user_id=user_id,
                    folder=folder,
                    content=content,
                    meta=meta,
                )
            except Exception as e:
                logger.error(f"Read memory error {memory_id}: {e}")
                return None

    async def delete_memory(self, user_id: str, folder: str, memory_id: str) -> bool:
        """物理删除一条记忆文件"""
        mem_path = os.path.join(
            self._get_entity_dir(user_id, folder), f"{memory_id}.md"
        )
        async with self._io_lock:
            try:
                if os.path.exists(mem_path):
                    await asyncio.to_thread(os.remove, mem_path)
                    self._invalidate_cache(user_id, folder)
                    return True
            except Exception as e:
                logger.error(f"Delete memory error {memory_id}: {e}")
        return False

    async def get_all_memories(self, user_id: str, folder: str) -> List[MarkdownMemory]:
        """获取某目录下所有的文件记忆 (性能允许范围内全量遍历)"""
        d = self._get_entity_dir(user_id, folder)
        if not os.path.exists(d):
            return []

        async with self._io_lock:

            def _scan():
                mems = []
                for fname in os.listdir(d):
                    if not fname.endswith(".md"):
                        continue
                    mem_id = fname[:-3]
                    fpath = os.path.join(d, fname)
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            raw = f.read()
                        meta, content = self._split_frontmatter(raw)
                        mems.append(
                            MarkdownMemory(
                                id=mem_id,
                                user_id=user_id,
                                folder=folder,
                                content=content,
                                meta=meta,
                            )
                        )
                    except Exception as e:
                        logger.warning(f"Could not load {fpath}: {e}")
                return mems

            return await asyncio.to_thread(_scan)

    # =============== BM25 检索核心 ===============

    def _invalidate_cache(self, user_id: str, folder: str):
        if user_id in self._bm25_cache and folder in self._bm25_cache[user_id]:
            del self._bm25_cache[user_id][folder]

    def _sync_build_bm25(self, memories: List[MarkdownMemory]) -> Dict[str, Any]:
        """(内部同步方法) 建立 BM25 索引"""
        tokenized_corpus = []
        for m in memories:
            text = m.content
            # 可以混合一些元数据作为关键词
            if "type" in m.meta:
                text += f" {m.meta['type']}"

            # 使用 jieba 进行分词以实现更高精度的匹配
            tokens = [t.lower() for t in jieba.lcut(text) if t.strip()]
            tokenized_corpus.append(tokens)

        bm25 = BM25Okapi(tokenized_corpus) if tokenized_corpus else None
        return {"bm25": bm25, "memories": memories}

    async def search(
        self,
        query: str,
        user_id: str,
        folder: str = "facts",
        k: int = 5,
        update_access: bool = True,
    ) -> List[MarkdownMemory]:
        """基于 BM25 和重要性时间权重的文本综合检索"""
        d = self._get_entity_dir(user_id, folder)
        if not os.path.exists(d):
            return []

        # 获取或缓存全量数据，目录级缓存，直到缓存失效
        cache_node = self._bm25_cache.get(user_id, {}).get(folder)
        if not cache_node:
            memories = await self.get_all_memories(user_id, folder)
            if not memories:
                return []
            cache_node = await asyncio.to_thread(self._sync_build_bm25, memories)
            if user_id not in self._bm25_cache:
                self._bm25_cache[user_id] = {}
            self._bm25_cache[user_id][folder] = cache_node

        bm25: Optional[BM25Okapi] = cache_node.get("bm25")
        memories: List[MarkdownMemory] = cache_node.get("memories", [])

        if not bm25 or not memories:
            return []

        query_tokens = [t.lower() for t in jieba.lcut(query) if t.strip()]
        if not query_tokens:
            return []

        bm25_scores = bm25.get_scores(query_tokens)

        # 综合打分：BM25 (相关性) + 基础重要性权重 + 最近访问加成
        now = time.time()
        scored_memories = []
        for idx, m in enumerate(memories):
            rel_score = bm25_scores[idx]
            # 为了防止纯重复语料或单文档导致的负分，这里我们降低剔除的阈值或者直接平移分数
            # 但实际上，如果分数不是绝对的不相关，我们可以容忍，或者只要 rel_score > 0。
            # 这里简单做个兼容：如果全是负数但最高，我们也不要截断，我们取一个比非常小的负数大的阈值
            if rel_score <= -100:
                continue

            imp = float(m.meta.get("importance", 5)) / 10.0

            # 时间衰减：越近期访问/创建，分数有少许加成
            last_accessed = m.meta.get("last_accessed", m.meta.get("timestamp", now))
            days_ago = max(0, (now - last_accessed) / 86400)
            time_decay = math.exp(-days_ago / 30.0)  # 近一月加重

            # 综合分数 (相关性是绝对主导，属性做微调)
            final_score = rel_score * (1.0 + imp * 0.2 + time_decay * 0.1)
            scored_memories.append((final_score, m))

        # 按分数排序取 top K
        scored_memories.sort(key=lambda x: x[0], reverse=True)
        top_mems = [sm[1] for sm in scored_memories[:k]]

        if update_access and top_mems:
            for m in top_mems:
                m.meta["access_count"] = m.meta.get("access_count", 0) + 1
                m.meta["last_accessed"] = time.time()
                await self.update_memory(m)

        return top_mems
