# docx2typed

[English version](https://github.com/LLLin000/docx2typed-typed-mode/blob/main/README.md) · [安装与协作指南](https://github.com/LLLin000/docx2typed-typed-mode/blob/main/Installation.md)

> 面向智能体、审阅者和开发者的结构保真 DOCX 文本编辑工具。

`docx2typed` 可以修改 `.docx` 中的文字，而不会把文档压平成有损的纯文本或 HTML。它会在 typed workdir 中保留文档格式、锚点、批注、修订、表格、内容控件和未触碰的包部件；只有明确要求的文字变化会写回新的 DOCX。

<p align="center">
  <img src="docs/assets/review-console-revisions.png" alt="docx2typed 审阅控制台展示修订和固定审阅索引" width="100%" style="max-width:100%;height:auto;display:block">
</p>

## 它能做什么

| 需求 | docx2typed 的做法 |
|---|---|
| 安全编辑文字 | 提取 workdir，源 `.docx` 保持不变。 |
| 保留格式 | 保留样式归属、段落结构、锚点和未触碰的包部件。 |
| 审阅修改 | 支持 Word 修订、批注和按段落跳转。 |
| 交付 DOCX | 构建新文件、独立验证，并用 LibreOffice 做互操作检查。 |

这是结构保真的编辑引擎，不是 Microsoft Word 的浏览器替代品。审阅控制台用于查看和决定修改，构建出的 DOCX 才是最终交付文件。

## 安装

环境要求：Python **3.11+**。最终互操作性检查推荐使用 LibreOffice Writer。只有需要手机访问时才需要安装 Tailscale。

### PyPI 安装

```bash
python -m pip install --upgrade docx2typed

docx2typed extract --help
```

如需安装隔离的命令行工具：

```bash
uv tool install --upgrade docx2typed
```

### 从源码安装

```bash
git clone https://github.com/LLLin000/docx2typed-typed-mode.git
cd docx2typed-typed-mode
python -m pip install -e .
```

## 快速开始

先提取源文档，源文件不会被修改：

```bash
docx2typed extract input.docx -o workdir
docx2typed view workdir --mode clean
```

在 `workdir/edit.md` 的相关文字区域内完成编辑，然后同步并构建新的 DOCX：

```bash
docx2typed edit sync workdir --no-track
docx2typed build workdir -o edited.docx
docx2typed verify workdir edited.docx
```

如果需要生成可审阅的 Word 修订，使用带作者名的同步命令：

```bash
docx2typed edit sync workdir --track --author "Reviewer"
docx2typed build workdir -o reviewed.docx
docx2typed verify workdir reviewed.docx
```

`verify` 会根据 workdir 检查输出文件。最后请用 Word 或 LibreOffice 打开生成的 DOCX 做视觉检查；文字长度变化导致的换行和分页变化属于正常排版重流。

## 审阅控制台

审阅控制台把 typed workdir 渲染为连续的文档正文。固定审阅索引可以跳转到对应修订或批注，同时保留正文阅读位置。

### 独立 HTML

```bash
python -m docx2typed.review_console workdir -o review.html
```

在浏览器中打开 `review.html`。

### 本地审阅服务

```bash
docx2typed-review workdir --host 127.0.0.1 --port 8876
```

在同一台电脑打开 <http://127.0.0.1:8876/>。

### 临时手机访问

需要在私有 tailnet 中短时间协作时：

```bash
docx2typed review workdir --tailscale --port 8876
```

在登录同一 Tailscale 网络的手机上打开命令打印的地址。请仅向需要协作的成员开放访问，不要把审阅端口暴露到公网。

<p align="center">
  <img src="docs/assets/review-console-desktop.png" alt="桌面端 docx2typed 审阅控制台展示连续正文和固定审阅索引" width="72%" style="max-width:100%;height:auto;display:block">
</p>

## 审阅修改

### 接受或拒绝修订

修订式编辑会生成真实的 Word `w:ins` 和 `w:del` 节点，已有修订仍可继续审阅。单项或全量决策都会写入新的 DOCX/workdir，不会原地修改源 workdir。

```bash
docx2typed decide accept-all \
  --workdir tracked-wd \
  --output accepted.docx \
  --workdir-out accepted-wd

docx2typed verify accepted-wd accepted.docx
```

### 批注

批注默认保留。工具可以根据批注内容处理正文，但输出仍保留批注 ID、作者、日期、文本和锚点。只有用户明确要求时才删除批注：

```bash
docx2typed decide comment-delete 1 --workdir workdir
```

### 表格与内容控件

表格单元格和内容控件段落中的文字可以通过 `edit.md` 或 MCP 编辑。专用表格命令可以增删、合并或拆分表格结构，不会静默重写单元格文字。具体命令语法见[能力参考](capabilities.md)。

## MCP 集成

安装完成后，可以为 Claude 添加 stdio MCP 服务：

```bash
claude mcp add docx2typed -- uvx docx2typed mcp
```

其他 MCP 宿主可以使用：

```json
{
  "mcpServers": {
    "docx2typed": {
      "command": "uvx",
      "args": ["docx2typed", "mcp"]
    }
  }
}
```

MCP 服务提供 workdir 查看、文字编辑、修订与批注审阅、表格操作、构建和验证，并使用与 CLI 相同的安全检查。

## 范围与默认行为

- 默认编辑面是文字；已有格式和文档结构会被保留，而不是重新设计。
- 批注会保留，除非明确要求删除。
- 格式归属不明确时会报告问题，而不是猜测结果。
- 文字变化造成的自动换行和分页变化属于正常排版重流；最终保真度以构建出的 DOCX 为准。

## 延伸阅读

- [安装与协作指南](Installation.md)
- [CLI 与 MCP 能力参考](capabilities.md)
- [端到端工作流](composites.md)
- [验证保证](verification.md)
