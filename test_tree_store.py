import asyncio
import time
from core.chat.tree_store import MarkdownTreeStore


async def test_add():
    store = MarkdownTreeStore()

    # 1. Add Memory
    mem = await store.add_memory(
        user_id="test_user",
        folder="facts",
        content="This is a test memory for the new MarkdownTreeStore!",
        meta={"importance": 8},
    )
    print(f"Added memory: {mem.id} in {mem.folder}")

    # 2. Get the memory back
    fetched = await store.get_memory("test_user", "facts", mem.id)
    print(f"Fetched memory content: {fetched.content}, Meta: {fetched.meta}")

    import jieba

    print(
        "Corpus tokens:",
        [
            t.lower()
            for t in jieba.lcut("This is a test memory for the new MarkdownTreeStore!")
            if t.strip()
        ],
    )
    print("Query tokens:", [t.lower() for t in jieba.lcut("test memory") if t.strip()])

    # 3. Perform a BM25 Search
    await asyncio.sleep(1)  # Test if there's an I/O delay or cache invalidation race
    results = await store.search("test memory", "test_user", "facts", k=5)
    print(f"Search results count: {len(results)}")
    for res in results:
        print(f" - [{res.id}] {res.content}")

    # 4. Profile Operations Example
    from core.chat.user_profile import UserProfileStore

    pstore = UserProfileStore()

    await pstore.add_fact("test_user", "Knows how to write YAML")
    await pstore.add_trait("test_user", "Programmer")

    p = await pstore.get_profile_prompt("test_user")
    print(f"\nGenerater Profile Prompt:\n{p}")


if __name__ == "__main__":
    asyncio.run(test_add())
