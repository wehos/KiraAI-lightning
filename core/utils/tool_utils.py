from abc import abstractmethod, ABC
from typing import Dict, Any


class BaseTool(ABC):
    def __init__(self, *args, **kwargs):
        self._event_context = None

    name = None
    description = None
    parameters = None

    def set_event_context(self, event):
        """由 tool_manager wrapper 注入当前会话的 event 上下文"""
        self._event_context = event

    @abstractmethod
    async def execute(self, *args, **kwargs) -> str:
        """工具的具体执行逻辑，子类必须实现"""
        pass

    @classmethod
    def get_schema(cls) -> Dict[str, Any]:
        """获取工具的function calling schema"""
        return {
            "name": cls.name,
            "description": cls.description,
            "parameters": cls.parameters
        }
