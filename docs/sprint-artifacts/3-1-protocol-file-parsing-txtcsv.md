---
baseline_commit: 91b3830fe7c3536987e6fb6ae1aeb9ff54122362
---

# Story 3.1: 协议文件解析 .txt/.csv

Status: review
Epic: 3 - 协议执行与数据记录
Story Key: 3-1-protocol-file-parsing-txtcsv
Story ID: 3.1

## Story

作为研究人员，
我需要加载旧系统兼容的 `.txt` 和 `.csv` 协议文件，
以便复用现有实验序列，并为后续呼吸门控、手动/TTL 触发、低抖动阀门动作和数据记录建立可靠输入。

## Acceptance Criteria

1. **支持 `.txt` 和 `.csv`**
   - Given 用户选择协议文件；
   - When 文件扩展名为 `.txt` 或 `.csv`；
   - Then 系统解析文件并返回结构化协议对象。
   - And 不支持的扩展名返回中文错误，不进入任何实验运行状态。

2. **解析 trial、timing、valve、trigger 和 metadata**
   - Given 协议文件格式有效；
   - When 解析完成；
   - Then 输出必须包含 document-level metadata 和按文件顺序保留的 trial 列表。
   - And 每个 trial 至少包含 `trial_id`、`timing_ms`、`duration_ms`、`valve`、`trigger`、`metadata`。
   - And `valve` 必须能映射到当前硬件变体允许的 10/20 通道范围。

3. **格式错误定位到行号和字段**
   - Given 协议文件存在格式错误；
   - When 解析失败；
   - Then 错误必须包含行号、字段名和中文可操作说明。
   - And 至少覆盖：空文件、缺少必填字段、非法数值、未知 trigger、阀门通道越界、无有效 trial。

4. **解析失败不留下部分运行状态**
   - Given 当前已有或没有有效协议；
   - When 新文件解析失败；
   - Then 不得保存部分 trial、推进 trial index、启动执行状态或准备任何硬件动作。
   - And 如已有上一份有效协议，应保持上一份协议不变；如没有，则保持未加载状态。

5. **协议页给出中文反馈**
   - Given 用户在“协议”页加载文件；
   - When 解析成功；
   - Then 页面显示文件名、trial 数量、trigger 摘要、metadata 摘要和 trial 预览。
   - When 解析失败；
   - Then 页面显示含行号和字段的中文错误，开始/触发类危险动作保持不可用。

## Tasks / Subtasks

- [x] 建立协议模型（AC: 2, 4）
  - [x] 新增 `app/models/protocol.py`，定义 `ProtocolDocument`、`ProtocolTrial` 和必要的 metadata 类型。
  - [x] 使用 dataclass 或等价轻量结构；不要引入新外部依赖。
  - [x] trigger 使用枚举或受控字符串集合，至少支持后续 Story 3.3 需要的 `manual`、`ttl`。
  - [x] 更新 `app/models/__init__.py`，导出协议模型。

- [x] 实现协议解析服务（AC: 1, 2, 3, 4）
  - [x] 新增 `app/services/protocol_parser.py`。
  - [x] `.csv` 使用 Python 标准库 `csv`；不要新增 pandas。
  - [x] `.txt` 支持制表符、逗号、分号或连续空白分隔的表格文本。
  - [x] 支持文件头部 `key=value` / `key: value` metadata 行；以 `#` 开头的注释行应忽略或作为 metadata 来源。
  - [x] 非核心列进入 trial-level metadata。
  - [x] 先完整校验所有行，全部通过后才返回 `ProtocolDocument`。
  - [x] 新增 `ProtocolParseError` 或等价结果类型，携带 `line_number`、`field`、`message`。
  - [x] 更新 `app/services/__init__.py`，导出解析服务和错误类型。

