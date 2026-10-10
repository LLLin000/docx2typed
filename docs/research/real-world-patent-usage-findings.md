# 真实专利流程使用调研：问题图谱与优化方向

调研日期：2026-10-10
数据源：
- 磁盘证据：`D:\L\OB\Literature-hub\Project\专利区`（20,101 文件；35 workspace；3,769 generation；1,418 DOCX；约 2.4 GB）
- 轨迹证据：OMP `~/.omp/agent/sessions/` 中 9 个专利相关 session（约 105 MB JSONL，610 条用户消息）

本文件只记录**可复现的事实**（含路径/拒绝码/时间戳），不做推测性结论。

---

## 0. 结论

工具在「不破坏格式」上已达到可被真实专利采用的水平——这是很高门槛的验证。

当前优化空间**不是单一的**，经证据细分后是**三类不同性质的问题**（详见 §5A）：

| 类型 | 性质 | 规模 | 手段 |
|---|---|---|---|
| **A. 原语已存在但不可发现** | 采用失败 | 235 次绕过已上线的工具 | skill/路由优化（低成本） |
| **B. 原语确实不存在** | 真实缺口 | ~686 次读取/自省/填充 | 新增工具 |
| **C. 原语存在但不够用/有缺陷** | 能力不足 + 引擎 bug | 105 次「当时没上线」+ 45 次上线后仍手写 | 修引擎、增强表达力 |

另有两条**独立于上述**的严重问题：

- **修订碎片化**（§1/§2）：单段落 47 条碎片修订，投影产出病句（`所述所述`），并造成编辑自锁。
- **违反自身安全不变量**（§5A.7）：引擎明令禁止绕过，实测仍有 **101 次**直接 repack 写 DOCX（其中 45 次发生在编辑原语齐备之后）。

因此正确做法是**先分类、再分治**，而非笼统地「加路由」或「加工具」。

---

## 1. P0 — 修订碎片化导致投影文本不可信

### 1.1 单段落被切成 40+ 条碎片修订

`20260721三个专利/同心圆版本链/workspace_0914_批注回修/revisions.json`（schema `typed-revisions-1`）：

| 段落 | 修订条数 |
|---|---|
| P94 | 47 |
| P52 | 44 |
| P51 | 35 |
| P49 | 32 |
| P72 | 31 |

合计 **372 条修订分布在 28 个段落**（作者 `Lin` 333 / `jinxq` 39）。

碎片本身是**字符级**的，例如 P94 的 insert 文本：
`'a2+/Cu2+'`、`'hiMA溶液'`、`'.5'`、`'7.6'`、`'6.'`、`'1%'`、`'219'`。

> 注：`'a2+/Cu2+'` 是 `Cu^{2+}` 在修订切分中被斩断的结果 —— 上下标标记与修订模型未联合设计（见 §4）。

### 1.2 引擎自己的投影已产出病句

`20260721三个专利/_review/zhijia/typed.md` 第 115 行实际存在：

```
所述所述
所述的为、为、
其质量分数为0.1%～0.5%；所述缓冲液为；
```

这不是用户输入，是 review 层投影产物。全目录 `grep -c 所述所述`（*.md）= 12。

### 1.3 用户已学会「不信任工具输出的文本」

session `01a0770f`（2026-09-08），用户原话：

> 当前检索层会把删除内容和新增内容拼在一起，所以像"68%～102%""碱性凝光固浴化""环氧乙烷辐照"这些，我**没有当成你最终正文里的真实表述**

`碱性凝光固浴化` 在 session `01a0770f` 与 `019f8514` 中均出现（`凝光固` 命中 4 次）。
用户必须拿旧版清洁正文手动对照增删关系，才能判断真实内容。

### 1.4 自锁：引擎拆碎，随后拒绝在碎片上编辑

`edit-span-crosses-revision-boundary` 是最高频补丁拒绝码（18 次）。其报错自认机制缺陷：

```
P5: old is visible in the paragraph but spans 3 revision-control boundary marker(s)
(insert-end, insert-start, revision-gap); text on the two sides is adjacent in the
plain view yet is not one editable span.
```

源码定位：`scripts/mcp_server.py:2155 _span_crosses_boundary` → `2213` 抛出该码。

同样症状也污染了逐字匹配：session `01a088e1` 出现
`text-not-found`：`P12: text '打印速率为5～20 mm/s，所得孔径为200～500 μm' not found at the target offset`
—— 文字确实在文档里，但因修订边界切开而匹配不到。`text-not-found` 全量 25 次。

---

## 2. P0 — 修订模式缺少「语义修订单元」

根因：引擎的修订模型是 **XML 级**（`w:id` 的 insert/delete 区间），而专利改写的真实单元是**句子/短语级**。
用户一次「按老师模板重写这句话」的操作，被记录成几十个字符级增删。

证据（用户原话，session `01a088e1` / `01a09dad`）：

