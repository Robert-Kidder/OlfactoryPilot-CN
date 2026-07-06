符号 ± 表示 ASCII 回车（CR，0x0D）。除特别说明外，命令对大小写不敏感；方括号为占位项。

## 1.1 串口基础参数

| 参数 | 默认值 | 备注 |
| :--- | :--- | :--- |
| Baud rate | 19200 | 主机与设备需一致 |
| Data bits | 8 |  |
| Parity | None |  |
| Stop bits | 1 |  |
| Flow control | None |  |

## 1.2 设备与模式管理

| 功能 | 命令格式 | 示例 | 说明 |
| :--- | :--- | :--- | :--- |
| 轮询（Polling）设备 | ［unit ID］ | $\mathrm{a} \leftarrow$ | 返回一行实时数据 |
| 修改 unit ID | ［current ID］＠［desired ID］ | $\mathrm{a} @ \mathrm{~b}$ | 仅在 Polling 模式下输入 |
| 进入流式 （Streaming）模式 | ［unit ID］＠＝＠ | $\mathrm{a} @=\ldots$ | 设备按固定间隔连续输出 |
| 退出流式模式 | ＠＠$=[$ desired ID $]$ | ＠＠$=\mathrm{a}$ | 停止流式并设定为该 ID |
| 设置流式输出间隔 | ［unit ID］w91＝［毫秒］ | $\mathrm{aw} 91=500 \leftarrow$ | 默认 50 ms |

## 1.3 计量基线（Tare）

| 功能 | 命令格式 | 示例 | 说明 |
| :--- | :--- | :--- | :--- |
| 流量清零 | ［unit ID］v ↓ | av | 需在无流状态下执行 |
| 绝对压力对大气清零＊ | ［unit ID］pc | apc | ＊带气压计型号可用 |

## 1.4 数据采集与描述

| 功能 | 命令格式 | 示例 | 说明 |
| :--- | :--- | :--- | :--- |
| 读取数据帧 （Polling） | ［unit ID］ | $\mathrm{a} \leftarrow$ | 返回：ID 绝对压 温度 体积流 质量流设定值 气体（以空格分隔） |
| 查询数据帧字段描述 | ［unit ID］？？d＊」 | a？？d＊ | 返回各列工程单位说明 |

## 1.5 设定值（Setpoint）控制

| 功能 | 命令格式 | 示例 | 说明 |
| :--- | :--- | :--- | :--- |
| 设定值（浮点） | ［unit ID］s［float］ ↓ | as15．44 | 负值用前缀连字符，如 as－15．44 |
| 设定值（整数） | ［unit ID］n［integer］ | an1500 | 部分场景提供整数接口 |

## 1.6 气体选择与 COMPOSER ${ }^{\text {TM }}$ 混气

| 功能 | 命令格式 | 示例 | 说明 |
| :--- | :--- | :--- | :--- |
| 查询内置气体列表 | ［unit ID］？？g＊↓ | a？？g＊ | 返回编号及名称 |
| 切换为某气体 | ［unit ID］g［Gas \＃］ | ag8 |  |
| 新建混气 （COMPOSER） | ［unit ID］gm［MixName］ ［Mix\＃］［Gas1\％］［Gas1\＃］ ［Gas2\％］［Gas2\＃］．．． | agm MyGas1 252 71.35719 .2589 .4 4 | Mix\＃236－255；名称 $\leq 6$ 字符；成分总和 $=100 \%$ |
| 选择／切换到某个用户混气 | ［unit ID］g［Mix\＃］ | ag255 |  |
| 删除某个混气 | ［unit ID］gd［Mix\＃］ | agd 252 |  |

## 1.7 阀控与面板锁定

| 功能 | 命令格式 | 说明 |
| :--- | :--- | :--- |
| 阀门保持当前开度 | ［unit ID］hp $\leftarrow$ |  |
| 阀门保持全关 | ［unit ID］hc ↓ |  |
| 取消阀门保持 | ［unit ID］c ↓ |  |
| 锁定前面板 | ［unit ID］l |  |
| 解锁前面板 | ［unit ID］u $\leftarrow$ |  |

## 1.8 当前可能交互流程

## 1．初始化与校准

流量清零：［unit ID］v」
压力清零（可选，带气压计型号）：［unit ID］pc

## 2．开启数据采集

进入流式模式：［unit ID］＠＝＠
设置输出间隔：［unit ID］w91＝［ms］

## 3．流量控制

设定目标流量：［unit ID］s［value］↓

## 4．数据读取与确认

流式模式自动输出或
轮询读取流量：［unit ID］
查询数据帧格式（开发初期执行一次即可）：［unit ID］？？d＊』

## 5．结束与复位

停止流式模式：＠＠＝［unit ID］

