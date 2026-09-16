# 外部 Office 接入适配研究

状态：研究结论；外部工具仍不进 core，F0 候选准入边界已落地，OfficeCLI/soffice 资格接入待实机证据。

## 结论先行

应该现在开始做外部接入，但只做一条**工具无关的候选文件准入边界**，不要先为 OfficeCLI、soffice、python-office 各写一套编辑器包装层。

推荐优先级：

1. **OfficeCLI：纳入外部结构编辑器候选**。它的 DOM、结构编辑、原始 XML、校验和 HTML/截图能力最贴近 `foreign-edit` 的目标；命令由 agent/skill 直接调用，engine 负责候选文件、版本、归因、校验、准入。
2. **soffice/LibreOffice：纳入渲染/转换接入；编辑接入后置**。先用 `--headless --convert-to` 产出 PDF/DOCX 证据；UNO 作为需要 Writer/Calc 对象模型时的专用通道。LibreOffice 保存 DOCX 会进入 foreign lane，不能直接写 live workdir。
3. **python-office：不纳入 core writer**。它是 Python 便捷封装，不是稳定的 CLI；其 Word 功能依赖 `poword`，官方代码明确要求 Windows + Microsoft Word。可以作为用户自带的批处理 helper，但输出必须走同一条候选文件准入。
4. **python-docx：保留现有依赖；不当作外部高保真编辑器**。读/生成 fixture/诊断可以继续用；一旦保存了候选 DOCX，应按 foreign output 处理。
5. **openpyxl：暂不进入 DOCX 接入**。它只解决 XLSX/XLSM 等 Excel OOXML，未来若有 spreadsheet workspace 再单独评估。

## 当前机器与仓库事实

- 当前 PATH 中未发现 `officecli` 或 `soffice`；当前 Python 环境未安装 `python-office`、`uno`、`pyoo`。
- 项目已安装 `python-docx==1.2.0`；`openpyxl` 在环境中存在，但不在本项目 `pyproject.toml` 的运行时依赖中。
- 仓库已有 `scripts/office_evidence.py`：按 phase 启动 LibreOffice，使用独立 `-env:UserInstallation=...` profile，执行 PDF/DOCX 转换，并记录原始输出、包校验和与语义 retention；这是资格/互操作证据 harness，不是 workspace ingress。
- 仓库已有 `scripts/qualify_adapters.py` 的 capture-only CLI/soffice 发现逻辑，可复用“只捕获、不解释”的原则，但不应把 qualification runner 直接变成产品编辑器。

## 工具能力盘点

### 1. OfficeCLI

官方定位是单二进制、面向 `.docx`、`.xlsx`、`.pptx` 的 CLI，不要求 Microsoft Office；官方文档把能力分为 L1 读取、L2 DOM、L3 原始 XML。

| 层级/类别 | 主要命令 | 功能 | 对本项目的判断 |
|---|---|---|---|
| 创建/读取 | `create`, `view`, `get`, `query`, `validate` | 创建空文档；查看 outline/stats/issues/text/annotated/html；按路径读节点；CSS-like 查询；Open XML 校验 | `view/get/query/validate` 可作为候选前后的观测与 gate 输入；`create` 不应创建 typed workspace |
| DOM 写入 | `set`, `add`, `remove`, `move`, `swap` | 改属性/文本/格式；插入/克隆元素；删除；移动；交换 | 适合 foreign candidate；只能写版本导出的 scratch 文件 |
| 批处理 | `batch` | 一次加载、多项操作、一次保存；官方 wiki 说明 v1.0.137+ 默认 atomic rollback，`--best-effort` 才保留部分成功 | 默认只允许 atomic；adoption 路径阻止 `--best-effort` |
| 结构/引用 | `refresh` | 重算 TOC、PAGE、交叉引用等 | 可作为候选操作后的明确步骤；仍需 engine 自己 validate/re-extract/verify |
| L3 | `raw`, `raw-set`, `add-part` | 读/改包内 XML，新增 part | 高风险；仅允许显式 part/target allow-list，默认拒绝进入 auto-adopt |
| 可重放 | `dump` | 导出 replayable batch JSON；可对 subtree 或 whole document | 只当观察/实验输入；不能把 dump→batch 当作保真证明 |
| 渲染 | `view html`, `view screenshot`, `view svg`, `view pdf` | HTML/截图/SVG/PDF/form 观测（部分能力由 plugin 提供） | 可提供 before/after 视觉证据；视觉结果不是结构证明 |
| 常驻进程 | `open`, `save`, `close` | 内存中保持文档；`save` flush 但继续 resident；`close` flush 并释放 | 外部 reader 前必须显式 flush；候选未 close/save 时拒绝 re-extract |
| 交互 | `watch`, `unwatch`, `goto`, `get ... selected`, `mark`, `unmark`, `get-marks` | HTML watch、浏览器选择、标记待审改动 | 可由人/agent 使用；不应嵌入 workspace session 或作为 engine 状态 |
| 扩展/接入 | `load_skill`, `plugins`, `mcp`, `config`, `install`, `merge` | 加载领域 skill、插件、MCP 注册、配置/安装、合并 | `mcp` 不应嵌套成 MCP-in-MCP；插件/安装不作为 engine 隐式动作 |

