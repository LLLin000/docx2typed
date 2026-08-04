---
name: docx2typed
description: >
  DOCX ⇄ 可编辑Markdown，格式锁定：只改文本、重建后格式逐段XML复刻。触发:
  "修改docx文字但不动格式"、"老师批注的docx要改"、"补充实施例"、
  "docx提取成markdown编辑"、"在保留格式前提下改专利/论文/合同文字"、
  或任何需要文本级编辑docx而格式（缩进/加粗/上标/批注/分页/下边框线）必须原样保留的任务。
---

# docx2typed — 格式锁定的DOCX文本编辑

把有格式的docx转成可编辑markdown；改完文本重建docx，格式与原文件100%一致（逐段XML验证）。
适用：老师批注后的专利docx需改文字、补实施例，但格式必须原样。

## 运行前提

工具本体在本skill目录内（`__init__.py`/`build.py`/`extract.py`/`verify.py`）。
**必须从skill的父目录运行**（`~/.omp/agent/skills/`），因为包名=目录名`docx2typed`：

```bash
cd ~/.omp/agent/skills
python -m docx2typed <命令>
```

## 定位

- ✅ 改 `[n] ` 后面的文字内容
- ✅ 空段落填 `[1] 文本` 行（继承相邻段落格式）
- ❌ 增删 `[n]` 行、改 `<!-- Pxx -->`/`<!-- XML:... -->`/meta、改段落数

## 步骤

### 1. 提取

```
python -m docx2typed extract <输入.docx> -o <工作目录>
```

产出：`<名>.md`（可编辑文本）+ `<名>.format.json`（锁定格式）+ `_template.docx`（模板）。
**完成准则**：三个文件都存在；md里有 `[n] ` 行和 `<!-- Pxx -->` 段标记。

### 2. 阅读（可选）

```
python -m docx2typed view <输入.md> [-o 可读全文.txt]
```

把run级md拼接成完整段落文本，便于读懂文章内容。`--no-paragraph-markers` 去掉段标记。
**完成准则**：输出能看清每段完整句子。

### 3. 编辑

只改 `[n] ` 后的文本。空段（无 `[n]` 行）可加 `[1] 文本` 行填空。
**完成准则**：所有需要改的文本已改；段落数、每段run数、行号连续性未变。

### 4. 重建

```
python -m docx2typed build <输入.md> <输入.format.json> -o <输出.docx>
```

**完成准则**：无报错；若报 `run count mismatch`/`paragraph count mismatch`/`run numbers not contiguous`，说明段落结构被改，需还原。

### 5. 验证（必做）

```
python -m docx2typed verify <原文件.docx> <输出.docx>
```

**完成准则**：输出 `identical: N/N`（N=原段落数）。有差异必须修复，不许跳过验证。

## 参考

### 工作目录文件

| 文件 | 用途 | 可编辑？ |
|---|---|---|
| `<名>.md` | 文本（每run一行） | ✅ 只改文字 |
| `<名>.format.json` | 全部格式XML | ❌ |
| `_template.docx` | 模板副本（styles/sectPr） | ❌ |

### 已知坑

- md 文件是 CRLF 行尾；文本编辑工具可能改行尾，build 不受影响（按行解析）
- 空段落初始无 `[n]` 行，填空时加一行即可；build 用继承格式创建 run
- 批注锚点（`<!-- XML:... -->`）、分页、bookmark 是锁定内容，编辑时跳过
