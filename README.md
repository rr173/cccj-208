# subwarehouse — 离线字幕入仓工具

把剪辑关系（轨片段）与按语言交接的素材字幕对齐，产出指定成片的可见字幕
时间段，落成一个**自包含的 SQLite 库**。审查员只用库即可回答：

- 某帧为什么显示这条文字（完整胜出路径 + 终点字幕 ID）；
- 某帧为什么是确认空白（显式遮空片段 ID）；
- 仍缺哪段资料（哪个素材、哪个语言、哪一帧没有已核查覆盖）。

工具只加工元数据与现成字幕：**不做**转码、识别、翻译或画面渲染。

运行要求：Python ≥ 3.8，仅标准库（`sqlite3`、`json`、`argparse` …）。

---

## 1. 交接包格式

一个交接包是一个目录，含一个 JSON 清单和最多三个 UTF-8 JSONL 表。
JSONL 每行一条记录，允许任意行序、允许空行；缺失的表按空表处理。

### 1.1 `manifest.json`（清单，必需）

```json
{
  "package":   "handover-2026-11-20",
  "languages": ["zho", "eng"],
  "assets":    {"M": 10, "N": 5},
  "sequences": {"S": 10, "R": 20}
}
```

| 字段 | 类型 | 含义 |
|---|---|---|
| `package` | string | 包标识（非空，≤200 字符，无控制字符） |
| `languages` | string[] | 本包声明的语言代码，非空、不重复；按语言交接即单元素数组 |
| `assets` | object | 素材唯一 ID → 正整数帧数 |
| `sequences` | object | 剪辑序列唯一 ID → 正整数帧数 |

素材与序列共用同一 ID 命名空间（不允许同名）。所有对象共享一个帧单位；
一切区间都是所属对象整数帧上的左闭右开 `[start,end)`，允许相接
（`end` == 下一条 `start`），不允许空区间（`start < end` 必须成立）。

### 1.2 `clips.jsonl`（轨片段）

```json
{"id":"r-bottom","seq":"R","track":0,"start":2,"end":17,"kind":"seq","ref":"S","base":0,"p":2,"q":3}
{"id":"r-blank","seq":"R","track":1,"start":11,"end":12,"kind":"blank"}
```

| 字段 | 类型 | 含义 |
|---|---|---|
| `id` | string | 片段稳定 ID，包内唯一 |
| `seq` | string | 所属序列 ID（必须在清单声明） |
| `track` | int | 轨级别，整数可负；**数值越大优先级越高** |
| `start`,`end` | int | 在所属序列上的局部区间 `[start,end)` |
| `kind` | string | `"asset"` 引用素材 / `"seq"` 引用序列 / `"blank"` 显式遮空 |
| `ref` | string | 被引用对象 ID（blank 无此字段） |
| `base` | int | 采样基准帧（blank 无此字段） |
| `p` | int | 速率分子，**非零**带符号整数；负值即倒放 |
| `q` | int | 速率分母，正整数 |

约束：

- 引用片段在本地帧 `start+k`（`k ∈ [0, end-start)`）读取子对象帧
  **`base + floor(k × p / q)`**，`floor` 向负无穷取整；嵌套时每层独立
  取整，不先把速率相乘，也不用浮点近似（实现全程整数运算）。
- 同一 `(seq, track)` 上片段不得相交（相接允许）。
- 实际采样越界（采到 `[0, 子对象帧数)` 之外，哪怕只差一帧）拒收。
- 引用未声明对象、引用环（含自引用）、重复片段 ID、非法区间一律拒收。
- `blank` 对所有语言显式遮空：即使底轨有字也不透出。
- 同一个子对象允许被多次引用。

### 1.3 `coverage.jsonl`（已核查覆盖区间）

```json
{"asset":"M","lang":"zho","start":0,"end":10}
```

供应方确认"这段素材字幕已检查"的区间。可多条、可重叠（取并集）。
某 `(asset, lang)` **完全没有**覆盖记录 = 该语言尚未交接（不是"没有
字幕"）。覆盖内没有字幕 = 已确认无该轨文字，工具不得猜填。

### 1.4 `cues.jsonl`（字幕条目）

```json
{"id":"A","asset":"M","lang":"zho","start":2,"end":5,"text":"原样文本"}
```

| 字段 | 类型 | 含义 |
|---|---|---|
| `id` | string | 条目稳定 ID，包内唯一 |
| `asset`,`lang` | string | 所属素材与语言（须在清单声明） |
| `start`,`end` | int | 素材帧上的区间 |
| `text` | string | 原样文本，工具不改写 |

约束：同一素材同一语言内条目不得相交；条目每一帧都必须落在该
`(asset, lang)` 覆盖并集内。

### 1.5 拒收与诊断

任何无效记录都带原位置（文件 + 1 起始行号）拒收，例如
`clips.jsonl:2: clips 'a' and 'b' overlap on track 0 of sequence 'R' ...`。
所有记录的文件与行号同时作为诊断信息随稳定 ID 存入库的 `sources` 表。

---

## 2. 解析语义

对某一语言、某一成片帧，从轨级别最大到最小，取第一个**不透明**结果：

| 情况 | 结果 | 是否挡住底轨 |
|---|---|---|
| 素材命中字幕 | `cue`（提供文字） | 是 |
| 在覆盖并集内但未命中 | 透明，继续看底轨 | 否 |
| 超出确认覆盖 | `unknown` | **是**（有待交接的字幕可能遮住底轨） |
| 显式遮空片段 | `blank`（已确认不显示文字） | 是 |
| 该轨此处无片段 | 透明 | 否 |
| 内层序列各轨皆透明 | 对父轨透明 | 否 |

只在根层把最终的透明写成 `silence`。被更高有效轨遮挡的低轨未知不产生
最终缺口。四种状态 `cue` / `blank` / `unknown` / `silence` 可区分。

**分段与合并**：每个声明语言的成片时间轴被完整划分成段，每帧有且只有
一个最终状态。段按语言、起帧稳定排序。相邻段只有在**状态、文本、完整
胜出路径（各级片段 ID 与路径参数）和终点证据（字幕 ID / 遮空片段 /
缺失素材语言）全部相同**时才合并；无路径的 `silence` 可任意合并。同一
字幕经两条不同剪辑路径显现，即使文字相同也是两个段。源帧每帧递增、
重复或跳跃都**不**拆段：段里保留能重算位置的各级路径参数
（`base/p/q` 与局部区间），查询时按需重算，不按帧跨度展开。