官方 DOCX DOM 文档覆盖 paragraph/run/table/cell/row、style、header/footer、section、bookmark/comment/footnote/endnote、field/TOC/numbering、SDT、drawing/OLE/diagram 等；但 watch 的可选中对象仍有限制，不能把 watch 的 path 当成 typed paragraph ID。

**重要运行时事实：**官方 `open` 文档说明任何首个命令都可能自动启动 resident；resident 内的修改可能只在内存，第三方程序（python-docx、openpyxl、Word、renderer）读磁盘时看不到最新内容。跨到 engine 之前必须 `officecli save` 或 `officecli close`，不能依赖 idle flush。

**版本风险：**当前机器没有该二进制，命令语义必须以实际 `officecli --version`、`help` 和 fixture probe 为准；adapter 记录版本、完整 argv、退出码、stdout/stderr 与输入/输出 hash，并在可复现流水线中 pin 版本或分发物 digest。

### 2. soffice / LibreOffice

LibreOffice 官方命令行文档支持：

| 类别 | 命令/参数 | 功能 | 对本项目的判断 |
|---|---|---|---|
| 发现/诊断 | `--help`, `--version` | 打印帮助/版本 | adapter capability probe 必须做 |
| 无 UI | `--headless`, `--invisible`, `--norestore`, `--nolockcheck` | 无界面运行；关闭恢复；减少实例/锁干扰 | 外部进程默认使用；仍需独立 profile、超时、进程回收 |
| profile | `-env:UserInstallation=...` | 指定非默认用户 profile | 每次 run/phase 用 scratch profile；禁止共享用户 profile |
| 打开/查看 | `--writer`, `--calc`, `--impress`, `--view`, `-o` | 指定组件；只读查看；编辑打开 | `--view` 只观测；`-o` 不得指向 live workdir |
| 转换 | `--convert-to <ext[:filter[:params]]>`, `--outdir` | DOC/DOCX/ODT/HTML/TXT/PDF，以及 XLSX/PPTX 等格式转换 | 首个接入目标：PDF 视觉证据；DOCX resave 只能 foreign candidate |
| 文本/打印 | `--cat`, `--print-to-file` | 文本导出；批量打印到文件 | 只作 read/preview 辅助，不建立 typed state |
| UNO server | `--accept=<UNO-URL>`, `--unaccept` | 监听外部 UNO 客户端；关闭 acceptor | 仅 loopback、随机端口/私有 profile、强制超时；不接受远程暴露 |
| 宏/脚本 | `macro://...` 等 | 启动文件并应用宏 | 默认阻止；不执行不可信文档宏 |

官方 filter 表确认 Writer 有 Word 2007/2010–365 DOCX 与 `writer_pdf_Export`，Calc/Impress 也有对应 OOXML/PDF filters。官方 PDF 参数支持页范围、书签、无障碍、压缩、密码、水印等；这些属于导出配置，不是 DOCX workspace 内容。

UNO 不是 `python-office`。LibreOffice SDK 官方文档提供 Python binding 及 `DocumentConverter`、`DocumentLoader`、`DocumentPrinter`、`DocumentSaver`、文本结构/替换/样式/图形等示例；典型流程是启动 `soffice --accept=socket,host=localhost,port=...;urp;StarOffice.ServiceManager`，然后由 UNO 客户端 load/edit/save。UNO 适合封装少数确实需要应用对象模型的能力，不适合把整个 Writer API 映射成 docx2typed 的第二套编辑 API。

