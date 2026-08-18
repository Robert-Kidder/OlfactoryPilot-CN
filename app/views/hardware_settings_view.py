from __future__ import annotations

from dataclasses import dataclass, replace

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.models import (
    ChannelDescriptor,
    ChannelVerification,
    HardwareConnectionConfig,
    HardwareProfile,
    SelectorConfig,
    VerificationStatus,
)


@dataclass(frozen=True, slots=True)
class HardwareChannelDraft:
    external_port: int
    display_name: str
    internal_valve: int | None
    target: str
    active_high: bool
    enabled: bool
    verification: ChannelVerification
    mapping_changed: bool = False

    @classmethod
    def from_descriptor(cls, descriptor: ChannelDescriptor) -> HardwareChannelDraft:
        return cls(
            external_port=descriptor.external_port,
            display_name=descriptor.display_name,
            internal_valve=descriptor.internal_valve,
            target=descriptor.target,
            active_high=descriptor.active_high,
            enabled=descriptor.enabled,
            verification=descriptor.verification,
        )


@dataclass(frozen=True, slots=True)
class HardwareProfileDraft:
    profile_name: str
    channels: tuple[HardwareChannelDraft, ...]
    selector: SelectorConfig | None
    max_total_sccm: float
    max_sample_a_sccm: float
    max_vacuum_c_sccm: float
    serial_port: str = ""
    ni_device_ids_text: str = "Dev1, Dev2"
    alicat_a_unit_id: str = "a"
    alicat_b_unit_id: str = "b"
    alicat_c_unit_id: str = "c"
    revision: int = 0

    @classmethod
    def from_profile(
        cls,
        profile: HardwareProfile,
        *,
        revision: int = 0,
    ) -> HardwareProfileDraft:
        return cls(
            profile_name=profile.profile_name,
            channels=tuple(
                HardwareChannelDraft.from_descriptor(channel)
                for channel in profile.channels
            ),
            selector=profile.selector,
            max_total_sccm=profile.max_total_sccm,
            max_sample_a_sccm=profile.max_sample_a_sccm,
            max_vacuum_c_sccm=profile.max_vacuum_c_sccm,
            serial_port=profile.connections.serial_port or "",
            ni_device_ids_text=", ".join(profile.connections.ni_device_ids),
            alicat_a_unit_id=profile.connections.alicat_a_unit_id,
            alicat_b_unit_id=profile.connections.alicat_b_unit_id,
            alicat_c_unit_id=profile.connections.alicat_c_unit_id,
            revision=revision,
        )

    def to_profile(self) -> HardwareProfile:
        return HardwareProfile(
            schema_version=1,
            profile_name=self.profile_name,
            channels=tuple(
                ChannelDescriptor(
                    external_port=channel.external_port,
                    internal_valve=channel.internal_valve,
                    target=channel.target,
                    active_high=channel.active_high,
                    enabled=channel.enabled,
                    display_name=channel.display_name,
                    verification=channel.verification,
                )
                for channel in self.channels
            ),
            selector=self.selector,
            connections=HardwareConnectionConfig(
                serial_port=self.serial_port,
                ni_device_ids=tuple(
                    value.strip()
                    for value in self.ni_device_ids_text.split(",")
                    if value.strip()
                ),
                alicat_a_unit_id=self.alicat_a_unit_id,
                alicat_b_unit_id=self.alicat_b_unit_id,
                alicat_c_unit_id=self.alicat_c_unit_id,
            ),
            max_total_sccm=self.max_total_sccm,
            max_sample_a_sccm=self.max_sample_a_sccm,
            max_vacuum_c_sccm=self.max_vacuum_c_sccm,
        )