---

## 3. 终端入口

### 构建

```bash
python3 -m subwarehouse build --package fixtures/sample --root R \
    --out lib.sqlite [--mem-budget-mb 256] [--stats]
```

- `--root` 指定成片（必须是清单里声明的序列）；库完整覆盖该成片的
  各声明语言。
- 目标已存在时**拒绝构建**，绝不覆盖既有库；发布前任何失败（无效输入、
  写盘失败）都会清走临时文件，不留下新的可打开半成品。发布用
  `os.link` 原子完成。
- `--mem-budget-mb` 是可说明、可设置的工作内存软预算（见 §5）。
- `--stats` 输出实际读取/求交的区间数、落盘分段数、峰值内存与耗时。

构建成功打印 JSON 摘要，含 `logical_version`：库与所处理的包、所选
成片及输入逻辑内容的绑定哈希（清单规范化形式 + 根序列 + 各表记录哈希
的有序链）。**打乱输入行序不会改变逻辑版本与查询事实**；改动任何一条
记录的逻辑内容则会改变。

### 按语言及帧段导出

```bash
python3 -m subwarehouse export --lib lib.sqlite [--lang zho] \
    [--start 0] [--end 100] [--format jsonl|text] [--out file]
```

默认导出全部声明语言的整个时间轴，按语言、起帧排序。JSONL 每行一段，
含状态、文本（cue 时）、终点证据与完整路径参数。

### 单帧检查

```bash
python3 -m subwarehouse inspect --lib lib.sqlite --lang zho --frame 12 [--json]
```

从库索引取段（不读源包、不重算全库），沿路径参数重算各级实际采到的帧，
列出每级片段 ID、轨与采样帧，最后给出：素材及 cue ID 与原文 / 缺的是
哪个素材语言的哪一帧 / 显式遮空片段 ID；`silence` 不编造来源。

---

## 4. 可复跑小样例

`fixtures/sample/`（由 `python3 tests/make_sample.py fixtures/sample`
重新生成）：

- 素材 `M`（10 帧）：覆盖 `[0,10)`，字幕 `A [2,5)`、`B [7,9)`；
- 素材 `N`（5 帧）：无字幕，只核查了 `[0,1)` 与 `[3,5)`；
- 子序列 `S`：`[0,10)` 倒放 `M`（`base=9, p=-1, q=1`）；
- 根 `R`（20 帧）：底轨 `[2,17)` 引用 `S`（`base=0, p=2, q=3`）；
  上轨 `[4,9)` 引用 `N[0,5)`，`[11,12)` 显式遮空。

复跑：

```bash
python3 tests/make_sample.py fixtures/sample
python3 -m subwarehouse build --package fixtures/sample --root R --out /tmp/sample.sqlite
python3 -m subwarehouse export --lib /tmp/sample.sqlite --format text
python3 -m subwarehouse inspect --lib /tmp/sample.sqlite --lang zho --frame 12
```

预期导出（`zho`）：

```
zho [0,4) silence
zho [4,5) cue "字幕B：采访结尾" cue=B asset=M via r-bottom -> s-rev
zho [5,7) unknown asset=N via r-top-n          # 未知，而不是 B
zho [7,10) silence
zho [10,11) cue "字幕A：采访开头" cue=A asset=M
zho [11,12) blank via r-blank                  # 中间被遮空
zho [12,14) cue "字幕A：采访开头" cue=A asset=M
zho [14,20) silence
```

`B` 最终仅在 `[4,5)`（`[5,7)` 被上轨 N 的未知挡住）；查根帧 12 会走到
子序列帧 6 和 `M` 的帧 3：

```
frame 12 lang zho -> cue  (segment [12,14))
  level 0: clip r-bottom  seq R track 0  local frame 12  base=0 p=2 q=3 -> seq S frame 6
  level 1: clip s-rev     seq S track 0  local frame 6   base=9 p=-1 q=1 -> asset M frame 3
endpoint: asset M cue A  text="字幕A：采访开头"
```

---

## 5. 库结构与内存预算

库（SQLite）三张表：

- `meta(key,value)`：格式版本、包标识、根序列及帧数、声明语言、
  `logical_version`、生成器与时间；
- `segments(lang,start,end,state,text,cue_id,asset_id,path)`：最终分段，
  主键 `(lang,start)`，`path` 为各级路径参数 JSON（根在前）；
- `sources(kind,id,file,line)`：稳定 ID → 原入口记录位置（诊断用）。

`--mem-budget-mb M`（默认 256）是软预算，按可说明的方式分配：约 60%
给 SQLite 页缓存（`PRAGMA cache_size`），其余约束 Python 侧批缓冲的
行数（插入/读取批量）。解释器基线与每轨一段的叠加状态不在预算内，
但有界且小。超过预算的临时数据落盘：所有中间结果（记录暂存、各
`(对象,语言)` 的不透明分段）都写在目标旁的暂存 SQLite 文件里，排序
由 SQLite 在盘上完成（`ORDER BY` 外部排序），发布后暂存文件删除。

---

## 6. 规模设计与基准

引擎**不按帧跨度遍历、不为每帧存表、不对字幕与片段做笛卡尔积**：

- 引用片段把子对象分段 `[cs,ce)` 通过闭式整数公式映回父级一个区间
  （`floor(k·p/q)` 单调，原像是单个整数区间），未被子对象采样的短字幕
  自然消失；
- 每个 `(序列,语言)` 自底向上只解析一次并物化到盘上暂存表，被多处
  引用时复用；
- 各轨分段流式合并（每轨恒一段的 k 路叠加），相邻同状态同路径段即时
  合并；
- 工作量随相关区间边界、实际访问的引用与输出分段数增长，与帧跨度无关。

基准（`python3 tools/bench_sparse.py`，生成脚本 `tools/gen_sparse.py`）：
两个稀疏包端点数量相同（各 2000 条覆盖 + 2000 条字幕 + 13 个片段），
帧跨度 200,000 对 3,000,000,000（15,000 倍，长例数十亿帧）。实测
（64 MiB 预算）：

| 包 | 帧数 | 读取/求交区间数 | 落盘分段 | 最终分段 | 峰值内存 | 构建耗时 |
|---|---|---|---|---|---|---|
| short | 200,000 | 9,478 | 5,432 | 914 | 21.1 MiB | 0.25 s |
| long | 3,000,000,000 | 9,501 | 5,455 | 951 | 21.2 MiB | 0.25 s |

