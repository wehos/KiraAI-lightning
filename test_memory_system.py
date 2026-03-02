"""
记忆系统集成测试

验证 TomlTreeStore + MemoryIndex (SQLite) + EntityProfileStore + MemoryDecayEngine 的核心功能。
TOML 文件格式 + 语义 ID + SQLite 索引。
"""

import asyncio
import os
import shutil
import time

try:
    import tomllib  # Python 3.11+
except ImportError:
    import tomli as tomllib  # Python 3.10 fallback

# 测试前设置临时 data 目录
TEST_DATA_DIR = "data_test"
TEST_DB_PATH = os.path.join(TEST_DATA_DIR, "memory", "memory_index.db")


def setup_test_env():
    """设置测试环境"""
    if os.path.exists(TEST_DATA_DIR):
        shutil.rmtree(TEST_DATA_DIR)

    import core.chat.memory_paths as mp
    mp.MEMORY_ROOT = os.path.join(TEST_DATA_DIR, "memory")
    mp.GLOBAL_DIR = os.path.join(mp.MEMORY_ROOT, "global")
    mp.ENTITIES_DIR = os.path.join(mp.MEMORY_ROOT, "entities")
    mp.ARCHIVE_DIR = os.path.join(mp.MEMORY_ROOT, "archive")

    import core.chat.memory_index as mi
    mi.DEFAULT_DB_PATH = TEST_DB_PATH


def teardown_test_env():
    import gc
    gc.collect()
    try:
        if os.path.exists(TEST_DATA_DIR):
            shutil.rmtree(TEST_DATA_DIR)
    except OSError:
        time.sleep(0.5)
        if os.path.exists(TEST_DATA_DIR):
            shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)


async def test_directory_structure():
    """测试目录结构创建"""
    from core.chat.memory_paths import ensure_directory_structure, MEMORY_ROOT, GLOBAL_DIR

    ensure_directory_structure()

    assert os.path.exists(MEMORY_ROOT), "MEMORY_ROOT should exist"
    assert os.path.exists(os.path.join(GLOBAL_DIR, "facts")), "global/facts should exist"
    assert os.path.exists(os.path.join(GLOBAL_DIR, "skills")), "global/skills should exist"
    assert os.path.exists(os.path.join(GLOBAL_DIR, "self", "facts")), "global/self/facts should exist"
    assert os.path.exists(os.path.join(GLOBAL_DIR, "self", "reflections")), "global/self/reflections should exist"

    print("✅ test_directory_structure passed")


async def test_toml_tree_store_crud():
    """测试 TomlTreeStore 的增删改查 + SQLite 索引同步"""
    from core.chat.memory_index import MemoryIndex
    from core.chat.toml_tree_store import TomlTreeStore
    from core.chat.memory_paths import ensure_directory_structure

    ensure_directory_structure()
    index = MemoryIndex(db_path=TEST_DB_PATH)
    store = TomlTreeStore(index=index)

    # 添加记忆（使用语义 ID）
    mem = await store.add_memory(
        content_text="用户喜欢 Python 编程",
        memory_type="fact",
        importance=7,
        tags=["programming", "python"],
        semantic_id="likes_python",
        entity_id="test_user_1",
        entity_type="user",
        folder="facts",
    )
    assert mem.id == "likes_python", f"ID should be 'likes_python', got '{mem.id}'"
    assert mem.text == "用户喜欢 Python 编程"
    assert mem.importance == 7
    assert "programming" in mem.tags

    # 验证 TOML 文件格式
    assert mem.file_path.endswith(".toml"), f"File should be .toml, got {mem.file_path}"
    with open(mem.file_path, "rb") as f:
        file_data = tomllib.load(f)
    assert "meta" not in file_data, "TOML file should NOT contain runtime meta"
    assert file_data["id"] == "likes_python"
    assert file_data["type"] == "fact"
    assert file_data["text"] == "用户喜欢 Python 编程"
    assert file_data["importance"] == 7
    assert "programming" in file_data["tags"]

    # 验证 SQLite 索引有 meta
    idx_meta = index.get_meta(mem.id)
    assert idx_meta is not None, "Index should have meta"
    assert idx_meta["importance"] == 7
    assert "programming" in idx_meta["tags"]
    assert idx_meta["entity_id"] == "test_user_1"

    # 读取记忆
    fetched = await store.get_memory(
        memory_id="likes_python",
        entity_id="test_user_1",
        entity_type="user",
        folder="facts",
    )
    assert fetched is not None
    assert fetched.text == mem.text
    assert fetched.importance == 7

    # 更新记忆
    fetched.text = "用户喜欢 Python 和 Rust 编程"
    fetched.importance = 9
    result = await store.update_memory(fetched)
    assert result is True

    # 验证更新同步到索引
    updated_meta = index.get_meta(mem.id)
    assert updated_meta["importance"] == 9

    updated = await store.get_memory(
        memory_id="likes_python",
        entity_id="test_user_1",
        entity_type="user",
        folder="facts",
    )
    assert updated.text == "用户喜欢 Python 和 Rust 编程"
    assert updated.importance == 9

    # 获取所有记忆
    all_mems = await store.get_all_memories(
        entity_id="test_user_1", entity_type="user", folder="facts"
    )
    assert len(all_mems) == 1

    # 删除记忆
    deleted = await store.delete_memory(
        memory_id="likes_python",
        entity_id="test_user_1",
        entity_type="user",
        folder="facts",
    )
    assert deleted is True
    assert index.get_meta(mem.id) is None

    all_mems = await store.get_all_memories(
        entity_id="test_user_1", entity_type="user", folder="facts"
    )
    assert len(all_mems) == 0

    index.close()
    print("✅ test_toml_tree_store_crud passed")


