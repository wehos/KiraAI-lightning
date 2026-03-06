# Global Memory 渐进式施工计划

> 最后更新：2026-03-04
> 状态：Phase 0 完成，Phase 1 待施工

---

## 背景

当前记忆系统在 entity 级别（user/group）运行良好，海马体管线（extract → dedup → store → elevate → profile）已稳定生产。但 `global/` 域几乎为空：

| 目录 | 设计用途 | 当前状态 |
|------|----------|----------|
| `global/facts/` | 跨用户的世界知识 | ❌ 无写入管线 |
| `global/self/facts/` | AI 自我觉察（Tier 1） | ❌ `record_self_awareness()` 无调用方 |
| `global/self/reflections/` | AI 行为模式（Tier 2） | ❌ 依赖 Tier 1 输入，连锁空置 |
| `global/skills/` | AI 能力记录 | ❌ 预留，未设计 |

persona_evolution 的三级漏斗（Tier 1 → Tier 2 → Tier 3 persona.txt）架构完整，但入口 `record_self_awareness()` 没有任何代码调用它，导致整条管线断路。

## 施工原则

1. **渐进式**：每阶段只做一件事，观察稳定后再推进下一阶段
2. **只存不读**：新管线先 shadow-write，不影响现有召回逻辑
3. **可回滚**：每阶段都可通过配置开关关闭，不需要改代码
4. **人工核验**：每阶段部署后人工检查 global/ 目录下的 JSON 质量

---

## Phase 0：架构审计（✅ 已完成）

- [x] 梳理 memory_paths.py 目录结构
- [x] 确认 _hippocampus_process 完整流程
- [x] 确认 persona_evolution 三级漏斗已就绪但无数据来源
- [x] 确认 lifecycle.py 的 evolution loop 在运行（7天间隔）
- [x] 确认 `record_self_awareness()` 无任何调用方
- [x] 确认 memory_decay 对 global/self/ 的衰减逻辑存在
- [x] 编写本施工计划

---

## Phase 1：自我觉察采集（只存不读）

**目标**：在海马体管线末尾增加"自我反思"步骤，让 AI 从每次对话中提取关于自身行为的觉察，写入 `global/self/facts/`。

**只存不读**：写入的数据不会被召回到 LLM 上下文，也不会影响回复质量。纯后台积累。

### 施工内容（✅ 已完成 2026-03-04）

1. ✅ **memory_extractor.py** 新增 `extract_self_awareness(conversation_text, ai_response_text) → list[str]`
   - 输入：本轮对话文本（ai_response_text 可选）
   - 输出：0-2 条自我觉察（大部分对话不会产出任何觉察）
   - Prompt 要求以"我"开头，过滤长度 5-200 字
   - 输出 NONE 或空 → 返回空列表

2. ✅ **memory_manager.py** 新增 `_collect_self_awareness()` + `set_persona_evolution()`
   - `_hippocampus_process` 步骤 7 调用 `_collect_self_awareness()`
   - 静默失败（不影响主流程）
   - 写入路径：`persona_evolution.record_self_awareness(content, importance=3, tags=["auto-extracted", "phase1"])`

3. ✅ **lifecycle.py** 在初始化 PersonaEvolutionEngine 后注入到 memory_manager
   - `self.memory_manager.set_persona_evolution(self.persona_evolution)`

4. ✅ **默认开启**：只要 persona_evolution 被注入就自动启用，无需手动开关

### 验收标准

- [ ] 开启开关后，正常对话能在 `global/self/facts/` 下产生 JSON 文件
- [ ] 产出频率合理（不是每次对话都产出，大约 30-50% 的对话会产出 0-1 条）
- [ ] 觉察内容有意义，不是对话内容的复述
- [ ] 关闭开关后完全无新增文件
- [ ] 不影响现有 entity 记忆的正常工作

### 人工核验要点

部署后 3-7 天，人工检查 `data/memory/global/self/facts/` 下的文件：
- 内容是否有意义？是否真的是"自我觉察"而不是"对话摘要"？
- 数量是否合理？（预期：每天 5-20 条，取决于对话量）
- importance 分布是否合理？（应该集中在 2-4）

---

## Phase 2：Tier 1 → Tier 2 升维验证

**前置条件**：Phase 1 运行 2+ 周，global/self/facts/ 积累 50+ 条觉察，人工核验质量合格。

**目标**：验证 `run_evolution_cycle()` 的 Tier 1 → Tier 2 升维逻辑是否正常工作。

### 施工内容

1. **调整 evolution 周期**：lifecycle.py 中 `_persona_evolution_loop` 从 7 天改为 3 天（加速验证）
2. **增加日志**：在 `run_evolution_cycle()` 中增加详细日志，记录：
   - 扫描到多少 Tier 1 facts
   - LLM 提炼出哪些 Tier 2 reflections
   - 哪些 Tier 1 facts 被吸收删除
3. **人工触发**：提供一个管理员工具（或 CLI 命令）可以手动触发 `run_evolution_cycle()`

### 验收标准