两者工作量与内存几乎相同——差异只来自区间内容而非跨度。基准随后把
长例输入包**移走**，仅凭库查回一个短区间与单帧路径（见脚本输出）。
成规模交付库不提交仓库；`out/` 已被 `.gitignore` 排除。

---

## 7. 验证

```bash
cd tests && python3 -m unittest discover -v
```

- `reference.py`：独立的逐帧参考程序，直接按语义逐帧列出覆盖、选择与
  路径；`test_invariance.py` 用 60 个随机种子的小包（正/反向分数速度、
  嵌套、遮空、多语言）把引擎与参考程序逐帧比对，并校验分段不变式
  （完整划分、相邻段身份必异）与乱序不变性。
- `test_mapping.py`：正/反向分数速度、取整后没被采样的短字幕（不出现）、
  两级 `p/q` 分别为 `2/3`、`3/2` 的路径（根帧 1 落到素材帧 0，而非速率
  相乘得到的帧 1）。
- `test_semantics.py`：同文本异路径不合并、不同语言覆盖、隐于高轨后的
  未知不产生缺口、跨层遮空、silence 合并。
- `test_sample.py`：§4 样例的全部预期。
- `test_validation.py`：缺引用、环（含自环）、同轨重叠、一帧越界、重复
  ID、字幕越出覆盖等的拒绝，以及失败前后库状态（既有库不被覆盖、
  发布失败不留半成品）。
- `test_scale.py`：数十亿帧跨度构建不逐帧展开；移走输入后仍能查回。

台账（§8）：

- `test_delivery_demo.py`：§8.7 交接演示全部步骤、稳定顺序与条数上限、
  请求幂等与冲突、换版时不掩盖在途指令。
- `test_delivery_content.py`：裁片与合并、同文异源、`blank`/`silence`、
  `unknown` 待补区间、缺资料保留旧内容、登记规则、多语言与逐目标分片表、
  两进程出单竞争。
- `test_delivery_receipts.py`：前缀与缓存、重复回执、整份输入的冲突退回、
  一序号一回执/一操作一序号/未知操作/串目标、拒绝不改内容、被杀进程的
  出单与回执导入均不漏半批。
- `test_delivery_cli.py`：机器可读子命令的往返与拒绝。
- `test_delivery_scale.py`：10^9 帧稀疏成片与短期样本工作量相同；长回执
  历史上补一个缺口后缓存链自动确认、单分片核对不扫历史。

## 8. 增量投递台账（ledger）

前七节回答"成片上应该出现什么"；本节回答"哪家平台**实际**已收到哪一段"。
台账是一个独立 SQLite 文件，只服务**一部**成片（固定的成片长度与根序列），
不需要重新跑解析引擎。实现：`subwarehouse/deliver.py`（模型）、
`subwarehouse/deliver_cli.py`（终端入口），库结构见 §8.8。

### 8.1 概念

- **目标（target）**：一个平台，附带它自己的**固定分片表**：分片名称唯一，
  相对帧区间无重叠、无空隙地铺满 `[0, 成片长度)`。不同目标可用完全不同的
  分片表，同一目标不同语言也各有分片表，首次登记时固定。
- **投递单元**：`(目标, 语言, 分片)`。
- **实际内容**：平台侧该单元当前持有的文字区间；初始为空。`blank` 与
  `silence` 都不产生文字。本工具发出的指令与接收回执是内容变化的唯一来源。

### 8.2 登记（`ledger-register`）

```bash
python3 -m subwarehouse ledger ledger-init --ledger film.ledger

python3 -m subwarehouse ledger ledger-register --ledger film.ledger \
    --target platA --release 1 --lib v1.sqlite --languages a.json
```

`--languages` 是 JSON 对象：`语言 -> [{name,start,end}, ...]`，区间是**成片
帧**上的相对起点；必须从 0 开始、首尾相接、最后一段结束于成片长度，名称在
该目标该语言内唯一。首次登记必须给出；之后语言集合与分片表固定，重复给出
相同表可以（只校验），给出不同表或未配置的语言即拒绝。

登记规则：

- 工具打开只读库校验：`root_frames` 必须等于该台账的成片长度，`root`
  必须等于首次登记的根序列，配置的每个语言都必须在库中声明。只读 `build`
  库即可，不写它。
- 每个目标的发行编号从 1 起、逐次加一；已登记编号不能换成另一份逻辑发行
  内容（按包标识、根、长度、`logical_version`、语言集合与编号计算的
  `release_hash` 判定）；同编号同内容的重试是幂等空操作。
- 登记时把库中 `cue` 段落**裁进各分片的相对帧区间**：同文且相接的区间合并；
  单元内容身份 = 规范化后的相对区间 + 原样文字，与路径、诊断行号、cue ID
  及库文件字节无关。`blank`、`silence` 不产生文字。分片内任意一帧是
  `unknown`，则本次**不提供该单元完整内容**，并逐段记下待补的成片区间。
- 裁剪结果与待补区间（`cuts`、`gaps`、`release_units`）随台账落盘，**原库
  移走后仍能出单与核对**。

### 8.3 出单（`ledger-dispatch` / `ledger-batch`）

```bash
python3 -m subwarehouse ledger ledger-dispatch --ledger film.ledger \
    --request take-17 --target platA --target platB --limit 200
python3 -m subwarehouse ledger ledger-batch --ledger film.ledger --request take-17
```

针对目标**当前最高发行编号**逐单元判断，不做全量重发：

| 已确认内容 | 期望内容 | 结果 |
|---|---|---|
| 与期望相同 | 同 | 不出单 |
| 有字幕 | 本次缺资料（`unknown`） | 保留平台内容，不出单 |
| 有字幕 | 确认全空 | `clear`（无替换内容） |
| 从未投递 / 已清除 | 空 | 不出单 |
| 其余差异 | | `replace` 整段替换内容（绝不旧行拼新行） |

- 每单元**同时最多一条未结操作**。即使已登记更高编号，旧操作在途时该单元
  等回执进入确认状态，随后再从**当时最新**的期望重新判断。
- 请求 id 由调用方给出，`--limit` 限制本批条数；条数用尽即截断并按稳定单元
  顺序（目标、语言、分片起始帧）留给后续批次，回执中 `more: true`。
- 同请求 id 重试**原样返回原批次**（包括空批次）；同 id 不同参数（目标集合
  或上限）报冲突。请求回复存于 `requests` 表。
- 指令粒度就是单元：同一批里内容相同的两个分片各得一条指令，不做跨分片
  合并。