async def test_semantic_id_fallback():
    """测试语义 ID 回退策略"""
    from core.chat.memory_index import MemoryIndex
    from core.chat.toml_tree_store import TomlTreeStore
    from core.chat.memory_paths import ensure_directory_structure

    ensure_directory_structure()
    index = MemoryIndex(db_path=TEST_DB_PATH)
    store = TomlTreeStore(index=index)

    # 不传 semantic_id，应自动生成
    mem = await store.add_memory(
        content_text="用户养了一只猫",
        importance=4,
        entity_id="test_user_2",
        entity_type="user",
        folder="facts",
    )
    assert mem.id, "Should have an auto-generated ID"
    assert "_" in mem.id, f"Fallback ID should contain underscore: {mem.id}"
    assert mem.file_path.endswith(".toml")

    index.close()
    print("✅ test_semantic_id_fallback passed")


async def test_fts5_search():
    """测试 FTS5 全文检索"""
    from core.chat.memory_index import MemoryIndex
    from core.chat.toml_tree_store import TomlTreeStore
    from core.chat.memory_paths import ensure_directory_structure

    ensure_directory_structure()
    index = MemoryIndex(db_path=TEST_DB_PATH)
    store = TomlTreeStore(index=index)

    await store.add_memory(
        content_text="用户是一名后端工程师，擅长 Python",
        importance=8, tags=["backend", "python"],
        semantic_id="backend_engineer",
        entity_id="search_user", entity_type="user", folder="facts",
    )
    await store.add_memory(
        content_text="用户讨厌写 CSS，觉得前端很烦",
        importance=6, tags=["frontend", "css"],
        semantic_id="hates_css",
        entity_id="search_user", entity_type="user", folder="facts",
    )
    await store.add_memory(
        content_text="用户养了一只叫小橘的猫",
        importance=4, tags=["pet", "cat"],
        semantic_id="pet_cat_xiaoju",
        entity_id="search_user", entity_type="user", folder="facts",
    )

    # FTS5 搜索
    results = await store.search(
        query="Python 后端开发",
        entity_id="search_user", entity_type="user", folder="facts",
        k=2,
    )
    assert len(results) > 0, "Should find at least one result"
    assert "Python" in results[0].text or "后端" in results[0].text

    # 跨目录搜索
    await store.add_memory(
        content_text="用户倾向于使用简洁的代码风格",
        importance=7, tags=["code-style"],
        semantic_id="prefers_concise_code",
        entity_id="search_user", entity_type="user", folder="reflections",
    )
    cross_results = await store.search_across_folders(
        query="代码风格",
        entity_id="search_user", entity_type="user",
        folders=["facts", "reflections"],
        k=3,
    )
    assert len(cross_results) > 0

    index.close()
    print("✅ test_fts5_search passed")


