---
chapter-id: appx-version
chapter-version: VERSION_PLACEHOLDER
category: meta
audience: [所有人]
keywords: [版本历史, 变更记录]
git-commit: GIT_COMMIT_PLACEHOLDER
git-date: GIT_DATE_PLACEHOLDER
last-reviewed: GIT_DATE_PLACEHOLDER
reviewer: Ω-Architect Team
doc-version: vVERSION_PLACEHOLDER (commit GIT_COMMIT_PLACEHOLDER)
---

# 版本历史

| 用户指南版本 | repo 版本 | repo commit | 日期 | 变更 |
|:------------|:----------|:------------|:-----|:-----|
| vVERSION_PLACEHOLDER | VERSION_PLACEHOLDER | `GIT_COMMIT_PLACEHOLDER` | GIT_DATE_PLACEHOLDER | 初始用户指南框架 |

**版本同步规则：**

1. **repo 版本** 在 `pyproject.toml` 的 `[project] version` 中维护，是唯一数据源
2. **文档版本** 与 repo 版本保持一致，在 `index.qmd` 和每章 `doc-version` 字段中标注
3. **git commit hash** 在 PDF 渲染前注入，提供精确的可溯源指针
4. **渲染流程：** `python3 scripts/generate_user_guide_metadata.py` → `quarto render` → `--restore`
