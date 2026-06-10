---
chapter-id: appx-metadata
chapter-version: 0.1.0
category: meta
audience: [所有人]
keywords: [元数据, 依赖图, 章节标准]
git-commit: GIT_COMMIT_PLACEHOLDER
git-date: GIT_DATE_PLACEHOLDER
last-reviewed: GIT_DATE_PLACEHOLDER
reviewer: GSNV Team
---

# 元数据标准与依赖图

## 章节元数据字段

每章的 YAML frontmatter 包含以下字段：

| 字段 | 类型 | 说明 | 示例 |
|:-----|:-----|:-----|:-----|
| `chapter-id` | str | 唯一章节标识 | `ch03` |
| `chapter-version` | str | 章节级版本号 | `0.1.0` |
| `category` | str | 分类标签 | `physics` |
| `audience` | list[str] | 目标读者 | `[理论部, 系统工程师]` |
| `keywords` | list[str] | 关键词 | `[NV色心, Hamiltonian, SNR]` |
| `git-commit` | str | 提交哈希 | (由脚本注入) |
| `git-date` | str | 提交日期 | (由脚本注入) |
| `last-reviewed` | date | 最后审阅日期 | `GIT_DATE_PLACEHOLDER` |
| `reviewer` | str | 审阅者 | `GSNV Team` |
| `prerequisites` | list[str] | 前置知识 | `[量子力学, 电磁学]` |
| `depends-on` | list[str] | 依赖的其他章节 | `[ch01]` |

## 章节依赖图

```{=latex}
\begin{forest}
for tree={grow=south,edge={gray!60},l sep=8pt,
  font=\small,parent anchor=north,child anchor=south,
  tier/.option=level}
[白皮书
  [idx: 索引页]
  [ch01: 项目概述
    [ch02: 系统架构]
    [ch03: 物理模型
      [ch04: FEM 交叉验证]
      [ch05: 工程设计]
    ]
  ]
  [ch06: 开源生态与结论]
  [appx: 元数据 \& 版本历史]
]
\end{forest}
```

## 版本锚点注入

每个 `.qmd` 文件内的 `GIT_COMMIT_PLACEHOLDER` 和 `GIT_DATE_PLACEHOLDER`
由 `scripts/generate_whitepaper_metadata.py` 在 `quarto render` 之前自动替换。
每次渲染时注入当前 HEAD 的 commit hash 和 date，确保 PDF 的版本锚点
与代码仓库状态一一对应。