### 3. `python-office` / `poword` / 相关 Python 包

`python-office`（`import office`）是大而全的办公便捷库，官方 README/Skills 索引列出 PDF、Word、Excel、PPT、图像、OCR、文件、视频、微信等几十个 one-line skills。Word 暴露的函数包括：

- `docx2pdf`
- `merge4docx`
- `doc2docx`
- `docx2doc`
- `docx4imgs`

但官方仓库 `office/api/word.py` 的 `_load_poword()` 明确提示：Word 功能依赖 `poword`，且仅支持安装 Microsoft Word 与 `poword` 的 Windows 环境；仓库 `setup.cfg` 也将 `poword` 声明为 Windows 条件依赖。`poword` PyPI 功能表同样只列出转换、合并、PDF、图片提取等少数功能。

因此：

- 它更像用户侧便捷函数集合，不是本项目应依赖的 canonical DOCX editor。
- 官方 packaging/README 没有稳定的 `python-office` CLI 入口说明；适配方式应是受控 Python subprocess 或用户自带 script，而不是猜测一个命令行协议。
- 它的 Word 路径与 `soffice` 不是同一后端，不能因为都叫“office”就互换。
- `merge4docx`、`docx2pdf` 等输出如果被用于 workspace，必须按外部候选文件重新校验、提取、归因；不能直接覆盖 `_template.docx` 或 `typed.md`。

### 4. `python-docx` 与 `openpyxl`

- `python-docx` 官方定位是创建/更新 Microsoft Word `.docx`，提供 paragraph/run/style/table/image/page break 等对象 API；项目已把它作为运行时依赖。它不是本项目的外部工具适配目标；保存后的文件应按外部 resave 处理，不能默认拥有 package/opaque/byte fidelity。
- `openpyxl` 官方定位是读写 Excel 2010 `xlsx/xlsm/xltx/xltm`，提供 Workbook/Worksheet/Cell API；它不处理 DOCX。其官方安全说明还提醒默认不防 XML quadratic blowup/billion laughs，若未来接入不可信 XLSX，需要 defusedxml/文件大小/资源限制。
- `uno` 是 LibreOffice 的 Python 绑定/桥接能力，不等于 `python-office`；当前项目 Python 环境没有 `uno` 包，不能假设 `pip install python-office` 会提供它。

## 应该适配什么

### 一个工具无关的准入边界

适配以下最小边界即可：

1. **能力发现**：工具路径、版本、平台、支持的格式/命令；未安装只返回 capability unavailable，不隐式安装。
2. **受控执行**：`argv` 数组而不是 shell 字符串；cwd、环境、超时、退出码、stdout/stderr、子进程回收全部记录。
3. **候选隔离**：输入只能是已保存版本的 DOCX export；输出写 scratch/用户明确的非保留路径；永不把 live workdir 交给外部工具。
4. **生命周期 flush**：OfficeCLI 必须 `save/close`；soffice/UNO 必须显式 save、dispose、退出并确认文件可读。
5. **证据封套**：记录 family/workspace、base version、候选路径、工具/version、完整 argv、输入/输出 SHA-256、包 member manifest、运行时日志、视觉 artifact。
6. **engine gate**：候选由 engine 自己做 package prevalidation、`extract_workdir`、`validate_workdir`、`verify_workdir`，再做 paragraph/token/opaque/package attribution；不能把外部工具的 `validate` 当成 docx2typed 的 verdict。
7. **准入结果**：安全时将重新提取的 baseline 作为当前 workspace 的下一版本；失败时保留候选与旧 workspace，拒绝，不原地修复。

这与 `docs/prd/foreign-edit.md` 一致：不要包装外部工具命令表；agent/skill 使用工具，engine 只拥有 boundary、evidence、attribution、gate、adoption。

### 默认适配/阻止矩阵