async def test_content_hash_dedup():
    """测试 SHA-256 内容哈希去重"""
    from core.chat.memory_index import MemoryIndex

    index = MemoryIndex(db_path=TEST_DB_PATH)

    index.upsert(
        memory_id="hash_test_1",
        raw_text="用户喜欢 Python",
        entity_id="hash_user",
        entity_type="user",
        folder="facts",
    )

    content_hash = MemoryIndex.content_hash("用户喜欢 Python")
    found = index.find_by_hash(content_hash, "hash_user", "user", "facts")
    assert found is not None, "Should find by hash"
    assert found["id"] == "hash_test_1"

    diff_hash = MemoryIndex.content_hash("用户讨厌 Python")
    not_found = index.find_by_hash(diff_hash, "hash_user", "user", "facts")
    assert not_found is None, "Should not find different content"

    index.close()
    print("✅ test_content_hash_dedup passed")


async def test_entity_profile():
    """测试实体画像 CRUD"""
    from core.chat.entity_profile import EntityProfileStore
    from core.chat.memory_paths import ensure_directory_structure

    ensure_directory_structure()
    store = EntityProfileStore()

    profile = await store.get_profile("profile_test_user", "user")
    assert profile.entity_id == "profile_test_user"
    assert profile.entity_type == "user"

    await store.update_profile(
        "profile_test_user", "user",
        name="Alice", nickname="小A",
        platform="telegram",
    )
    updated = await store.get_profile("profile_test_user", "user")
    assert updated.name == "Alice"
    assert updated.nickname == "小A"

    await store.add_trait("profile_test_user", "技术导向")
    await store.add_fact("profile_test_user", "喜欢 Rust 语言")

    profile = await store.get_profile("profile_test_user", "user")
    assert "技术导向" in profile.traits
    assert "喜欢 Rust 语言" in profile.facts

    prompt = await store.get_profile_prompt("profile_test_user", "user")
    assert "Alice" in prompt
    assert "技术导向" in prompt

    await store.update_profile("test_group_1", "group", name="技术讨论组")
    group_profile = await store.get_profile("test_group_1", "group")
    assert group_profile.name == "技术讨论组"
    assert group_profile.entity_type == "group"

    print("✅ test_entity_profile passed")


async def test_memory_decay():
    """测试记忆衰减引擎"""
    from core.chat.memory_index import MemoryIndex
    from core.chat.toml_tree_store import TomlTreeStore
    from core.chat.memory_decay import MemoryDecayEngine
    from core.chat.memory_paths import ensure_directory_structure

    ensure_directory_structure()
    index = MemoryIndex(db_path=TEST_DB_PATH)
    store = TomlTreeStore(index=index)
    engine = MemoryDecayEngine(store)

    mem = await store.add_memory(
        content_text="用户三个月前提到过一次某个话题",
        importance=2,
        semantic_id="old_topic_mention",
        entity_id="decay_user", entity_type="user", folder="facts",
    )

    old_time = time.time() - 90 * 86400
    index.update_meta(mem.id, last_accessed=old_time)
    index._conn.execute(
        "UPDATE memories SET timestamp = ? WHERE id = ?", (old_time, mem.id)
    )
    index._conn.commit()
    meta = index.get_meta(mem.id)

    score = engine.calculate_retention_score(meta)
    assert score < 0.4, f"Old low-importance memory should have low score, got {score}"

    deleted, downgraded = await engine.garbage_collect("decay_user", "user", "facts")
    assert deleted > 0 or downgraded > 0, "Should have removed or downgraded the memory"

    index.close()
    print("✅ test_memory_decay passed")


async def test_archive():
    """测试记忆归档（TOML 格式）"""
    from core.chat.memory_index import MemoryIndex
    from core.chat.toml_tree_store import TomlTreeStore
    from core.chat.memory_paths import ensure_directory_structure, ARCHIVE_DIR

    ensure_directory_structure()
    index = MemoryIndex(db_path=TEST_DB_PATH)
    store = TomlTreeStore(index=index)

    mem = await store.add_memory(
        content_text="将被归档的记忆",
        importance=3,
        semantic_id="to_be_archived",
        entity_id="archive_user", entity_type="user", folder="facts",
    )

    result = await store.archive_memory(
        memory_id=mem.id,
        entity_id="archive_user", entity_type="user", folder="facts",
    )
    assert result is True

    fetched = await store.get_memory(mem.id, "archive_user", "user", "facts")
    assert fetched is None

    assert index.get_meta(mem.id) is None

    archive_file = os.path.join(ARCHIVE_DIR, f"{mem.id}.toml")
    assert os.path.exists(archive_file), "Archive file should exist"
    with open(archive_file, "rb") as f:
        archive_data = tomllib.load(f)
    assert "meta" in archive_data, "Archive should contain meta for recovery"

    index.close()
    print("✅ test_archive passed")


