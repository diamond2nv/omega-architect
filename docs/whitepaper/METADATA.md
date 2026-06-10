---
chapter-id: appx-metadata
chapter-version: 0.1.0
category: meta
audience: [所有人]
keywords: [元数据, 依赖图, 章节标准]
git-commit: GIT_COMMIT_PLACEHOLDER
git-date: GIT_DATE_PLACEHOLDER
last-reviewed: GIT_DATE_PLACEHOLDER
reviewer: Ω-Architect Team
---

# 元数据标准与依赖图

## 章节元数据字段

| 字段 | 类型 | 说明 | 示例 |
|:-----|:-----|:-----|:-----|
| `chapter-id` | str | 唯一标识 | `ch03` |
| `chapter-version` | str | 章节版本 | `0.1.0` |
| `category` | str | 分类 | `methodology` |
| `audience` | list[str] | 目标读者 | `[初级研究员, 系统工程师]` |
| `keywords` | list[str] | 关键词 | `[搜索限制, MCP]` |
| `git-commit` | str | 提交哈希 | (脚本注入) |
| `last-reviewed` | date | 审阅日期 | `GIT_DATE_PLACEHOLDER` |
| `prerequisites` | list[str] | 前置知识 | `[LLM tool_calls]` |
| `depends-on` | list[str] | 依赖章节 | `[ch02]` |

## 章节依赖图

```{=latex}
\begin{forest}
for tree={grow=south,edge={gray!60},l sep=8pt,
  font=\small,parent anchor=north,child anchor=south}
[Ω-Architect 白皮书
  [idx: 索引页]
  [ch01: 项目概述
    [ch02: 系统架构
      [ch03: 核心循环]
      [ch04: 实验验证]
      [ch05: 工程实现]
    ]
  ]
  [ch06: 生态与结论]
  [appx: 元数据 \& 版本历史]
]
\end{forest}
```

## 版本锚点注入

`GIT_COMMIT_PLACEHOLDER` / `GIT_DATE_PLACEHOLDER` 由 `generate_whitepaper_metadata.py` 自动替换。
渲染后 `--restore` 恢复占位符，源文件保持 git 干净。