- 「这里不是完全修订模式加入的，说明你有些直接没用修订模式」
- 「记得用修订模式啊」（多次）
- 相关拒绝码：`revision-text-mutated-in-direct-mode`（2）、`edit-mode-ambiguous`（1）

**修复方向**：为 `track=true` 引入「合并为单个 insert/delete 对」的语义改写原语 ——
一次 `document_patch` 的**一个 hunk** 应产出一个连贯插入 + 一个连贯删除，而非逐字符 diff。
这是从根上消除 §1.1/§1.2 文本污染的**唯一**办法。

---

## 3. P1 — generation × 全量 ledger 复制导致存储/历史失控

### 3.1 实测数据

- 3 个专利 → **35 个 workspace**（同心圆一个 16 个：`workspace_review_0821`、`_abbr`、`_abbr_final`、`_tracked`、`_tracked_final`、`_python_restore_0821`、`_clean`…）
- 单 workspace **211 个 generation**
- 全专利区 **3,769 个 generation 目录**、75 个 `.docx2typed-store`
- `20260721三个专利` = **2.0 GB**；同心圆单 workspace store = 67 MB

### 3.2 根因：每个 generation 复制整份 operation-ledger

`workspace_review_0821_abbr/.docx2typed-store/generations/<gid>/` 内含
`operation-ledger.json`（370–377 KB）、`_template.docx`（44 KB）、`edit.md`、`islands.json`（59 KB）等全套投影。

- 单 workspace 210 份 ledger，合计 **38 MB**（`du -ch .../operation-ledger.json`）
- 一个 generation 的 evidence：`operation: replace_text, operation_id: abbr-160`
  → **每次 `replace_text` 调用沉淀一个完整 generation**

即：把「每次原子操作」都固化为带全套快照的目录，且 ledger 被反复整份复制。

### 3.3 副作用：证据冗余但验证稀缺

| 产物 | 数量 |
|---|---|
| `.evidence.json`（mutation） | **1,146** |
| `verify.evidence.json` | **49**（占 4%） |
| 外部可见 `operation-ledger.json` | **3** |

SKILL.md 要求交付闭环：`clean → build_docx → verify_output → Word check`。
真实数据里 build/mutation 遍地，独立 verify 只有 4%，且 ledger 全埋在 generation 内部、外部不可见。

**结论**：真实工作流**没有走在工具设计的交付路径上**，而是把工具当成「能保住格式的编辑器」。
专利流程里真正的「验证」是**老师评审 + 用户逐条核对批注**（人工、语义），不是引擎的字节级 verify。

---

## 4. P1 — 上下标 × 修订未联合设计

关键词命中（全 session 文本）：

| 关键词 | 次数 |
|---|---|
| 下标 | 626 |
| 上下标 | 491 |
| 上标 | 383 |
| superscript | 286 |
| subscript | 196 |
| vertAlign | 171 |

用户原话：
- 「需要做 word 的上标，但是我们的 docx2typed 好像没考虑这个问题是吧」（`019f8514`，7 月）
- 「现在上下标是不是变成 `^{}` 这种格式了」（`01a09dad`，9 月）
- 「有一些文字也上标了，然后该上标的没上标」（`01a09dad`）

即便 9 月引入 `^{…}`/`_{…}`，**实际仍在上标错误上反复返工**；
且标记在修订切分中被斩断（§1.1 的 `'a2+/Cu2+'`）。

---

## 5. P0（最高杠杆）— 引擎已有原语，agent 却手写代码实现

### 5.1 规模

扫描 9 个 session 中 agent 自己写的 `eval`/`bash` 代码：

**手写 OOXML/zip 操作的代码块：727 次**

| 用途分类 | 次数 |
|---|---|
| write-docx | 161 |
| **read-comments** | **153** |
| audit/count | 144 |
| unpack | 100 |
| compare-versions | 87 |
| sub/superscript | 85 |
| inspect-revisions | 69 |
| normalize-text | 20 |
| mutate-rPr | 13 |

同时 agent 用 `write` 工具写了 **86 个 `.py` 脚本**（`build_docx.py`×9、`build_raw.py`×3、`audit_align.py`×3、`fix_format.py`、`strip_comments.py`、`_extract_raw.py`…）。

### 5.2 决定性对照：`list_comments` 早已存在，且问题随时间**恶化**

引擎**确实提供** `list_comments()`（`scripts/mcp_server.py:6489`，返回 id/author/date/text/
`anchor_paragraphs`/`anchored_text`，并明确写着「parsing `word/comments.xml` by hand
loses the anchor mapping」）与 `get_comment()`（`6503`）。

git 溯源（`scripts/mcp_server.py`）：

```
d7d75da  2026-08-08 17:17  Skill/MCP: comment-first-class tools, verify evidence, comment-kept contract
```

即 `list_comments` 自 **2026-08-08** 起就可用。但按日期统计手写批注读取 vs 引擎调用：

