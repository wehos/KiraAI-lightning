"""
用户画像存储层，使用 JSON 文件持久化用户结构化信息
"""

import time
from dataclasses import dataclass, field, fields

from core.logging_manager import get_logger
from core.chat.tree_store import MarkdownTreeStore, MarkdownMemory

logger = get_logger("user_profile", "green")


@dataclass
class UserProfile:
    """用户画像"""

    user_id: str
    platform: str = ""
    name: str = ""
    nickname: str = ""
    traits: list[str] = field(default_factory=list)
    preferences: dict = field(default_factory=dict)
    relationships: dict = field(default_factory=dict)
    facts: list[str] = field(default_factory=list)
    last_interaction: float = 0.0
    interaction_count: int = 0
    extra: dict = field(default_factory=dict)


class UserProfileStore:
    """基于文件树设计的用户画像存储管理 (使用 MarkdownTreeStore)"""

    def __init__(self):
        # 使用类级别的共享或独立的 TreeStore 都可以，这里直接实例化或可以依赖注入
        # 为了与 MemoryManager 共享目录结构并实现隔离，我们在初始化时直接内部创建一个 instance。
        # (通常 profile 是不会非常频繁触发查询所以没有太大开销，也可以外部注入以共享锁)
        self.tree_store = MarkdownTreeStore()
        # 由于我们依赖异步的 TreeStore，所有涉及画像的操作必须是异步的或者我们在必要时同步等待
        # 原系统都是同步方法，我们将把所有方法改造为 async
        logger.info("UserProfileStore initialized (Tree-based Markdown backend)")

    async def _get_or_create_profile(self, user_id: str) -> MarkdownMemory:
        """获取并在没有时创建该用户的 profile.md"""
        profile = await self.tree_store.get_memory(
            user_id, folder="", memory_id="profile"
        )
        if profile is None:
            # 创建默认的 Profile 骨架
            meta = {
                "type": "profile",
                "traits": [],
                "preferences": {},
                "relationships": {},
                "facts": [],
                "interaction_count": 0,
                "last_interaction": 0.0,
                "platform": "",
                "name": "",
                "nickname": "",
                "extra": {},
            }
            content = (
                f"# Profile for {user_id}\n\n此文件自动生成，保存用户的全局画像设定。"
            )
            profile = await self.tree_store.add_memory(
                user_id=user_id,
                folder="",
                content=content,
                meta=meta,
                explicit_id="profile",
            )
        return profile

    async def get_raw_profile(self, user_id: str) -> UserProfile:
        """获取用户画像结构"""
        mem = await self._get_or_create_profile(user_id)
        # 将 YAML Meta 映射回 DataClass
        return UserProfile(
            user_id=user_id,
            platform=mem.meta.get("platform", ""),
            name=mem.meta.get("name", ""),
            nickname=mem.meta.get("nickname", ""),
            traits=mem.meta.get("traits") or [],
            preferences=mem.meta.get("preferences") or {},
            relationships=mem.meta.get("relationships") or {},
            facts=mem.meta.get("facts") or [],
            last_interaction=mem.meta.get("last_interaction", 0.0),
            interaction_count=mem.meta.get("interaction_count", 0),
            extra=mem.meta.get("extra") or {},
        )

    # 原本同步的各种增删改查现在都要变成 async 的。
    async def get_profile(self, user_id: str) -> UserProfile:
        return await self.get_raw_profile(user_id)

    async def update_profile(self, user_id: str, **kwargs) -> UserProfile:
        mem = await self._get_or_create_profile(user_id)
        allowed = {f.name for f in fields(UserProfile)} - {"user_id"}

        for key, value in kwargs.items():
            if key in allowed:
                mem.meta[key] = value

        mem.meta["last_interaction"] = time.time()
        await self.tree_store.update_memory(mem)
        return await self.get_raw_profile(user_id)

    async def add_trait(self, user_id: str, trait: str):
        mem = await self._get_or_create_profile(user_id)
        traits = mem.meta.get("traits", [])
        if trait not in traits:
            traits.append(trait)
            mem.meta["traits"] = traits
            await self.tree_store.update_memory(mem)

    async def remove_trait(self, user_id: str, trait: str):
        mem = await self._get_or_create_profile(user_id)
        traits = mem.meta.get("traits", [])
        if trait in traits:
            traits.remove(trait)
            mem.meta["traits"] = traits
            await self.tree_store.update_memory(mem)

    async def add_fact(self, user_id: str, fact: str):
        mem = await self._get_or_create_profile(user_id)
        facts = mem.meta.get("facts", [])
        if fact not in facts:
            facts.append(fact)
            mem.meta["facts"] = facts
            await self.tree_store.update_memory(mem)

    async def update_fact(self, user_id: str, old_fact: str, new_fact: str):
        mem = await self._get_or_create_profile(user_id)
        facts = mem.meta.get("facts", [])
        if old_fact in facts:
            idx = facts.index(old_fact)
            facts[idx] = new_fact
            mem.meta["facts"] = facts
            await self.tree_store.update_memory(mem)

    async def remove_fact(self, user_id: str, fact: str):
        mem = await self._get_or_create_profile(user_id)
        facts = mem.meta.get("facts", [])
        if fact in facts:
            facts.remove(fact)
            mem.meta["facts"] = facts
            await self.tree_store.update_memory(mem)

    async def set_relationship(self, user_id: str, target: str, relation: str):
        mem = await self._get_or_create_profile(user_id)
        rels = mem.meta.get("relationships", {})
        rels[target] = relation
        mem.meta["relationships"] = rels
        await self.tree_store.update_memory(mem)

    async def increment_interaction(self, user_id: str):
        mem = await self._get_or_create_profile(user_id)
        mem.meta["interaction_count"] = mem.meta.get("interaction_count", 0) + 1
        mem.meta["last_interaction"] = time.time()
        await self.tree_store.update_memory(mem)

    async def increment_and_update_profile(self, user_id: str, **kwargs):
        mem = await self._get_or_create_profile(user_id)
        mem.meta["interaction_count"] = mem.meta.get("interaction_count", 0) + 1
        mem.meta["last_interaction"] = time.time()

        allowed = {f.name for f in fields(UserProfile)} - {"user_id"}
        for key, value in kwargs.items():
            if key in allowed:
                mem.meta[key] = value

        await self.tree_store.update_memory(mem)

    async def get_profile_prompt(self, user_id: str) -> str:
        """将用户画像格式化为 prompt 文本"""
        profile = await self.get_raw_profile(user_id)
        parts = []
        if profile.name:
            parts.append(f"名字: {profile.name}")
        if profile.nickname:
            parts.append(f"昵称: {profile.nickname}")
        if profile.platform:
            parts.append(f"平台: {profile.platform}")
        if profile.traits:
            parts.append(f"特征: {', '.join(profile.traits)}")
        if profile.preferences:
            prefs = ", ".join(f"{k}: {v}" for k, v in profile.preferences.items())
            parts.append(f"偏好: {prefs}")
        if profile.relationships:
            rels = ", ".join(f"{k}: {v}" for k, v in profile.relationships.items())
            parts.append(f"关系: {rels}")
        if profile.facts:
            facts_str = "\n  ".join(f"- {f}" for f in profile.facts)
            parts.append(f"已知事实:\n  {facts_str}")
        if profile.interaction_count:
            parts.append(f"互动次数: {profile.interaction_count}")
        return "\n".join(parts) if parts else "暂无画像信息"

    async def delete_profile(self, user_id: str) -> bool:
        """删除用户画像文件"""
        return await self.tree_store.delete_memory(user_id, "", "profile")