- [x] 接入最小协议 UI（AC: 1, 3, 5）
  - [x] 新增 `app/views/protocol_view.py`，包含加载按钮、路径显示、摘要区、错误区和 trial 预览。
  - [x] 替换 `app/views/main_window.py` 里的协议占位页：`self._build_tab("协议", "协议执行占位")`。
  - [x] 在 `app/controllers/main_controller.py` 增加协议加载处理函数，只做文件选择结果处理、调用解析服务、更新状态和 UI。
  - [x] 解析成功后才写入 `state.loaded_protocol` 或 controller 等价字段；解析失败不得覆盖当前有效协议。
  - [x] 本 story 不启用开始、暂停、手动触发或 TTL 触发；如果 UI 预留按钮，默认禁用。

- [x] 补充自动化测试（AC: 1, 2, 3, 4, 5）
  - [x] 新增 `tests/test_protocol_parser.py`。
  - [x] 新增 `tests/fixtures/protocols/`，至少包含有效 `.csv`、有效 `.txt`、缺字段、非法数值、未知 trigger、阀门越界、空文件。
  - [x] 测试 trial 顺序、字段归一化、metadata 合并和错误行号/字段。
  - [x] 测试失败原子性：解析服务失败不返回部分 document；控制器加载失败不覆盖上一份有效协议。
  - [x] 如新增 `ProtocolView`，补充轻量 UI 冒烟测试，复用现有 `qt_app` / `qtbot` fixture。

- [x] 工程验证（AC: 全部）
  - [x] 运行 `D:\miniconda3\envs\code\python.exe -m pytest tests/test_protocol_parser.py`。
  - [x] 运行 `D:\miniconda3\envs\code\python.exe -m pytest`。
  - [x] 运行 `D:\miniconda3\envs\code\python.exe -m ruff check app tests`。

## Dev Notes

### 需求来源

- PRD FR2.2 要解析旧系统兼容的 `.txt` 和 `.csv` 实验协议文件；FR2.1 自动会话文件命名和 FR2.3 `.raw/.log` 输出属于后续 story，不要在 3.1 中提前实现。来源：`docs/prd.md#FR2：文件与会话`
- Epic 3 覆盖 FR2.1、FR2.2、FR2.3、FR5.1、FR5.2、FR5.3；Story 3.1 只负责协议文件解析、错误定位和加载反馈。来源：`docs/epics.md#Epic-3-协议执行与数据记录`
- 实施就绪报告建议从 Story 3.1 开始，并提示旧法国软件协议格式仍需通过 PDF、样例文件或真实实验文件继续确认。因此实现应保守、可扩展，用 fixture 驱动兼容，不要把未确认格式写死成唯一格式。来源：`docs/implementation-readiness-report-2025-12-08.md#主要风险`

### 架构约束

- 项目采用 MVC + Worker + HAL。协议解析是纯业务服务，放在 `app/services/`；协议数据结构放在 `app/models/`；界面只负责选择文件和展示结果。来源：`docs/architecture.md#分层结构`
- 所有真实硬件访问必须经过 HAL。本 story 不能写阀门、主阀、Alicat、TTL 或 NI 设备，也不能启动执行 worker。来源：`docs/architecture.md#HAL-硬件抽象`
- 架构文档建议 Epic 3 新增协议模型、协议解析服务、会话记录服务和执行控制器。本 story 只实现协议模型与解析服务，并留下后续执行所需的数据契约。来源：`docs/architecture.md#协议与数据`
- 默认配置以 `config/default_config.json` 为当前真实来源。校验 valve 通道时应读取当前 `AppState` 的硬件变体和阀门映射，不要另建独立映射表。来源：`docs/architecture.md#配置来源`

### UX 约束

- UX 要求“协议”页显示当前 trial、下一个气味、触发模式、剩余时间和运行状态。本 story 只需实现加载后的摘要、trigger 摘要和 trial 预览，执行态留给 Story 3.2-3.4。来源：`docs/ux-design.md#协议页`
- 解析错误必须定位到行号，并说明字段问题；错误提示要说明发生了什么以及用户下一步应做什么。来源：`docs/ux-design.md#文案规范`
- 开始、暂停、停止按钮必须遵守安全联锁。本 story 不实现开始执行；如预留执行按钮，必须保持禁用。来源：`docs/ux-design.md#协议页`

