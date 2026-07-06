# GMMap

> MIT LEAN Group — Memory-Efficient 3D Occupancy Map Using Gaussian Mixture Model

- **Paper:** IEEE TRO 2024 | arXiv:2306.03740 | DOI: `10.1109/TRO.2024.3351266`
- **GitHub:** [mit-lean/GMMap](https://github.com/mit-lean/GMMap) (28MB C++/CUDA, 69⭐)
- **本地路径:** `~/Gitlab/Agentic4Sci/GMMap/`

## 本地状态

- ✅ **已克隆** (via tarball archive, 29MB, master branch)
- 271 files, ~35K LOC C++
- C++ with CUDA support, CMake build system
- Git init: `14c7354`, remote: `origin https://github.com/mit-lean/GMMap.git`
- 完整代码树含: `src/`, `include/`, `libs/` (nigh, rtree, colormap, dataset_utils), `example/`, `path_planning_example/`

| 参数 | 值 |
|:-----|:----|
| 表示 | 高斯混合模型 $\sum_{k=1}^K \pi_k \mathcal{N}(\mu_k, \Sigma_k)$ |
| 构建算法 | 闭式 EM，线性时间 |
| 查询 | $O(\log M + k)$ (R-Tree) |
| 内存节省 | 67× vs OctoMap |
| 后继硬件 | Gleanmer SoC (6 mW, 16nm) |

- TeX 源码: `/tmp/gmmap-src/` (arXiv e-print 2306.03740v3)
- Wiki 实体: [[gmmap-occupancy-map]]
