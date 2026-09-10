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

当前正式信息架构只包含“手动实验”和“设置”工具页；设置不表达为实验模式。Auto 尚未实现，验收后才作为第二个实验页面加入。预测试、协议模式、校准、清洗和呼吸实验不加入当前正式导航。未来 Auto 的 trigger/gating 以实验人员能理解的 trial、行号、排队/最新和最小间隔表达，不直接暴露 bit、lease、epoch 等底层术语。

### Simulation 界面规则

- simulation 与 real 共用同一套正式窗口、页面和中文交互。不得增加模拟专用页面、“模拟验证”按钮、Mock 用户文案、测试专用设置或普通用户无需理解的内部说明。
- simulation 启动只允许一个非侵入式全局标记，让开发人员知道当前没有控制真实硬件；该标记不得改变页面结构、产品流程或普通用户文案。
- 模拟 telemetry、动作回执和正向验证只能驱动模拟展示与 `MOCK_VERIFIED`，不得显示为“现场已确认”“生产可用”或 `PHYSICAL_VERIFIED`。
- backend 的 `MOCK_VERIFIED` / `PHYSICAL_VERIFIED` 安全证据隔离不得因共用 UI 而改变。
- 截图 fixture 可以构造待验证、待现场确认、可用和失败等视觉状态，但这些状态只用于视觉审查，不写回 production evidence 或本机真实硬件配置。
- offscreen simulation smoke 只证明应用可构造、显示并正常退出；真实气路、安全联锁和现场可用性仍必须由对应 HIL evidence 证明。

### 手动实验页

- 固定显示机外气口 1–20，不根据“10/20 通道变体”改变布局；当前未接入位置变灰且不能产生硬件 intent。
- 可用气口显示机外编号和可选别名；default、hover、pressed、selected、actually open、fault、disabled 必须可区分。
- Manual 与 Settings 始终以零补齐的面板编号（如“气口 04”）作为主信息；有别名时才在第二行显示别名，无别名时不保留空副标题。
- 别名单行显示，按实际像素宽度使用 `QFontMetrics.elidedText()` 尾部省略；只有发生截断时才使用 QFluentWidgets ToolTip 显示完整别名。别名不得改变字体、卡片高度或 2×10 布局。
- 当前初始化可用位置为 2/4/6/8/12/14/16/18，但界面只消费 HardwareProfile，不得在 View 中写死。
- A 样品流量、B 主气流、C 真空和刺激时长均支持独立输入与步进；“总流量 1500 ml/min”只作弱化的派生信息，不是可编辑 authority。
- 供气和释放按钮只发送 intent；按钮文案、使能、倒计时和完成状态全部由 immutable Snapshot 驱动。
- 手动刺激从全部目标 open receipt 的共同就绪时刻起算，由 `ActuationWorker` 自动关闭；UI 定时器仅刷新显示。
- 气流图必须准确标注当前实际可观测量。只有 A 路 telemetry 时，不得称为 A+B 总流量，也不得显示未经观测证明的“稳定”。
- `InfoBar` 只提示新异常或用户需要知道的操作结果；每个窗口只有一个 sticky winner，按 critical > error > warning > info > success 抢占，同 identity 的文案或严重度变化原地更新。actionable 通知不受视觉 severity 影响，保持到用户关闭或 condition resolved；用户关闭 winner 后，该 episode 不轮播已有 lower/equal backlog，只有新 higher condition 可以再次提示。actionable 存在时到达的 non-actionable success/info/warning 直接退休，condition 解除后不得回放。普通连接成功和无操作价值的正常阶段不创建提示。只有真正阻断当前操作的问题才使用 Fluent Dialog，不使用 `QMessageBox`。
- “本次已完成”只作瞬时成功反馈，不长期占据 idle 控制区。Header、实时曲线、流量设置、实验控制和停止按钮的最终美术仍留给 HIL 后的 Manual Product UI Build。