| 能力 | 默认策略 | 原因/条件 |
|---|---|---|
| OfficeCLI `view/get/query/validate` | 适配 | 只读观测、能力探针、候选自检；仍需 engine 自己 gate |
| OfficeCLI `set/add/remove/move/swap` | 适配 | 版本导出上的结构/样式 foreign edit；target 必须可归因 |
| OfficeCLI `batch` | 适配 | 只用默认 atomic；拒绝 adoption 场景的 `--best-effort` |
| OfficeCLI `raw/raw-set/add-part` | 默认阻止 auto-adopt | package/opaque casualty 风险；只有显式 part/target allow-list + 归因可解释时才考虑确认/采用 |
| OfficeCLI `dump→batch` | 实验/观察 | 可重放不等于保真；不得用来证明未触及的 part 没变化 |
| OfficeCLI `view html/screenshot` | 适配为视觉证据 | 可检查布局；不替代 package diff/verify |
| OfficeCLI `open` resident | 适配生命周期 | 进入第三方读取前必须 flush；不得持有 live workspace |
| OfficeCLI `watch/selected/mark` | 工具侧使用，不进 core | 交互状态属于 watch 进程，不属于 workspace timeline |
| soffice `--version/--help` | 适配 | capability probe |
| soffice `--headless --convert-to pdf` | 首选适配 | 低耦合渲染/视觉证据；输出不是 workspace source |
| soffice `--convert-to docx` | foreign candidate | LibreOffice reserialization/filters 可能改写关系、revision、bookmark、part；需完整 retention qualification |
| soffice UNO | 后置适配 | 只封装明确缺口；loopback、独立 profile、随机端口、超时、dispose |
| soffice `macro://`/不可信宏 | 阻止 | 外部代码执行风险 |
| soffice 默认 profile/远程 accept | 阻止 | profile 锁、跨任务污染、远程控制风险 |
| `python-office` Word 写入 | 不进 core；只能外部候选 | Windows + Microsoft Word 条件依赖；功能/版本/包保真不适合作为引擎事实 |
| `python-office` PDF/图片/批处理 | 可选 helper | 只产生外部 artifact；不触碰 workspace canonical state |
| `python-docx` 读 | 保留 | 已是项目依赖，适合解析/fixture/诊断 |
| `python-docx` 保存 | foreign lane | 保存结果须 re-extract/verify/attribution |
| `openpyxl` 处理 DOCX | 阻止 | 格式不匹配 |
| `openpyxl` 处理 XLSX | 暂不纳入 | 项目当前是 DOCX workspace；未来另起 spreadsheet capability |

## 外部结果如何进入 workspace

### 当前可用路径与缺口

当前仓库已经具备：

- `build_docx(version=V)`：从已保存版本导出，未保存 draft 会以 `version-save-required` 拒绝；成功 export 会登记为该 family 的 `managed-export` observation，并记录版本。
- `workspace_registry`：以 exact SHA-256、file object、metadata hint 分级解析 DOCX observation；exact hash 唯一时自动解析，多 family 同 hash 或证据不足时询问。
- `workdir_open(<DOCX>)`：解析到已有 family 后打开其 workspace。
- `workspace_adopt(token, family_id)`：只把一个外部 DOCX observation 绑定到已知 family，并要求 `workdir_open`；它是 lineage adoption，不是“把 changed DOCX 导入当前 canonical state”。
- `workspace_fork(docx, outdir)`：从 DOCX 新建 family/workspace，记录 origin；适合用户明确要另起一份文档。
- `_adopt_baseline` + `publish_current(origin="baseline-transition")`：已有 table/decision lane 在同一 workspace 中采用重新生成的 baseline。

已落地：`foreign_edit_prepare(candidate receipt)` 与 `foreign_edit_adopt(candidate, candidate_id?, consent_token?, family_id?, base_version?, target?)`。手工候选必须显式给出 family 与 base version；`workspace_adopt` 仍是 lineage binding，不是同一件事。

### 推荐端到端流程

```text
workdir_open(workdir)
  → commit_sync（若 draft dirty）
  → foreign_edit_prepare(target, version=V)     # candidate + receipt
  → 外部工具只改 candidate.docx
  → flush/save/close，确认 candidate 落盘
  → foreign_edit_adopt(candidate, candidate_id=FC…)
       → receipt 固定 base；无 receipt 时要求显式 family_id + base_version
       → candidate/receipt freshness 与 HEAD/base 冲突检查
       → engine package prevalidate
       → extract candidate 到 scratch workdir
       → validate + verify
       → deterministic alignment + paragraph/token/opaque/package/section attribution
       → target/scope/conflict/normalization gate，必要时一次 consent
       → safe: _adopt_baseline + publish_current(origin="baseline-transition")
       → unsafe: refuse；旧 workspace 与 candidate 都保留
  → workdir_open 原 workspace，继续 native lane
```