### 当前代码状态

- `app/views/main_window.py` 当前只创建协议占位页：`self.tabs.addTab(self._build_tab("协议", "协议执行占位"), "协议")`。实现时应替换为 `ProtocolView`，不要把解析逻辑塞进 `MainWindow`。
- `app/controllers/main_controller.py` 已承担业务编排、安全状态和 UI 状态更新，已有 `handle_*` 风格槽函数。协议加载应沿用这个风格。
- `app/models/app_state.py` 已保存配置、硬件变体、阀门映射、阈值和安全状态。若新增 `loaded_protocol`，应为可空字段，不影响启动、自检、校准或预检默认路径。
- `app/services/__init__.py` 和 `app/models/__init__.py` 显式导出公共类型；新增服务/模型后要同步导出，方便测试和后续 story 复用。
- `config/default_config.json` 当前包含 `hardware_variant`、`valve_mapping.variants["20-channel"]` 和 `["10-channel"]`。解析器或控制器校验 valve 范围时不能假设永远 20 通道。

### 文件结构要求

- 新模型：`app/models/protocol.py`
- 新服务：`app/services/protocol_parser.py`
- 新 UI：`app/views/protocol_view.py`
- 可能更新：`app/models/__init__.py`、`app/services/__init__.py`、`app/models/app_state.py`、`app/controllers/main_controller.py`、`app/views/main_window.py`
- 新测试：`tests/test_protocol_parser.py`，必要时新增 `tests/test_protocol_view.py` 或控制器测试。
- 新 fixture：`tests/fixtures/protocols/`
- 不要把样例协议、临时解析脚本或实验输出放在项目根目录。来源：`docs/project-structure.md#新增文件放置规则`

### 解析格式建议

在没有真实旧协议样例前，实现一个保守兼容层：

- `.csv` 使用 `csv.Sniffer` 或受控 delimiter 列表识别逗号、分号、制表符。
- `.txt` 先识别注释/metadata 行，再识别带 header 的分隔表，最后处理连续空白分隔表。
- 核心字段别名建议：
  - `trial`: `trial`、`trial_id`、`index`、`试次`
  - `timing_ms`: `timing`、`timing_ms`、`onset_ms`、`time_ms`
  - `duration_ms`: `duration`、`duration_ms`、`stim_ms`
  - `valve`: `valve`、`channel`、`odor_valve`、`气味通道`
  - `trigger`: `trigger`、`trigger_mode`、`mode`
- 数值字段统一转换为 `int` 或 `float` 后进入模型；错误要保留原始文本用于提示。
- 如果后续拿到真实 ProgOlfactoTao 样例与上述别名不同，应先补 fixture 和测试，再扩展别名表。

### 测试要求

- 当前项目测试使用 `pytest` 和 `pytest-qt`，测试目录为 `tests/`，配置位于 `pytest.ini`。来源：`docs/project-structure.md#pytest`
- ruff 目标版本为 `py311`，规则来自 `ruff.toml`，新增代码需通过 `ruff check app tests`。来源：`docs/project-structure.md#ruff`
- 单元测试优先覆盖纯解析服务，避免为了 parser 测试启动 Qt 或硬件 worker。
- UI 测试只做轻量冒烟：验证加载区、摘要区、错误区存在，以及中文错误可显示。
- 失败原子性必须有测试：先加载有效协议，再加载错误协议，断言当前协议仍为第一份有效协议。

### Previous Story Intelligence

- Story 2.7 已完成校准 UI 优化，经验是只在需要处更新 View 和 Controller，保留已有业务服务不动，并补专门测试文件。来源：`docs/sprint-artifacts/2-7-calibration-ui-optimization.md`
- Epic 2 中预检和校准已建立中文状态反馈、按钮禁用和轻量 Qt 测试模式。Story 3.1 应复用这些风格。

### Git Intelligence