async def test_global_memory():
    """测试全局域记忆"""
    from core.chat.memory_index import MemoryIndex
    from core.chat.toml_tree_store import TomlTreeStore
    from core.chat.memory_paths import ensure_directory_structure, get_global_self_dir

    ensure_directory_structure()
    index = MemoryIndex(db_path=TEST_DB_PATH)
    store = TomlTreeStore(index=index)

    global_self = get_global_self_dir()

    mem = await store.add_memory(
        content_text="我回答用户问题时倾向于过于详细",
        memory_type="fact",
        importance=3,
        tags=["self-awareness"],
        semantic_id="verbose_answers",
        base_dir=global_self,
        folder="facts",
    )
    assert mem.id == "verbose_answers"

    all_self_facts = await store.get_all_memories(
        base_dir=global_self, folder="facts"
    )
    assert len(all_self_facts) >= 1
    found = any("过于详细" in m.text for m in all_self_facts)
    assert found, "Should find the global self fact"

    index.close()
    print("✅ test_global_memory passed")


async def test_memory_router():
    """测试回复意愿路由"""
    from core.chat.memory_router import MemoryRouter

    router = MemoryRouter()

    router.buffer_message("session_1", "user", "你好，帮我看个问题", user_id="u1")
    assert not router.should_flush("session_1"), "Should not flush with 1 message"

    for i in range(4):
        router.buffer_message("session_1", "user", f"消息 {i}", user_id="u1")

    assert router.should_flush("session_1"), "Should flush at max messages"

    ctx = router.flush_and_evaluate("session_1")
    assert ctx is not None
    assert len(ctx.messages) == 5
    assert ctx.reply_score > 0, "Should have positive reply score"
    assert router.should_reply(ctx), "Should want to reply"

    router.buffer_message("session_2", "user", "测试", mentioned=True)
    router._buffer_timestamps["session_2"] = time.time() - 10
    ctx2 = router.flush_and_evaluate("session_2")
    assert ctx2.mentioned is True
    assert ctx2.reply_score == 1.0

    print("✅ test_memory_router passed")


async def test_index_rebuild():
    """测试从 TOML 文件系统重建索引"""
    from core.chat.memory_index import MemoryIndex
    from core.chat.toml_tree_store import TomlTreeStore
    from core.chat.memory_paths import ensure_directory_structure, MEMORY_ROOT

    ensure_directory_structure()
    index = MemoryIndex(db_path=TEST_DB_PATH)
    store = TomlTreeStore(index=index)

    mem1 = await store.add_memory(
        content_text="重建测试记忆 1",
        importance=7,
        semantic_id="rebuild_test_1",
        entity_id="rebuild_user", entity_type="user", folder="facts",
    )
    mem2 = await store.add_memory(
        content_text="重建测试记忆 2",
        importance=5,
        semantic_id="rebuild_test_2",
        entity_id="rebuild_user", entity_type="user", folder="facts",
    )

    # 清空索引（模拟丢失）
    index._conn.execute("DELETE FROM memories")
    index._conn.execute("DELETE FROM memories_fts")
    index._conn.commit()
    assert index.get_meta(mem1.id) is None

    # 重建（现在扫描 .toml 文件）
    index.rebuild_index_from_files(MEMORY_ROOT)

    rebuilt1 = index.get_meta(mem1.id)
    rebuilt2 = index.get_meta(mem2.id)
    assert rebuilt1 is not None, "Should rebuild mem1 index"
    assert rebuilt2 is not None, "Should rebuild mem2 index"
    assert rebuilt1["importance"] == 7, "Should preserve importance from TOML"

    index.close()
    print("✅ test_index_rebuild passed")


async def main():
    setup_test_env()
    try:
        await test_directory_structure()
        await test_toml_tree_store_crud()
        await test_semantic_id_fallback()
        await test_fts5_search()
        await test_content_hash_dedup()
        await test_entity_profile()
        await test_memory_decay()
        await test_archive()
        await test_global_memory()
        await test_memory_router()
        await test_index_rebuild()
        print("\n🎉 All 11 tests passed!")
    finally:
        teardown_test_env()


if __name__ == "__main__":
    asyncio.run(main())