Settings 默认进入设置首页，以“气口配置”和“线路与设备”两个入口卡片进入子页，并用紧凑 breadcrumb 返回；主导航不再直接打开 2×10 总览，Manual 的“气口设置”快捷入口仍直接进入气口配置。“气口配置”包含固定 2×10 气口总览、单口详情、页内验证任务和保存动作。Tile 同时表达未启用、待验证、待现场确认、可用、需检查和独立 selected；selected 只使用琥珀轮廓，不能替代状态。`MOCK_VERIFIED` 显示“待现场确认”，只有与当前 mapping 匹配的 `PHYSICAL_VERIFIED` 才显示“可用”。未启用的 Settings Tile 仍可选择编辑；Manual 未验证或未启用 Tile 必须明确 unavailable 且不能驱动动作。

“线路与设备”默认以两列紧凑表从控制通道 01 开始展示线路映射；断开设备后，用户可显式点击“编辑线路”修改 canonical NI target，polarity 只在该高级编辑态中可改，普通查看态不得用整行或文字点击改变二值状态。非法、重复、与 selector 冲突或引用未登记设备的 target 必须即时显示错误且不能完成编辑/保存；完成合法编辑后查看态立即与 draft 一致。设备连接参数可直接编辑，连接设备后上述控件全部 disabled。名称、控制通道、串口、设备和 Alicat 输入均有内容驱动的 maximum width，不横跨桌面窗口。Settings、Manual、滚动 viewport 与 Card 使用统一 page/primary surface/secondary surface/border/amber/text/success/warning/error tokens，Mica 保持关闭。

验证启动确认必须显示面板气口、验证流量和最长验证时间。默认验证流量 1500 ml/min、默认20秒，但流量必须大于0且不超过当前 sample A 上限、设备量程与现场证据上限，时长只允许1–60秒，均不得 silent clamp。PREPARING 显示安全准备，RUNNING 使用结构化 monotonic deadline 驱动单调递减倒计时和 determinate progress，UI timer 不承担关阀；RUNNING 可立即选择“没有或位置不对”“出气正确”或停止，系统先安全收口再处理结果。timeout 只收口并进入 AWAITING_CONFIRMATION，不自动成功。simulation 的正向结果只写 `MOCK_VERIFIED` 并显示“待现场确认”，不能形成 production availability；真实物理流程只有在动作完成、安全关闭和用户正向确认的可信合同全部成立时才能写 `PHYSICAL_VERIFIED`。保存及停止等结果只在对应动作附近给出简短反馈；rollback 后端继续保留，但普通页面不显示 rollback。

### 通用数值输入规则

- 用户手动调节的气流 SpinBox 统一使用 100 ml/min 步进；用户手动调节的秒级时间 SpinBox 统一使用 5 s 步进，并复用集中规则。
- `singleStep` 只控制箭头和步进键；用户仍可按控件当前允许的输入精度直接键盘输入任何通过现有校验的合法值，不按步进网格做 round、snap 或强制量化，也不改变持久化值。
- 协议/TXT 的 `duration_ms`、parser 毫秒精度、Worker deadline、内部 timing 和测试时钟不受 UI 步进限制。
- polarity 不在普通气口设置中暴露，只在线路高级编辑流程中显示和修改；`active_high`、mapping fingerprint 与 verification invalidation 语义保持不变。
- 不用解释性小字重复说明显而易见的控件；本次实际参数只在需要用户确认的操作节点显示。安全前提、危险后果、合法范围、单位及无障碍提示不属于可删除的重复说明。

### HIL 后 UI/UX 收敛项

- Settings 两张入口卡和子页面返回/层级导航均不是最终方案；后续按“气口 / 设备连接 / 验证设置 / 高级”或经产品设计确认的类似分组统一重构。
- “线路与设备”当前混合单气口线路与全局连接配置，后续重新分类；profile/config 名称不作为普通用户主要设置。
- 线路表、表单宽度、Card spacing 与字体层级继续统一。
- Manual 与 Settings 的最终视觉语言在 HIL 后统一完成。
- Manual 实时曲线、Header、流量区、实验控制、PortTile 和停止按钮留到最终 Product UI Build。

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