| 日期 | 手写 zip 读 comments | 调用引擎 |
|---|---|---|
| 2026-09-01 | **41** | 3 |
| 2026-09-06 | 6 | 2 |
| 2026-09-08 | **13** | 0 |
| 2026-09-10 | 6 | 2 |
| 2026-09-29 | 8 | 1 |
| 2026-09-30 | 7 | 0 |
| 2026-10-07 | 3 | 0 |
| 2026-10-10 | 3 | 0 |

汇总：

| 时段 | 手写 | 引擎调用 |
|---|---|---|
| 08-08 **之前** | 10 | 5 |
| 08-08 **之后**（原语已存在） | **87** | 17 |

> **97 次手写中有 87 次发生在 `list_comments` 已存在之后。**
> 这不是「能力缺失」，而是**能力被发现/被采用的失败**，且随会话演进而**恶化**（9/01 单日 41 次）。

手写代码样本（session，逐字）：

```python
import zipfile, re
from xml.etree import ElementTree as ET
z = zipfile.ZipFile(path)
com = ET.fromstring(z.read('word/comments.xml'))
for c in com.findall('w:comment', ns):
    cid = c.get('{...}id'); author = c.get('{...}author') ...
```

### 5.3 根因诊断（已从「能力缺失」修正为「采用失败」）

`list_comments` 的输出形态**其实已经够用**（含 `anchored_text` + `anchor_paragraphs`），
因此上一版「输出形态不匹配」的假设**被证伪**。真实根因是三层：

1. **路由缺失（主因）**：SKILL.md 的 fast-path router 表（第 20–31 行）中，
   「读批注/看修订」只被指向 `references/review.md`（需额外加载），
   **没有一条「意图 → `list_comments`」的直达条目**。agent 在编辑路径上默认走
   「解 zip」这一通用动作，而不会为了读批注去加载一个额外 reference。
2. **通用动作赢过专用工具**：`zipfile`+`ET` 对 LLM 而言是**高熟悉度、零发现成本**的路径；
   专用 MCP 工具若不在一级路由上，就会被绕过。
3. **缺「回修一站式视图」**：真实任务需要「批注 + 锚定句 + **该段是否有未决修订** + 归属作者」
   四者合一的视图（§1 的碎片修订会切断锚定句），当前 `list_comments` 不含「段落修订状态」，
   agent 于是用 zip 自己拼 —— 这是**唯一**确实存在的能力缺口。

### 5.4 优化方向（最高杠杆）

> 原则：**把确定性、机械、高频的读取/审计工作，从 agent 的「思考 + 写代码」收回到引擎的一次调用**。
> 并且必须**把工具放进一级路由**，否则再强的原语也会被 zipfile 绕过（§5.2 已证）。

按已证缺口排序：

1. **路由修补（成本最低、收益最高）**：在 SKILL.md router 表增加直达行
   `读批注 / 老师批注回修 → list_comments → 逐条 get_comment`；
   在 `references/editing.md` 的「Route by intent」同样登记。
   —— 单此一项即可覆盖 §5.2 的 87 次绕过。
2. **`comments_digest`**：在 `list_comments` 基础上补「锚定段落是否含未决修订、
   修订作者、锚定句的**合并后**连贯文本」—— 补齐 §5.3(3) 唯一真实缺口。
3. **`revisions_digest`**：每段落返回「合并后的连贯文本 + 增删计数 + 作者」
   —— 替代 69 次手写 revisions 解析，并为 §1/§2 的语义合并铺路。
4. **`document_compare`**：两版本结构化差异 —— 替代 87 次手写 compare。
5. **`sub_superscript_audit`**：列出应上/下标未标记、及误标位置 —— 替代 85 次手写检查。

**可验收判据**（回归基线）：

| 指标 | 现状 | 目标 |
|---|---|---|
| 手写 `comments.xml` 读取 | 87 次（8-08 后） | → 0 |
| `list_comments` 调用 | 17 次 | → 80+ |
| 手写 OOXML/zip 代码块 | 727 | → < 50 |

**方法论结论**：衡量一个 MCP 原语是否成功，不能看它「是否存在」，要看
**它在一级路由上是否比通用动作（zipfile/ET）更先被发现**。§5.2 的曲线说明
当前的 skill 路由设计不足以让专用工具赢过通用动作。

---

## 5A. 关键细分：手写代码里「工具缺失」vs「工具被绕过」（决定性）

§5 说「727 次手写」过于笼统。把每个手写块按其**所需能力**与**该能力的上线日期**对齐后，结论更精确：

### 5A.1 工具上线时间线（`git log -S`，`scripts/mcp_server.py`）

| 能力 | 上线日期 |
|---|---|
| `insert_paragraph` / `delete_paragraph` / `diff_preview` / `verify_output` | 2026-08-06 |
| `list_comments` / `get_comment` | 2026-08-08 |
| `review_preflight` | 2026-08-09 |
| `document_patch` | 2026-09-07 |
| `document_replace` / `format_span` | 2026-09-10 |
| `document_read(view="issues")`（确定性文档检查） | 2026-09-10 |
| `^{…}` / `_{…}` 上下标标记 | 2026-09-14 |

