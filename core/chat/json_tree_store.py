"""
JSON Tree Store — 基于文件树 + SQLite 索引的记忆存储引擎

架构分离:
- JSON 文件：纯内容存储（id、type、content）→ 内容真相源
- SQLite（MemoryIndex）：所有 meta 数据（importance、tags、timestamps 等）→ 索引与查询

搜索完全委托给 MemoryIndex（FTS5 + 可选向量混合检索）。
"""

import json
import os
import time
import uuid
import asyncio
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field

from core.logging_manager import get_logger
from .memory_paths import (
    ENTITIES_DIR,
    GLOBAL_DIR,
    get_entity_folder,
    ensure_entity_dirs,
)
from .memory_index import MemoryIndex

logger = get_logger("json_tree_store", "green")


# ========== 数据模型 ==========


@dataclass
class JsonMemory:
    """一条记忆实体

    文件中只存储: { id, type, content }
    Meta 完全由 MemoryIndex（SQLite）管理。
    运行时通过 meta 字段暴露索引中的数据。
    """

    id: str
    type: str  # fact | reflection | skill | episodic
    content: dict = field(default_factory=dict)

    # 运行时 meta（来自 SQLite，不写入 JSON 文件）
    meta: dict = field(default_factory=dict)

    # 存储定位信息（不序列化）
    _entity_id: str = field(default="", repr=False)
    _entity_type: str = field(default="", repr=False)
    _folder: str = field(default="", repr=False)
    _base_dir: str = field(default="", repr=False)

    @property
    def raw_text(self) -> str:
        return self.content.get("raw_text", "")

    @property
    def importance(self) -> int:
        return self.meta.get("importance", 5)

    @importance.setter
    def importance(self, value: int):
        self.meta["importance"] = max(1, min(10, value))

    @property
    def access_count(self) -> int:
        return self.meta.get("access_count", 0)

    @property
    def last_accessed(self) -> float:
        return self.meta.get("last_accessed", self.meta.get("timestamp", 0))

    @property
    def timestamp(self) -> float:
        return self.meta.get("timestamp", 0)

    @property
    def tags(self) -> list:
        return self.meta.get("tags", [])

    @property
    def file_path(self) -> str:
        if self._base_dir:
            d = self._base_dir
            if self._folder:
                d = os.path.join(d, self._folder)
            return os.path.join(d, f"{self.id}.json")
        else:
            d = get_entity_folder(self._entity_id, self._entity_type, self._folder)
            return os.path.join(d, f"{self.id}.json")

    def to_file_dict(self) -> dict:
        """序列化为文件格式（纯内容，无 meta）"""
        return {
            "id": self.id,
            "type": self.type,
            "content": self.content,
        }

    def to_full_dict(self) -> dict:
        """序列化为完整格式（含 meta，用于 API 输出等场景）"""
        return {
            "id": self.id,
            "type": self.type,
            "meta": self.meta,
            "content": self.content,
        }

    @classmethod
    def from_file_dict(cls, data: dict, meta: dict = None, **location_kwargs) -> "JsonMemory":
        """从文件字典 + 索引 meta 反序列化"""
        return cls(
            id=data.get("id", ""),
            type=data.get("type", "fact"),
            content=data.get("content", {}),
            meta=meta or {},
            **location_kwargs,
        )

    @classmethod
    def from_dict(cls, data: dict, **location_kwargs) -> "JsonMemory":
        """兼容旧格式（含 meta 的 JSON 文件）反序列化"""
        return cls(
            id=data.get("id", ""),
            type=data.get("type", "fact"),
            meta=data.get("meta", {}),
            content=data.get("content", {}),
            **location_kwargs,
        )

    def touch_access(self):
        """标记一次访问"""
        self.meta["access_count"] = self.access_count + 1
        self.meta["last_accessed"] = time.time()


# ========== 存储引擎 ==========


