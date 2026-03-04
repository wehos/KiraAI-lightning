"""
疲劳系统插件

让 AI 拥有疲劳感，通过三个维度叠加计算疲劳值 (0-100)：

1. 日内节律 (Circadian)：模拟生物钟
   - 深夜 2-6 点疲劳高，上午/晚上精力充沛
   - 贡献范围：0-30

2. 会话强度 (Session Load)：当前 wake 周期内的消息处理量
   - 每处理一条消息累加疲劳，空闲时缓慢恢复
   - 贡献范围：0-40（可配置）

3. 全局负荷 (Global Load)：过去 N 分钟内全局消息总量
   - 高强度对话后整体疲劳基线上移
   - 贡献范围：0-30（可配置）

效果：
- 通过 ON_LLM_REQUEST 注入疲劳状态描述到 system prompt，LLM 自然调整语气
- 通过 get_fatigue() 暴露给 debounce 插件，影响回复意愿阈值
"""

import math
import time
import asyncio
from datetime import datetime
from collections import deque

from core.plugin import BasePlugin, logger, on, Priority
from core.chat.message_utils import KiraMessageEvent, KiraMessageBatchEvent
from core.provider import LLMRequest
from core.prompt_manager import Prompt


class DefaultPlugin(BasePlugin):
    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)

        # ---------- 日内节律配置 ----------
        self.enable_circadian = cfg.get("enable_circadian", True)
        self._peak_ranges = self._parse_hour_ranges(cfg.get("peak_hours", "10-13,19-22"))
        self._low_ranges = self._parse_hour_ranges(cfg.get("low_hours", "2-6"))

        # ---------- 会话疲劳配置 ----------
        self.session_fatigue_rate = float(cfg.get("session_fatigue_rate", 2.0))
        self.session_fatigue_cap = float(cfg.get("session_fatigue_cap", 40))

        # ---------- 全局负荷配置 ----------
        self.global_window = float(cfg.get("global_window_minutes", 60)) * 60  # → 秒
        self.global_fatigue_per_msg = float(cfg.get("global_fatigue_per_msg", 0.3))
        self.global_fatigue_cap = float(cfg.get("global_fatigue_cap", 30))

        # ---------- 恢复 ----------
        self.recovery_rate = float(cfg.get("recovery_rate", 5.0))  # 每分钟

        # ---------- 运行时状态 ----------
        # 会话疲劳：sid → 累积疲劳原始值
        self._session_fatigue: dict[str, float] = {}
        # 会话最后活跃时间：用于计算空闲恢复
        self._session_last_active: dict[str, float] = {}
        # 全局消息时间戳记录（deque 做滑动窗口）
        self._global_msg_timestamps: deque[float] = deque()

    async def initialize(self):
        logger.info(
            f"[Fatigue] Initialized — circadian={self.enable_circadian}, "
            f"session_rate={self.session_fatigue_rate}, "
            f"global_window={self.global_window / 60:.0f}min"
        )

    async def terminate(self):
        pass

    # ==========================================
    # 静态工具
    # ==========================================

    @staticmethod
    def _parse_hour_ranges(text: str) -> list[tuple[int, int]]:
        """解析 '2-6,23-24' → [(2,6),(23,24)]"""
        ranges = []
        for part in text.split(","):
            part = part.strip()
            if "-" in part:
                try:
                    a, b = part.split("-", 1)
                    ranges.append((int(a), int(b)))
                except ValueError:
                    pass
        return ranges

    @staticmethod
    def _hour_in_ranges(hour: int, ranges: list[tuple[int, int]]) -> bool:
        for lo, hi in ranges:
            if lo <= hour < hi:
                return True
        return False

    # ==========================================
    # 疲劳值计算
    # ==========================================

    def _circadian_fatigue(self) -> float:
        """日内节律疲劳 (0-30)"""
        if not self.enable_circadian:
            return 0.0

        hour = datetime.now().hour
        minute = datetime.now().minute
        t = hour + minute / 60.0  # 精确到分钟的小数时间

        # 在低谷时段
        if self._hour_in_ranges(hour, self._low_ranges):
            return 25.0 + 5.0 * math.sin(math.pi * (t - 2) / 4)  # 2-6点峰值30

        # 在高峰时段
        if self._hour_in_ranges(hour, self._peak_ranges):
            return 5.0  # 非常清醒

        # 过渡时段：用正弦曲线模拟
        # 以 4:00 为疲劳最高点(30)，14:00 为最低点(5)
        # sin 周期 = 24h
        fatigue = 17.5 + 12.5 * math.cos(math.pi * (t - 4) / 12)
        return max(0.0, min(30.0, fatigue))

    def _session_fatigue_value(self, sid: str) -> float:
        """会话强度疲劳 (0-session_fatigue_cap)

        raw 值随消息累加，空闲时按 recovery_rate 衰减。
        """
        raw = self._session_fatigue.get(sid, 0.0)
        last_active = self._session_last_active.get(sid)
        if last_active is not None:
            idle_minutes = (time.time() - last_active) / 60.0
            recovered = idle_minutes * self.recovery_rate
            raw = max(0.0, raw - recovered)
            self._session_fatigue[sid] = raw  # 更新衰减后的值
        return min(raw, self.session_fatigue_cap)

    def _global_fatigue_value(self) -> float:
        """全局负荷疲劳 (0-global_fatigue_cap)"""
        now = time.time()
        cutoff = now - self.global_window
        # 清理过期记录
        while self._global_msg_timestamps and self._global_msg_timestamps[0] < cutoff:
            self._global_msg_timestamps.popleft()
        count = len(self._global_msg_timestamps)
        return min(count * self.global_fatigue_per_msg, self.global_fatigue_cap)

    def get_fatigue(self, sid: str = "") -> float:
        """获取总疲劳值 (0-100)，供外部插件调用"""
        circadian = self._circadian_fatigue()
        session = self._session_fatigue_value(sid) if sid else 0.0
        global_load = self._global_fatigue_value()
        total = circadian + session + global_load
        return min(100.0, max(0.0, total))

    def get_fatigue_breakdown(self, sid: str = "") -> dict:
        """获取疲劳值分项明细"""
        return {
            "circadian": round(self._circadian_fatigue(), 1),
            "session": round(self._session_fatigue_value(sid) if sid else 0.0, 1),
            "global": round(self._global_fatigue_value(), 1),
            "total": round(self.get_fatigue(sid), 1),
        }

    # ==========================================
    # 疲劳状态描述生成
    # ==========================================

    @staticmethod
    def _fatigue_description(total: float, breakdown: dict) -> str:
        """根据疲劳值生成自然语言状态描述，注入到 system prompt"""
        hour = datetime.now().hour

        if total <= 15:
            energy = "精力充沛，状态非常好"
            style_hint = "回复可以活泼、热情、详细一些"
        elif total <= 30:
            energy = "状态不错，比较有精神"
            style_hint = "正常回复即可"
        elif total <= 50:
            energy = "有一点点累了，但还好"
            style_hint = "回复可以稍微简洁一些，偶尔可以表达一下'有点累了'的感觉"
        elif total <= 70:
            energy = "挺累的，有点想休息"
            style_hint = "回复明显简短，语气更慵懒，可以说'好困啊'之类的，对不太重要的话题可以敷衍一点"
        elif total <= 85:
            energy = "非常疲惫，快撑不住了"
            style_hint = "回复很简短，语气很困倦，可以用'嗯..''好的..'这种简短回应，表达想睡觉的意愿"
        else:
            energy = "困到不行，随时要睡着的感觉"
            style_hint = "极简回复，甚至可以发一些'zzz''💤'之类的，或者直接说想去睡了"

        # 时间感知
        if 0 <= hour < 6:
            time_feel = "现在是深夜/凌晨，你会觉得很困"
        elif 6 <= hour < 9:
            time_feel = "刚起床，还没完全清醒"
        elif 9 <= hour < 12:
            time_feel = "上午时间，精神逐渐变好"
        elif 12 <= hour < 14:
            time_feel = "午后容易犯困"
        elif 14 <= hour < 18:
            time_feel = "下午时间"
        elif 18 <= hour < 22:
            time_feel = "晚上，比较放松的时间"
        else:
            time_feel = "深夜了，开始犯困"

        lines = [
            f"[你当前的精力状态] {energy}（疲劳度 {total:.0f}/100）",
            f"[时间感知] {time_feel}",
            f"[表达建议] {style_hint}",
            f"注意：疲劳感应该自然地融入你的回复风格中，而不是每句话都提到累。"
            f"就像真人一样——累了会简短回复、语气变软，但不会每句都说'我好累'。",
        ]
        return "\n".join(lines)

    # ==========================================
    # 事件钩子
    # ==========================================

    @on.im_message(priority=Priority.MEDIUM)
    async def on_message(self, event: KiraMessageEvent):
        """每条消息到达时更新疲劳状态"""
        sid = event.session.sid
        now = time.time()

        # 更新全局消息计数
        self._global_msg_timestamps.append(now)

        # 更新会话疲劳（先应用衰减再累加）
        _ = self._session_fatigue_value(sid)  # 触发衰减计算
        self._session_fatigue[sid] = self._session_fatigue.get(sid, 0.0) + self.session_fatigue_rate
        self._session_last_active[sid] = now

    @on.llm_request(priority=Priority.LOW)
    async def inject_fatigue_prompt(self, event: KiraMessageBatchEvent, req: LLMRequest):
        """在 LLM 请求前注入疲劳状态描述"""
        sid = event.session.sid
        total = self.get_fatigue(sid)
        breakdown = self.get_fatigue_breakdown(sid)

        description = self._fatigue_description(total, breakdown)
        logger.debug(
            f"[Fatigue] sid={sid} total={total:.1f} "
            f"(circadian={breakdown['circadian']}, "
            f"session={breakdown['session']}, "
            f"global={breakdown['global']})"
        )

        # 追加到 system prompt 末尾
        req.system_prompt.append(
            Prompt(
                description,
                name="fatigue_state",
                source="system",
            )
        )