> 注意：`document_patch` 直到 **9-07** 才存在，`^{}` 直到 **9-14**。
> 即这套工具面**本身就是被这批专利使用「逼」出来的**——早期手写代码中相当一部分是「当时确实没有原语」。

### 5A.2 按能力归因（对齐日期后）

| 需要的能力 | 绕过**已存在**的工具 | 工具**当时尚未**上线 |
|---|---|---|
| 读批注 comments | **142** | 28 |
| 读修订 revisions | **78** | 4 |
| 上下标 subsup | 15 | **73** |
| 合计（可归因部分） | **235** | 105 |

**结论**：
- 「工具当时没有」= 105 次 → 属于**正常的能力演进**，已被后续上线消化（如 `^{}` 覆盖了 subsup 的 73 次）。
- 「绕过了已存在的工具」= **235 次** → 属于**真正的采用失败**，是可直接优化的对象。
- 剩余 ~686 次落在「无对应原语」的读取/枚举类（`z.namelist()` 打印包结构、遍历 `w:p`、计数）——
  这些**引擎确实没有对应工具**（见 §5A.3），属于**真实能力缺口**。

### 5A.3 已确认的**真实能力缺口**（引擎没有对应原语）

1. **包/部件级自省**：`zipfile.namelist()` + 打印 part 树 → 引擎无「列出包部件」工具。
   （`document_read(view="capabilities")` 只报文档能力，不报 package parts。）
2. **段落遍历/计数视图**：大量 `for p in ...w:p` + `len()` → 引擎无「段落统计/密度」工具。
3. **整文档结构审计**：需要「全文 X 出现次数、哪些段缺 Y」的**一次性报告**；
   `view="issues"` 只覆盖 4 类固定检查（见 §5A.4），不足以覆盖用户的临时审计需求。
4. **模板填充（template-fill）**：见 §5A.5，**无任何原语**，只能手写 `deepcopy` + body 拼接。
5. **版本对照**：`history_diff` 只对比 Version；用户要对比的是**两个外部 DOCX 文件**（老师返回版 vs 我改版），引擎无此工具。

### 5A.4 最刺眼的一处：`view="issues"` 存在却无人知晓

`document_read(view="issues")`（`mcp_server.py:3154 _document_issues`，2026-09-10 上线）
**已在做**「缺上标、标点全半角混用、全称重复定义、批注锚点落入删除区」这 4 类确定性检查，
并为每项附 `paragraph_id` 与可发送的 `fix`——这正是 agent 手写 144 次 audit + 85 次 subsup 检查想做的事。

但：

```
grep -rn "issues" SKILL.md references/editing.md   →  0 命中
```

**这个工具在 agent 所依赖的路由文档里完全不存在。** 它不是「能力不足」，是**根本没被写进 agent 的视野**。

### 5A.5 真实缺口之一：模板填充没有原语

证据（session `019f8514`，2026-07-24，手写 12 次 docx repack）：把「老师模板」的批注/页眉/样式/骨架原样保留，
只把正文段落替换成本案内容。agent 只能手写：

```python
with ZipFile(teacher_path,'r') as zt, ZipFile(v10_word,'r') as zs:
    t_body = etree.fromstring(zt.read('word/document.xml')).find('.//w:body', ns)
    s_body = etree.fromstring(zs.read('word/document.xml')).find('.//w:body', ns)
    keep = list(t_body)[:list(t_body).index(t_paras[6])+1]   # 老师前半段原文保留
    new  = keep + [deepcopy(t_paras[7]), deepcopy(t_paras[9])] + [deepcopy(p) for p in s_paras[9:]]
    for c in list(t_body): t_body.remove(c)
    for c in new: t_body.append(c)
```

而且这段代码还在**顺带修引擎的两个 bug**（见 §5A.6）。这是一个明确、可产品化、高频（用户每次要用老师模板都要做）的缺口。

### 5A.6 手写代码里暴露出的引擎自身缺陷（已定位到源码行）

#### (a) 已证实的引擎 bug：`vertAlign` 合成时**盲插到 rPr 末尾**

> ⚠️ 上一版报告在这里的表述不准确（说「agent 把 vertAlign 移到首位是修复」）。
> 经源码核验：**agent 的「移到 index 0」是错的**，但**底层确实存在一个真实的引擎 bug**。两者都要修。

**引擎有两套 rPr 写入逻辑，一套正确、一套错误：**

| 位置 | 行为 | 是否正确 |
|---|---|---|
| `mcp_server.py:457 _RPR_ORDER` + `:506` 按索引插入（`format_span` 路径） | 用 38 项 CT_RPr schema 顺序表插入 | ✅ 正确 |
| `typed_core.py:433 vertical_style_variant`（`^{…}`/`_{…}` 合成路径） | `root.append(ET.Element(w("vertAlign"), ...))` **盲加到末尾** | ❌ **违反 schema** |