- 两个进程同时规划不会给同一单元开出两条未确认指令：写事务（`BEGIN
  IMMEDIATE`）+ `ops` 上的部分唯一索引 `ops_one_open` 双重保证；后到者
  要么等待锁（30s），要么看到该单元已有在途操作而不出单。
- 投递重试沿用原 `op_id`；平台按 `op_id` 去重，不为重送追加回执。
- 指令含 `op_id`、目标、语言、分片、`shard_start`、发行编号、动作、替换
  内容（`clear` 为空数组）与 `payload_hash`；握有指令 JSON 即可投递，无需
  再读原库。

### 8.4 回执（`ledger-receipts` / `ledger-progress`）

```bash
python3 -m subwarehouse ledger ledger-receipts --ledger film.ledger rec-03.jsonl rec-01.jsonl
python3 -m subwarehouse ledger ledger-progress --ledger film.ledger --target platA
```

回执行（JSONL，每行一条）：

```json
{"target":"platA","receipt":3,"op_id":"platA.r1.zho.a0.1","status":"applied"}
{"target":"platA","receipt":4,"op_id":"platA.r1.zho.a1.1","status":"rejected","reason":"窗口占用"}
```

- 每个目标**独立编号**，从 1 连续；覆盖该目标所有语言与分片。平台对每条
  最终处理的操作只产生一条回执：`applied`，或 `rejected`（须有 `reason`，
  平台内容不变）。
- 文件可乱序、可与先前文件交叠、可整份重复；**相同回执重复到达不产生任何
  变化**。
- 只确认 **1 到第一个缺号之前** 的连续前缀。缺号之后的回执落盘待用
  （`pending`），不改已确认内容、不给单元解占；缺口补齐时自动按序吃进已缓存
  的后续回执，且只处理缓存链，不重放旧历史。
- 整份输入原子生效：任何冲突都会退回**整份输入**，已接收的资料不变。冲突
  包括：一个序号对应了不同回执；同一操作占用多个序号；引用未知操作；操作
  与目标不符；未知目标；回执字段非法。
- 已应用的回执才改变实际内容与解占；被拒回执只结束本次尝试，单元立即可按
  最新期望重新出单（若内容仍需变化，会得到**新的** `op_id`，因为编号含
  尝试序号）。
- 进度、在途占用、实际内容与历史证据与回执在同一事务中落盘；进程在写入中途
  被杀死时，重新打开只能看到整批之前或之后的完整状态（`synchronous=FULL`）。

### 8.5 交班核对（`ledger-verify`）

```bash
python3 -m subwarehouse ledger ledger-verify --ledger film.ledger --target platA
python3 -m subwarehouse ledger ledger-verify --ledger film.ledger --target platA \
    --lang zho --shard a0 --release 1 --upto 4
```

- 目标级输出当前确认前缀与首个缺号、在途操作数与最高发行编号。
- 单元级（`--lang` + `--shard`）在给定**已登记发行编号**（缺省为最高）与
  回执位置下，输出平台当时的确认内容：文字区间或空，并区分：
  - `initial_empty`：尚无任何已应用指令（初始空）；
  - `cleared`：最近一条已应用指令是清除；
  - `subtitles`：当前持有文字；
  - 期望侧另有 `empty` / `incomplete`（本次缺资料）与待补成片区间。
- 还给出最近一次实际生效的指令与回执、`matches_expected`，以及该单元的所有
  被拒尝试（含拒绝原因）。
- `--upto 0` 表示初始状态；位置必须落在确认前缀内，否则拒绝。**拒绝或尚未
  确认的记录永远不作为证据**（`evidence: false`）。
- 核对完全由台账中已关闭的回执与存下的替换内容回答，不回读原库、不扫全部
  回执历史（按 `(target, receipt)`、`(target, lang, shard, receipt)` 索引
  定位单行）。**旧位置的结果不会随今日出单或新回执改变**。

### 8.6 操作员补收与继续投递

1. 平台分批回传回执文件。任意顺序、任意重叠导入：
   `ledger-receipts ... f1.jsonl f2.jsonl`。若某文件整份被拒，错误会指明
   文件与行号，修好该文件后重导即可；先前已接收的资料不受影响。
2. `ledger-progress --target T` 看 `confirmed_prefix` 与 `first_missing`；
   `pending_receipts` 是已落盘待用的缺号之后回执。
3. 补回缺失序号的文件后，其后的缓存回执自动确认，无需重发。
4. 再次 `ledger-dispatch`（**新的** 请求 id，或沿用尚未取过的请求 id）按
   最新确认状态与最新发行编号出单；仍缺资料的分片会保留平台旧内容，等资料
   补齐后的下一次出单再发。

### 8.7 交接演示

`python3 tools/demo_ledger.py`（夹具：`tests/demo.py`，三片各四帧）：

| 步骤 | 目标 A 前缀 | 说明 |
|---|---|---|
| 第一版三片各发一条 | 0 | 平台未回执前不算发布 |
| 序号 3 先到 | 0 | 落盘待用，不确认 |
| 收到序号 1 | 1 | 只有第一片确认 |
| 收到序号 2 | 3 | 后两片一起确认（3 自动吃进） |
| 第二版：第一片全空、第二片缺一帧、第三片同文拆成两段 | 3 | 只开第一片的清除单；第三片内容不变 |
| 清除被拒 | 4 | 第一片仍有旧字 |
| 再出单 | 4 | **新操作 ID** 的清除单 |
| 新清除成功 | 5 | `--upto 4` 回查仍是旧字，`--upto 5` 为空 |

演示还给出：目标 B 的前缀不被 A 的回执推进；换版时未结指令不会被掩盖；
`tests/platform_sim.py` 是**独立的**小型平台模拟（只认指令/回执 JSON），
测试用它区分"已发出"与"已确认"。

### 8.8 台账结构与有界处理

表（SQLite）：

- `meta(key,value)`：格式、生成器、成片长度与根、各目标确认前缀、批次计数；
- `targets(target,frames,root)`；
- `shards(target,lang,ord,name,target_start,target_end)`：固定分片表；
- `releases` / `release_langs`：发行编号、库路径、`logical_version`、
  `release_hash`；
- `release_units(target,seq,lang,shard,items,content_hash,has_gap)`：
  每单元的内容摘要（相对区间 + 原样文字的增量哈希）与是否缺资料；
