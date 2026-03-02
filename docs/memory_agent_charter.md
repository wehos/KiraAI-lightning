# Memory Agent 原则与目录结构宪章 (Draft)

## 1. 核心思想
Memory Agent 旨在通过 **TOML 文件目录树 + SQLite 索引**，来管理和沉淀用户的行为、偏好、长期事实及系统技能。记忆应该像一座整洁、具备**呼吸感**的图书馆：
- **扁平克制**：既不把所有知识堆在一个文件里，也不会建立深不见底的文件夹层级（最大深度建议不超过五级）。
- **语义明确**：目录和文件名本身需要具备自解释性。
- **可迭代性**：每一条记忆/技能应当能被不断改写（Merge/Update）或在失去相关性时被遗忘（Archive/Delete）。

## 2. 目录结构规范
整个架构以实体（User/Group/Channel等）或全局空间（Global）为一级分类，以记忆类型为二级分类。

**双层存储架构**：
- **TOML 文件**：人类可读可编辑的内容真相源（语义 ID 作为文件名）
- **SQLite 索引**（`memory_index.db`）：运行时 meta（access_count、last_accessed 等）+ FTS5 全文检索 + 可选向量索引

```text
data/memory/
├── memory_index.db             # SQLite 持久化索引（可从文件重建）
├── global/                     # 全局记忆与技能 (跨用户共享)
│   ├── skills/                 # **完全兼容 Claude Skills 规范的技能包**
│   │   └── code_review/        # 技能包目录
│   │       ├── SKILL.md        # 技能的主指令文件 (包含 YAML Frontmatter)
│   │       └── scripts/        # 技能的附属执行脚本
│   ├── facts/                  # 系统级世界常识、全局设定的状态
│   └── self/                   # 自我认知与系统人设 (Agent 自身的行为边界、核心信念与演化属性)
│       ├── facts/              # Tier 1 瞬时自觉
│       └── reflections/        # Tier 2 自我反思
├── entities/                   # 实体边界 (包含用户、群组、频道等)
│   ├── user_{id}/              # 用户专有域
│   │   ├── profile.json        # 绝对核心画像（结构化的字典：姓名、主要特征等）
│   │   ├── facts/              # 陈述性记忆（细粒度的客观事实）
│   │   │   └── hates_css.toml  # 文件名 = 语义 ID（snake_case slug）
│   │   └── reflections/        # 反思与总结（由海马体进程合并事实生成的高级洞察）
│   │       └── tech_stack.toml # 语义 ID 使目录对人类更具可读性
│   ├── group_{id}/             # 群组/多人群聊专有域 (记载群画像、群体规约与集体记忆)
│   └── channel_{id}/           # 频道专有域 (Discord/Slack 等特定业务频道的公共语境)
├── archive/                    # 归档记忆（衰减后移入，保留完整 meta 方便恢复）
```

> **注意**：旧版 `episodic/` 目录已移除。情景类记忆通过 `facts/` + 带时序标签（如 `tags = ["event", "2026-03"]`）的方式表达，避免类型膨胀。

## 3. 文件格式规范 (TOML Schema)

### 3.1 设计原则
记忆文件采用 **TOML** 格式，取代原先的 JSON。理由：
- **人类可编辑**：支持注释、无尾逗号问题、多行字符串天然支持
- **扁平结构**：所有字段顶层平铺，无 `content.raw_text` 嵌套
- **运行时 meta 分离**：`access_count`、`last_accessed` 等运行时状态存储在 SQLite 索引中，不污染文件

### 3.2 核心 Schema 定义 (TOML 文件)
```toml
# 用户对 CSS 的态度
id = "hates_css"
type = "fact"
text = "用户讨厌写 CSS，觉得前端很烦"
importance = 6
tags = ["frontend", "preference"]

[source]
session = "telegram:pm:12345"
time = 2026-03-01T14:30:00+08:00
```

### 3.3 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | **语义化 slug**（snake_case），同时作为文件名。如 `hates_css`、`pet_cat_xiaoju`。由 LLM 生成，回退为 `前缀_hash` |
| `type` | string | 只有两种：`fact`（客观事实）和 `reflection`（升维洞察）|
| `text` | string | 记忆正文，扁平顶层，直接可读 |
| `importance` | int (1-10) | 用户可手动编辑的重要性评分 |
| `tags` | array[string] | 跨实体的过滤维度，如 `["frontend", "preference"]` |
| `[source]` | table (可选) | 来源信息：`session`（会话 ID）、`time`（ISO 时间戳）|

### 3.4 SQLite 索引中的运行时 Meta
以下字段**不写入 TOML 文件**，由 `MemoryIndex`（SQLite）统一管理：
- `timestamp`：创建时间（Unix float）
- `last_accessed`：最近访问时间
- `access_count`：被实质引用的次数
- `content_hash`：SHA-256 内容指纹（快速去重）
- `entity_id` / `entity_type` / `folder`：定位信息
- `file_path`：对应 TOML 文件路径（灾难恢复时重建索引）