**CT_RPr 序列要求** `vertAlign`(idx 31) **必须早于** `lang`(35)、`rtl`(32)、`eastAsianLayout`、`specVanish`、`oMath`；
也晚于 `sz`/`szCs`/`highlight`/`u`/`shd`。

**复现**（基样式 rPr 以 `w:lang` 结尾时）：

```xml
输入： <w:rPr><w:rFonts .../><w:sz w:val="21"/><w:szCs w:val="21"/><w:lang .../></w:rPr>
append 后： ...<w:lang .../><w:vertAlign w:val="superscript"/>   ← vertAlign 落在 lang 之后
```

**影响范围（已修正）**：

> ⚠️ 本节初版曾写「55 个以 `sz`/`szCs` 结尾的样式会产出非法 XML」——**该判断错误**。
> 按 CT_RPr 序列，`sz`/`szCs` **必须早于** `vertAlign`，因此以它们结尾时追加是**合法的**。
> 只有基样式**包含位置晚于 `vertAlign` 的元素**（`rtl`/`cs`/`em`/`lang`/`eastAsianLayout`/`specVanish`/`oMath`/`rPrChange`）
> 时，追加才违法。

重新统计（`专利区/**/styles.json` 全量）：

| 指标 | 数量 |
|---|---|
| 不含 vertAlign 的样式总数 | 1,244 |
| 其中包含 `w:lang` 的样式 | **4** |
| 其中「包含晚于 vertAlign 的元素」的样式（= 真正会触发） | **4** |

4 个触发样式全部是 `<w:rFonts/><w:lang/>` 这一形状（位于 `_review/weiqiu`、`_review/zhijia`）。
即：**缺陷真实且可达，但在本语料中触发面窄（4/1244）**——属**潜伏缺陷**，
一旦 `^{…}` 命中这些样式的文字即产出非法 XML。

**精确触发条件（已闭环）**：

对触发样式 `s_57d5352dc19a523b`（形状 `<w:rFonts/><w:lang/>`）逐项核查：

| 检查 | 结果 |
|---|---|
| 在 `_review/zhijia/typed.md` 中被引用 | **104 次** |
| 这些 span 的实际文本 | 全部为标点（`（`/`）`/`-`） |
| 文件中 `^{…}` / `_{…}` 标记数 | **0** |
| 含元素电荷（superscript 候选）的 span 数 | **0** |

→ **缺陷真实可达（104 处样式引用），但在本语料中从未实际触发**（无上下标标记、无候选电荷）。
这是一个**潜伏缺陷**：只要将来对这类样式的文字加 `^{2+}`，就会立即产出非法 XML。
因此该修复属于**预防性根治**，而非对已损坏文件的事后修补——这也解释了为何用户未报告 Word 打开失败。

**为什么测试没拦住**：`tests/test_vertical_style.py` 只断言
`assert "w:vertAlign" in xml`，**从不校验子元素顺序**。

**修复状态：已实施（2026-10-10）**

| 项 | 内容 |
|---|---|
| 根因 | `scripts/typed_core.py` 的 `vertical_style_variant` 用 `root.append(...)` 盲插 |
| 修复 | 抽出 `_RPR_ORDER` / `_RPR_POSITION` / `insert_rpr_child()` 为**共享 schema 顺序原语**；`append` → `insert_rpr_child` |
| 统一 | `mcp_server.py` 原先自带的 `_RPR_ORDER` 副本改为**从 `typed_core` 导入**（真·单一来源），并保留其 lxml 专用插入逻辑 |
| 兼容 | 相对导入放入既有 dual-mode（包/直脚本）import 块，保住 `test_direct_script_fallback` |
| 测试 | 新增 `tests/test_rpr_schema_order.py`（10 例，三层断言） |
| 回归 | 全量 `636 passed`（修复前 1 failed） |

**不变量**：`insert_rpr_child` **只移动被插入的那个元素**，绝不重排既有子元素——
符合 docx2typed「最小修改、最大保真」原则。

#### (b) 待闭环：重复 `w:pPr`

手写脚本中还有 `for extra in p.findall('w:pPr',ns)[1:]: p.remove(extra)`。
这说明存在重复 `pPr`，但**尚无证据表明它导致 Word 打开失败**。
按「先止损、后归因」原则：需保留修复前文件 + 记录验证器输出，**完成缺陷闭环后再改**，不要凭猜测修。


### 5A.7 违反自身安全不变量的规模

SKILL.md 「Hard safety invariants」第 1 条：**NEVER modify a DOCX outside docx2typed's mutation/build path**。

实测（专利 session 内 `ZipFile(... 'w')` + `writestr` 且目标为 `.docx`）：

| 时段 | docx repack-write 次数 |
|---|---|
| `document_patch` 上线(9-07)**之前** | 56 |
| 之后 | **45** |

合计 **101 次**。9-01 单日 20 次、9-08 单日 **29** 次。即：**即便引擎的编辑原语已齐备，agent 仍在大量直接改包**。

