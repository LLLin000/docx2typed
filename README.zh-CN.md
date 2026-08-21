# docx2typed

[English version](README.md) · [安装与协作指南](Installation.md)

> 使用自包含 Rust 二进制、MCP 审阅工作流和独立验证的结构保真 DOCX 编辑工具。

`docx2typed` 修改 `.docx` 中的文字，不把文档压平成有损的纯文本或 HTML。
原有格式、修订、批注、表格、内容控件、锚点和未触碰的包部件由 typed
workdir 与字节保真构建链保护。

## 这是哪个仓库？

这是 **Rust 生产仓库**：

- CLI：`docx2typed <command>`
- MCP：`docx2typed mcp`，通过干净 stdio 通信
- 浏览器审阅服务：`docx2typed review <workdir>`
- Python：单独的离线参照实现，不是运行时 fallback

Python 参照实现位于
[`LLLin000/docx2typed`](https://github.com/LLLin000/docx2typed)。Rust 生产
运行不要求 Python、`uvx`、源码 checkout 或 Python MCP 启动器。

## 安装路径

目前没有统一的一键安装器。按实际场景选择一条路径：

| 场景 | 支持的路径 |
|---|---|
| Windows 用户，手里已有二进制 | 使用 `scripts/install_binary.ps1` |
| macOS/Linux 用户，手里已有二进制 | 复制到自己管理的 `bin` 目录，并用绝对路径配置 MCP |
| 开发者 | `cargo build --release --locked`，直接使用生成的二进制 |
| 发布操作者 | 使用 `scripts/package_release.ps1` 生成签名 target bundle |

PowerShell 安装器**仅支持 Windows**，也不会自动下载二进制。本仓库没有
Rust 运行时的 `install.sh`、Homebrew、apt、winget 或 PyPI 安装路径。

## Windows：构建并安装

在 Rust checkout 中执行：

```powershell
cargo build --release --locked
$source = (Resolve-Path .\target\release\docx2typed.exe).Path
& $source --version --json
```

使用 receipt-safe 生命周期安装器安装已验证的二进制：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_binary.ps1 `
  -Action install `
  -Bin $source
```

默认安装目录：

```text
%LOCALAPPDATA%\docx2typed\
├── bin\docx2typed.exe       已安装的 Rust 二进制
├── receipt.json              版本、SHA-256 和所有权路径
└── mcp.config.json           使用绝对路径的 MCP 片段
```

安装器不会修改 `PATH`。可以使用绝对路径，或只在当前 PowerShell 会话中
显式加入：

```powershell
$bin = Join-Path $env:LOCALAPPDATA 'docx2typed\bin\docx2typed.exe'
$env:Path = "$(Split-Path $bin);$env:Path"
& $bin --version --json
```

生命周期操作要求已有 receipt：

```powershell
powershell -File .\scripts\install_binary.ps1 -Action update -Bin $source
powershell -File .\scripts\install_binary.ps1 -Action rollback
powershell -File .\scripts\install_binary.ps1 -Action uninstall
```

`update` 保留一个已校验的备份；`rollback` 消耗该备份；`uninstall` 只删除
receipt 记录且哈希仍匹配的文件，检测到用户改动时会拒绝猜测。

## macOS/Linux：使用构建或发布的二进制

本仓库目前没有跨平台安装器。构建或解压出匹配目标平台的 release bundle
后，将二进制放到自己管理的目录：

```bash
cargo build --release --locked
mkdir -p "$HOME/.local/bin"
cp target/release/docx2typed "$HOME/.local/bin/docx2typed"
chmod 755 "$HOME/.local/bin/docx2typed"
export PATH="$HOME/.local/bin:$PATH"
docx2typed --version --json
```

如果使用 release bundle，复制二进制前先校验 `SHA256SUMS.txt` 及其 detached
signature。bundle 包含二进制、校验和、provenance、SBOM、许可证和签名；不
包含 Python runtime 或后台服务。

## 配置 MCP

标准 MCP 配置直接指向已安装的 Rust 二进制：

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

在 macOS/Linux 上，把 `command` 替换为复制后的二进制绝对路径。Windows
安装器会把同样的结构写入
`%LOCALAPPDATA%\docx2typed\mcp.config.json`。只在获得授权后将该对象复制
到宿主 MCP 配置，并保留已有服务器。

不要把 `command` 替换为 `python`、`uvx`、`cargo run`、相对路径或仓库导入。
MCP 进程将协议回复写到 stdout，日志写到 stderr。

直接 smoke 已安装的服务器：

```text
{"tool":"engine_info","args":{}}
{"tool":"tools/list","args":{}}
```

预期：每个请求一行 `OK`，`tools/list` 返回冻结的 36 个工具。

## 第一个文档

源 DOCX 永远不会被覆盖。typed workdir 包含提取出的状态、不可变模板、
指纹和 generation store。

```powershell
$bin = Join-Path $env:LOCALAPPDATA 'docx2typed\bin\docx2typed.exe'
& $bin extract input.docx -o workdir --json
& $bin enumerate workdir --json
& $bin edit text workdir P0.0 "old text" "new text" --json
& $bin build workdir -o output.docx --json
& $bin verify workdir output.docx --json
```

修订、批注、表格结构和人机交接使用 `revisions`、`decide`、`comment` 以及
MCP 审阅工具。完整命令面见 [`capabilities.md`](capabilities.md)。

## 浏览器审阅

提取 workdir 后启动本地审阅服务：

```powershell
& $bin review workdir --host 127.0.0.1 --port 8876
```

打开 <http://127.0.0.1:8876/>。浏览器只负责排队决策和补丁，不会在 Agent
不知情时写入 DOCX。Agent 应用队列、构建新输出并执行独立验证。

Rust 二进制没有 `--tailscale` 模式，也不会静默放宽到 `0.0.0.0`。远程访问
必须绑定明确受控的私有接口并配置宿主 ACL。

## 交付门禁

每次交付遵循：

```text
extract → inspect/enumerate → edit 或 review → build → independent verify → 可选 Office 检查
```

`verify` 独立重新推导基线，检查文字、样式、受保护结构、修订/批注和包部件
身份。build 成功不能替代 verify。Office 保存/重新打开是依赖宿主环境的可选
检查；没有真实 Office 主机时，记录 `not-run-no-host`。

## 延伸阅读

- [安装与协作指南](Installation.md)
- [CLI 与 MCP 能力参考](capabilities.md)
- [端到端工作流](composites.md)
- [验证保证](verification.md)
- [Agent Skill](SKILL.md)