### 3.5 关键约束
1. **扁平优先**：`text` 为顶层字段，不再嵌套在 `content.raw_text` 中。结构化查询通过 SQLite 索引完成。
2. **TOML 合法性**：所有 `.toml` 文件必须是合法 TOML。Python 3.11+ 用内置 `tomllib` 读取，3.10 回退 `tomli`；写入统一使用 `tomli_w`。
3. **索引可重建**：SQLite 索引是文件的加速层，可随时从 TOML 文件重建（`rebuild_index_from_files`）。兼容旧 JSON 格式的迁移扫描。

## 4. Agent 行为守则 (Agent Directive)
当 Agent 承担“海马体”或“管理员”角色运行记忆整理流程时，必须坚守以下铁律：

1. **唯一性审查（Deduplication）**：
   在写入前，必须通过工具排查是否已存在相似或同义的旧记忆。若存在，你的首选策略是**提取并更新（Merge / Append）旧文件**，绝不能简单地抛弃旧文或无脑创建新文件造成冗余。
2. **信息升维（Reflection）**：
   当 `facts/` 目录下的某类底层碎片积累达到阈值，你需要主动启动反思流程，将它们**提炼升维**成更全面的分析报告放入 `reflections/`，并赋予高度 `importance`。随后必须**删除或归档**那些被吸收掉的初级事实。
3. **优先级分层**：
   - 当前对话直接产生的即时偏好 → 写入 `facts/`（type = "fact"）
   - 多条事实升维的高层洞察 → 写入 `reflections/`（type = "reflection"）
   - 对用户性格、人设产生决定性颠覆的新认知 → 更新 `profile.json`
   - 与工作流、命令执行格式相关的普遍抽象 → 提炼升格写入 `skills/`
4. **动态遗忘与重要性衰减 (Decay & Forgetting)**：
   记忆是会呼吸的。`importance` 绝非一成不变的护身符：
   - **自动衰减**：伴随时间流逝，如果记忆未能被检索命中（即 `last_accessed` 久远），后台维护脚本会自动按衰减曲线扣减其 `importance` 分值。
   - **主动降级**：当 Agent 在反思或比对时发现某些旧事实已经被新事实证实失效（比如用户彻底换了技术栈），Agent 应当主动**下调**该旧记忆的 `importance`。
   - **垃圾回收**：当一条记忆的 `importance` 衰减降至 `<= 3` 且长期未被访问时，Agent 必须在例行维护触发时将其从主体库中剔除（物理删除或移入 `archive/`），保持库的高频纯净度与检索帧率。

## 5. 高级特性：原生兼容 Claude Skills
为了让 Memory Agent 拥有生产级的工作流能力，`skills/` 目录将直接兼容并挂载原生 **Claude Skills**。在这套混合架构中（记忆存 `TOML` + `SQLite`，技能装 `Claude Skill 包`），我们引入以下三大进阶机制：

1. **渐进式加载（Progressive Disclosure）**
   - 检索时，绝不立即把体积庞大的 `text` 字段或大篇幅的 `SKILL.md` 塞入上下文。系统仅从 SQLite 索引提取 meta 层，以及 `SKILL.md` 头部的 YAML Frontmatter 注入 Prompt。
   - 当模型判定需要更详细信息时，再通过特定工具接口动态拉取对应 TOML 文件的完整内容，极致节省 Token 并保持注意力焦点。

2. **可执行的动作层（Executable Scripts）**
   - 记忆不再仅仅是”被动查阅”的文本。`skills/` 目录下的挂载包可以包含 `scripts/` 子文件夹。
   - 只要大模型认为情境匹配，即可直接调用环境工具跑通这些 Bash/Python 脚本，实现”能行动的记忆”。

3. **三级作用域与优先级覆盖（Hierarchy Override）**
   - 检索聚合时遵循严格的就近覆盖法则：`User/Group/Channel 实体技能` > `Global 技能`。
   - 如果用户专属目录下存在与全局同名的记忆或技能（例如 `entities/user_A/skills/code_review/` vs `global/skills/code_review/`），系统必须屏蔽更宽泛的全局记忆，强制继承并应用特写实体的定制化干预。

## 6. 自我认知与 Persona 演进路径 (Identity Evolution)
Agent 的核心人设（`persona`）是最应当保持稳定的基石。为了防止 Agent 被短期的聊天轻易“精神分裂”或“洗脑”，我们规定一套严格的**三级漏斗演进机制**，连接 `global/self/` 与底层的 `persona_manager.py` (`persona.txt`)：