> 这是比「绕过 `list_comments`」严重得多的问题：读批注绕过只损失效率，
> **写包绕过直接违反保真契约**，且用户拿到的 docx 可能已脱离引擎的 verify 覆盖范围。

### 5A.8 因此，优化不能只是「加路由」

按上述证据，三类问题需要**三种不同手段**：

| 类型 | 证据 | 手段 |
|---|---|---|
| **A. 工具已存在但不可发现** | `view="issues"` 0 命中；`list_comments` 后仍手写 142 次 | **skill/route 优化**（低成本，但必须做） |
| **B. 工具不存在** | 模板填充、包部件自省、段落统计、双文件对照 | **新增工具** |
| **C. 工具存在但不够用/有 bug** | docx repack 45 次在 9-07 后；重复 pPr；vertAlign 顺序 | **修引擎 + 提高原语表达力** |

**只做 A 会留下 B 和 C；只做 B 会继续被 A 浪费。** 必须按 §5A.2 的归因分别处理。

---
## 6. P2 — review 控制台通道与 workspace 语义

- `agent-preflight-required` **6 次**：`reject_revision` 被拒，要求先走
  `review_preflight → review_inbox → review_apply_*` 全链路（session `01a0eb60`，2026-09-30）
- `unsupported-decisions-schema` 8 次
- `workspace-unbound` 8 次、`workspace-adoption-required` 1 次、`source-modified-outside-engine` 1 次
- 用户需自命名 `workspace_0914_authorfix` / `_figfix` / `_批注回修` / `_V1备份` 来区分 →
  workspace 的语义/寻址不足以让用户信任「一个文档一个工作区」
- 用户 session `01a02205` 专门问「你看一下 skill 怎么开这个 review 窗口的」→ 启用路径对 agent 不清晰

---

## 7. 全量拒绝码统计（用于回归基线）

| 拒绝码 | 次数 |
|---|---|
| text-not-found | 25 |
| edit-span-crosses-revision-boundary | 18 |
| stale-document-view | 12 |
| workdir-invalid | 10 |
| unsupported-decisions-schema | 8 |
| workspace-unbound | 8 |
| edit-span-crosses-protected-marker | 7 |
| agent-preflight-required | 6 |
| text-ambiguous | 5 |
| region-out-of-range | 5 |
| paragraph-not-found | 5 |
| patch-hunks-invalid | 3 |
| revision-text-mutated-in-direct-mode | 2 |
| ambiguous-alignment | 2 |
| output-path-reserved | 2 |
| placeholder-in-edit-span / edit-mode-ambiguous / font_portability / transition-not-reconstructed / source-modified-outside-engine / workspace-adoption-required | 各 1 |

---

## 8. 优先级汇总

| 优先级 | 问题 | 关键证据 | 修复杠杆 |
|---|---|---|---|
| **P0** | **专用原语被通用动作（zipfile）绕过** | `list_comments` 存在后仍手写 87 次；总手写 727 次 | ①SKILL 一级路由登记 ②digest 类原语 |
| **P0** | 修订碎片化 + 投影污染 | 单段 47 修订；`所述所述`；`碱性凝光固浴化` | hunk 级语义 insert/delete 合并 |
| **P0** | 修订边界自锁 | `edit-span-crosses-revision-boundary` 18 次 | 同上（根治后自动消失） |
| **P1** | generation × 全量 ledger | 210 份 ledger；38 MB；总 2.0 GB | ledger 增量/外置；generation 去重 |
| **P1** | verify 闭环名存实亡 | 1146 mutation vs 49 verify | 让 verify 适配「人工核对」真实流程 |
| **P1** | 上下标 × 修订 | 关键词 1,676 次 | 与 §2 联合设计 |
| **P2** | workspace 泛滥 / review 通道 | 35 workspace；preflight 6 次 | 一 family 一 workspace；通道简化 |

### 顶层判断

1. **保真度已经不是瓶颈**（用户愿意拿真实专利来用，是很高门槛的验证）。
2. **最大的优化方向是「把确定性工作从 agent 收回引擎」**：
   凡「读批注 / 看修订 / 比版本 / 查上下标 / 解 OOXML」这类机械高频动作，
   都应由一条 MCP 调用完成，且**必须出现在一级路由表**上。
   否则 agent 会用最熟悉的通用动作（`zipfile`+`ET`）绕过它 —— §5.2 已用 87 次实测证明。
3. **其次是「修订模型语义化」**：当前 XML 级修订模型制造了碎片、污染与自锁；
   这同时是 §1、§2、§4 的共同根因。

---

## 9. 复现清单（供回归）