- 最近提交主要是统一中文主线文档、工程基线和项目资料清理。当前 story 应保持中文文档与 Python 3.11 基线一致，不引入 Poetry、pandas 或新的外部解析依赖。
- 近期提交没有协议解析实现，不能假设已有旧模块可复用；但必须复用现有 MVC、服务导出、pytest 和 ruff 约定。

### 明确不做

- 不实现 trial 执行状态机、呼吸门控等待、手动/TTL 推进、低抖动阀门动作或会话 `.raw/.log` 写入。
- 不直接控制硬件，不调用 HAL 写输出，不改变当前阀门或流量状态。
- 不在本 story 中解决所有未知旧协议变体；未知格式应通过 fixture 驱动增量兼容。

## References

- `docs/prd.md#FR2：文件与会话`
- `docs/prd.md#FR5：协议执行`
- `docs/epics.md#Epic-3-协议执行与数据记录`
- `docs/architecture.md#分层结构`
- `docs/architecture.md#协议与数据`
- `docs/ux-design.md#协议页`
- `docs/project-structure.md#新增文件放置规则`
- `docs/project-context.md#架构原则`
- `docs/implementation-readiness-report-2025-12-08.md#建议下一步`
- `app/views/main_window.py`
- `app/controllers/main_controller.py`
- `app/models/app_state.py`
- `app/services/__init__.py`
- `tests/conftest.py`
- `pytest.ini`
- `ruff.toml`

## Dev Agent Record

### Agent Model Used

GPT-5 Codex

### Debug Log References

- `python3 _bmad/scripts/resolve_customization.py --skill .agents/skills/bmad-dev-story --key workflow` 在当前 Windows 会话启动失败，已按技能 fallback 手动读取 `customize.toml`、团队/用户覆盖和 `_bmad/bmm/config.yaml`。
- `D:\miniconda3\envs\code\python.exe -m pytest tests/test_protocol_parser.py`：10 passed。
- `D:\miniconda3\envs\code\python.exe -m pytest tests/test_protocol_parser.py tests/test_protocol_view.py`：12 passed。
- `D:\miniconda3\envs\code\python.exe -m ruff check app tests`：All checks passed。
- `D:\miniconda3\envs\code\python.exe -m pytest`：134 passed。

### Completion Notes List

- 新增协议 dataclass 模型与 `TriggerMode`，导出给后续 Epic 3 story 复用。
- 新增无外部依赖的 `.txt` / `.csv` 解析服务，支持 metadata、字段别名、trial-level metadata、受控 trigger、当前硬件阀门映射校验和中文可操作错误。
- 新增协议页最小 UI 与控制器加载流程；解析成功才写入 `state.loaded_protocol`，失败时保留上一份有效协议，且开始/手动触发/TTL 触发控件保持禁用。
- 新增 parser、控制器原子性和 `ProtocolView` 冒烟测试夹具，覆盖有效文件、空文件、缺字段、非法数值、未知 trigger、阀门越界和无有效 trial。

### File List

- `app/controllers/main_controller.py`
- `app/models/__init__.py`
- `app/models/app_state.py`
- `app/models/protocol.py`
- `app/services/__init__.py`
- `app/services/protocol_parser.py`
- `app/views/__init__.py`
- `app/views/main_window.py`
- `app/views/protocol_view.py`
- `docs/sprint-artifacts/3-1-protocol-file-parsing-txtcsv.md`
- `docs/sprint-artifacts/sprint-status.yaml`
- `tests/fixtures/protocols/empty_protocol.csv`
- `tests/fixtures/protocols/invalid_number.csv`
- `tests/fixtures/protocols/missing_field.csv`
- `tests/fixtures/protocols/no_trials.txt`
- `tests/fixtures/protocols/unknown_trigger.csv`
- `tests/fixtures/protocols/valid_protocol.csv`
- `tests/fixtures/protocols/valid_protocol.txt`
- `tests/fixtures/protocols/valve_out_of_range.csv`
- `tests/test_protocol_parser.py`
- `tests/test_protocol_view.py`

## Change Log

- 2026-07-09：实现协议文件解析、最小协议 UI、控制器原子加载和自动化测试，状态更新为 review。