class JsonTreeStore:
    """基于文件 + SQLite 索引的记忆管理系统

    文件系统: 纯内容 JSON (id, type, content)
    SQLite: 所有 meta + FTS5 全文索引 + 可选向量索引
    """

    def __init__(self, index: MemoryIndex = None):
        os.makedirs(ENTITIES_DIR, exist_ok=True)
        os.makedirs(GLOBAL_DIR, exist_ok=True)

        self.index = index or MemoryIndex()

        # 读写锁 per 目录
        self._locks: Dict[str, asyncio.Lock] = {}
        logger.info("JsonTreeStore initialized (JSON files + SQLite index)")

    def _get_lock(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    @staticmethod
    def _resolve_dir(
        entity_id: str = "",
        entity_type: str = "",
        folder: str = "facts",
        base_dir: str = "",
    ) -> str:
        if base_dir:
            d = base_dir
            if folder:
                d = os.path.join(d, folder)
            return d
        else:
            return get_entity_folder(entity_id, entity_type, folder)

    @staticmethod
    def _cache_key(
        entity_id: str = "", entity_type: str = "", folder: str = "", base_dir: str = ""
    ) -> str:
        if base_dir:
            return f"global:{base_dir}:{folder}"
        return f"{entity_type}:{entity_id}:{folder}"

    # ==========================================
    # CRUD 操作
    # ==========================================

    async def add_memory(
        self,
        content_text: str,
        memory_type: str = "fact",
        importance: int = 5,
        tags: list = None,
        source: dict = None,
        structured_data: dict = None,
        explicit_id: str = "",
        entity_id: str = "",
        entity_type: str = "user",
        folder: str = "facts",
        base_dir: str = "",
    ) -> JsonMemory:
        """写入一条新的记忆"""
        now = time.time()
        mem_id = explicit_id if explicit_id else uuid.uuid4().hex[:12]

        content = {
            "raw_text": content_text,
            "structured_data": structured_data or {},
        }

        meta = {
            "importance": max(1, min(10, importance)),
            "timestamp": now,
            "access_count": 0,
            "last_accessed": now,
            "tags": tags or [],
            "source": source or {},
        }

        memory = JsonMemory(
            id=mem_id,
            type=memory_type,
            content=content,
            meta=meta,
            _entity_id=entity_id,
            _entity_type=entity_type,
            _folder=folder,
            _base_dir=base_dir,
        )

        # 确保目录存在
        if not base_dir and entity_id:
            ensure_entity_dirs(entity_id, entity_type)

        lock_key = self._cache_key(entity_id, entity_type, folder, base_dir)
        async with self._get_lock(lock_key):
            # 1. 写 JSON 文件（纯内容）
            await asyncio.to_thread(self._sync_write_file, memory)

            # 2. 写 SQLite 索引（meta + 全文）
            await asyncio.to_thread(
                self.index.upsert,
                memory_id=mem_id,
                raw_text=content_text,
                memory_type=memory_type,
                importance=meta["importance"],
                tags=meta["tags"],
                source=meta["source"],
                entity_id=entity_id,
                entity_type=entity_type,
                folder=folder,
                base_dir=base_dir,
                file_path=memory.file_path,
                timestamp=now,
                last_accessed=now,
                access_count=0,
            )

        logger.debug(
            f"Memory added: type={memory_type}, id={mem_id}, "
            f"entity={entity_type}:{entity_id}, folder={folder}"
        )
        return memory

    async def update_memory(self, memory: JsonMemory) -> bool:
        """更新记忆（文件内容 + 索引 meta）"""
        lock_key = self._cache_key(
            memory._entity_id, memory._entity_type, memory._folder, memory._base_dir
        )
        async with self._get_lock(lock_key):
            try:
                # 1. 更新 JSON 文件
                await asyncio.to_thread(self._sync_write_file, memory)

                # 2. 更新索引
                await asyncio.to_thread(
                    self.index.upsert,
                    memory_id=memory.id,
                    raw_text=memory.raw_text,
                    memory_type=memory.type,
                    importance=memory.importance,
                    tags=memory.tags,
                    source=memory.meta.get("source", {}),
                    entity_id=memory._entity_id,
                    entity_type=memory._entity_type,
                    folder=memory._folder,
                    base_dir=memory._base_dir,
                    file_path=memory.file_path,
                    timestamp=memory.timestamp,
                    last_accessed=memory.last_accessed,
                    access_count=memory.access_count,
                )
                return True
            except Exception as e:
                logger.error(f"Failed to update memory {memory.id}: {e}")
                return False

    async def get_memory(
        self,
        memory_id: str,
        entity_id: str = "",
        entity_type: str = "user",
        folder: str = "facts",
        base_dir: str = "",
    ) -> Optional[JsonMemory]:
        """精确获取一条记忆（文件内容 + 索引 meta）"""
        d = self._resolve_dir(entity_id, entity_type, folder, base_dir)
        fpath = os.path.join(d, f"{memory_id}.json")

        if not os.path.exists(fpath):
            return None

        try:
            # 读文件内容
            file_data = await asyncio.to_thread(self._sync_read_file, fpath)

            # 读索引 meta
            idx_meta = await asyncio.to_thread(self.index.get_meta, memory_id)
            meta = {}
            if idx_meta:
                meta = {
                    "importance": idx_meta.get("importance", 5),
                    "timestamp": idx_meta.get("timestamp", 0),
                    "access_count": idx_meta.get("access_count", 0),
                    "last_accessed": idx_meta.get("last_accessed", 0),
                    "tags": idx_meta.get("tags", []),
                    "source": idx_meta.get("source", {}),
                }
            else:
                # 回退：从旧格式 JSON 中读取 meta（兼容迁移）
                meta = file_data.get("meta", {})

            return JsonMemory.from_file_dict(
                file_data,
                meta=meta,
                _entity_id=entity_id,
                _entity_type=entity_type,
                _folder=folder,
                _base_dir=base_dir,
            )
        except Exception as e:
            logger.error(f"Read memory error {memory_id}: {e}")
            return None

    async def delete_memory(
        self,
        memory_id: str,
        entity_id: str = "",
        entity_type: str = "user",
        folder: str = "facts",
        base_dir: str = "",
    ) -> bool:
        """物理删除一条记忆（文件 + 索引）"""
        d = self._resolve_dir(entity_id, entity_type, folder, base_dir)
        fpath = os.path.join(d, f"{memory_id}.json")

        lock_key = self._cache_key(entity_id, entity_type, folder, base_dir)
        async with self._get_lock(lock_key):
            try:
                if os.path.exists(fpath):
                    await asyncio.to_thread(os.remove, fpath)
                await asyncio.to_thread(self.index.delete, memory_id)
                logger.debug(f"Memory deleted: {memory_id}")
                return True
            except Exception as e:
                logger.error(f"Delete memory error {memory_id}: {e}")
        return False

    async def archive_memory(
        self,
        memory_id: str,
        entity_id: str = "",
        entity_type: str = "user",
        folder: str = "facts",
        base_dir: str = "",
    ) -> bool:
        """将记忆移入归档目录"""
        from .memory_paths import get_archive_dir

        memory = await self.get_memory(memory_id, entity_id, entity_type, folder, base_dir)
        if not memory:
            return False

        archive_dir = get_archive_dir()
        os.makedirs(archive_dir, exist_ok=True)
        archive_path = os.path.join(archive_dir, f"{memory_id}.json")

        try:
            # 归档时写入完整数据（含 meta，方便恢复）
            await asyncio.to_thread(
                self._sync_write_to_path, memory.to_full_dict(), archive_path
            )
            await self.delete_memory(memory_id, entity_id, entity_type, folder, base_dir)
            logger.debug(f"Memory archived: {memory_id}")
            return True
        except Exception as e:
            logger.error(f"Archive memory error {memory_id}: {e}")
            return False

    async def get_all_memories(
        self,
        entity_id: str = "",
        entity_type: str = "user",
        folder: str = "facts",
        base_dir: str = "",
    ) -> List[JsonMemory]:
        """获取指定目录下所有记忆（文件内容 + 索引 meta）"""
        d = self._resolve_dir(entity_id, entity_type, folder, base_dir)
        if not os.path.exists(d):
            return []

        def _scan():
            mems = []
            for fname in os.listdir(d):
                if not fname.endswith(".json"):
                    continue
                fpath = os.path.join(d, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    mem_id = data.get("id", fname[:-5])

                    # 从索引读 meta
                    idx_meta = self.index.get_meta(mem_id)
                    meta = {}
                    if idx_meta:
                        meta = {
                            "importance": idx_meta.get("importance", 5),
                            "timestamp": idx_meta.get("timestamp", 0),
                            "access_count": idx_meta.get("access_count", 0),
                            "last_accessed": idx_meta.get("last_accessed", 0),
                            "tags": idx_meta.get("tags", []),
                            "source": idx_meta.get("source", {}),
                        }
                    else:
                        # 兼容旧格式
                        meta = data.get("meta", {})

                    mems.append(
                        JsonMemory.from_file_dict(
                            data,
                            meta=meta,
                            _entity_id=entity_id,
                            _entity_type=entity_type,
                            _folder=folder,
                            _base_dir=base_dir,
                        )
                    )
                except Exception as e:
                    logger.warning(f"Could not load {fpath}: {e}")
            return mems

        return await asyncio.to_thread(_scan)

    # ==========================================
    # 检索（委托给 MemoryIndex）
    # ==========================================

    async def search(
        self,
        query: str,
        entity_id: str = "",
        entity_type: str = "user",
        folder: str = "facts",
        base_dir: str = "",
        k: int = 5,
        update_access: bool = False,
        query_embedding: list = None,
    ) -> List[JsonMemory]:
        """混合检索（FTS5 + 可选向量）

        Args:
            query: 搜索查询文本
            entity_id / entity_type / folder / base_dir: 定位范围
            k: Top-K
            update_access: 是否更新 access_count
            query_embedding: 可选查询向量（有则启用混合检索）
        """
        # 委托给 MemoryIndex
        results = await asyncio.to_thread(
            self.index.hybrid_search,
            query=query,
            query_embedding=query_embedding,
            entity_id=entity_id,
            entity_type=entity_type,
            folder=folder,
            base_dir=base_dir,
            k=k,
        )

        if not results:
            return []

        # 将索引结果转换为 JsonMemory 对象
        memories = []
        for r in results:
            mem = JsonMemory(
                id=r["id"],
                type=r.get("memory_type", "fact"),
                content={"raw_text": r.get("raw_text", "")},
                meta={
                    "importance": r.get("importance", 5),
                    "timestamp": r.get("timestamp", 0),
                    "access_count": r.get("access_count", 0),
                    "last_accessed": r.get("last_accessed", 0),
                    "tags": r.get("tags", []),
                    "source": r.get("source", {}),
                },
                _entity_id=entity_id or r.get("entity_id", ""),
                _entity_type=entity_type or r.get("entity_type", ""),
                _folder=folder or r.get("folder", ""),
                _base_dir=base_dir or r.get("base_dir", ""),
            )

            # 尝试加载完整 content（包含 structured_data）
            fpath = r.get("file_path", "") or mem.file_path
            if fpath and os.path.exists(fpath):
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        file_data = json.load(f)
                    mem.content = file_data.get("content", mem.content)
                except Exception:
                    pass

            memories.append(mem)

        if update_access:
            for mem in memories:
                mem.touch_access()
                await asyncio.to_thread(self.index.touch_access, mem.id)

        return memories

    async def search_across_folders(
        self,
        query: str,
        entity_id: str = "",
        entity_type: str = "user",
        folders: list = None,
        k: int = 5,
        query_embedding: list = None,
    ) -> List[JsonMemory]:
        """跨多个目录检索，合并排序"""
        if folders is None:
            folders = ["facts", "reflections"]

        all_results = []
        for folder in folders:
            results = await self.search(
                query=query,
                entity_id=entity_id,
                entity_type=entity_type,
                folder=folder,
                k=k,
                query_embedding=query_embedding,
            )
            all_results.extend(results)

        # 按 meta._score（如果有）或 importance 排序
        import math
        now = time.time()
        all_results.sort(
            key=lambda m: m.importance * 0.6
            + math.exp(-(now - m.last_accessed) / 86400 / 30.0) * 0.4,
            reverse=True,
        )
        return all_results[:k]

    # ==========================================
    # 索引管理
    # ==========================================

    async def rebuild_index(self):
        """从文件系统重建 SQLite 索引（灾难恢复）"""
        from .memory_paths import MEMORY_ROOT
        await asyncio.to_thread(self.index.rebuild_index_from_files, MEMORY_ROOT)
        logger.info("Index rebuilt from files")

    async def ensure_indexed(self, memory: JsonMemory):
        """确保单条记忆在索引中（用于旧文件迁移）"""
        existing = await asyncio.to_thread(self.index.get_meta, memory.id)
        if not existing:
            await asyncio.to_thread(
                self.index.upsert,
                memory_id=memory.id,
                raw_text=memory.raw_text,
                memory_type=memory.type,
                importance=memory.importance,
                tags=memory.tags,
                source=memory.meta.get("source", {}),
                entity_id=memory._entity_id,
                entity_type=memory._entity_type,
                folder=memory._folder,
                base_dir=memory._base_dir,
                file_path=memory.file_path,
                timestamp=memory.timestamp,
                last_accessed=memory.last_accessed,
                access_count=memory.access_count,
            )

    # ==========================================
    # 内部方法
    # ==========================================

    @staticmethod
    def _sync_write_file(memory: JsonMemory):
        """写入 JSON 文件（纯内容，无 meta）"""
        fpath = memory.file_path
        os.makedirs(os.path.dirname(fpath), exist_ok=True)
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(memory.to_file_dict(), f, ensure_ascii=False, indent=2)

    @staticmethod
    def _sync_read_file(fpath: str) -> dict:
        with open(fpath, "r", encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def _sync_write_to_path(data: dict, fpath: str):
        os.makedirs(os.path.dirname(fpath), exist_ok=True)
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
