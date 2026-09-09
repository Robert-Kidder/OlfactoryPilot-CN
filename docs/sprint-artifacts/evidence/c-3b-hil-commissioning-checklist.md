# C.3b HIL Commissioning 未执行 Checklist

> **状态：未执行 checklist。** 本文件仅冻结现场门禁与留证字段，不代表任何真实硬件结果。C.3a 不连接真实硬件、不执行 HIL；必须等待 C.3b 人工批准后，才可由现场人员逐项填写。

## 现场门禁

- [ ] **A. 洁净介质：** 首次 HIL 不接受试者、不用气味样品，只用实验室批准的洁净气体/空气。
- [ ] **B. 出口畅通：** 被验证气口出口完全畅通；手只放在出口前方感受气流，不捏住、封堵或直接堵住管口。
- [ ] **C. 急停可达：** 物理急停/电源停止手段随手可触。
- [ ] **D. 启动前只读确认：** 非 simulation/mock；local config 为目标实验台配置；Dev1/Dev2 身份；Alicat 串口；A/B/C Unit ID；当前 mapping；selector target/polarity；A/B/C setpoint 为安全初值；气味阀1–20初始关闭。
- [ ] **E. 单口首轮：** 第一轮只验证一个气口，不批量验证8路。
- [ ] **F. 完整留证：** 记录 command、exact receipt、NI target、flow setpoint/readback、verification run identity、open/close 时间、safe-close 完成和用户现场观察。
- [ ] **G. 真实 timing evidence：** USB-6001 DO 为 software-timed；C.3b 实测 command→DAQ write receipt、early result→close receipt、timeout→close receipt latency，UI countdown/fake clock 不得替代真实 timing evidence。
- [ ] **H. 异常即停：** 出现 unexpected valve/flow、通信中断、receipt mismatch、安全状态变化、mapping 与现场出口不符或无法安全归零/关闭，立即停止本轮并执行既有 global stop/recovery，不得自动继续下一气口。

## 本轮记录（保持空白，待人工批准后填写）

- 执行日期/人员：
- 实验台与 local config 标识：
- Dev1/Dev2 身份：
- Alicat 串口及 A/B/C Unit ID：
- verification run identity：
- 验证气口 / NI target / polarity：
- flow setpoint / readback：
- command 与 exact receipt 记录位置：
- open / close / safe-close 时间：
- 用户现场观察：
- 异常与 global stop/recovery 记录：
- 结论与审批签名：
