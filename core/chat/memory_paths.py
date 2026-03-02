"""
记忆系统路径管理中枢

统一管理 data/memory/ 下所有目录结构的创建与路径解析。
支持四种实体域：user, group, channel, global
"""

import os
import re
from pathlib import Path

from core.logging_manager import get_logger

logger = get_logger("memory_paths", "green")

# ========== 根目录常量 ==========
MEMORY_ROOT = os.path.join("data", "memory")
GLOBAL_DIR = os.path.join(MEMORY_ROOT, "global")
ENTITIES_DIR = os.path.join(MEMORY_ROOT, "entities")
ARCHIVE_DIR = os.path.join(MEMORY_ROOT, "archive")

# ========== 实体类型 ==========
ENTITY_USER = "user"
ENTITY_GROUP = "group"
ENTITY_CHANNEL = "channel"
VALID_ENTITY_TYPES = {ENTITY_USER, ENTITY_GROUP, ENTITY_CHANNEL}

# ========== 记忆子目录 ==========
MEMORY_FOLDERS = ("facts", "reflections", "skills")

# ========== ID 安全校验 ==========
_SAFE_ID_RE = re.compile(r"^[\w\-.:]+$")


def _validate_id(entity_id: str) -> str:
    """校验实体 ID，防止路径穿越"""
    if not entity_id or not _SAFE_ID_RE.match(entity_id):
        raise ValueError(f"不合法的实体 ID: {entity_id!r}")
    return entity_id


# ========== 实体路径 ==========

def get_entity_dir(entity_id: str, entity_type: str) -> str:
    """获取实体根目录: data/memory/entities/{type}_{id}/"""
    if entity_type not in VALID_ENTITY_TYPES:
        raise ValueError(f"未知实体类型: {entity_type!r}, 可选: {VALID_ENTITY_TYPES}")
    _validate_id(entity_id)
    return os.path.join(ENTITIES_DIR, f"{entity_type}_{entity_id}")


def get_entity_folder(entity_id: str, entity_type: str, folder: str) -> str:
    """获取实体下的子目录: data/memory/entities/{type}_{id}/{folder}/"""
    return os.path.join(get_entity_dir(entity_id, entity_type), folder)


def get_entity_profile_path(entity_id: str, entity_type: str) -> str:
    """获取实体画像文件路径: data/memory/entities/{type}_{id}/profile.json"""
    return os.path.join(get_entity_dir(entity_id, entity_type), "profile.json")


# ========== 全局路径 ==========

def get_global_dir() -> str:
    """data/memory/global/"""
    return GLOBAL_DIR


def get_global_self_dir() -> str:
    """data/memory/global/self/"""
    return os.path.join(GLOBAL_DIR, "self")


def get_global_facts_dir() -> str:
    """data/memory/global/facts/"""
    return os.path.join(GLOBAL_DIR, "facts")


def get_global_skills_dir() -> str:
    """data/memory/global/skills/"""
    return os.path.join(GLOBAL_DIR, "skills")


# ========== 归档路径 ==========

def get_archive_dir() -> str:
    """data/memory/archive/"""
    return ARCHIVE_DIR


# ========== 快捷方式（最常用） ==========

def get_user_dir(user_id: str) -> str:
    return get_entity_dir(user_id, ENTITY_USER)


def get_user_folder(user_id: str, folder: str) -> str:
    return get_entity_folder(user_id, ENTITY_USER, folder)


def get_group_dir(group_id: str) -> str:
    return get_entity_dir(group_id, ENTITY_GROUP)


def get_group_folder(group_id: str, folder: str) -> str:
    return get_entity_folder(group_id, ENTITY_GROUP, folder)


def get_channel_dir(channel_id: str) -> str:
    return get_entity_dir(channel_id, ENTITY_CHANNEL)


def get_channel_folder(channel_id: str, folder: str) -> str:
    return get_entity_folder(channel_id, ENTITY_CHANNEL, folder)


# ========== 目录初始化 ==========

def ensure_directory_structure():
    """创建完整的记忆目录骨架（启动时调用一次）"""
    dirs_to_create = [
        MEMORY_ROOT,
        ENTITIES_DIR,
        ARCHIVE_DIR,
        # global
        GLOBAL_DIR,
        os.path.join(GLOBAL_DIR, "facts"),
        os.path.join(GLOBAL_DIR, "skills"),
        os.path.join(GLOBAL_DIR, "self"),
        os.path.join(GLOBAL_DIR, "self", "facts"),
        os.path.join(GLOBAL_DIR, "self", "reflections"),
    ]
    for d in dirs_to_create:
        os.makedirs(d, exist_ok=True)

    logger.info("Memory directory structure initialized")


def ensure_entity_dirs(entity_id: str, entity_type: str):
    """为特定实体创建子目录（懒创建，首次写入时调用）"""
    base = get_entity_dir(entity_id, entity_type)
    os.makedirs(base, exist_ok=True)

    # 不同实体类型有不同的子目录集合
    if entity_type == ENTITY_USER:
        folders = ("facts", "reflections")
    elif entity_type == ENTITY_GROUP:
        folders = ("facts", "reflections")
    elif entity_type == ENTITY_CHANNEL:
        folders = ("facts",)
    else:
        folders = ("facts",)

    for folder in folders:
        os.makedirs(os.path.join(base, folder), exist_ok=True)


# ========== 扫描工具 ==========

def list_all_entities(entity_type: str = None) -> list[tuple[str, str]]:
    """扫描 entities/ 目录，返回所有 (entity_id, entity_type) 对

    Args:
        entity_type: 可选过滤，只返回指定类型的实体
    """
    results = []
    if not os.path.exists(ENTITIES_DIR):
        return results

    for dirname in os.listdir(ENTITIES_DIR):
        # 格式: {type}_{id}
        for et in VALID_ENTITY_TYPES:
            prefix = f"{et}_"
            if dirname.startswith(prefix):
                eid = dirname[len(prefix):]
                if entity_type is None or et == entity_type:
                    results.append((eid, et))
                break

    return results
