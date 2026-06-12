---
chapter-id: appx-metadata
chapter-version: VERSION_PLACEHOLDER
category: meta
audience: [所有人]
keywords: [元数据, 依赖图, 章节标准]
git-commit: GIT_COMMIT_PLACEHOLDER
git-date: GIT_DATE_PLACEHOLDER
last-reviewed: GIT_DATE_PLACEHOLDER
reviewer: Ω-Architect Team
doc-version: vVERSION_PLACEHOLDER (commit GIT_COMMIT_PLACEHOLDER)
---

# 元数据标准与依赖图

## 章节元数据字段

| 字段 | 类型 | 说明 | 示例 |
|:-----|:-----|:-----|:-----|
| `chapter-id` | str | 唯一标识 | `ch03` |
| `chapter-version` | str | 章节版本 | `VERSION_PLACEHOLDER` |
| `category` | str | 分类 | `usage` |
| `audience` | list[str] | 目标读者 | `[AI4Math算法组, 系统工程师]` |
| `keywords` | list[str] | 关键词 | `[Quick Start, 安装]` |
| `git-commit` | str | 提交哈希 | (脚本注入) |
| `last-reviewed` | date | 审阅日期 | `GIT_DATE_PLACEHOLDER` |
| `prerequisites` | list[str] | 前置知识 | `[系统架构]` |
| `depends-on` | list[str] | 依赖章节 | `[ch02]` |

## 章节依赖图

```{=latex}
\begin{forest}
for tree={grow=south,edge={gray!60},l sep=8pt,
  font=\small,parent anchor=north,child anchor=south}
[Ω-Architect 用户指南
  [idx: 索引页]
  [ch01: 项目概述
    [ch02: 系统架构
      [ch03: 性能指标]
      [ch04: Quick Start]
    ]
  ]
  [ch05: 生态与对比
    [ch06: 路线图]
  ]
  [appx: 验证实例 \& 数据]
]
\end{forest}
```

## 版本锚点注入

`VERSION_PLACEHOLDER`, `GIT_COMMIT_PLACEHOLDER`, `GIT_DATE_PLACEHOLDER` 由 `scripts/generate_user_guide_metadata.py` 自动替换。

**规则：** 用户指南版本与 `pyproject.toml` 中的 repo 版本同步。PDF 渲染前注入 HEAD commit hash 和日期。渲染后 `--restore` 恢复占位符，源文件保持 git 干净。
