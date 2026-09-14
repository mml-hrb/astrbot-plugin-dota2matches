# astrbot_plugin_dota2 · Dota2 数据查询助手

基于 [OpenDota API](https://docs.opendota.com/) 的 AstrBot 插件，提供 Dota2 战绩查询与 AI 复盘分析能力。

## 功能

| # | 能力 | 指令 |
|---|------|------|
| 1 | 绑定玩家（昵称 / 32 位 account_id / 64 位 SteamID） | `/d2 绑定 <目标>`、`/d2 解绑` |
| 2 | 按玩家查询战绩 | `/d2 战绩 [场次] [目标]` |
| 3 | AI 分析近期表现与打法风格（默认最近 20 场，场次可调） | `/d2 分析 [场次] [目标]` |
| 4 | AI 深度复盘单场比赛（走势 / 质量评估 / 十人点评） | `/d2 单场 <比赛ID> [目标]` |
| 5 | 监听玩家，比赛结束且详细数据就绪后自动推送分析，支持解绑 | `/d2 监听`、`/d2 取消监听`、`/d2 监听列表` |
| 6 | **自然语言识别**：不用记指令，直接说人话就能触发 | 见下方「自然语言」 |
| 7 | **催解析并等待**：单场复盘遇到未解析的局，自动催解析、每分钟检查一次、最多等 10 分钟 | `/d2 单场 <比赛ID>`、`/d2 催解析 <比赛ID>` |

辅助指令：`/d2 帮助`、`/d2 我的`、`/d2 绑定列表`、`/d2 资料 [目标]`、`/d2 英雄 [目标]`。

## 自然语言（免指令）

不用背指令，直接说人话即可触发对应功能：

```
帮我看看我的战绩                     → 战绩查询
分析一下天鸽最近的发挥                 → AI 近期分析
这局 8993438099 复盘一下              → 单场深度复盘（未解析会自动催解析并等待）
催一下 8993438099 的解析              → 只催 OpenDota 解析
最近 20 把打得怎么样                  → 按 20 场分析
天鸽的英雄池 / 我段位多少              → 英雄池 / 玩家资料
把天鸽加入监听                        → 添加监听
取消监听天鸽 / 取消全部监听            → 取消监听
监听列表                             → 查看本会话监听
```

设计要点：

- **识别不出来就不出声**：闲聊（「在吗」「中午吃什么」）不会被当成数据查询，
  消息会原样交给 AstrBot 默认大模型正常回复，插件不会抢答；
- **群聊默认需要 @ 机器人**：避免群里别人随口一句话就触发查询、白耗 OpenDota 配额，
  可在配置中关闭（`nlu_group_require_at`）；
- **敏感操作先确认**：「绑定 / 解绑 / 添加监听」在自然语言触发时会先回一句确认，
  你回复「确认」才真正执行，「取消」则不执行（`nlu_confirm_sensitive`）；
- **不依赖大模型**：规则引擎免费且即时；如需更强的口语理解，可开启
  `nlu_llm_fallback`，规则没把握时额外调用一次模型做意图分类。

自然语言入口与指令入口走的是**同一套实现**，因此行为、权限、输出格式完全一致。

## 安装

1. 把 `astrbot_plugin_dota2` 目录放到 `AstrBot/data/plugins/` 下；
2. 在 AstrBot WebUI 的「插件管理」中重载插件；
3. 打开插件配置，按需填写（可选）OpenDota API Key 与 HTTP 代理。

依赖：`httpx`（AstrBot 本体已内置）。

## 指令详解

### 绑定

```
/d2 绑定 天鸽                  # 昵称（走 OpenDota 搜索）
/d2 绑定 86745912              # 32 位 account_id
/d2 绑定 76561198047011640      # 64 位 SteamID
/d2 绑定 https://steamcommunity.com/profiles/76561198047011640
```

绑定按「会话 + 用户」维度保存，群聊里每个人可以各自绑定自己的账号。
昵称搜索命中多个同名玩家时，插件会列出候选并提示改用账号 ID。

`/d2 解绑` 会同时清理该账号在当前会话下的监听。

### 查询

```
/d2 战绩                       # 自己的最近 20 场
/d2 战绩 50                    # 自己的最近 50 场
/d2 战绩 20 天鸽                # 指定玩家的最近 20 场
/d2 资料 86745912               # 玩家资料、段位、生涯胜率、常用英雄
/d2 英雄                        # 英雄使用统计
```

### AI 分析（近期表现）

```
/d2 分析                        # 默认最近 20 场
/d2 分析 40                     # 最近 40 场
/d2 分析 20 天鸽                # 指定玩家
```

生成的报告包含：一句话结论 → 近期状态走势 → 打法风格画像 → 数据亮点与短板 → 可执行的改进建议。

数据侧会聚合：胜率与最近 10 场胜率、连胜连败、前后半段胜率对比（判断状态趋势）、
场均 K/D/A 与 KDA、GPM/XPM、补刀/反补、英雄/建筑伤害与治疗、分路分布、游走局数、
英雄池胜率、单排/组队比例，并附上完整的逐场明细表。

### AI 复盘（单场比赛）

```
/d2 单场 8989601141
/d2 单场 8989601141 天鸽        # 指定重点点评的玩家
```

报告包含：比赛走势复盘 → 比赛质量评估 → 焦点玩家点评 → 其余选手点评 → 关键转折点。

喂给模型的原始数据包括：

- 对局基础信息（时长、模式、比分、一血、买活、region/cluster/patch、BP 顺序）
- 逐分钟经济差与经验差采样、最大领先时间点、经济领先易手次数、翻盘深度（胜方曾落后的最大幅度）
- 关键事件时间轴（推塔、肉山、不朽盾、信使、买活）
- 团战列表（起止时间、阵亡人数、双方经济净变化）
- 十人完整数据（等级、KDA、净经济、GPM/XPM、补刀、伤害、参团率、控制、真假眼、出装）
- 焦点玩家的深入数据：补刀效率、击杀记录、买活时间、关键装备时间线、假眼/真眼/堆野/吃符
- 焦点玩家近 10 场的整体状态（用于判断本场是正常发挥还是异常）

### 催解析 + 等待解析（单场复盘）

**OpenDota 收录一场比赛 ≠ 解析完这场比赛的录像。** 只有解析完成才有逐分钟经济、
团战、出装这些数据，而 AI 复盘的价值几乎全在这里。未解析的局只有 KDA、时长、
英雄这类基础字段。

`/d2 单场 <比赛ID>` 在遇到未解析的局时会自动走「催 + 等」：

```
/d2 单场 8995419388          # 未解析 → 自动催解析并等待（每分钟检查，最多 10 分钟）
/d2 单场 8995419388 skip     # 不等，直接用基础数据出报告
/d2 催解析 8995419388         # 只催 OpenDota 解析，不等结果
```

完整流程：

1. 拉取比赛数据，用 `od_data.has_parsed`（或玩家级 `gold_t` 是否填充）判断是否已解析；
2. 未解析时立即向 OpenDota 提交解析申请（`POST /request/{match_id}`，按 10 倍额度计费），
   并回一条说明消息告诉你「已排队、每分钟检查一次、最多等 10 分钟」；
3. **等待在后台任务里进行**，不占用对话——你可以继续发别的指令；
4. 之后每 `parse_check_interval`（默认 60 秒）检查一次，每次检查都播报当前状态
   （已等待多久 / 还剩多久 / 卡在哪一步）；期间大约每 5 分钟补交一次解析申请
   （OpenDota 的解析任务偶有丢单）；
5. 解析完成后，**自动把完整复盘报告推送到发起指令的会话**；
6. 超过 `parse_wait_timeout`（默认 10 分钟）仍未解析：发一条明确的通知并放弃这一场，
   **不会影响其他任务**，并提示你可以稍后重试或用 `skip` 看基础数据版。

为什么等待要放到后台：AstrBot 的流水线是「洋葱模型」——handler 每 `yield` 一条结果，
后续阶段（含默认大模型）就会整体执行一次。如果 handler 直接挂在那里等十分钟、
逐分钟 `yield` 进度，等于给流水线制造十次执行机会，默认大模型就会插嘴。
后台任务用 `context.send_message` 主动推送则完全没有这个问题。

### 监听

```
/d2 监听                        # 监听自己绑定的账号，推送到当前会话
/d2 监听 86745912               # 监听指定账号
/d2 监听列表                     # 查看本会话的监听与等待中的分析
/d2 取消监听 86745912
/d2 取消监听 全部                # 取消「你自己添加」的全部监听
```

`取消监听` 只能操作自己添加的监听；`admin_users` 中列出的用户（以及 AstrBot 自带
管理员）可以取消本会话内他人的监听。单会话监听数量受 `watch_max_per_session` 限制。

工作流程：

1. 添加监听时记录当前最新比赛作为**基线**，只有之后的新比赛才会推送（不会补推历史）；
2. 后台按 `watch_interval` 轮询被监听玩家的比赛列表；
3. 发现新比赛后，若还没有逐分钟级别的解析数据，会主动向 OpenDota 提交解析任务
   （`POST /request/{match_id}`），并重试直到详细数据就绪，**等数据齐了再生成分析**；
4. **一局只生成一次分析**并推送到所有相关会话（QQ 群聊会 `@` 添加监听的人）；
   若这一局有多位被监听的玩家在打，报告会为每位焦点玩家分别深入点评；
5. 超过 `watch_max_parse_attempts` 次仍未拿到解析数据时：
   - `watch_fallback_unparsed = true`（默认）：用基础数据照常推送，并在报告中注明数据不完整；
   - `watch_fallback_unparsed = false`：放弃该场比赛，只记录日志。

投递语义与几个容易踩的点：

- **至少一次投递**：`last_match_id` 只在推送成功之后才落盘。重启、崩溃或推送失败
  都不会让比赛永久丢失，下一轮会重新发现并补推；推送失败的会话保持原基线。
- **一局只分析一次**：同一场比赛若有多位被监听的玩家参与（群里几个人各自关注不同的人，
  恰好撞上同一局时很常见），只生成**一份**报告——报告里为每位焦点玩家各给一段
  「深入数据」并逐一单独点评，同一个群聊只收到一条推送，该群里的多位添加者会被一并 `@`。
  比赛详情与解析申请同样只做一次，不会重复消耗配额。
- **两个独立的等待计数**：比赛刚打完时可能还没被 OpenDota 收录，这段等待由
  `watch_max_wait_attempts` 单独计数，不占用解析次数，因此比赛稍晚被收录后
  依然会正常提交解析任务。
- **重试节拍**：等待解析的重试有独立的 30 秒节拍（不是 `watch_interval`），
  `next_try_at` 会被按时兑现。
- **权限**：`取消监听` 只能取消自己添加的监听；`admin_users` 中列出的用户
  （以及 AstrBot 自带管理员）可以取消本会话内他人的监听。单会话监听数量受
  `watch_max_per_session` 限制。
- **平台限制**：比赛推送依赖「机器人主动发消息」的能力。QQ 官方机器人等通道
  不支持主动消息，添加监听时会直接给出警告。

数据会落盘在 `AstrBot/data/plugin_data/astrbot_plugin_dota2/bindings.json`，
重启后监听关系依然有效。

### 模型自检

```
/d2 模型测试                     # 自检专用 API Key 通道是否可用
```

用一次极短请求探通模型通道，回显接口地址、模型名、耗时；失败时给出具体错误
（HTTP 状态码 / 连接情况）和对应的排查建议。没配专用 Key 时会说明当前走的是
AstrBot 提供商，并实测一次该通道。

## 用你自己的模型跑报告（可选）

默认情况下，AI 报告用的是 AstrBot 里配置的模型。如果你想让 **Dota2 插件的报告单独走一个通道**
（例如换成更便宜的 `deepseek-chat`，而机器人闲聊仍用原来的模型），在插件配置里填
「插件专用 API Key」即可。

三步搞定：

1. **填 Key**：`llm_api_key` = 你的 API Key；
2. **填地址**：`llm_base_url` 可以直接填服务商名（会自动补全），也可以填完整 base_url：

   | 服务商名 | 实际地址 |
   |----------|----------|
   | `deepseek` | `https://api.deepseek.com/v1` |
   | `kimi` / `moonshot` | `https://api.moonshot.cn/v1` |
   | `qwen` / `dashscope` | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
   | `zhipu` / `glm` | `https://open.bigmodel.cn/api/paas/v4` |
   | `siliconflow` | `https://api.siliconflow.cn/v1` |
   | `openrouter` | `https://openrouter.ai/api/v1` |
   | `ollama` | `http://127.0.0.1:11434/v1` |

   中转站 / 自建网关直接填完整地址（到 `/v1` 为止，不要带 `/chat/completions`）。
3. **填模型名**：`llm_model`，例如 `deepseek-chat`、`gpt-4o-mini`、`qwen-plus`。

填完在群里发 **`/d2 模型测试`** 自检一次，会回显接口地址、模型名、耗时，失败时给出具体错误和排查建议。

行为约定：

- 只对「分析 / 单场 / 监听推送」的报告生效；自然语言的意图识别仍用 AstrBot 默认模型；
- 配了专用 Key 但调用失败时，默认自动回退到 AstrBot 的模型重试一次（可用
  `llm_fallback_on_error` 关闭）；
- 不填专用 Key 时行为与以前完全一致，不影响现有部署；
- 只依赖 Python 标准库实现，不新增第三方依赖。

## 配置项

在 AstrBot 管理面板的插件配置页可视化修改。

| 配置 | 默认值 | 说明 |
|------|--------|------|
| `opendota_api_key` | 空 | 可选。匿名额度约 60 次/分钟、2000 次/天；填 Key 后可达 1200 次/分钟 |
| `http_proxy` | 空 | 国内服务器建议配置，例如 `http://127.0.0.1:7890` |
| `request_timeout` | 30 | 普通接口超时（秒）；单场详情接口自动用 3 倍 |
| `max_retries` | 3 | 网络错误 / 429 / 5xx 的重试次数 |
| `rate_limit_per_minute` | 55 | 本地限流，保护 OpenDota 配额 |
| `default_match_count` | 20 | 默认查询/分析场次 |
| `max_match_count` | 50 | 允许的最大场次 |
| `enable_llm_analysis` | true | 关闭后只输出整理好的原始数据 |
| `llm_provider_id` | 空 | 指定用于分析的模型，留空用会话默认模型。配了专用 API Key 后，本项仅作回退 |
| `llm_api_key` | 空 | 插件专用 API Key（OpenAI 兼容）。填了就优先走自己的通道，留空回退 AstrBot |
| `llm_base_url` | 空 | 专用通道接口地址；也可只填服务商名（`deepseek`/`kimi`/`qwen`/`zhipu`/`siliconflow`/`openrouter`/`ollama`）自动补全 |
| `llm_model` | 空 | 专用通道模型名，如 `deepseek-chat`。使用专用 Key 时必填 |
| `llm_temperature` | 0.7 | 专用通道采样温度，复盘建议 0.3~0.7 |
| `llm_max_tokens` | 0 | 专用通道最大输出长度，0 = 不限制。报告被截断时可调大 |
| `llm_timeout` | 120 | 专用通道请求超时（秒），建议不低于 60 |
| `llm_proxy` | 空 | 专用通道专用代理，留空不跟随 `http_proxy` |
| `llm_fallback_on_error` | true | 专用通道失败时是否自动回退 AstrBot 提供商重试一次 |
| `analysis_system_prompt` | 内置 | 分析报告的系统提示词，可自定义人设与输出规范 |
| `analysis_as_image` | true | 长报告转图片发送；t2i 不可用时自动回退为文本 |
| `max_message_length` | 1500 | 单条消息字符上限，超出自动拆分 |
| `parse_wait_enabled` | true | 「/d2 单场」遇到未解析的比赛时是否自动催解析并等待 |
| `parse_submit_request` | true | 等待前是否先提交一次解析申请（`POST /request/{id}`，按 10 倍额度计费） |
| `parse_check_interval` | 60 | 等待解析时每隔多久查一次状态（秒），最低 20 |
| `parse_wait_timeout` | 600 | 等待解析的最长时间（秒），默认 10 分钟 |
| `parse_notify_progress` | true | 等待期间是否每次检查都播报进度（群聊嫌吵可关闭） |
| `parse_max_concurrent` | 3 | 同时等待解析的场次上限，超出后退化为「直接用基础数据出报告」 |
| `parse_force_reparse` | false | 已解析的比赛也重新提交解析申请（一般不需要） |
| `watch_enabled` | true | 是否启用后台监听 |
| `watch_interval` | 180 | 拉取被监听玩家比赛列表的间隔（秒）。等待解析的重试有独立的 30 秒节拍，不受此值影响 |
| `watch_require_parsed` | true | 是否等详细解析数据就绪再分析 |
| `watch_max_parse_attempts` | 20 | 等待解析（比赛已收录但未解析）的最大尝试次数 |
| `watch_max_wait_attempts` | 30 | 等待 OpenDota 收录比赛的最大尝试次数，与解析次数分开计数 |
| `watch_fallback_unparsed` | true | 超时后是否仍用基础数据推送 |
| `watch_match_analysis_count` | 10 | 推送时附带的近期场次（0 = 只做单场复盘） |
| `watch_notify_at` | true | 推送时是否 `@` 添加监听的人（仅 QQ 等平台） |
| `watch_concurrency` | 2 | 监听检查并发数 |
| `watch_max_pending` | 20 | 等待解析的条目上限，超出后暂时让出最旧条目并在下一轮重新入队 |
| `watch_max_per_session` | 5 | 单会话可添加的监听数量上限 |
| `admin_users` | 空 | 监听管理员用户 ID（逗号分隔），可取消本会话内他人添加的监听 |
| `mask_others_id` | true | 在「绑定列表」中隐藏他人的账号与用户 ID |

## 数据来源与已知限制

- 数据全部来自 OpenDota 公共 API，**不需要 Steam API Key**。
- OpenDota 对局数据本身有延迟（通常落后官方几分钟到一小时），监听推送天然存在时延。
- **解析数据需要申请**：逐分钟经济、团战、出装日志等来自录像解析。未解析的比赛只有基础统计。
  插件会自动提交解析任务（`POST /request/{match_id}`，该接口按 10 倍额度计费），
  但解析队列可能排队较久，这也是监听推送「不是秒推」的原因。
  单场复盘遇到未解析的局会自动「催 + 等」（见上方「催解析 + 等待解析」章节），
  最多等 10 分钟；想跳过等待用 `/d2 单场 <比赛ID> skip`。
- **`od_data` 是状态对象，不是「已解析」标志**：`/matches/{id}` 返回的
  `od_data = {has_api, has_gcdata, has_parsed, has_archive}` 只要比赛被收录就存在。
  判断是否解析完成要看 `has_parsed`（或玩家级 `gold_t` 是否被填充）；
  `has_gcdata = false` 表示 OpenDota 连游戏客户端数据都没拿到，属未解析。
- **主动推送依赖平台能力**：比赛结束后的推送需要机器人能主动发消息。QQ 官方机器人等
  通道不支持主动消息，此时添加监听会给出警告，推送也无法送达；这类场景建议改用
  `/d2 单场 <比赛ID>` 手动复盘。
- 玩家需在 Dota 2 设置中开启「公开比赛数据（Expose Public Match Data）」，否则 OpenDota 查不到比赛。
- `/players/{id}/matches` 接口只返回 KDA 等基础字段，GPM/XPM/补刀/伤害等字段来自
  `/players/{id}/recentMatches`，而后者**最多只有 20 场**。因此查询超过 20 场时，
  超出部分没有经济类字段，聚合统计里会自动标注可用样本数。
- OpenDota 的 `/search`（昵称搜索）偶发响应缓慢，插件为其单独放宽了超时；
  若持续失败，请直接使用账号 ID 绑定。
- 英雄名使用 OpenDota 提供的英文名（如 `Luna`、`Nature's Prophet`）。

## 文件结构

```
astrbot_plugin_dota2/
├── main.py              # 插件入口：指令注册、事件处理、监听后台任务
├── dota_api.py          # OpenDota 异步客户端（限流 / 重试 / 缓存 / 解析申请）
├── dota_parse.py        # 解析状态判断 + 「催解析并等待」轮询
├── dota_nlu.py          # 自然语言意图识别（规则优先，可选大模型兜底）
├── dota_store.py        # 绑定与监听关系的落盘读写
├── dota_format.py       # 原始数据 → 可读文本 / 结构化提示词素材
├── dota_analyzer.py     # 提示词构造 + 大模型调用
├── _conf_schema.json    # 插件配置 Schema
├── metadata.yaml        # 插件元数据
└── requirements.txt
```

## 开发自测

仓库根目录的 `tests/` 提供一组不依赖真实 AstrBot 的验证脚本（`run_selftest.py`
会真实访问 OpenDota API，其余使用替身）：

```bash
python tests/run_selftest.py         # 端到端跑通全部指令与监听推送链路
python tests/watch_logic_check.py    # 监听核心逻辑的行为断言（不联网）
python tests/unit_check.py           # 纯函数单测
python tests/focus_check.py          # 单场「焦点玩家深入数据」（含多焦点）与提示词校验
python tests/event_takeover_check.py # 事件接管（直发 + 终止传播）的行为校验
python tests/nlu_check.py            # 自然语言入口：意图识别 + 端到端分发
python tests/parse_wait_check.py     # 催解析 + 等待解析：成功 / 超时 / 跳过 / 并发
python tests/watch_deliver_check.py  # 监听推送：标题推进基线、正文回退与逐块收敛
python tests/llm_channel_check.py    # 专用模型 API Key：地址归一 / 返回体解析 / 回退优先级
python tests/od_data_probe.py        # 真实拉取指定比赛的 od_data，探查解析状态
```

## 相关链接

- OpenDota API 文档：https://docs.opendota.com/
- AstrBot 插件开发文档：https://docs.astrbot.app/dev/star/plugin-new.html
- AstrBot 插件配置文档：https://docs.astrbot.app/dev/star/guides/plugin-config.html

> 提示：`metadata.yaml` 中的 `repo` 字段为占位地址，正式发布时请替换为你自己的仓库地址。