- `cuts(...) 、gaps(...)`：单元文字区间与待补成片区间；
- `ops(op_id, ..., request, batch, seq, action, payload_hash, content_hash,
  status, receipt, applied, reason, created_utc, closed_utc)`，部分唯一索引
  保证每单元至多一条 `open`；
- `op_items(op_id, rel_start, rel_end, text)`：整段替换内容（回执后可独立
  核对，不读原库）；
- `confirmed(target,lang,shard,state,action,receipt,seq,op_id,content_hash)`；
- `pending(target,receipt,op_id,applied,reason)`：缺号后的缓存回执；
- `requests(request,targets,limit_n,body,created_utc)`：出单请求的幂等回复。

有界处理：

- 载荷按**区间**生成，从不枚举帧；`--limit` 限制单批条数。
- 登记时按帧序**流式**读库分段，并与分片表做双指针归并：内存里只有当前分段
  跨越的少量分片与一条开启中的区间；摘要增量更新，`cuts`/`gaps`/`release_units`
  以有界批量写入。
- 出单用一条磁盘上的连接（`shards` ⋈ `confirmed` ⋈ `release_units`）逐行
  扫描，只在真正出单时才取该单元的 `cuts` 行；每个单元的状态都是 O(1)。
- 回执导入先流式校验、写入 `temp` 暂存表，再逐批归并；不把回执文件或历史读
  进内存。核对单位置按索引取单行，不扫历史。

### 8.9 规模与测量

生成程序 `tools/gen_ledger_scale.py`（运行时生成，不提交），基准
`tools/bench_ledger.py`：

```bash
python3 tools/bench_ledger.py --workdir out/ledger --history 3000
```

实测（40,000 分片、两种语言、4,000 条字幕、64 MiB 缓存；峰值 RSS 含解释器
基线）：

| 包 | 帧数 | 读取分段 | 裁剪区间 | 出单条数 | 峰值内存 | 登记耗时 |
|---|---|---|---|---|---|---|
| short | 100,000 | 16,002 | 19,200 | 19,200 | 94.4 MiB | 1.19 s |
| long | 1,000,000,000 | 16,002 | 21,352 | 21,352 | 103.8 MiB | 1.15 s |

两者读取分段数完全相同；裁剪区间只随"字幕是否恰好压在分片边界"变化，与
10,000 倍的帧跨度无关。回执侧：先在目标上积累 3,000 次发行、9,000 条已确认
回执，再人为留下一个缺号并把后两条回执先导入——补齐后前缀 9000 → 9003，
**2 条缓存回执在同一次导入中确认（0.008 s）**，单分片核对 0.004 s，内存
31.5 MiB。`tests/test_delivery_scale.py` 用运行时样本固定这些断言（含
10^9 帧稀疏成片与"决不看跨度"的对比）。

## 9. 跨成片素材更正影响盘点（review）

供应方送来一条素材的更正时，节目组想在接受更正**以前**知道该叫哪些成片的
审看员返工。本节交付的是**复审清单**：不生成新版成片库，不改旧库或原包，
也不做投递。实现：`subwarehouse/review.py`（模型）、
`subwarehouse/review_cli.py`（终端入口）。

### 9.1 盘点目录与基准

```bash
python3 -m subwarehouse review review-init --package pkg --dir inv \
    --program R1 --program R2 [--mem-budget-mb 256] [--stats]
python3 -m subwarehouse review review-info --dir inv
```

`review-init` 用一份**现有格式**的交接包（§1）建立一个可反复打开的盘点
目录（`inv/inventory.sqlite`），`--program` 登记哪些序列是需要送审的
成片（可多个、可重复给出）。目录自包含：校验后的三张表、可达范围内各
`(素材,语言)` 与 `(序列,语言)` 的不透明分段、每部登记成片的最终分段全部
物化在库内，**交接包移走后照样工作**。这是一次性成本；`review-info`
给出的 `baseline` 是目录的基准标识（清单规范化形式 + 登记成片 + 各表
记录哈希的有序链），之后每份更正单都指名它。

### 9.2 更正单（补丁）格式

编辑不再交整份字幕包，只交一份更正单（一个 JSON 对象）：

```json
{
  "baseline": "<review-info 给出的基准标识>",
  "order":    "corr-2026-11-20-01",
  "windows":  [
    {"asset": "T", "lang": "zho", "start": 2, "end": 5,
     "coverage": [{"start": 2, "end": 4}],
     "cues":     [{"id": "A", "start": 2, "end": 4, "text": "订正后文本"}]}
  ]
}
```

语义：

- 更正**不改剪辑、不改序列长度**：只在半开窗口 `[start,end)` 内替换某个
  素材、某种语言的已核查覆盖与字幕。
- 窗口内的新覆盖是**全量声明**：窗口里没声明覆盖的帧变成待确认
  （`unknown`）；窗口里没声明的字幕即不存在。
- 基准字幕凡与窗口相交，必须**完整**落在同一个窗口内（不隐式裁断）；
  它们被窗口内的新字幕整体替换。窗口外的覆盖与字幕不变。
- 一份更正单可带多个窗口；同一 `(asset, lang)` 的窗口不得相交（相接
  允许）。`coverage`/`cues` 缺省为空数组（整窗撤销覆盖）。
- 整单生效后的字幕仍须满足原有约束：字幕落在窗口声明的覆盖并集内、
  同窗字幕不相交、字幕 ID 全包唯一（被本单替换掉的旧 ID 可以重用）。
- **基准不符、窗口越界或任何一处无效，整单不出清单**；错误会指出属于
  哪一项（`item.window` 为窗口在单中的序号，涉及字幕时另有
  `item.cue`/`item.coverage`）。试算不叠加：每次都与目录里的同一基准
  比较，被拒的单不留下任何痕迹，下一次有效试算仍用原基准。
- 接受的更正单调换记录顺序（窗口、覆盖、字幕的书写次序）**不改变**
  复审清单。

### 9.3 试算与复审清单

```bash
python3 -m subwarehouse review review-trial --dir inv \
    --order corr.json --out list.jsonl [--stats]
```

清单是 JSONL，每行一段真正发生变化的帧段，按 **成片、语言、起帧** 稳定
排序；连续帧仅在类别与改前、改后证据（状态、文字、完整获胜剪辑链、终点
字幕 ID / 缺失素材 / 遮空片段）全部相同时才合并。路径保留各级
`base/p/q` 与局部区间，足以推算段内任一帧，不展开成逐帧表。同一素材的
多次使用各自采样、各自成段。

