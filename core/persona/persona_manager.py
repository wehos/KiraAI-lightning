"""
Persona 管理器

管理 Agent 核心人设 (data/persona.txt)，支持热加载和人设跃迁合入。
"""

import os
import time

from core.utils.path_utils import get_data_path
from core.logging_manager import get_logger

logger = get_logger("persona_manager", "green")


class PersonaManager:
    def __init__(self):
        self.persona_path = get_data_path() / "persona.txt"
        self.persona_str = ""
        self.persona_mtime = 0.0
        self.reload_persona()

    def get_persona(self) -> str:
        """获取 persona 文本，支持热加载（文件修改自动重载）"""
        try:
            mtime = os.path.getmtime(self.persona_path)
            if mtime != self.persona_mtime:
                self.reload_persona()
        except FileNotFoundError:
            pass
        return self.persona_str

    def update_persona(self, text: str):
        """完整替换 persona 文本"""
        self.persona_str = text
        with open(self.persona_path, "w", encoding="utf-8") as f:
            f.write(text)
        self.persona_mtime = os.path.getmtime(self.persona_path)
        logger.info("Persona updated")

    def reload_persona(self):
        """从文件重载 persona"""
        if not self.persona_path.exists():
            self.persona_path.write_text("")
        with open(self.persona_path, "r", encoding="utf-8") as f:
            self.persona_str = f.read()
        self.persona_mtime = os.path.getmtime(self.persona_path)

    def merge_reflection(self, reflection_text: str, source_id: str = "") -> bool:
        """将一条反思不可逆地合入 persona 基座

        宪章 §6 第三级跃迁: reflection → persona
        合入后该反思应被销毁（由调用方处理）。

        Args:
            reflection_text: 反思内容
            source_id: 来源 reflection 的 ID（用于追溯）

        Returns:
            True if successful
        """
        try:
            current = self.get_persona()
            timestamp = time.strftime("%Y-%m-%d %H:%M", time.localtime())

            # 追加到 persona 末尾，带时间戳和来源标记
            merge_block = (
                f"\n\n[PERSONA LEAP | {timestamp}]\n"
                f"{reflection_text}\n"
                f"[Source: {source_id}]"
            )

            new_persona = current.rstrip() + merge_block
            self.update_persona(new_persona)

            logger.info(
                f"Persona leap: reflection merged (source={source_id})"
            )
            return True
        except Exception as e:
            logger.error(f"Persona merge failed: {e}")
            return False
