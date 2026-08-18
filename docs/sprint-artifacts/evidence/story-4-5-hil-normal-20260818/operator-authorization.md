# 单次运行人工授权记录

- 日期：2026-08-18
- 关联运行：`story45-normal-20260818-211027-13556`
- 授权人：Jing（现场操作者，本会话）
- 场景：Story 4.5 `normal`，仅一次运行
- 候选 commit：`154e379da400b326162f84097b79270a5f455e0c`
- 授权 payload digest：`2b2013664f802d4fbda9351e6a902e5ee788c046fe59e0beafd94dcdc793af92`
- 完整 token：`STORY45:normal:154e379da400b326162f84097b79270a5f455e0c:2b2013664f802d4fbda9351e6a902e5ee788c046fe59e0beafd94dcdc793af92`
- 操作者原文：`已打开上游洁净 Air，确认执行 STORY45:normal:154e379da400b326162f84097b79270a5f455e0c:2b2013664f802d4fbda9351e6a902e5ee788c046fe59e0beafd94dcdc793af92`

该消息把“打开上游 Air”和 token 确认写在同一条回复中，而运行手册的理想顺序是先确认 manifest/token、再打开 Air；因此这是一次已记录的程序偏差，不能作为未来复跑范例。runner 只有在接收并核验 token 后才允许 manifest 写入到达 HAL，但本次会话没有可导出的消息 ID 或精确墙钟时间，本文不补造这些字段。

该确认在管理规则上只绑定上述候选、场景、授权 payload 和 manifest 中列出的单次写入/自动收尾；运行收口后不得复用，不授权复跑、fault 场景、额外 NI/Alicat 写入或 push。token 本身是由场景、commit 与 payload digest 确定性生成的，不含 nonce/expiry，因此“一次性失效”是现场授权规则，不是 token 的密码学或跨进程技术属性。此文件是根据本次人机会话补录的 post-run human attestation，不是 runner 原始输出，也不是独立转录证明。