```json
{"program": "R1", "lang": "zho", "start": 3, "end": 4, "class": "playback",
 "before": {"state": "cue", "text": "字幕A", "cue": "A", "asset": "T",
            "path": [{"clip": "r1-s", ...}, {"clip": "s-t", ...}]},
 "after":  {"state": "cue", "text": "字幕B", "cue": "B", "asset": "U",
            "path": [{"clip": "r1-s", ...}, {"clip": "s-u", ...}]}}
```

变化分两类：

| 类别 | 含义 | 例子 |
|---|---|---|
| `playback` 需重看播放 | 播放判断变了 | 文字原样不同；已知有字↔无字；已知↔待确认 |
| `evidence` 只需复核证据 | 播放判断相同，状态或来源变了 | 同文换了字幕 ID；同文经另一条剪辑链；同样待确认但缺的是另一段素材 |

`blank` 与 `silence` 同属"已知无字"；`cue` 的播放判断就是原样文本。
stdout 的摘要给出**每个受影响成片和语言在这两类下各有多少帧**
（`counts`）；候选窗口先取并集再比较，重叠窗口不会重复计数。低轨有更正
但始终被上轨遮住、变更帧被快放跳过、语言无关，都不会进清单。

### 9.4 手算表：为什么两份清单不能相加

五帧子序列 `S`：上轨以 `base=8, p=-2, q=1` 采九帧素材 `T`（依次取源帧
8、6、4、2、0）；`T` 原来全覆盖，仅 `[2,5)` 有字幕 A。下轨等速读另一块
已全覆盖的素材 `U`，其中 `[2,4)` 有字幕 B。成片甲以 `base=1, p=3, q=4`
取五帧（落到 `S` 帧 1、1、2、3、4），成片乙只取 `S` 的帧 1 和 3。

| 更正单 | 甲 `R1` | 乙 `R2` |
|---|---|---|
| ① 单把 `U` 的 B 改为 D | 无变化（上轨 A 始终遮住 U） | 无变化 |
| ② 单更正 `T` 的 `[2,5)`（新覆盖仅 `[2,4)`、无字幕） | `[2,3)` A→待确认；`[3,4)` A→B | `[1,2)` A→B |
| ①+② 同单 | `[2,3)` A→待确认；`[3,4)` A→**D** | `[1,2)` A→**D** |

逐帧看 ② 单独作用：甲帧 2 → `S2` → `T4`，`T4` 被撤销覆盖 → 待确认；
甲帧 3 → `S3` → `T2`，覆盖无字 → 透出下轨 `U3` 的 B；乙帧 1 → `S3`
同理得 B。①单独作用时 B/D 都藏在 A 后面，两部都不用返工。两笔同单时，
刚露出的底轨已经是 D 而不是 B——**把 ① 的空清单与 ② 的清单相加会得到
B，是错的**；待确认区间 `[2,3)` 则不变。所以每份更正单必须按一个整体
对基准盘点（`tests/test_review.py::HandCalcTest` 固定了这张表）。

### 9.5 局部求值与工作量统计

逐单遍历全部成片、为每个成片重建两份库再取差，不能用于日常流程。试算
从更正触达的**引用、时间范围和语言**出发：

- 从被更正的 `(素材,语言)` 沿反向引用图（建目录时物化、只含登记成片
  可达的边）爬到受影响的序列与成片；无关成片根本不被访问。
- 每级把子对象的变更窗口用闭式整数公式映回父级（与 §6 同一
  `lift_interval`），合并成各序列的**候选窗口**；同一子序列被多处引用
  时只算一次，候选窗口取并集，不重复计数。
- 只有候选窗口非空的序列被重算，且只重算候选窗口：窗口内叠加**全部**
  轨（于是没被更正、却可能露出来的底轨仍会被找到），未触达的子对象直接
  读基准物化分段，被更正的素材读按窗口补丁物化的分段。自底向上一次
  完成，不重算整库。
- 全程不按媒体帧数迭代；基准与输出都不一次性装进内存（分段流式读取、
  清单行落盘排序后流式写出）。

`--stats` 给出可读的工作量：`refs_walked`（走过的引用边）、
`sequences_touched`/`sequences_rejudged`（爬到 / 真正重算的序列）、
`candidate_windows`/`candidate_frames`（重新判定的范围）、`read`（读到的
各类候选区间行数）、`written`（物化的补丁分段）、`review_rows` 与峰值
内存、耗时。

生成样例 `tools/gen_review_scale.py`、基准 `tools/bench_review.py`
（运行时生成，不提交）：

```bash
python3 tools/bench_review.py --workdir out/review
```

基准固定局部素材、剪辑与一笔小更正（热区 1000 帧、窗口 `[500,503)`），
把不相干成片数量（4 → 256）与未触达时段（10^5 → 10^9 帧）同时放大。
实测（64 MiB 预算）：

| 配置 | 成片数 | 帧数 | 建目录 | refs | 重算序列 | 候选窗口 | 候选帧 | 读区间 | 清单行 | 试算耗时 |
|---|---|---|---|---|---|---|---|---|---|---|
| few-short | 5 | 100,000 | 0.10 s | 2 | 2 | 2 | 6 | 30 | 2 | 0.057 s |
| many-short | 257 | 100,000 | 1.12 s | 2 | 2 | 2 | 6 | 30 | 2 | 0.058 s |
| few-long | 5 | 1,000,000,000 | 0.08 s | 2 | 2 | 2 | 6 | 30 | 2 | 0.056 s |
| many-long | 257 | 1,000,000,000 | 1.17 s | 2 | 2 | 2 | 6 | 30 | 2 | 0.064 s |

四种配置的**每次试算工作量与清单一字不差**；只有建目录的一次性成本随
成片数量增长。基准最后把最大的输入包移走再试算，结果不变。
`tests/test_review_scale.py` 用运行时样本固定这些断言。

### 9.6 验证

- `test_review.py`：§9.4 手算表三笔单；同文换证据、同文异链、待确认换
  素材、撤覆盖遮住底轨、删字幕露出底轨、隔帧跳过更正、显式遮空保护、
  始终被遮住的低轨、不受影响的语言、首次交接的语言、同对相接多窗、
  复用被替换字幕 ID、不可达素材等独立小样本；基准不符、越界、跨窗
  字幕、窗相交、字幕越出声明覆盖、ID 冲突等整单拒绝；被拒后仍用原
  基准；目录字节在试算前后不变；交接包移走后试算照常；以及把随机小包
  的试算结果与"补丁后的包用原 `build` 全量重建再逐帧取差"对比的模糊
  测试。
