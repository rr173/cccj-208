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

## 8. 目录

```
subwarehouse/    工具源码（manifest/ingest/engine/library/builder/cli）
tests/           单元测试、逐帧参考程序、样例生成器
tools/           gen_sparse.py 稀疏对生成、bench_sparse.py 基准
fixtures/sample/ 小夹具（§4 样例交接包）
```