@dataclass(frozen=True, slots=True)
class HardwareSettingsSnapshot:
    profile: HardwareProfile
    revision: int = 0
    can_edit: bool = False
    can_save: bool = False
    can_mock_verify: bool = False
    can_request_physical_verification: bool = False
    rollback_available: bool = False
    save_in_progress: bool = False
    status_text: str = "硬件方案只读。"
    detail_text: str = "请先断开设备并确认系统处于安全状态。"


class HardwareSettingsView(QWidget):
    """编辑 HardwareProfile 候选；验证、保存和回滚都只发布 intent。"""

    candidate_changed = Signal(object)
    save_requested = Signal(object, int)
    rollback_requested = Signal(int)
    mock_verify_requested = Signal(int, object)
    physical_verify_requested = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rendering = False
        self._snapshot: HardwareSettingsSnapshot | None = None
        self._draft: HardwareProfileDraft | None = None
        self.name_inputs: dict[int, QLineEdit] = {}
        self.internal_inputs: dict[int, QSpinBox] = {}
        self.target_inputs: dict[int, QLineEdit] = {}
        self.polarity_inputs: dict[int, QComboBox] = {}
        self.enabled_checks: dict[int, QCheckBox] = {}
        self.verification_labels: dict[int, QLabel] = {}
        self.mock_buttons: dict[int, QPushButton] = {}
        self.physical_buttons: dict[int, QPushButton] = {}

        self.profile_name_label = QLabel("硬件方案：-")
        self.status_label = QLabel("硬件方案只读。")
        self.detail_label = QLabel("请先断开设备并确认系统处于安全状态。")
        self.status_label.setWordWrap(True)
        self.detail_label.setWordWrap(True)

        connection_group = QGroupBox("连接参数（仅保存配置，不探测硬件）")
        connection_layout = QGridLayout()
        self.serial_port_input = QLineEdit()
        self.serial_port_input.setPlaceholderText("例如 COM6；Mock 可留空")
        self.ni_device_ids_input = QLineEdit()
        self.ni_device_ids_input.setPlaceholderText("例如 Dev1, Dev2")
        self.alicat_unit_inputs: dict[str, QLineEdit] = {}
        connection_layout.addWidget(QLabel("COM 端口"), 0, 0)
        connection_layout.addWidget(self.serial_port_input, 0, 1)
        connection_layout.addWidget(QLabel("NI device IDs（逗号分隔）"), 1, 0)
        connection_layout.addWidget(self.ni_device_ids_input, 1, 1, 1, 3)
        for column, channel in enumerate(("A", "B", "C"), start=1):
            unit_input = QLineEdit()
            unit_input.setMaxLength(1)
            unit_input.setPlaceholderText(channel.lower())
            self.alicat_unit_inputs[channel] = unit_input
            connection_layout.addWidget(QLabel(f"Alicat {channel} unit ID"), 2, column - 1)
            connection_layout.addWidget(unit_input, 3, column - 1)
        connection_group.setLayout(connection_layout)

        self.serial_port_input.textChanged.connect(
            lambda value: self._update_connections(serial_port=value)
        )
        self.ni_device_ids_input.textChanged.connect(
            lambda value: self._update_connections(ni_device_ids_text=value)
        )
        for channel, input_control in self.alicat_unit_inputs.items():
            input_control.textChanged.connect(
                lambda value, key=channel: self._update_connections(
                    **{f"alicat_{key.lower()}_unit_id": value}
                )
            )

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.addWidget(connection_group)
        basic_group = QGroupBox("气口配置")
        basic_layout = QGridLayout()
        headers = (
            "机外气口",
            "显示名称",
            "内部阀位",
            "启用",
            "验证状态",
            "Mock 验证",
            "物理验证",
        )
        for column, header in enumerate(headers):
            basic_layout.addWidget(QLabel(header), 0, column)

        for port in range(1, 21):
            name_input = QLineEdit()
            name_input.setMaxLength(80)
            internal_input = QSpinBox()
            internal_input.setRange(0, 20)
            internal_input.setSpecialValueText("未配置")
            enabled = QCheckBox("启用")
            verification = QLabel("待验证")
            verification.setWordWrap(True)
            mock_button = QPushButton("执行 Mock 验证")
            physical_button = QPushButton("申请物理验证授权")
            physical_button.setToolTip(
                "此按钮只提交授权申请，不会直接操作 NI、Alicat 或任何真实硬件。"
            )
            self.name_inputs[port] = name_input
            self.internal_inputs[port] = internal_input
            self.enabled_checks[port] = enabled
            self.verification_labels[port] = verification
            self.mock_buttons[port] = mock_button
            self.physical_buttons[port] = physical_button
            basic_layout.addWidget(QLabel(str(port)), port, 0)
            basic_layout.addWidget(name_input, port, 1)
            basic_layout.addWidget(internal_input, port, 2)
            basic_layout.addWidget(enabled, port, 3)
            basic_layout.addWidget(verification, port, 4)
            basic_layout.addWidget(mock_button, port, 5)
            basic_layout.addWidget(physical_button, port, 6)
            name_input.textChanged.connect(
                lambda value, p=port: self._update_channel(p, display_name=value)
            )
            internal_input.valueChanged.connect(
                lambda value, p=port: self._update_channel(
                    p,
                    internal_valve=value or None,
                    mapping_changed=True,
                )
            )
            enabled.toggled.connect(
                lambda value, p=port: self._update_channel(p, enabled=value)
            )
            mock_button.clicked.connect(
                lambda _checked=False, p=port: self._request_mock_verification(p)
            )
            physical_button.clicked.connect(
                lambda _checked=False, p=port: self.physical_verify_requested.emit(p)
            )
        basic_group.setLayout(basic_layout)
        content_layout.addWidget(basic_group)

        advanced_group = QGroupBox("高级/开发者设置（NI target 与输出极性）")
        advanced_layout = QGridLayout()
        advanced_layout.addWidget(QLabel("机外气口"), 0, 0)
        advanced_layout.addWidget(QLabel("NI target"), 0, 1)
        advanced_layout.addWidget(QLabel("输出极性"), 0, 2)
        for port in range(1, 21):
            target_input = QLineEdit()
            target_input.setPlaceholderText("例如 Dev1/P0.0")
            polarity_input = QComboBox()
            polarity_input.addItem("高电平开启", True)
            polarity_input.addItem("低电平开启", False)
            self.target_inputs[port] = target_input
            self.polarity_inputs[port] = polarity_input
            advanced_layout.addWidget(QLabel(str(port)), port, 0)
            advanced_layout.addWidget(target_input, port, 1)
            advanced_layout.addWidget(polarity_input, port, 2)
            target_input.textChanged.connect(
                lambda value, p=port: self._update_channel(
                    p,
                    target=value,
                    mapping_changed=True,
                )
            )
            polarity_input.currentIndexChanged.connect(
                lambda _index, p=port: self._update_channel(
                    p,
                    active_high=bool(self.polarity_inputs[p].currentData()),
                    mapping_changed=True,
                )
            )
        advanced_group.setLayout(advanced_layout)
        content_layout.addWidget(advanced_group)
        content_layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        self.save_button = QPushButton("保存候选硬件方案")
        self.rollback_button = QPushButton("显式回滚到上一已知可用方案")
        actions = QHBoxLayout()
        actions.addWidget(self.save_button)
        actions.addWidget(self.rollback_button)
        actions.addStretch()

        layout = QVBoxLayout()
        layout.addWidget(self.profile_name_label)
        layout.addWidget(self.status_label)
        layout.addWidget(self.detail_label)
        layout.addWidget(scroll)
        layout.addLayout(actions)
        self.setLayout(layout)

        self.save_button.clicked.connect(self._request_save)
        self.rollback_button.clicked.connect(self._request_rollback)

    @property
    def draft(self) -> HardwareProfileDraft | None:
        return self._draft

    def render_snapshot(self, snapshot: HardwareSettingsSnapshot) -> None:
        if not isinstance(snapshot, HardwareSettingsSnapshot):
            raise TypeError("硬件设置 View 只能渲染 HardwareSettingsSnapshot。")
        self._snapshot = snapshot
        self._draft = HardwareProfileDraft.from_profile(
            snapshot.profile,
            revision=snapshot.revision,
        )
        self._rendering = True
        try:
            self.serial_port_input.setText(self._draft.serial_port)
            self.ni_device_ids_input.setText(self._draft.ni_device_ids_text)
            self.alicat_unit_inputs["A"].setText(self._draft.alicat_a_unit_id)
            self.alicat_unit_inputs["B"].setText(self._draft.alicat_b_unit_id)
            self.alicat_unit_inputs["C"].setText(self._draft.alicat_c_unit_id)
            for channel in self._draft.channels:
                port = channel.external_port
                self.name_inputs[port].setText(channel.display_name)
                self.internal_inputs[port].setValue(channel.internal_valve or 0)
                self.target_inputs[port].setText(channel.target)
                polarity_index = self.polarity_inputs[port].findData(
                    channel.active_high
                )
                self.polarity_inputs[port].setCurrentIndex(max(0, polarity_index))
                self.enabled_checks[port].setChecked(channel.enabled)
                self.verification_labels[port].setText(
                    self._verification_text(channel)
                )
        finally:
            self._rendering = False

        self.profile_name_label.setText(f"硬件方案：{snapshot.profile.profile_name}")
        self.status_label.setText(snapshot.status_text)
        self.detail_label.setText(snapshot.detail_text)
        for control in (
            self.serial_port_input,
            self.ni_device_ids_input,
            *self.alicat_unit_inputs.values(),
        ):
            control.setEnabled(snapshot.can_edit and not snapshot.save_in_progress)
        for port in range(1, 21):
            for control in (
                self.name_inputs[port],
                self.internal_inputs[port],
                self.target_inputs[port],
                self.polarity_inputs[port],
                self.enabled_checks[port],
            ):
                control.setEnabled(snapshot.can_edit and not snapshot.save_in_progress)
            self.mock_buttons[port].setEnabled(
                snapshot.can_mock_verify and not snapshot.save_in_progress
            )
            self.physical_buttons[port].setEnabled(
                snapshot.can_request_physical_verification
                and not snapshot.save_in_progress
            )
        self.save_button.setEnabled(snapshot.can_save and not snapshot.save_in_progress)
        self.rollback_button.setEnabled(
            snapshot.can_save
            and snapshot.rollback_available
            and not snapshot.save_in_progress
        )
        self.save_button.setText(
            "正在校验并写入…" if snapshot.save_in_progress else "保存候选硬件方案"
        )

    def render_profile(
        self,
        profile: HardwareProfile,
        revision: int,
        can_save: bool,
        message: str = "",
        rollback_available: bool = False,
    ) -> None:
        self.render_snapshot(
            HardwareSettingsSnapshot(
                profile=profile,
                revision=revision,
                can_edit=can_save,
                can_save=can_save,
                can_mock_verify=can_save,
                can_request_physical_verification=can_save,
                rollback_available=rollback_available,
                status_text=message or ("可以编辑并保存候选方案。" if can_save else "硬件方案只读。"),
                detail_text=(
                    "物理验证必须另行授权；本页不会直接操作真实硬件。"
                ),
            )
        )

    def render_permissions(
        self,
        *,
        can_save: bool,
        message: str = "",
        rollback_available: bool | None = None,
    ) -> None:
        """Refresh only gates/status while preserving the user's draft."""

        if self._snapshot is None:
            return
        snapshot = replace(
            self._snapshot,
            can_edit=bool(can_save),
            can_save=bool(can_save),
            can_mock_verify=bool(can_save),
            can_request_physical_verification=bool(can_save),
            rollback_available=(
                self._snapshot.rollback_available
                if rollback_available is None
                else bool(rollback_available)
            ),
            status_text=message or (
                "可以编辑并保存候选方案。"
                if can_save
                else "硬件方案只读。"
            ),
        )
        self._snapshot = snapshot
        self.status_label.setText(snapshot.status_text)
        self.detail_label.setText(snapshot.detail_text)
        for control in (
            self.serial_port_input,
            self.ni_device_ids_input,
            *self.alicat_unit_inputs.values(),
        ):
            control.setEnabled(snapshot.can_edit and not snapshot.save_in_progress)
        for port in range(1, 21):
            for control in (
                self.name_inputs[port],
                self.internal_inputs[port],
                self.target_inputs[port],
                self.polarity_inputs[port],
                self.enabled_checks[port],
            ):
                control.setEnabled(snapshot.can_edit and not snapshot.save_in_progress)
            self.mock_buttons[port].setEnabled(
                snapshot.can_mock_verify and not snapshot.save_in_progress
            )
            self.physical_buttons[port].setEnabled(
                snapshot.can_request_physical_verification
                and not snapshot.save_in_progress
            )
        self.save_button.setEnabled(snapshot.can_save and not snapshot.save_in_progress)
        self.rollback_button.setEnabled(
            snapshot.can_save
            and snapshot.rollback_available
            and not snapshot.save_in_progress
        )

    def _update_channel(self, external_port: int, **changes) -> None:
        if self._rendering or self._draft is None:
            return
        channels = list(self._draft.channels)
        index = external_port - 1
        channels[index] = replace(channels[index], **changes)
        self._draft = replace(self._draft, channels=tuple(channels))
        self.verification_labels[external_port].setText(
            self._verification_text(channels[index])
        )
        try:
            candidate = self._draft.to_profile()
        except ValueError as exc:
            self.status_label.setText(f"候选配置尚未通过校验：{exc}")
            return
        self.candidate_changed.emit(candidate)

    def _update_connections(self, **changes) -> None:
        if self._rendering or self._draft is None:
            return
        self._draft = replace(self._draft, **changes)
        try:
            candidate = self._draft.to_profile()
        except ValueError as exc:
            self.status_label.setText(f"候选连接参数尚未通过校验：{exc}")
            return
        self.candidate_changed.emit(candidate)

    def _request_save(self) -> None:
        if self._draft is not None:
            try:
                candidate = self._draft.to_profile()
            except ValueError as exc:
                self.status_label.setText(f"候选配置不能保存：{exc}")
                return
            self.save_requested.emit(candidate, self._draft.revision)

    def _request_rollback(self) -> None:
        if self._snapshot is not None:
            self.rollback_requested.emit(self._snapshot.revision)

    def _request_mock_verification(self, external_port: int) -> None:
        if self._draft is None:
            return
        try:
            candidate = self._draft.to_profile()
        except ValueError as exc:
            self.status_label.setText(f"候选配置不能进行 Mock 验证：{exc}")
            return
        self.mock_verify_requested.emit(external_port, candidate)

    @staticmethod
    def _verification_text(channel: HardwareChannelDraft) -> str:
        if channel.mapping_changed:
            return "配置已变更，请重新验证"
        verification = channel.verification
        date = f" · {verification.verified_at}" if verification.verified_at else ""
        labels = {
            VerificationStatus.PENDING: "待验证",
            VerificationStatus.MOCK_VERIFIED: f"Mock 验证通过{date}",
            VerificationStatus.PHYSICAL_VERIFIED: f"物理验证通过{date}",
            VerificationStatus.MAPPING_CHANGED: "配置已变更，请重新验证",
            VerificationStatus.INCOMPLETE: "验证未完成",
            VerificationStatus.FAILED: "验证失败，请检查接线",
        }
        return labels[verification.status]
