# docx2typed

[English version](README.md) · [安装与协作指南](Installation.md)

> 使用签名 Rust 二进制、MCP 审阅工作流和独立验证的结构保真 DOCX 编辑工具。

`docx2typed` 修改 `.docx` 中的文字，不把文档压平成有损的纯文本或 HTML。原有格式、修订、批注、表格、内容控件、锚点和未触碰的包部件由 typed workdir 与字节保真构建链保护。

## 运行时边界

生产环境只使用**自包含 Rust 二进制**：

- CLI：`docx2typed <command>`
- MCP：`docx2typed mcp`，通过干净 stdio 通信
- 审阅服务：`docx2typed review <workdir>`
- Python：仅用于开发资格验证的离线参照实现，不作为生产 fallback

当前 RC 已完成 Rust CLI/MCP 和真实文档迁移资格验证。没有 Word/LibreOffice 主机时，Office 保存/重新打开资格门保持诚实的 `not-run-no-host`；本仓库不构建 Office COM 自动化。

## 从源码快速开始

构建 release 二进制：

```powershell
cargo build --release
```

Windows 上使用 receipt-safe 生命周期安装器：

```powershell
powershell -File scripts/install_binary.ps1 `
  -Action install `
  -Bin target\release\docx2typed.exe
```

验证安装结果：

```powershell
docx2typed --version --json
docx2typed extract input.docx -o workdir --json
```

安装器会在 `%LOCALAPPDATA%\docx2typed` 写入
`receipt.json` 和 `mcp.config.json`。后续使用 `-Action update`、`rollback` 或
`uninstall`，不要手动覆盖 receipt 管理的文件。

## 编辑文档

源 DOCX 不会被覆盖。workdir 保存 typed 状态、不可变模板、指纹和 generation store。

```powershell
# 发现可编辑 leaf 路径和文档结构
docx2typed enumerate workdir --json

# 编辑一个文本 leaf；P0.0 仅为示例路径
docx2typed edit text workdir P0.0 "旧文本" "新文本" --json

# 构建并独立验证新的 DOCX
docx2typed build workdir -o output.docx --json
docx2typed verify workdir output.docx --json
```

Agent 编辑时使用 MCP。宿主配置必须指向已安装二进制的绝对路径，并只传入
`mcp` 参数：

```json
{
  "mcpServers": {
    "docx2typed": {
      "command": "C:\\Users\\<you>\\AppData\\Local\\docx2typed\\bin\\docx2typed.exe",
      "args": ["mcp"]
    }
  }
}
```

MCP 接口冻结为 36 个工具。常规会话顺序：

```text
workdir_open → list_paragraphs/get_paragraph → replace_text 或 batch_edit
→ diff_preview → commit_sync → build_docx → verify_output
```

审阅工具还覆盖修订决策、批注处理、表格结构操作和 human-agent 队列交接，且不绕过 store 与独立 verifier。

## 浏览器审阅

先提取 workdir，再启动本机审阅服务：

```powershell
docx2typed review workdir --host 127.0.0.1 --port 8876
```

打开 <http://127.0.0.1:8876/>。浏览器只负责审阅和交接，不会静默改写源 DOCX。人的决定会进入队列或导出为文件，之后由 Agent 事务化应用并重新构建输出。

如需手机或另一台机器访问，只绑定明确受控的私有接口并使用宿主网络控制。Rust 二进制没有 `--tailscale` 模式，也不会静默放宽到 `0.0.0.0`。

## 修订、批注和表格操作

```powershell
# 查看 DOCX 或 workdir 中的修订
docx2typed revisions list workdir --json

# 查看某个修订的 accept/reject 视图
docx2typed revisions view workdir accept --json

# 使用 fingerprint 防护应用单条决定
docx2typed decide accept "part|kind|w:id|fingerprint" `
  --workdir workdir --fingerprint <fingerprint> --json

# 查看或明确删除批注
docx2typed comment list workdir --json
docx2typed comment delete workdir <comment-id> --json

# 表格操作生成新的 DOCX 和新的 clean workdir
docx2typed decide table-insert-row T0 --workdir workdir `
  --args "1" --output table.docx --workdir-out table-workdir --json
```

批注默认保留。只有用户明确要求时才删除批注。表格操作不会重写单元格文字；合并可能丢失文字时必须显式传入 `--discard-content`。

## 交付门

每次交付遵循：

```text
extract → inspect/edit → build → independent verify → 可选 Office 检查
```

`verify` 独立于 `build`：它重新推导基线，检查文字、样式、受保护结构、修订/批注和包部件身份。构建成功不能替代独立验证。

## 延伸阅读

- [安装与协作指南](Installation.md)
- [CLI 与 MCP 能力参考](capabilities.md)
- [端到端工作流](composites.md)
- [验证保证](verification.md)
- [Agent Skill](SKILL.md)
