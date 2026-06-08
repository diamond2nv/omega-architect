# Ω-Architect 外部参考资料集

> 技术报告中可准确引用的 arXiv ID、DOI、GitHub URL 等来源资料。
> 最后更新: 2026-06-08

---

## 1. Lean 4 定理证明器与编程语言

| 字段 | 内容 |
|------|------|
| **标题** | The Lean 4 Theorem Prover and Programming Language |
| **作者** | Leonardo de Moura, Sebastian Ullrich |
| **会议** | Automated Deduction – CADE 28, LNCS vol 12699, Springer (2021) |
| **DOI** | [10.1007/978-3-030-79876-5_37](https://doi.org/10.1007/978-3-030-79876-5_37) |
| **类型** | 同行评审会议论文 (CC BY 4.0) |
| **指标** | 34k 访问, 249 引用 |
| **引用** | 所有依赖 Lean 4 的项目（Omega、Goedel、Physlib）的元引用 |

---

## 2. Physlib: 物理学形式化 Lean 4 库

| 字段 | 内容 |
|------|------|
| **GitHub** | [github.com/leanprover-community/physlib](https://github.com/leanprover-community/physlib) |
| **网站** | [physlib.io](https://physlib.io) |
| **Lean 版本** | v4.30.0 |
| **Commits** | 3,158 (master) |
| **LICENSE** | 开源 |
| **前身** | HepLean / PhysLean + Lean-QuantumInfo 合并 |
| **引用方式** | 访问其 GitHub 页面获取引用指引；另请引用原始论文 HepLean 和 Lean-QuantumInfo |

**相关 arXiv 论文:**
- arXiv:2603.08139 — 首次发现物理学论文错误（2HDM 形式化）

---

## 3. Tooby-Smith: 形式化发现文献错误

| 字段 | 内容 |
|------|------|
| **标题** | Formalizing the stability of the two Higgs doublet model potential into Lean: identifying an error in the literature |
| **作者** | Joseph Tooby-Smith |
| **arXiv** | [2603.08139](https://arxiv.org/abs/2603.08139) [hep-ph] |
| **DOI** | [10.48550/arXiv.2603.08139](https://doi.org/10.48550/arXiv.2603.08139) |
| **类型** | arXiv preprint (同行评审前) |
| **Subjects** | hep-ph, cs.LO, hep-th |
| **出版年份** | 2026 (v2) |
| **引用** | 作为 AI/形式化发现物理文献错误的**里程碑案例** |

---

## 4. Maniatis et al.: 2HDM 势稳定性 (被发现有错误的论文)

| 字段 | 内容 |
|------|------|
| **标题** | Stability and Symmetry Breaking in the General Two-Higgs-Doublet Model |
| **作者** | Maniatis, von Manteuffel, Nachtmann |
| **期刊** | Eur.Phys.J.C48:805-823,2006 |
| **arXiv** | [hep-ph/0605184](https://arxiv.org/abs/hep-ph/0605184) |
| **DOI** | [10.1140/epjc/s10052-006-0016-6](https://doi.org/10.1140/epjc/s10052-006-0016-6) |
| **类型** | 同行评审期刊论文 |
| **状态** | **已由形式化发现定理漏铜—作者确认并将发布勘误** |

---

## 5. New Scientist 报道

| 字段 | 内容 |
|------|------|
| **标题** | Computer finds flaw in major physics paper for first time |
| **作者** | Matthew Sparkes |
| **来源** | [New Scientist](https://www.newscientist.com/article/2520546-computer-finds-flaw-in-major-physics-paper-for-first-time/) |
| **日期** | 2026-03-26 |
| **类型** | 新闻报道（非同行评审，但可作为时间线和社会影响引用） |
| **引用** | 说明形式化在物理学中的实际影响力的**传播层证据** |

---

## 6. Jixia (稷下): Lean 4 静态分析工具

| 字段 | 内容 |
|------|------|
| **GitHub** | [github.com/frenzymath/jixia](https://github.com/frenzymath/jixia) |
| **作者** | PKU BICMR AI for Math 团队 (frenzymath 组织) |
| **兼容 Lean** | v4.16.0 |
| **目的** | Lean-aware IDE + ML 数据提取 |
| **许可** | 仓库含 LICENSE 文件 |
| **输出格式** | JSON（声明/符号/Elaboration/行级） |
| **引用** | 在 Omega 的技术报告中可作为"Lean 数据提取工具链参考" |

---

## 与 Ω-Architect 的关联

| 资料 | 关联 |
|------|------|
| Lean 4 (CADE 2021) | Omega 构建于 Lean 4 之上 — 语言的元引用 |
| Physlib | 物理形式化管线参考；展示了 Lean 在非数学领域的能力 |
| Tooby-Smith (2603.08139) | **Omega 论文的核心论据**：形式化可发现人类遗漏的错误 |
| Maniatis et al. (hep-ph/0605184) | Physlib 形式化的目标论文—作为案例 |
| New Scientist 报道 | 社会传播层证据—可引用说明形式化的影响力已成新闻 |
| Jixia | Omega T3 训练管线的数据提取参考 |

---

## 推荐引用格式 (BibTeX)

```bibtex
@inproceedings{moura2021lean4,
  author    = {Leonardo de Moura and Sebastian Ullrich},
  title     = {The {Lean} 4 Theorem Prover and Programming Language},
  booktitle = {Automated Deduction -- CADE 28},
  series    = {LNCS},
  volume    = {12699},
  publisher = {Springer},
  year      = {2021},
  doi       = {10.1007/978-3-030-79876-5_37}
}

@misc{toobysmith2026formalizing,
  author    = {Joseph Tooby-Smith},
  title     = {Formalizing the stability of the two Higgs doublet model potential into {Lean}: identifying an error in the literature},
  year      = {2026},
  eprint    = {2603.08139},
  archivePrefix = {arXiv},
  primaryClass = {hep-ph}
}

@article{maniatis2006stability,
  author    = {Maniatis, M. and von Manteuffel, A. and Nachtmann, O.},
  title     = {Stability and Symmetry Breaking in the General Two-Higgs-Doublet Model},
  journal   = {Eur. Phys. J. C},
  volume    = {48},
  pages     = {805--823},
  year      = {2006},
  doi       = {10.1140/epjc/s10052-006-0016-6}
}

@misc{physlib,
  title     = {{Physlib}: Digitalizing Physics into {Lean}},
  howpublished = {\url{https://github.com/leanprover-community/physlib}},
  note      = {Lean v4.30.0, 3,158 commits}
}

@misc{jixia,
  title     = {{Jixia}: A static analysis tool for {Lean} 4},
  howpublished = {\url{https://github.com/frenzymath/jixia}},
  note      = {PKU BICMR AI for Math}
}

@article{sparkes2026computer,
  author    = {Matthew Sparkes},
  title     = {Computer finds flaw in major physics paper for first time},
  journal   = {New Scientist},
  year      = {2026},
  date      = {2026-03-26},
  url       = {https://www.newscientist.com/article/2520546-computer-finds-flaw-in-major-physics-paper-for-first-time/}
}
```
