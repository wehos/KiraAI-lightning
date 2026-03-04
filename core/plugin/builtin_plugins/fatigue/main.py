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
        self.session_fatigue_rate = float(cfg.get("session_fatigue_rate", 5.0))
        self.session_fatigue_cap = float(cfg.get("session_fatigue_cap", 50))

        # ---------- 全局负荷配置 ----------
        self.global_window = float(cfg.get("global_window_minutes", 60)) * 60  # → 秒
        self.global_fatigue_per_msg = float(cfg.get("global_fatigue_per_msg", 0.8))
        self.global_fatigue_cap = float(cfg.get("global_fatigue_cap", 40))

        # ---------- 恢复 ----------
        self.recovery_rate = float(cfg.get("recovery_rate", 3.0))  # 每分钟

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
        """日内节律疲劳 (0-40)

        大幅强化：深夜凌晨直接给高疲劳基线，让 AI 在深夜真的很困。
        """
        if not self.enable_circadian:
            return 0.0

        hour = datetime.now().hour
        minute = datetime.now().minute
        t = hour + minute / 60.0  # 精确到分钟的小数时间

        # 在低谷时段（2-6点）：疲劳 30-40，非常困
        if self._hour_in_ranges(hour, self._low_ranges):
            return 32.0 + 8.0 * math.sin(math.pi * (t - 2) / 4)  # 峰值40

        # 在高峰时段：疲劳 5-8，清醒但不是零
        if self._hour_in_ranges(hour, self._peak_ranges):
            return 6.0

        # 深夜 22-2 点：疲劳 20-32，逐渐变困
        if hour >= 22 or hour < 2:
            if hour >= 22:
                progress = (t - 22) / 4  # 22→26(2am) 映射到 0→1
            else:
                progress = (t + 2) / 4
            return 20.0 + 12.0 * progress

        # 其他过渡时段：余弦曲线
        fatigue = 20.0 + 14.0 * math.cos(math.pi * (t - 4) / 12)
        return max(0.0, min(40.0, fatigue))

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
            style_hint = "回复可以活泼、热情。但依然保持简洁，单条消息不超过50字。"
            length_rule = "每条消息控制在50字以内。"
        elif total <= 30:
            energy = "状态不错，比较有精神"
            style_hint = "正常聊天。保持简洁自然。"
            length_rule = "每条消息控制在40字以内。"
        elif total <= 45:
            energy = "开始有点累了"
            style_hint = (
                "你现在有点疲倦。回复要明显变短，语气随意一些。"
                "可以偶尔说'有点累了''好困'。不重要的话题可以敷衍。"
            )
            length_rule = "【强制】每条消息不超过30字。只发1条消息，不要发第2条。"
        elif total <= 60:
            energy = "挺累的，不太想说话"
            style_hint = (
                "你现在很累，不想多说。回复尽量简短——几个字到十几个字就够了。"
                "语气慵懒，比如'嗯''哦''好的呀..''知道了~'。"
                "对不感兴趣的话题直接敷衍或不回。"
            )
            length_rule = "【强制】每条消息不超过20字。严格只发1条消息。"
        elif total <= 75:
            energy = "非常疲惫，快撑不住了"
            style_hint = (
                "你现在非常累，几乎不想说话。只用最短的方式回应。"
                "比如'嗯..''好~''哈哈''困..''emmm'这种级别的回复。"
                "或者直接发个颜文字/表情包代替文字。"
                "可以主动表达想休息了。"
            )
            length_rule = "【强制】每条消息不超过10字。严格只发1条消息。能用颜文字代替就不要打字。"
        else:
            energy = "困到不行，随时要睡着"
            style_hint = (
                "你现在困到极致，基本不想回复任何东西。"
                "只回'zzz''💤'、一个颜文字、或者'我要睡了..'。"
                "对大部分消息可以选择不回复（发送空<msg></msg>）。"
                "只有被@才勉强回一个字。"
            )
            length_rule = "【强制】每条消息不超过5字或一个颜文字。严格只发1条。大部分情况直接不回复。"

        # 时间感知
        if 0 <= hour < 6:
            time_feel = "现在是深夜/凌晨，你非常困，身体在强烈抗议还没睡"
        elif 6 <= hour < 9:
            time_feel = "刚起床，还迷迷糊糊的"
        elif 9 <= hour < 12:
            time_feel = "上午，精神还行"
        elif 12 <= hour < 14:
            time_feel = "午后犯困时间"
        elif 14 <= hour < 18:
            time_feel = "下午"
        elif 18 <= hour < 22:
            time_feel = "晚上，比较放松"
        elif 22 <= hour < 24:
            time_feel = "深夜了，该睡了"
        else:
            time_feel = "凌晨，你应该已经睡着了"

        lines = [
            f"## 你当前的精力与情绪状态（必须遵守）",
            f"疲劳度：{total:.0f}/100 — {energy}",
            f"时间感知：{time_feel}",
            f"",
            f"### 回复规则（本轮强制生效）",
            f"{length_rule}",
            f"{style_hint}",
            f"",
            f"### 注意事项",
            f"- 疲劳感要自然融入风格，不要每句都说'我好累'，但要通过回复长度和语气体现出来",
            f"- 累了就是回复短、语气软、不想多聊——这才是真实的疲劳表现",
            f"- 越累越不要发多条消息，1条就够了",
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