1. **第一级：瞬时自觉 (`global/self/facts/`)**
   - 当在对话中 Agent 意识到自身表现有待微调（例如：“我刚刚的代码回答太啰嗦了，下次要精简”），它只会生成一条普通 `fact` 存入 `global/self/facts/`。
   - 这类记忆 `importance` 较低，会在日常对话检索中影响 Agent，但极易随时间衰减殆尽。
2. **第二级：刻意练习与反思 (`global/self/reflections/`)**
   - 当后台海马体进程发现关于“回答要精简”的 `facts` 大量沉淀时，它会触发反思，将其提炼升维为一条 `reflection`（例如：“系统倾向于简练专业的对话风格”）。
   - 反思级自我的 `importance` 极高，且不易衰减（相当于形成了习惯）。
3. **第三级：核心人设跃迁 (The Persona Leap)**
   - 只有当一条针对自身的 `reflection` 存活了极长的时间，或者其累积的 `access_count` 突破了系统级阈值时，Agent 才会触发一次“顿悟”（Identity Evaluation）。
   - 此时，脚本会提取这条反思，**不可逆地改写并合并入** `persona_manager.py` 所指向的核心基座库（目前为 `data/persona.txt`，未来可升级为绝对权重最高的 `global/self/profile.json`）。
   - 一旦合入 Persona，对应的 Reflection 碎片将被彻底销毁。此时，这个习惯已经成为了 Agent 基因的一部分，**极难被再次改变**。

## 7. 记忆提取与下发链路 (Memory Extraction Pipeline)
光有存放是不够的。提取不仅需要精准，更要在高并发（如千人群聊）场景下保持极低的开销屏障。提取过程分为以下漏斗：

1. **时间感知与回复意愿研判 (Temporal Awareness & Reply Intent)**
   - **聚合感知**：无论是私聊还是高频群聊，系统都不再孤立地对“单条弹出的消息”进行反射性提取。相反，所有即时消息会先滑入一个临时的上下文时间窗口（Time Window buffer），进行**合并分析**。
   - **内容时效与延迟惩罚 (Time-Awareness)**：系统必须对时间流逝保持极高的敏感度。如果某条原本需要回复的消息因为系统调度队列或其他原因被积压（例如已过去 60 秒），由于语境可能已经发生翻篇，Agent 的回复意愿应受到**大幅折扣（Delay Penalty）**。对于明显具有时效性约束的询问，过期的讨论将直接放弃下发。
   - **回复意愿 (Reply Intent)**：配置一个极其轻量的本地路由 Agent（或快速判别模型）。基于这段合并上下文和时间延迟惩罚，研判 Agent 当前是否依然具有强烈的“即时回复必要”（被明确艾特、语境恰好指向自身职能、或用户在等待响应）。
   - **按需组装 Query**：只有在敲定“该我发言了”的回复意愿后，轻量节点才会从这整个合并后的窗口上下文中，精准归纳出需要查询的**搜索关键词 (Query)** 和**相关实体 (Entities)**，触发后续的混合检索，从而彻底抹平因为碎嘴或调度延迟引发的性能雪崩与诡异回复。
2. **混合检索引擎 (Hybrid Retrieval)**
   - 提取系统不会傻傻地把几千个 TOML 文件灌给大模型，而是通过 `TomlTreeStore` 委托 `MemoryIndex`（SQLite）进行极速查询。
   - 查找算法结合了三维打分：**语义相关度 (FTS5 全文检索 + 可选向量混合)** + **先验重要性 (`importance`)** + **时间衰减法则 (根据 `last_accessed` 扣除衰老分)**。
   - **两级去重**：Level 1 = SHA-256 内容哈希精确匹配（零 LLM 调用）；Level 2 = FTS5 语义搜索 + LLM 判断（duplicate/update/new）。
3. **渐进式注入 (Progressive Prompt Injection)**
   - 对于 `skills/` 等大型技能包，系统优先将检索得分 Top N 的技能文件夹内的 `SKILL.md` 的**高度浓缩版摘要/元数据**放入系统级提示词，并告诉模型：”你需要时可以通过工具查阅详细指南”。
   - 对于普通 `facts/` 或 `reflections/`，如果打分极高，则将其 `text` 字段实体化拼接入**近期记忆增强槽 (Memory Augmented Slot)** 随用户消息一并发送。
4. **提取后维护：何谓”真正采纳” (Access Count Mutation)**
   - 并非所有被上一环节无脑捞出来的记忆都可以 +1。
   - **工具/链路确认**：对于按需加载的技能，只有大模型切实调用了该工具去查阅，才算采纳；对于直接注入槽位的事实，如果最终大模型的回答/反思记录里**实质性地引用或受到该事实影响**（可以通过事后的批处理审计，或者在回复中附带 `used_memories_id` 来要求模型自陈），该记忆在 SQLite 索引中的 `access_count` 才予以 +1 且刷新 `last_accessed`。
   - 这一规则从根本上杜绝了因为模型乱召回或低质匹配导致的”垃圾记忆得分膨胀”。
