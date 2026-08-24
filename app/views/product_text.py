from __future__ import annotations

_REPLACEMENTS = (
    ("方案 B V3", ""),
    ("V3", ""),
    ("Mock 验证", "测试气口"),
    ("Mock", "离线测试"),
    ("mock_verified", "测试通过"),
    ("intent", "请求"),
    ("安全收敛", "安全停止"),
    ("申请物理验证授权", "现场测试"),
    ("已由用户验证", "测试通过"),
    ("Story 4.6", "当前版本"),
    ("HardwareProfile", "硬件配置"),
    ("ChannelRegistry", "气口映射"),
    ("FlowWorker", "设备控制"),
    ("Worker", "设备控制"),
    ("Controller", "控制程序"),
    ("HAL", "硬件接口"),
    ("snapshot", "设备状态"),
    ("epoch", "操作批次"),
    ("selector", "气路选择器"),
    ("CONFIG_CHANGE", "设置更新"),
    ("MANUAL lease", "手动控制权"),
    ("manual owner", "手动控制程序"),
    ("owner handoff", "控制权交接"),
    ("owner", "控制程序"),
    ("lease", "控制权"),
    ("SAFE", "正常"),
    ("FAIL", "未通过"),
)


def user_facing_text(value: object) -> str:
    """Translate controller diagnostics into concise operator-facing text."""

    text = str(value or "")
    for source, target in _REPLACEMENTS:
        text = text.replace(source, target)
    return " ".join(text.split())
