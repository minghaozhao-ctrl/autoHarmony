# 获取 dump 文件加速：历史评估（已归档）

> 本文档记录的是当年**引入 hypium daemon 快路径**时的可行性评估。该方案现已废弃：
> 项目已**完全移除 hypium**，统一使用 hdc `uitest dumpLayout`。

## 现行方案
- dump 走 hdc：`uitest dumpLayout -e uniqueId -p <remote>` + `hdc file recv`，
  **不带 `-a`**（`-a` 会额外 dump 全部窗口，4.8~5.0s/次，是旧慢路径的主因）。
- 单次 dump 耗时随设备而异：真机约 0.7s、模拟器约 1.9s（`file recv` 约 0.03~0.09s）。
- 结论：去掉 daemon、改用不带 `-a` 的 hdc dump 后，达到与当年 daemon 快路径相当的
  单步速度，且无连接握手开销、无额外依赖、少一类崩溃源。

## 历史数据（仅存档，来自当年真机实测）
| 步骤 | 慢路径（hdc uitest **带 -a**） | 快路径（hypium daemon） |
|------|---------------------|-----------------|
| connect | —（无） | 2.3~5.2s（一次性） |
| 每次 dump | 4.8~5.0s | 0.62~0.79s |
| 坐标点击 | ~0.35s (uiInput) | 0.23s (driver.touch) |

## 为什么不再需要 daemon
- daemon 的优势来自"复用连接 + 一次 connect 摊销 N 次 dump"；
- 而 hdc 不带 `-a` 的 dump 本身已足够快（亚秒级），连接握手成本反而成了净负担；
- 且 hdc 路径能拿到更丰富的 `-e uniqueId` 字段，diff / 裁决 / 页面状态摘要管线零改动复用。