- `test_review_scale.py`：§9.5 的工作量不变式与移包试算。
- 原有构建（§3）与投递台账（§8）入口不变，全部测试照旧通过。

## 10. 语料出库（corpus）

节目档案组从已入仓的字幕里为检索模型团队准备训练与验证样本。成片能播，
不代表采访的每个源帧都获准用于这项内部用途；同一段采访还可能被不同
节目组换名复用；旧批次已交给下游，不能为了容纳一条新样本就重新给旧
样本分组。本命令从编辑选中的成片窗口产出**本批**的训练、验证字幕数据，
以及没有出库的窗口及原因。它只读现成的 `build` 库：不建成片库、不改
剪辑、不投递字幕。实现：`subwarehouse/corpus.py`（模型）、
`subwarehouse/corpus_cli.py`（终端入口）。

### 10.1 命令与输入

```bash
python3 -m subwarehouse corpus corpus-export \
    --libraries libs.json --samples samples.jsonl \
    --mapping map.jsonl --grants grants.jsonl \
    --out batch-03 [--registry batch-02/registry.sqlite] \
    [--mem-budget-mb 256] [--stats]
python3 -m subwarehouse corpus corpus-registry-info --registry batch-03/registry.sqlite
```

四种机器可读输入（JSON/JSONL，JSONL 每行一条、允许空行）：

**库清单**（JSON 对象）：`{"libraries": [{"alias","path","logical_version"}]}`。
别名唯一；相对路径按库清单所在目录解析。每个列出的库都以只读方式打开并
核对 `logical_version`，不符即整批拒绝。

**样本单**（JSONL）：`{"id","library","start","end","languages":[...]}`。
`id` 本次唯一且不得出现在上批登记簿中；`library` 必须是库清单里的别名；
`[start,end)` 是成片帧上的半开窗口，不得超出该片长度；`languages` 非空、
不重复、且都在该库声明。

**映射表**（JSONL）：`{"library","asset","key"}`，把 `(库别名, 素材 ID)`
映射到档案组认定的源素材键。跨库映射到同一键表示同一素材且共享源帧坐标；
**相同的本地素材 ID 不会被擅自认定为同一素材**——身份只来自这张表。同一
`(library, asset)` 出现两次（无论键是否相同）都是输入错误。

**授权表**（JSONL）：`{"key","lang","start","end"}`，按 `(源素材键, 语言)`
给出允许进入语料的源帧区间；区间可重叠，按并集理解。源素材长度不在库中，
故只校验 `0 <= start < end`。

**上批登记簿**（可选）：上一批输出的 `registry.sqlite`，自包含（样本 ID、
去向、关系键），旧成片库已移走也能工作。

重复样本 ID、冲突映射、非法区间、库版本不符、样本超出成片、引用未声明
语言、登记簿自相矛盾（同一连通组两侧）都算**输入错误**：整次不交付，
输出目录不出现。

### 10.2 裁剪与授权

先决定一个窗口能不能作为**完整样本**交付。对窗口内每个语言裁出获胜字幕
段（与窗口求交），保留窗口相对起止与原样文字。以下任一情况**整个样本
拒收**，不降级成交付剩余语言的一条记录：

1. 窗口内任一语言有待确认帧（`unknown`）——原因给出语言与成片区间
   （同时给出窗口相对区间）；
2. 全部语言均无字幕（`no_cues`）；
3. 有字幕的素材未在映射表中（`unmapped_asset`，给出素材与区间）；
4. 任意一帧获胜字幕实际采到的源帧不在该语言授权并集内
   （`unauthorized`，给出一对确实未获准的成片帧与源帧，含窗口相对帧号）。

已知无字（`blank`/`silence`）不需要素材授权。授权**只看实际采到的帧**：
倒放跳过一个未授权源帧不算违规；嵌套取整逐级计算，不把先后两次取整折成
一个速度。实现不按窗口帧数逐帧检查：一条字幕段实际采到的源帧落在闭式
求出的包围盒内，把授权并集在盒内的**空隙**逐层用 `lift_interval` 映回
成片帧，原像非空的空隙才是违规——工作量随触到的授权区间数增长，与窗口
跨度无关（§10.6）。

拒收记录写入 `rejected.jsonl`，**不带字幕文本**，不参加后续分组。

### 10.3 可出库字幕行

`train.jsonl` / `validation.jsonl` 每行一个样本：

```json
{"id": "s1", "library": "main", "logical_version": "<库逻辑版本>",
 "window": [0, 4), "languages": ["zho"],
 "cues": [{"lang": "zho", "start": 0, "end": 4, "text": "原样文字",
           "cue": "A", "asset": "T", "source_key": "K",
           "path": [{"clip": "r-t", "base": 8, "p": -2, "q": 1, ...}],
           "proof": [{"window": [0, 1), "source_frames": [8, 9),
                      "grant": [8, 9]}, ...]}],
 "keys": [{"kind": "src", "key": "K"},
          {"kind": "txt", "lang": "zho", "text": "原样文字"}]}
```

每条字幕可回查原库版本（样本级 `logical_version`）、素材、字幕 ID、取帧
路径及对应的授权区间：一条字幕跨几段授权时，`proof` 把证明拆成若干段
（每段给出窗口相对区间、实际采到的源帧范围与授权它的授权区间），**不拆
碎字幕正文**。`keys` 是该样本参与分组的全部关系键（见下节）。

### 10.4 分组与去向

通过授权的样本与登记簿里的全部历史成员一起分组。两类关系键，不同命名
空间：

- `src`：窗口内实际交付的所有语言的字幕来源的源素材键（同一采访的不
  相交片段也不能分到两边）；
- `txt`：同一语言下相同的非空规范化字幕文本。规范化 = Unicode NFC +
  连续空白收成一个空格并去首尾空白；不改大小写、不去标点；不同语言的
  同字串不算重复。

共用任一键即同一边，可经其他样本传递连接（连通分量）。去向规则：

- 连通组只有一侧历史成员：新成员全部去那一侧；
- 没有历史成员：对组内字典序最小的样本 ID 用种子规则定侧——
  `sha256(b"subwarehouse-corpus/1\n" + 最小样本ID的UTF-8)`，摘要首字节
  偶数去 `train`、奇数去 `validation`；不凑比例；
- 新样本把历史训练和历史验证连成一组：本批该组的**全部新成员隔离**，
  历史记录原样继承，隔离样本不写入新登记簿。