准入规则沿用 PRD：

- `delta ⊆ target`、无 normalization、无 opaque/package surprise：auto-adopt。
- 目标内且只有可解释的附带变化（如目标隐含的 style definition）：一次确认。
- 范围外、不可归因、opaque/package casualty：拒绝，不以“用户确认”覆盖。
- unsaved draft 或同一段落两侧同时变化：`foreign-edit-conflict`，拒绝，不自动 merge。
- 采用成功后仍是同一个 family/workspace timeline，只是 baseline re-root；候选文件继续作为 observation/evidence 保留。
- 用户明确想从候选另起一份文档时，不走 adopt，走 `workspace_fork`，再 `workdir_open` 新 workspace。

## 落地顺序

### Phase 0：只读 capability probe

不安装依赖，不改 workspace：

- `officecli --version`、`officecli help`、`officecli help docx paragraph --json`；对 fixture 执行 `view text/html`、`validate`。
- `soffice --version`、独立 profile 下对 fixture `--headless --convert-to pdf`；记录输出与退出码。
- 运行项目现有 office evidence corpus；把 unavailable 诚实记为 not-run，不伪造 pass。

### Phase 1：OfficeCLI foreign candidate（engine 侧已完成）

`foreign_edit_adopt` 及准入 gate 已落地。用 OfficeCLI 的 `set/add/batch` 覆盖一个可归因结构/样式 fixture，验证：

- 候选输入确实来自 `build_docx(version=V)`；
- resident flush 后 engine 看到的是新文件；
- 未触及内容的 paragraph/opaque/package 没有意外变化；
- adoption 后可继续 `workdir_open`/native edit；
- 冲突、target 外变化、未知 part 变化 fail closed。

### Phase 2：接 soffice 作为 renderer/converter

先只做 PDF/视觉证据。再用已有 qualification harness 测量 DOCX resave 的 retention 规则；未有实机 evidence 时，禁止把 soffice DOCX 保存结果 auto-adopt。

### Phase 3：UNO 专用缺口；python-office 可选插件

只有出现明确能力缺口才写 UNO operation；`python-office` 保持用户侧 helper，不进 `pyproject.toml` core dependencies，也不作为 workspace canonical writer。

## 暂不应做的事

- 不把 live typed workdir 交给任何外部工具。
- 不让外部工具覆盖 `_template.docx`、`typed.md`、`format.json`、`styles.json` 或 store generation。
- 不把 PDF/HTML/截图反向导入 canonical workspace。
- 不先实现 OfficeCLI/soffice/python-office 的全量命令映射。
- 不把 `workspace_adopt` 改成无证据的 changed-DOCX merge。
- 不因工具未安装就自动下载安装或修改项目依赖。

## 官方来源

- OfficeCLI 官方 README：<https://github.com/iOfficeAI/OfficeCLI>
- OfficeCLI 官方 agent skill/命令与能力表：<https://raw.githubusercontent.com/iOfficeAI/OfficeCLI/refs/heads/main/SKILL.md>
- OfficeCLI `open/close` resident 与 flush：<https://github.com/iOfficeAI/OfficeCLI/wiki/command-open>
- OfficeCLI `batch` atomic/`--best-effort`：<https://github.com/iOfficeAI/OfficeCLI/wiki/command-batch>
- LibreOffice 命令行参数：<https://help.libreoffice.org/latest/en-US/text/shared/guide/start_parameters.html>
- LibreOffice 转换 filters：<https://help.libreoffice.org/latest/en-US/text/shared/guide/convertfilters.html>
- LibreOffice PDF CLI 参数：<https://help.libreoffice.org/latest/en-US/text/shared/guide/pdf_params.html>
- LibreOffice SDK API 总览：<https://api.libreoffice.org/>
- LibreOffice 官方 Python/UNO 示例：<https://api.libreoffice.org/examples/examples.html>
- python-office PyPI：<https://pypi.org/project/python-office/>
- python-office 官方仓库：<https://github.com/CoderWanFeng/python-office>
- poword PyPI：<https://pypi.org/project/poword/>
- python-docx 官方文档：<https://python-docx.readthedocs.io/en/latest/>
- openpyxl 官方文档：<https://openpyxl.readthedocs.io/en/stable/>
- 项目外部编辑 PRD：`docs/prd/foreign-edit.md`
- 项目 workspace 参考：`references/workspace.md`