| 现象 | 复现路径 |
|---|---|
| 碎片修订 | `同心圆版本链/workspace_0914_批注回修/revisions.json` → P94/P52 |
| 投影病句 | `_review/zhijia/typed.md:115`（`所述所述`） |
| ledger 倍增 | `workspace_review_0821_abbr/.docx2typed-store/generations/*/operation-ledger.json`（210 份） |
| 手写批注读 | 任一 session 中 `zipfile + comments.xml` 的 `eval` 调用 |
| 边界拒绝 | `scripts/mcp_server.py:2155` `_span_crosses_boundary`；`2213` 抛码 |
| vertAlign schema bug | `scripts/typed_core.py:433` `root.append(...)`；对照 `mcp_server.py:457 _RPR_ORDER` |
| 测试漏网 | `tests/test_vertical_style.py` 仅 `assert "w:vertAlign" in xml`，无顺序断言 |

---

## 10. 方法论修正与架构评估（回应外部评审）

本节回应一份外部架构评审。评审指出本报告存在方法论与架构问题，以下逐条核实并采纳。

### 10.1 统计口径必须重建（成立，采纳）

本报告混用了多个不同口径的数字，**不可相加**：

| 数字 | 实际口径 |
|---|---|
| 727 | 手写 OOXML/zip **代码块**数 |
| 235 | 按能力上线日期归因的**绕过事件**数 |
| 105 | 所需能力**当时未上线**的事件数 |
| ~686 | 「无对应原语」的读取/自省**代码块**数（含非专利 session 噪声） |
| 87 / 142 | §5.2 与 §5A.2 的 batch 读取，**统计范围不同** |

一个代码块可能对应多个能力需求，一次能力需求可能对应多次调用。必须建立**四种独立计数**：

| 统计单位 | 回答的问题 |
|---|---|
| User task | 用户实际要完成多少项任务 |
| Agent action | Agent 实际执行了多少次动作 |
| Capability demand | 每次动作需要哪些能力 |
| Failure / bypass event | 哪里出现失败、重试或绕过 |

> **修正**：§5/§5A 的绝对数字应视为**量级指示**，不作精确指标；正式指标须按四分法重建（见 §10.4）。

### 10.2 「修订碎片化」的修复定义需要放宽（成立，采纳）

原报告 §2 建议「一个 hunk 产出**一组**连续 insert/delete」。评审指出这会丢失 run 级格式或错误合并作者。

**修正后定义**：

> 一个**用户意图**对应一个**逻辑修订单元**（`change_group_id`）；
> 该单元在底层**可以包含多个带格式的 run、以及必要的多个 OOXML 节点**。

**逻辑层合并，物理层允许异构**；不能机械地把多个 run 压成一个平坦 `w:ins`。

### 10.3 值得借鉴的既有设计（成立，采纳）

