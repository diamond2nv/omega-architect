---
chapter-id: appx-version
chapter-version: 0.1.0
category: meta
audience: [所有人]
keywords: [版本历史, 变更记录]
git-commit: GIT_COMMIT_PLACEHOLDER
git-date: GIT_DATE_PLACEHOLDER
last-reviewed: GIT_DATE_PLACEHOLDER
reviewer: GSNV Team
---

# 版本历史

## 版本对应关系

| 白皮书版本 | repo tag / commit | 日期 | 变更 |
|:----------|:------------------|:-----|:-----|
| v0.1.0 | `GIT_COMMIT_PLACEHOLDER` (当前 main) | GIT_DATE_PLACEHOLDER | 初始白皮书框架 |
| — | `bc6eca9` | GIT_DATE_PLACEHOLDER | Inner Loop v0.3 |
| — | `75e27c4` | 2026-06-08 | Inner Loop v0.2 |

## 版本规则

- **白皮书版本**与 repo 版本号同步（当前 `0.1.0`）
- 每次 repo 版本提升时，所有 doc 的 `chapter-version` 同步更新
- PDF 渲染时自动从 git HEAD 注入 commit hash
- 版本锚点记录在每章 frontmatter 的 `git-commit` 和 `git-date` 字段
