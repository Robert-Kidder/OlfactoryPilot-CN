---
title: '修正现场 NI 硬件清单与 Dev3 说明'
type: 'chore'
created: '2026-07-30'
status: 'done'
archived_to_project_docs: '2026-07-30'
review_loop_iteration: 0
baseline_commit: 'b1cfab9507543db8b1fdb59be5dc80650496591a'
context:
  - '{project-root}/docs/project-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 当前项目说明和配置模板把 NI USB-6501 `Dev3` 写成既有硬件要求，但用户已根据机箱实物与 NI 官方设备图片确认现场没有该设备，NI MAX 与 NI-DAQmx 也只检测到两台 USB-6001（`Dev1`、`Dev2`）。这会让真实硬件自检产生误导性失败，并让操作员误以为需要寻找或购买一台未参与阀门映射的设备。

**Approach:** 将当前生产硬件基线修正为 `Dev1 + Dev2 + Alicat A/B/C`；把 Dev3 表述为“当前实验台未安装、当前控制链不依赖”的历史预留项。同步默认配置和本机配置模板，使真实自检只要求 Dev1、Dev2，同时保留日后扩展 USB-6501 时通过本机覆盖配置登记的能力。

## Boundaries & Constraints

**Always:** 记录用户基于机箱实物和 NI 官方设备图片确认当前实验台未安装 USB-6501；保持现有 20 通道 `valve_mapping`、主阀、AI0、AI6 和 Alicat A/B/C 映射不变；说明三块上方电路板不是 NI USB-6501。

**Ask First:** 若后续可读铭牌、采购清单或重新连接后的 NI MAX 证明存在 USB-6501，需由用户确认其用途和设备名后再加入生产要求。

**Never:** 不修改真实阀门线路，不执行 NI 数字输出，不开阀，不把电源模块、阀驱动板或 Alicat 误写成 Dev3，不重写历史 Story/HIL 记录中当时的现场证据。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 当前现场 | DAQmx 仅枚举 Dev1/Dev2，二者均为 USB-6001 | 生产自检要求 Dev1、Dev2，不因缺少 Dev3 失败 | COM6、Alicat 和其他安全门禁仍独立检查 |
| 日后发现 Dev3 | USB-6501 铭牌和 NI MAX 设备身份均确认 | 通过 `local_config.json` 显式加入 `ni_devices` | 未确认用途前不加入阀门映射 |
| 当前实物确认 | 用户对照 NI 官方设备图片检查机箱 | 文档记录当前实验台未安装 USB-6501 | 若以后硬件扩展则重新登记 |

</frozen-after-approval>

## Code Map

- `docs/project-context.md` -- 当前硬件清单与长期映射说明。
- `docs/architecture.md` -- 默认配置与本机覆盖配置的职责边界。
- `config/default_config.json` -- 通用自检设备要求和 Mock 默认配置。
- `config/local_config.example.json` -- 当前实验台真实配置模板。
- `app/services/hardware_check_service.py` -- 缺少显式设备配置时的旧 USB-6501 fallback。
- `tests/test_app.py` -- HardwareCheckService 的设备匹配与默认行为覆盖。

## Tasks & Acceptance

**Execution:**
- [x] `docs/project-context.md` -- 将 Dev3 改为当前未安装的历史预留项，明确当前实际控制链只使用 Dev1、Dev2。
- [x] `docs/architecture.md` -- 记录现场硬件清单必须由 NI MAX/铭牌确认，本机覆盖可登记可选扩展设备。
- [x] `config/default_config.json`、`config/local_config.example.json` -- 将生产自检清单改为 Dev1、Dev2，不触碰阀门映射。
- [x] `app/services/hardware_check_service.py`、`tests/test_app.py` -- 将无显式配置时的安全 fallback 与当前双 USB-6001 基线一致，并验证两个设备必须分别存在。

**Acceptance Criteria:**
- Given 当前 NI MAX 只检测到 Dev1、Dev2，when 使用更新后的真实配置自检，then 两台 USB-6001 均独立通过且不会要求 Dev3。
- Given 当前 20 通道配置，when 比较变更前后 `valve_mapping`，then 21 个气味阀/主阀目标完全不变。
- Given 文档被现场操作员阅读，when 查找 USB-6501/Dev3，then 能明确知道当前实验台未安装该设备、它也不是 Story 3.5 HIL 的必需设备。
- Given 日后确认 USB-6501，when 更新本机覆盖配置，then 可恢复 Dev3 自检而无需修改通用阀门映射。

## Design Notes

设备型号与“是否接入当前控制链”是两个不同事实。本次由用户对照 NI 官方设备图片检查机箱实物，结合 NI MAX/DAQmx 当前在线清单，确认当前实验台未安装 USB-6501。因此当前基线移除 Dev3 的强制要求，但保留其历史与可选扩展身份。

## Verification

**Commands:**
- `python -m pytest tests/test_app.py -q -p no:cacheprovider` -- HardwareCheckService 设备清单测试通过。
- `python -m ruff check app/services/hardware_check_service.py tests/test_app.py` -- 无静态检查问题。
- `git diff --check` -- 文档与配置格式无空白错误。
- 只读运行 `System.local().devices` -- 当前仅报告 `Dev1|USB-6001`、`Dev2|USB-6001`。

## Suggested Review Order

**设备身份与自检边界**

- 精确匹配设备别名和型号，阻止 Dev10 或错误型号冒充。
  [`hardware_check_service.py:12`](../../../app/services/hardware_check_service.py#L12)

- 默认自检只要求当前两台 USB-6001。
  [`hardware_check_service.py:92`](../../../app/services/hardware_check_service.py#L92)

- 设备别名与产品型号在同一判定点验证。
  [`hardware_check_service.py:133`](../../../app/services/hardware_check_service.py#L133)

**现场硬件真源**

- 项目上下文明确当前未安装 Dev3。
  [`project-context.md:66`](../../project-context.md#L66)

- 架构说明规定铭牌、NI MAX 与本机覆盖职责。
  [`architecture.md:98`](../../architecture.md#L98)

**配置与回归**

- 通用配置只要求 Dev1、Dev2。
  [`default_config.json:68`](../../../config/default_config.json#L68)

- 真实本机模板不再包含 Dev3。
  [`local_config.example.json:5`](../../../config/local_config.example.json#L5)

- 错误别名与错误型号均有回归保护。
  [`test_app.py:677`](../../../tests/test_app.py#L677)