| 项目 | 借鉴机制 | 对应问题 |
|---|---|---|
| **ProseMirror** | Transaction / Step / **Mapping**：一次编辑同时产出结果 + 变更步骤 + 旧→新位置映射 | §1；批注锚点应随事务移动而非事后重搜 |
| **CKEditor 5** | 按作者/类型/**tracking session** 分组建议 | §2 语义修订单元 |
| **Open XML SDK** | `OpenXmlValidator` 独立结构验证 | §10.5 |
| **LSP** | Diagnostics 与 **Code Action** 关联 | `view="issues"` 的 `fix` 应升级为可预览/可执行/带前置条件 |
| **Open-Xml-PowerTools** | `WmlComparer`（双文档比较生成修订）、`DocumentBuilder`/`DocumentAssembler`（装配） | §5A.5 模板填充；§5A.3 双文件对照 |

优先级（评审）：ProseMirror 最有架构价值，CKEditor 5 最贴近当前问题，Open XML SDK 最适合补验证。

### 10.4 用 9 个真实 session 建立 Agent 回归基准（成立，采纳——本报告最有价值的延展）

比「让 `list_comments` 从 17 涨到 80」更重要的，是把真实 session 提取成**可复现任务集**。

先选 30–50 个代表性任务，覆盖：读批注、句子回修、上下标、模板填充、版本对照、修订冲突、交付验证。

| 指标 | 期望方向 |
|---|---|
| 任务一次完成率 | ↑ |
| 绕过专用工具比例 | ↓ |
| 常规流程手写 OOXML 次数 | → 0 |
| 无效重试 / 拒绝码次数 | ↓ |
| 原始文件意外修改次数 | **0** |
| 未授权内容/格式变化 | **0** |
| 发布件验证覆盖率 | **100%** |

**关键实验：控制变量法**——同一 Agent/模型/文档/任务/工具版本，
① 旧 skill ② 仅替换一级路由与工具描述，对比工具选择行为与成功率。
才能区分「**不知道工具存在**」与「**知道但不好用**」。

> ⚠️ 对新能力须做**历史任务重放**，不能把「历史上工具尚未上线」导致的手写计入新版缺陷（即 §5A.2 的 105 次须剔除）。

### 10.5 验证层须从「格式验证」升级为「语义契约验证」（成立，采纳）

| 验证 | 重点 |
|---|---|
| Schema validation | OOXML 节点合法、包关系有效（对应 §5A.6） |
| Structural preservation | 页眉页脚/样式/图片/批注等非目标内容未被异常改变 |
| Revision consistency | 接受/拒绝投影正确，修订未丢失或串联 |
| Semantic QA | 重复词、错误上下标、术语、参数遗漏 |

Semantic QA 须**区分确定性规则与启发式建议**：格式错误可自动修复；
「某化学式是否应上标」需学科知识，**不应无条件自动改写**。

**投影一致性测试**（比搜索 `所述所述` 更接近根治）：
> 对同一输入，先算 `final` 投影；再在隔离副本中接受全部修订后提取正文，**两者必须一致**。`original` 同理。
> 超出支持范围的对象应显式标记 `unverified`，而非假装通过。

### 10.6 更正：1146 vs 49 ≠「4% 验证率」（成立，采纳）

mutation 与 verify 证据**不是同一级别的事件**，不能直接相除。
正确指标是**按「实际发布的文档版本」统计验证覆盖率**。

### 10.7 工具面应收敛而非扩张（部分成立，需权衡）

评审建议收敛为 5 入口：`document_inspect` / `compare` / `plan` / `apply` / `verify`。

**采纳理由**：工具越多，模型选择空间与上下文开销越大；一级路由应回答「这类任务从哪个入口进」。

**补充**：`view="issues"` 的证据说明问题**不在「工具太多」而在「工具没进路由」**——
收敛接口时**必须同步修路由**，否则新入口同样被 zipfile 绕过。
另需核验工具名/描述/schema 与 Agent 实际可见列表一致；`readOnlyHint` 只是提示，**不构成安全边界**。

### 10.8 安全必须从提示词约束升级为工程约束（成立，采纳）

> 只要 Agent 有任意执行 Python 的权限，SKILL.md 写一百遍「禁止改 DOCX」也拦不住 §5A.7 的 101 次 repack。

```
原始输入不可变 → 修改写入暂存区 → 验证暂存件 → 仅由受信发布器交付
```

环境允许则用文件权限/隔离限制写源文件；**无法限制时至少做到**：
未经引擎登记与验证的 DOCX，**不得被标记为可信交付件**。

### 10.9 架构方向：从「工具集」升级为「文档编辑引擎」（采纳，长期目标）

评审的四断点（发现/表达/语义/验证）与本报告 §5A.8 三分类**同构但更完整**：
B 类应拆为「表达断点」与「语义断点」。

内核五阶段（**内部阶段，不必然是 MCP 工具**）：

```
INSPECT → PLAN → APPLY → VERIFY → PUBLISH
（失败不得发布；原始文件保持不变）
```

两个**必须优先建成的基础**：
1. **稳定文本投影 + Source Map**：`original`/`final`/`markup` 三视图 +
   节点引用/范围映射/修订组/批注锚/保护区间。
   **这是修订语义化、跨修订编辑、批注定位、文档比较的共同地基**。
2. **DOCX 安全内核**：XML 子元素插入、修订生成、包关系、发布验证统一收敛，先修 §5A.6。

**边界原则**：
> 专利写作 skill 负责内容/专利逻辑/结构规范/证据充分性；
> docx2typed 负责定位/格式/修订/版本/验证/交付；两者通过**结构化编辑计划**衔接。
> 专利是 docx2typed 的**首个高强度验证场景**，不应让它变成只能处理专利的特殊引擎。

### 10.10 修正后的优先级

| 优先级 | 事项 | 验收标准 |
|---|---|---|
| **P0-A** | 修 §5A.6 `vertAlign` schema bug + 原始样本回归 | 样本 schema 错误 0，Word 正常打开 |
| **P0-B** | 可信交付门禁（§10.8） | 未验证的直接写包**不得**成为可信产物 |
| **P0-C** | 修 `original`/`final` 投影与修订映射 | 投影与「接受/拒绝全部修订」结果一致 |
| **P0-D** | 一级路由与工具描述修补 | 同任务下专用工具选择正确率 ↑ |
| **P1-A** | 语义修订事务（§10.2 定义） | 一次句子级操作 = 一个逻辑修订组，格式/作者完整 |
| **P1-B** | 回修统一视图、双文件对照 | 不手写 XML 即可完成典型批注回修 |
| **P1-C** | 存储优化（append-only log + 快照 + 内容寻址） | ledger 增量化 |
| **P2** | 模板装配、工作流编排 | 完整老师模板回修端到端测试 |

### 10.11 尚需验证的评审推断（不盲从）

1. 重复 `w:pPr` 是否**确实**导致 Word 打开失败——缺验证器输出（§5A.6b），须先闭环再改。
2. 需确认 `_review/zhijia/styles.json` 中 `<vertAlign/><w:lang/>` 来自 `_RPR_ORDER` 路径（合法）
   还是 `append` 路径（非法），以界定 bug 真实影响面。
3. 一级工具收敛对 Agent 选择行为的**实际影响**，须用 §10.4 控制变量实验验证，而非假设。
