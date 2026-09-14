# C.3b 连接状态 Simulation 证据（2026-09-14）

本目录仅记录 MockHAL simulation 的产品界面检查，不是 RealHAL、真实 NI/Alicat 或气路 HIL 证据。本轮没有打开 COM6、创建真实 NI task 或运行任何 HIL 脚本。

执行入口：`python scripts/capture_c3b_connection_ui.py`

检查结果：

- 启动后的唯一自动连接完成，显示“设备已连接”，无重复成功通知。
- Global Stop 安全收口后显示“设备未连接”和“重新连接”，未显示“已安全停止”。
- 在未连接状态保持 10 秒，connection request count 不变，没有自动重新连接。
- 人工点击“重新连接”后，唯一连接 transaction 再执行一次并回到“设备已连接”。
- 三种状态切换期间 Header 尺寸由自动化回归固定校验，未观察到位置跳动。

截图及 SHA-256：

- `device-connected.png`：`44F91073A19654A6097C89EF02D70CD34A316524CA98D10471800EDB7EB16F86`
- `device-disconnected-reconnect.png`：`34586683C8192B96937BF61101A1B839CA8FBC602D9641B6D4C60835BF67DA0F`
- `device-reconnected.png`：`44F91073A19654A6097C89EF02D70CD34A316524CA98D10471800EDB7EB16F86`

首次连接与重新连接的安全 idle 画面相同，因此两张已连接截图的哈希一致；这是预期结果。
