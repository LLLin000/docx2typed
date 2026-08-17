# docx2typed

[English version](https://github.com/LLLin000/docx2typed-typed-mode/blob/main/README.md) · [安装与协作指南](https://github.com/LLLin000/docx2typed-typed-mode/blob/main/Installation.md)

> 结构保真的 DOCX 编辑工具，提供浏览器审阅界面和 Agent 交接流程。

`docx2typed` 可以修改 `.docx` 中的文字，而不会把文档压平成有损的纯文本或 HTML。Agent 工作时，文档格式、批注、修订、表格、内容控件、锚点和未触碰的文档部件都会受到保护。

<p align="center">
  <img src="docs/assets/review-console-revisions.png" alt="docx2typed 审阅控制台展示修订和固定审阅索引" width="100%" style="max-width:100%;height:auto;display:block">
</p>

## 选择使用方式

| 你想要…… | 从这里开始 |
|---|---|
| 在浏览器里审阅文档 | [使用审阅控制台](#使用审阅控制台) |
| 让 Agent 修改文档 | [让 Agent 完成编辑](#让-agent-完成编辑) |
| 安装工具并连接 Agent | [配置 Agent](#配置-agent) |

## 使用审阅控制台

审阅控制台是给人的操作界面。正文以连续页面展示，旁边固定显示修订和批注索引。

### 打开审阅会话

最简单的方式是直接告诉 Agent：

> 请为这个文档打开浏览器审阅会话，先保护原文件，并在生成最终 DOCX 前把审阅地址发给我。

想自己从零开始，任意 `.docx`（用自己的文件即可）：

```bash
docx2typed extract input.docx -o workdir
docx2typed-review workdir --host 127.0.0.1 --port 8876
```

`extract` 会从你的文件创建 typed `workdir`，并且绝不修改原文件。然后在
浏览器打开 <http://127.0.0.1:8876/>。如果只需要静态、只读页面：

```bash
python -m docx2typed.review_console workdir -o review.html
```

### 在页面中审阅

1. 使用 **修订**、**最终**、**原文**，对比修订视图、最终视图和原文视图。
2. 在固定侧栏点击某条修订或批注，正文会跳到对应段落，并保留审阅上下文。
3. 对修订选择 **接受**、**拒绝** 或 **暂缓**，也可以补充审阅意见。
4. 在正文中选中文字，可以 **调整** 文字或 **添加批注** 给 Agent。
5. 在实时服务中保存决定后点击 **发送给 agent**。浏览器只负责排队，Agent 会应用修改并返回新的审阅快照。
6. 在独立 HTML 页面中点击 **导出决策**，下载 `review-decisions.json` 交给 Agent。没有 Agent 时，可以自己应用导出的决策：

```bash
docx2typed decide apply --workdir workdir --file review-decisions.json
```

`accept`/`reject` 条目会逐条应用；**暂缓**（defer）条目会跳过并保留，供之后再次处理（例如 `docx2typed decide accept-all --workdir workdir --output decided.docx --workdir-out decided-wd`）。

浏览器是审阅和交接界面，不会静默重写源 DOCX。Agent 负责应用修改、构建、验证，并完成 Word/LibreOffice 最终检查。

### 手机审阅

需要在私有 Tailscale 网络中短时间协作时：

```bash
docx2typed review workdir --tailscale --port 8876
```

在登录同一 tailnet 的手机上打开命令打印的地址。请只向需要协作的成员开放访问，不要把审阅端口暴露到公网。

<p align="center">
  <img src="docs/assets/review-console-desktop.png" alt="桌面端 docx2typed 审阅控制台展示连续正文和固定审阅索引" width="72%" style="max-width:100%;height:auto;display:block">
</p>

## 让 Agent 完成编辑

把源 DOCX 和你希望得到的结果告诉 Agent。你不需要编辑 `typed.md`、管理修订 ID，也不需要把 skill 文件复制到某个隐藏目录。

可以这样提出请求：

> 请修改 `input.docx`，目标是：[目标]。保持原文件不变。[保留修订 / 直接应用修改]。除非我明确要求，否则保留现有批注。第一轮完成后启动浏览器审阅，等待我的决定，再构建并验证最终 DOCX。返回输出文件路径，并简要说明修改内容以及剩余的批注和修订。

Agent 应该完成：

1. 需要时安装或启用 `docx2typed` skill 及其运行环境。
2. 将源文件复制到新的 workdir，并报告开始时的文档状态。
3. 只执行明确要求的文字修改或表格操作。
4. 启动浏览器审阅控制台，让你检查第一轮结果。
5. 读取你接受、拒绝、暂缓或按文字定位提出的决定。
6. 构建新的 DOCX，独立验证，并完成 Word/LibreOffice 互操作检查。

批注默认保留。需要删除批注时请明确提出。文字长度变化可能导致换行和分页变化，这不等于文档格式被修改。

## 配置 Agent

生产环境只解析签名的 Rust 二进制；Python 包仅保留为离线 Oracle（参照实现），
用于差分资格验证。

### 1. Agent 一句话自动配置（推荐）

把这句话发给 Agent：

> 请安装并启用 `docx2typed` skill，用 `scripts/install_binary.ps1` 安装签名的 `docx2typed` Rust 二进制，为当前宿主配置 MCP，并验证二进制、MCP 服务和 Skill 都已就绪；保留现有 Agent 配置。

这条路径应完成三层配置：

| 层级 | 预期结果 |
|---|---|
| 签名 Rust 二进制 | 已安装的绝对路径上 `docx2typed --version --json` 报告 `docx2typed-rust`。 |
| MCP | 当前宿主已配置使用已安装二进制绝对路径（`args: ["mcp"]`）的 `docx2typed` MCP 入口。 |
| Skill | 当前宿主通过标准 skill 管理器加载 `docx2typed/SKILL.md`。 |

Skill 的位置和宿主配置由 Agent 处理。用户不需要复制 `SKILL.md`，也不需要自己编辑 MCP JSON。具体的 Agent 安装流程见[安装与协作指南](Installation.md)。

### 2. 用户自行安装（Windows，签名 Rust 二进制）

用 receipt-safe 生命周期安装器安装自包含的签名发布二进制：

```powershell
powershell -File scripts/install_binary.ps1 -Action install -Bin target\release\docx2typed.exe
```

安装器以原子方式发布 `bin\docx2typed.exe`、`receipt.json` 和 `mcp.config.json`
（其 `command` 为已安装二进制的绝对路径）。支持 `update` / `rollback` /
`uninstall`，均 receipt-safe。

Python 参照实现仅为离线资格 Oracle：在开发检出中 `python -m pip install -e .`
可为差分测试提供 Oracle。它绝不参与生产 CLI、MCP 或 Skill 配置的解析。

## 能保留什么

| 需求 | 保证 |
|---|---|
| 保护源文件 | 提取和审阅不会覆盖原始 `.docx`。 |
| 保留格式 | 现有样式归属、段落结构、锚点和未触碰的包部件受到保护。 |
| 审阅修改 | Word 修订、批注和按段落跳转仍然可用。 |
| 交付 DOCX | Agent 构建新文件、独立验证，并使用 Word 兼容工具检查结果。 |

这是结构保真的编辑引擎，不是 Microsoft Word 的浏览器替代品。浏览器负责帮助人审阅决定，构建出的 DOCX 才是最终交付文件。

## 延伸阅读

- [安装与协作指南](Installation.md)
- [CLI 与 MCP 能力参考](capabilities.md)
- [端到端工作流](composites.md)
- [验证保证](verification.md)
