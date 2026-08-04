# docx2typed — DOCX ⇄ 可编辑Markdown（格式锁定）

## 定位

**只改文本，不改格式。** 把有格式的docx转成可编辑的markdown；改完文本重建docx，格式与原文件100%一致（逐段XML级验证）。

适用场景：老师批注后的专利docx需要修改文字、补充实施例，但格式（缩进/加粗/上标/批注/分页/下边框线）必须原样保留。

## 用法

```bash
# 1. 提取：docx → .md + .format.json + _template.docx
python -m docx2typed extract 输入.docx -o 输出目录

# 2. 编辑：改 .md 里的文本（见下方规则）

# 3. 重建：.md + format.json → docx
python -m docx2typed build 输入.md 输入.format.json -o 输出.docx

# 4. 验证：对比重建结果与原文件，必须 identical: N/N
python -m docx2typed verify 原文件.docx 重建.docx
```

## 编辑规则（.md 文件）

```markdown
<!-- meta ... -->            ← 头部信息，不要动

<!-- P0 -->                  ← 段落标记，不要动
[1] 发明名称：一种...          ← [n] 行 = 一个run（格式块），只改后面的文字
[2] 一种可                    ← 不要增删行、不要改行号

<!-- P5 -->
<!-- XML:<w:commentRangeStart w:id="0"/> -->   ← 非文本元素，不要动
[1] 说 明 书 摘 要
```

- ✅ 可以：修改 `[n] ` 后面的文字内容
- ✅ 可以：在**空段落**（只有`<!-- Pxx -->`、没有`[n]`行）里添加 `[1] 文本` 行——工具会用默认格式创建run
- ❌ 不可以：增删 `[n]` 行、改 `<!-- Pxx -->`、改 `<!-- XML:... -->`、改meta
- ❌ 不可以：改段落数（build会校验）

## 工作原理

- `.format.json` 保存每段的完整XML（pPr、run rPr、中间元素、rsid），extract时从原docx固化
- `.md` 只保存文本（每run一行），用户编辑的就是它
- build 以 json 为骨架、md 为文本源重建，逐字节复刻格式
- verify 对每个段落做XML级对比（rsid剥离、属性排序归一化后），0差异才算通过

## 文件

```
docx2typed/
  __init__.py    CLI入口
  extract.py     docx → md + json + 模板副本
  build.py       md + json → docx
  verify.py      两个docx逐段XML对比
```
