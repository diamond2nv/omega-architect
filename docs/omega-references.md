# Ω-Architect 外部参考资料集

> **Doc version**: `0.1.0` — matches repo version bc6eca9
> **Last updated**: 2026-06-10

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
| **STAR-PólyaMath (2605.19338)** | 多Agent数学推理框架 — 编排状态机 + 持久化元策略师，与Omega的三层编排架构高度同构 |
| **Arbor (2606.11926)** | 自主科研框架 — Coordinator + Executor + Hypothesis Tree，与Omega的Orchestrator+delegate_task+ErrorMemory结构同构，2.5× Codex/Claude Code |

---

## 7. STAR-PólyaMath: Multi-Agent Reasoning

| 字段 | 内容 |
|------|------|
| **arXiv** | [2605.19338](https://arxiv.org/abs/2605.19338) [cs.MA, cs.AI, cs.CL] |
| **作者** | Jiaao Wu, Xian Zhang, Hanzhang Liu, Sophia Zhang, Fan Yang, Yinpeng Dong |
| **提交** | 2026-05-19 |
| **GitHub** | [github.com/Julius-Woo/STAR-PolyaMath](https://github.com/Julius-Woo/STAR-PolyaMath) |
| **类型** | arXiv preprint, CC BY 4.0 |
| **sf_id** | 1781872531847888890 |
| **核心创新** | Persistent Meta-Strategist: 跨尝试记忆 + 元策略控制，使系统跳出无效循环 |
| **架构** | 编排状态机 + nested challenge-step-replan loops, reasoning-free Python orchestrator |
| **性能** | 8 顶赛 SOTA, AIME/Putnam/HMMT 满分, Apex 2025: 93.75% vs GPT-5.5 80.21% |
| **Omega 关联** | 编排状态机 = Omega Layer 3 多Agent模式; Meta-Strategist × BudgetTracker/ConvergenceTracker 互补 |

---

## 8. Arbor: Hypothesis-Tree Autonomous Research

| 字段 | 内容 |
|------|------|
| **arXiv** | [2606.11926](https://arxiv.org/abs/2606.11926) [cs.CL, cs.AI] |
| **作者** | Jiajie Jin, Yuyang Hu, Kai Qiu et al. (RUC-NLPIR) |
| **提交** | 2026-06-10 |
| **GitHub** | [github.com/RUC-NLPIR/Arbor](https://github.com/RUC-NLPIR/Arbor) |
| **项目主页** | [ruc-nlpir.github.io/Arbor](https://ruc-nlpir.github.io/Arbor/) |
| **类型** | arXiv preprint, CC BY-SA 4.0 |
| **sf_id** | 1781872531847888891 |
| **核心创新** | Hypothesis Tree Refinement (HTR): 持久化树链接假设→证据→洞见 |
| **架构** | Long-lived Coordinator + Short-lived Executors + HTR tree |
| **性能** | 6/6 real tasks best; >2.5× Codex/Claude Code; MLE-Bench Lite 86.36% Any Medal |
| **Omega 关联** | Coordinator ↔ Layer 3 Orchestrator; HTR ↔ ErrorMemory; Worktree ↔ delegate_task; 树状搜索可补充Omega当前的DFS/Beam/Hybrid模式 |

---

## 9. Neural Gabor Splatting: 3DGS × Neural Gabor 高频表面重建

| 字段 | 内容 |
|------|------|
| **arXiv** | [2604.15941](https://arxiv.org/abs/2604.15941) [cs.CV, cs.GR] |
| **作者** | Haato Watanabe, Nobuyuki Umetani (The University of Tokyo) |
| **提交** | 2026-04-17 (CVPR 2026 Accepted) |
| **类型** | arXiv preprint, CC BY 4.0 |
| **sf_id** | 222663517775942 |
| **核心创新** | 为每个 Gaussian primitive 嵌入轻量 MLP 编码颜色变化 (Neural Gabor)，配合频率感知稠密化策略控制 primitive 数量 |
| **搜索视角** | 3DGS 的 primitive 放置 = BFS 搜索（大量 primitive 覆盖场景）；Neural Gabor 的 MLP 增强 = 每 primitive 内的局部 DFS（通过 MLP 参数空间搜索颜色模式）。频率感知剪枝 = 自适应 BFS 剪枝 |
| **与建筑地图关系** | 3DGS 和 GMMap 共享 Gaussian 表示 DNA，但目标不同。GMMap 面向机器人建图（实时、低功耗、占据概率），3DGS 面向视觉渲染（照片级质量、实时渲染）。Neural Gabor 架起桥梁：用 MLP 增强 Gaussian 表达力，类似 GMMap 的 GMM 混合表示 |

## 10. Low-Cost Neural Radiance Fields: NeRF 加速变体对比

| 字段 | 内容 |
|------|------|
| **arXiv** | [2605.09312](https://arxiv.org/abs/2605.09312) [cs.CV] |
| **作者** | Alice Huang, Prathamesh Sonawane, Yashdeep Thorat, Yug Rao (UIUC) |
| **提交** | 2026-05-10 |
| **类型** | arXiv preprint, CC BY 4.0 |
| **sf_id** | 222663541861894 |
| **核心创新** | DS-NeRF/TensoRF/HashNeRF 三加速体系统比较 + TensoRF-DS 深度监督扩展 + HashNeRF 残差/卷积架构变体 |
| **搜索视角** | NeRF 训练 = MLP 权重的 DFS（SGD 路径）；架构变体搜索 = BFS 在架构空间中展开（HashNeRF 4 变体 = beam width 4，匹配迭代预算的等时评估）。结论准确: "iso-time evaluation → none conclusively outperform" = 搜索未越过对比基线 = 空间尚未充分覆盖 |
| **关联** | 与 [[nerf-neural-radiance-fields]] 实体页、[[3d-gaussian-splatting]] 实体页形成 CV 场景表示搜索的完整谱系 |

## 11. APRIL: Learning to Repair Lean Proofs from Compiler Feedback

| 字段 | 内容 |
|------|------|
| **arXiv** | [2602.02990](https://arxiv.org/abs/2602.02990) [cs.LG] |
| **作者** | Evan Wang, Simon Chess, Daniel Lee, Siyuan Ge, Ajit Mallavarapu, Jarod Alper, Vasily Ilin |
| **提交** | 2026-02-03 (v2: 2026-03-13) |
| **类型** | ICLR VerifAI Workshop 2026, 15 pages, 6 figures |
| **sf_id** | 222703385769606 |
| **核心创新** | APRIL 数据集: 260,000 条 LLM 生成的 Lean 证明失败 → 修正的监督训练对，带编译器诊断和自然语言解释 |
| **关键结果** | 4B 微调模型在单步修复上超越最强开源基线 |
| **Omega 关联** | 直接可训练 P1 错误分类器。APRIL 的 4 层编译器反馈（语法→类型→目标→逻辑错误）与 Omega 的 ErrorMemory 错误类型体系同构 |
| **引用** | 待添加 BibTeX |

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