训练、验证、拒收、隔离互斥，每个本批 ID 恰好一个去向。隔离证据按组
共享一份（`evidence.jsonl` 每组一行）：覆盖全组的键标注生成树
（`edges`，每条边标明把成员连在一起的素材键或字幕键）、本批被隔离
成员、两侧历史样本，能从任一被隔离新成员走到两侧历史样本；不为每个
新成员复制整条链。`quarantine.jsonl` 每行一个被隔离样本及其组 ID。

### 10.5 输出与登记簿

输出目录（已存在则拒绝，绝不覆盖）：

| 文件 | 内容 |
|---|---|
| `train.jsonl` / `validation.jsonl` | 实际交付的两侧样本（§10.3） |
| `rejected.jsonl` | 拒收窗口与可定位原因（无字幕文本） |
| `quarantine.jsonl` | 被隔离样本 ID 与组 ID |
| `evidence.jsonl` | 每个隔离组一份共享证据图 |
| `registry.sqlite` | 新登记簿，可直接作为下一批 `--registry` |
| `manifest.json` | 本批摘要：库版本、计数、种子、文件清单 |

登记簿（`subwarehouse-corpus-registry/1`）：`members(sid, side, comp)`
记录历批每个已交付样本的去向与连通组，`keys(comp, kind, klang, kbody)`
按连通组记录当时的全部关系键（组级去重存储，连通性与逐样本记录完全
等价）；历史行原样继承，新交付样本连同其连通组的键追加进去。相同逻辑
输入重排后，归属、证据和记录顺序相同（所有输出按样本 ID 等稳定排序）。
所有文件先写入输出旁的临时目录，全部写完并 fsync 后整体 `rename` 发布；
整批输入未校验完或中途写失败，消费端不会看到半成品目录。已有发布目录
与输入库只读。

### 10.6 规模设计与基准

两类会增长的工作量，都有界：

1. **长窗口的授权与映射查询**：授权检查按授权并集在采样包围盒内的空隙
   逐层闭式映回，不按窗口帧数逐帧检查；每个 `(键,语言)` 的授权并集只
   物化一次，之后按索引取与包围盒相交的区间。
2. **热门关系键的成员关联**：关系表 `(键, 成员)` 落盘，按键有序扫描一
   遍即完成并查集合并，每个键的成员呈星形接入，合并边数 = 成员数 −
   连通组数；登记簿按连通组存键。绝不枚举成员两两组合。

中间字幕、关系表、合并边、授权并集都写在输出旁的暂存 SQLite 里（排序
由 SQLite 在盘上完成）；登记簿流式读入读出；并查集只在内存保存样本 ID
级的父指针（每节点一项，聚合信息只在根上）。

生成程序 `tools/gen_corpus_scale.py`，基准 `tools/bench_corpus.py`
（运行时生成，不提交）：

```bash
python3 tools/bench_corpus.py --workdir out/corpus
```

实测（64 MiB 预算）：稀疏窗口在帧跨度 200,000 对 3,000,000,000（15,000
倍）下，读取分段 6,000 对 6,000、读取授权区间 6,001 对 6,001、落盘字幕
3,000 对 3,000——完全相同，出库各约 0.4 s；交付结构一致。一个热门键扩
至 100,000 成员：关系行 200,000（每样本 2 行）、合并边 99,999（成员−1）、
登记簿键 2 行、成员 100,000 行，约 36 s——关联数随成员线性增长，不是
两两组合（两两组合将是约 50 亿）。`tests/test_corpus_scale.py` 用运行时
样本固定这些不变式。

### 10.7 可复跑演示

`python3 tools/demo_corpus.py`（夹具运行时生成于 `out/demo-corpus/`）：

1. **倒放授权**：窗口四帧倒序采到源帧 8、6、4、2，授权恰好覆盖
   `{2,4,6,8}` 时通过（跳过的 3、5、7 未授权也不算违规），证明拆成四段；
   仅撤掉源帧 4 的授权，拒收原因定位到窗口内第三帧（`window_frame: 2`、
   `source_frame: 4`），且不输出正文。
2. **跨批隔离**：上批训练样本 `old-tr`（键 `txt:zho:共享文本`）、上批
   验证样本 `old-val`（键 `src:K1`）只在登记簿里（旧库已移走）。本批 X
   经源素材键 `K1` 连到 `old-val`，Y 经一条相同文字连到 X、再经另一条
   文字连到 `old-tr`：X、Y 同时隔离，证据给出
   `old-val — X — Y — old-tr` 整条链。若 Y 因一帧未获授权先被拒收，
   X 进入验证。
3. **多语言与本地 ID**：多语言样本的去向由全部语言的字幕共同决定；
   两库相同的本地素材 ID 映射到不同档案键时不会误连。

### 10.8 验证

- `test_corpus.py`：§10.7 三个演示案例的全部预期（倒放窗口授权、
  跨批隔离与证据链、多语言与本地 ID 区分）；未知帧、无字幕、未映射、
  未授权四类拒收定位；空白与静默免授权；嵌套取整不折叠；规范化
  （NFC、空白折叠、分语言、大小写与标点敏感）；种子定侧；重复样本
  ID、冲突映射、非法区间、库版本不符、窗口越界、未声明语言、双侧
  登记簿等整批拒绝；输入乱序后输出逐字节一致；登记簿跨批链接且旧库
  移走后照常；CLI 往返与拒绝。
- `test_corpus_scale.py`：稀疏长窗口工作量与跨度无关；热门键两万
  成员下关联数保持线性（基准工具演示十万成员）。

## 11. 目录

```
subwarehouse/    工具源码（manifest/ingest/engine/library/builder/cli；
                 deliver.py / deliver_cli.py 为投递台账；
                 review.py / review_cli.py 为更正影响盘点；
                 corpus.py / corpus_cli.py 为语料出库）
tests/           单元测试、逐帧参考程序、样例生成器；
                 demo.py 交接演示夹具、platform_sim.py 平台模拟；
                 test_review*.py 盘点测试；test_corpus*.py 语料出库测试
tools/           gen_sparse.py / bench_sparse.py（库）；
                 gen_ledger_scale.py / bench_ledger.py（台账）；
                 gen_review_scale.py / bench_review.py（盘点）；
                 gen_corpus_scale.py / bench_corpus.py（语料出库）；
                 demo_ledger.py（交接演示状态表）；
                 demo_corpus.py（语料出库演示）
fixtures/sample/ 小夹具（§4 样例交接包）
```
