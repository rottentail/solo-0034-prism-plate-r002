# 准入验证证据汇总（机器生成）

生成时间（UTC）：2026-11-18T13:47:32Z
观察到的生产预算：segments × width × height × 4 ≤ 160,000,000
观察到的 HTTP 体限制：8388608 字节；本测试未修改任何生产限额。

## 1. 可复现矩阵：静态上界 vs 实际测量

| 稿件 | 族 | 请求体字节 | 展开边数 | 静态乘积 | 生产判定/HTTP | PNG 中位 ms（min–max, 3次） | SVG ms | 分类 |
|---|---|---:|---:|---:|---|---|---:|---|
| a1-line-loose-192 | A-near-gate | 11,421 | 241 | 26.7M | admit/201 | 56 (56–58) | 0 | accepted |
| a2-curve-dense-192 | A-near-gate | 11,196 | 2,882 | 318.7M | reject/400 | 369 (365–373) | 1 | bounding-reject |
| a3-curve-dense-occluded-192 | A-near-gate | 11,408 | 2,886 | 319.2M | reject/400 | 13 (12–13) | 1 | conservative-reject |
| a4-curve-occluded-legal-192 | A-near-gate | 4,276 | 966 | 106.8M | admit/201 | 12 (12–13) | 0 | accepted |
| b1-line-loose-96 | B-admitted-pair | 11,431 | 241 | 6.7M | admit/201 | 13 (13–14) | 0 | accepted |
| b2-curve-dense-96 | B-admitted-pair | 11,185 | 2,882 | 79.7M | admit/201 | 92 (90–92) | 1 | accepted |
| c1-curve-extreme-512 | C-extreme | 23,011 | 6,050 | 6343.9M | reject/400 | 8835 (8823–8843) | 2 | bounding-reject |
| c2-curve-extreme-occluded-512 | C-extreme | 23,231 | 6,054 | 6348.1M | reject/400 | 120 (118–120) | 2 | conservative-reject |
| c3-curve-max-admitted-512 | C-extreme | 1,028 | 146 | 153.1M | admit/201 | 341 (341–344) | 0 | accepted |

## 2. 判定分类计数

- accepted: 5
- bounding-reject: 2
- conservative-reject: 2

## 3. 通道与不变量

- 建版通道：4/9 被拒；拒绝无残留 = True
- 封包导入：4/9 被拒；拒绝无残留 = True；重名冲突409 = True；篡改摘要400 = True
- 8MiB 体闸：HTTP 400，无残留 = True
- PUT 修订拒绝：{'over_budget_put_status': 400, 'error': 'scene exceeds bounded rendering work', 'revision_count_unchanged': True, 'revision_digest_unchanged': True, 'no_artifact_growth': True}
- 跨实例摘要稳定：True
- 异步取消终态有界：{'submit_status': 202, 'cancel_status': 202, 'terminal_state': 'cancelled', 'terminal_is_bounded': True}

## 4. 有界截止时间证据（独立探针，非生产 30s 路径）

```json
{
  "case": "c1-curve-extreme-512",
  "probe_timeout_ms": 2000,
  "raster": [
    {
      "ok": false,
      "timed_out": true,
      "error": "context deadline exceeded",
      "elapsed_millis": 2005,
      "cancel_observed_millis": 2005
    }
  ],
  "svg_still_succeeds": true
}
```

## 5. 关键对比

- a1 vs a2：请求体相差约 2%，展开边数 241 vs 2,882（12 倍），静态乘积 26.7M vs 318.7M；仅按字节判安全会放过 a2 的成像。
- a2 vs a3：静态乘积几乎相同（318.7M vs 319.2M），实测 PNG 373ms vs 13ms（约 29 倍差）；a3 被画布外裁域完全遮住，是被静态上界误伤的合法设计。
- c1 vs c2：静态乘积同为约 63.5 亿，实测 8.9s vs 0.12s（约 74 倍差），进一步证明上界不感知可见性。
- b1 vs b2：两者均被接受，同字节量级下实测 PNG 13ms vs 92ms（约 7 倍），证明复杂度差异在准入区内同样真实存在。

