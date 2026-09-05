# OlfactoryPilot-CN UX 设计说明

## 1. 设计目标

界面服务于实验操作，不做营销式展示。用户打开软件后应能立即判断设备是否可用、当前正在执行什么、当前可以执行哪些操作，以及异常时需要采取什么行动。所有主要文字使用简体中文；技术缩写只在用户任务确实需要时显示。

## 2. 全局布局

- 正式产品使用 QFluentWidgets `FluentWindow`，只构造已验收页面，不提供新旧界面切换、后台 legacy UI 或未定能力的占位入口。
- 顶部正常态可以只显示“设备已连接”。`SAFE`、`LOW_FLOW`、`DATA_STALE`、armed、lease、owner、receipt、epoch、generation 等内部状态码不得直接作为普通产品界面文案；需要展示时必须转换成自然中文和用户行动，也不要用“系统正常”“安全正常”“当前就绪”“手动模式”等无操作价值的近义文案替代后继续常驻。
- 页面只回答：设备是否可用、当前正在执行什么、当前能做什么、是否需要用户行动。连接动作只保留一个明确入口。

## 3. 视觉规范

- 使用 QFluentWidgets Dark Theme；背景为近黑、深墨绿/石墨色，主文字白色，次要文字灰色。
- 主题强调色为琥珀金 `#E2AD50`，用于选择、主操作和焦点，不表示安全警告。
- selected：琥珀 Card 背景/边缘强调；actual open：绿色图标 Badge；fault：红色且形态不同的图标 Badge；warning：黄色；disabled：中性灰。
- 字体使用 Windows 默认中文字体；界面现代、克制，不复刻旧 HTML prototype 或旧 QSS 控制台。

## 4. 页面要求

正式产品信息架构包含“手动实验”“自动实验”两个实验页面和“设置”工具页；设置不表达为第三种实验模式。预测试、协议模式、校准、清洗和呼吸实验不加入当前正式导航。Auto 的 trigger/gating 以实验人员能理解的 trial、行号、排队/最新和最小间隔表达，不直接暴露 bit、lease、epoch 等底层术语。

### 手动实验页

- 固定显示机外气口 1–20，不根据“10/20 通道变体”改变布局；当前未接入位置变灰且不能产生硬件 intent。
- 可用气口显示机外编号和可选别名；default、hover、pressed、selected、actually open、fault、disabled 必须可区分。
- 有别名时以别名为主信息，并用较弱 Caption 固定显示零补齐编号（如“气口 01”）；无别名时只显示编号，不保留空副标题。
- 别名单行显示，按实际像素宽度使用 `QFontMetrics.elidedText()` 尾部省略；只有发生截断时才使用 QFluentWidgets ToolTip 显示完整别名和气口编号。别名不得改变字体、卡片高度或 2×10 布局。
- 当前初始化可用位置为 2/4/6/8/12/14/16/18，但界面只消费 HardwareProfile，不得在 View 中写死。
- A 样品流量、B 主气流、C 真空和刺激时长均支持独立输入与步进；A+B 只作弱化的派生总送风显示，不是可编辑 authority。
- 供气和释放按钮只发送 intent；按钮文案、使能、倒计时和完成状态全部由 immutable Snapshot 驱动。
- 手动刺激从全部目标 open receipt 的共同就绪时刻起算，由 `ActuationWorker` 自动关闭；UI 定时器仅刷新显示。
- 气流图必须准确标注当前实际可观测量。只有 A 路 telemetry 时，不得称为 A+B 总流量，也不得显示未经观测证明的“稳定”。
- `InfoBar` 只提示新异常或用户需要知道的操作结果；每个窗口只有一个 sticky winner，按 critical > error > warning > info > success 抢占，同 identity 的文案或严重度变化原地更新。actionable 通知不受视觉 severity 影响，保持到用户关闭或 condition resolved；用户关闭 winner 后，该 episode 不轮播已有 lower/equal backlog，只有新 higher condition 可以再次提示。actionable 存在时到达的 non-actionable success/info/warning 直接退休，condition 解除后不得回放。普通连接成功和无操作价值的正常阶段不创建提示。只有真正阻断当前操作的问题才使用 Fluent Dialog，不使用 `QMessageBox`。
- 后续 Manual Product UI Build 统一把派生值写作“总流量 1500 ml/min”，不把领域公式 `A+B` 当作产品文案；“本次已完成”只作瞬时成功反馈，不长期占据 idle 控制区。Header、实时曲线、流量设置、实验控制、气口 Tile 和停止按钮的其余视觉问题也统一留到该轮处理。

Settings 使用 Pivot 划分“气口配置”和“线路与设备”两个独立区，拥有独立于 Manual 的 Tile/presentation。“气口配置”包含固定 2×10 气口总览、单口详情、页内验证任务和保存动作；Tile 固定显示“未使用 / 待验证 / 可用 / 异常”四态及对应图标，selected 只作琥珀选择强调，不覆盖状态。`MOCK_VERIFIED`、changed 和 incomplete 都显示“待验证”，只有与当前 mapping 匹配的 `PHYSICAL_VERIFIED` 才显示“可用”，Badge 不显示日期。“线路与设备”不使用可展开高级区，只读显示当前气口的控制通道、输出线路和开启方式，以及 1–20 控制通道表和两列设备连接信息，不新增编辑能力。连接后 mapping 只读；只有 clean saved profile 且设备 connected/ready/safe idle 时才显示可用的单口验证动作。dirty draft 在验证按钮附近只提示“保存后验证”。验证确认只显示面板气口和约 20 秒，不显示控制通道、流量值或底层线路；进行中锁定区段和全部气口选择，在单口验证面板显示进度、剩余时间和“立即停止”，不另设页级 status。模拟检查结束后仍回到“待验证”，不显示绿色成功或“验证完成”。保存及停止等结果只在对应动作附近给出简短反馈。rollback 后端继续保留，但普通页面不显示 rollback。

Auto 的具体布局不在本轮设计或实现；立项时应从上述用户任务与已验证领域契约继续设计。清洗、配置等既有服务层安全约束仍以 PRD 与架构为准。

## 5. 文案规范

- 所有面向用户的按钮、标签、提示和错误使用简体中文。
- 错误提示要说明“发生了什么”“系统采取了什么安全动作”和“用户下一步应做什么”。
- 示例：
  - “气流不足，请检查管路后重试。”
  - “协议文件无效，第 12 行缺少 valve 字段。”
  - “写入失败，请检查磁盘空间或目录权限。”
- 颜色只作辅助，安全、警告、失败和进行中状态必须同时提供文字。
- 用户可见字符串必须通过 UTF-8 严格解码、Unicode replacement character、常见乱码和旧英文提示自动化审计；设备 ID、协议字段、扩展名和 machine key 采用显式白名单。

## 6. 可用性原则

- 需要用户行动的安全异常始终可见；正常态不显示内部安全码。
- 危险动作必须有明确反馈。
- 不让用户猜测硬件是否已连接或阀门是否已关闭。
- 动效只提供即时操作反馈，不承担硬件时序或真实状态。
- 开发新页面时应复用集中主题和组件；新链验收后删除旧 `PreTestView`、旧 View 计时、重复状态和确认无用的弹窗/代码。