- [ ] Tier 1 facts 达到阈值（5+）后，自动触发升维
- [ ] 升维产出的 reflections 质量合理（行为模式总结，不是简单复述）
- [ ] 被吸收的 Tier 1 facts 正确删除
- [ ] `global/self/reflections/` 下出现 JSON 文件

### 人工核验要点

- reflections 内容是否准确概括了多条 facts 的共性？
- 是否有信息丢失（重要的 facts 被过早吸收）？
- importance 分布是否合理？（Tier 2 应该 7-10）

---

## Phase 3：接入召回（读取 global/self）

**前置条件**：Phase 2 验证通过，Tier 2 reflections 质量稳定。

**目标**：将 `global/self/reflections/` 中的内容接入 LLM 上下文，让 AI 的自我认知影响回复。

### 施工内容

1. **prompt_manager.py** 增加 global self-awareness 召回：
   - 在构建 system prompt 时，从 `global/self/reflections/` 召回 top-K 条 reflections
   - 注入到 persona 区域附近（"你对自己的认知"）
   - 限制 token 数量（最多 200 tokens）

2. **召回策略**：
   - 按 importance × access_count 排序
   - 只召回 Tier 2 reflections（不召回 Tier 1 facts，太碎片化）
   - 每次召回后更新 access_count

### 验收标准

- [ ] AI 的回复风格能体现出自我认知的影响
- [ ] 不会出现"我的 reflection 说..."这种元认知泄漏
- [ ] Token 开销可控（< 200 tokens/request）

---

## Phase 4：Tier 3 人设跃迁

**前置条件**：Phase 3 运行 1+ 月，确认 self-awareness 对回复质量有正面影响。

**目标**：启用 Tier 2 → Tier 3 跃迁，将稳定的行为模式不可逆合入 persona.txt。

### 施工内容

1. **check_persona_leap()** 已实现，只需确认触发条件合理
2. **增加人工审批机制**：跃迁前先写入 pending 队列，管理员确认后才执行
3. **备份机制**：跃迁前自动备份 persona.txt

### 风险控制

- Tier 3 是**不可逆**操作，必须有人工审批
- 初期设置极高阈值（access_count ≥ 20, importance ≥ 9, 存活 60 天）
- 每次跃迁间隔至少 14 天

---

## Phase 5：跨实体知识（global/facts）

**前置条件**：Phase 1-4 全部稳定运行。

**目标**：从对话中提取跨用户的通用世界知识，写入 `global/facts/`。

### 设计方向（待细化）

- 什么算"世界知识"？——不属于任何特定用户、但对 AI 有用的信息
- 示例：「b站直播的弹幕礼仪」「这个群的梗和暗号」「游戏术语解释」
- 如何避免与 group facts 重复？——group facts 记录群组特征，global facts 记录可迁移知识
- 召回时机：所有对话都可以召回 global facts（不限于特定 entity）

---

## Phase 6：能力记忆（global/skills）

**最远期目标，暂不展开。**

预期方向：AI 记录自己学会的技能（如"我能帮用户查游戏攻略"），用于能力自描述和任务路由。

---

## 进度追踪

| Phase | 状态 | 开始日期 | 完成日期 | 备注 |
|-------|------|----------|----------|------|
| 0 - 架构审计 | ✅ 完成 | 2026-03-04 | 2026-03-04 | 本文档 |
| 1 - 自我觉察采集 | 🚧 已部署待验证 | 2026-03-04 | - | 代码已就位，默认开启，待人工核验 |
| 2 - 升维验证 | ⏳ 待 Phase 1 | - | - | |
| 3 - 接入召回 | ⏳ 待 Phase 2 | - | - | |
| 4 - 人设跃迁 | ⏳ 待 Phase 3 | - | - | 需要人工审批机制 |
| 5 - 跨实体知识 | 💭 设计中 | - | - | |
| 6 - 能力记忆 | 💭 远期 | - | - | |

---

## 附录：当前数据流

```
Chat Messages
    ↓
chat_memory.json (sliding window, 3 chunks)
    ↓
_hippocampus_process()
    ├─→ extract_personal_facts (LLM) ──→ route → dedup → store → entities/user_xxx/facts/
    ├─→ extract_group_facts (LLM) ────→ store → entities/group_xxx/facts/
    ├─→ check_elevation_trigger ──────→ generate_reflections → entities/xxx/reflections/
    └─→ update_entity_profile ────────→ entities/user_xxx/profile.json
        ↓
    [Phase 1 将在此处追加]
    └─→ extract_self_awareness (LLM) → persona_evolution.record_self_awareness()
                                         → global/self/facts/  (Tier 1)
        ↓
    Every 3-7 days (lifecycle loop):
    run_evolution_cycle()
    ├─→ Tier 1 (5+ facts) → LLM 提炼 → Tier 2 (global/self/reflections/)
    └─→ Tier 2 → check_persona_leap() → Tier 3 (persona.txt, 不可逆)

recall(query, entity) → FTS5 + [vector] → top-K memories for LLM context
    [Phase 3 将增加 global/self/reflections/ 召回]
```
