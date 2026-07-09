# OlfactoryPilot-CN 架构文档

## 1. 架构目标

本项目是 Windows 桌面硬件控制软件，核心目标是安全、稳定、可测试地替代 ProgOlfactoTao 的实验控制能力。架构必须让 UI、业务流程、硬件访问和数据记录彼此解耦，避免所有逻辑堆在界面代码中。

## 2. 技术栈

- Python：3.11。
- GUI：PySide6。
- 图形显示：pyqtgraph。
- NI 设备：nidaqmx。
- RS232：pyserial。
- 测试：pytest、pytest-qt。
- 代码检查：ruff，目标版本 `py311`。
- 打包：PyInstaller。
- 依赖管理：`requirements.txt` 和 `requirements-dev.txt`。
- CI：GitHub Actions，Windows runner，执行 ruff、pytest 和 PyInstaller 打包。

## 3. 分层结构

```text
app/
  main.py              # 应用入口、命令行参数、QApplication 初始化
  controllers/         # 业务编排、界面状态、安全动作入口
  models/              # 会话、配置、硬件状态、协议等数据结构
  views/               # PySide6 界面组件
  workers/             # 硬件工作线程、低抖动执行逻辑
  services/            # HAL、配置、日志、协议解析等服务
config/
  default_config.json  # 默认配置
docs/                  # 中文需求、架构、UX、story 和项目说明
scripts/               # 本地 CI、硬件探测、状态生成脚本
tests/                 # 自动化测试
```

## 4. 关键设计

### MVC + Worker

- View 只处理显示和用户输入。
- Controller 接收界面事件，调用模型和服务，发出状态更新。
- Worker 在线程中处理硬件读写、协议时序和安全检查，避免阻塞 UI。
- Qt signal/slot 用于 UI 线程和工作线程之间通信。

### HAL 硬件抽象

所有硬件访问必须通过 HAL：

- Real HAL：连接真实 NI 设备和 Alicat 串口设备。
- Mock HAL：模拟呼吸信号、阀门状态和流量反馈，用于开发、演示和测试。

新增硬件功能时，应优先扩展 HAL 接口和测试，而不是在界面代码里直接调用驱动。

### 安全策略

- 气流低于阈值时，阻止阀门、主阀和加热器动作。
- 退出、停止、异常断连时关闭所有阀门。
- 安全状态必须由 Worker/HAL 层保证，不能只依赖按钮是否可点击。
- 关键安全事件写入日志。

### 协议与数据

Epic 3 将实现协议文件解析与执行。建议新增：

- 协议模型：保存 trial、timing、valve、trigger、metadata。
- 协议解析服务：负责 `.txt`、`.csv` 解析和错误定位。
- 会话记录服务：负责 `.raw` 信号和 `.log` 事件输出。
- 执行控制器：处理手动触发、TTL 触发、呼吸门控和暂停/停止。

## 5. 配置来源

默认配置位于 `config/default_config.json`。运行时应以该文件为默认来源，未来可扩展用户配置文件，但不能让多个配置文件同时成为“真实来源”。硬件通道映射、主阀线路、阈值、默认流量和界面文字应保持清晰可追踪。

## 6. 测试策略

- 单元测试：协议解析、配置读取、HAL 行为、补偿逻辑。
- 控制器测试：安全联锁、状态转换、异常处理。
- UI 冒烟测试：应用启动、核心页面加载、按钮状态。
- 模拟模式测试：不依赖真实硬件即可运行 CI。

真实硬件验证结果应记录到 sprint artifact 或专门的测试记录中，不应替代自动化测试。
