# -*- coding: utf-8 -*-
"""
Message Bar Tamer  (v0.2)
=========================
屏蔽 / 减弱 QGIS 地图画布顶部的 QgsMessageBar 提示横幅（红色 / 黄色）。

原理
----
QGIS 内部、插件、算法报错，最终几乎都会调用 `QgsMessageBar.pushMessage(...)`
（或 `pushWidget(...)`）。本插件在初始化时把这两个方法「打补丁」（monkey-patch）：
在消息真正上屏之前，根据设置决定「放行 / 屏蔽 / 限时被自动收起」。

v0.2 新增能力
-------------
1. 关键词屏蔽：可填入若干关键词（每行一个），命中标题/正文的 Info/Warning
   横幅会被静音，从而精准屏蔽某个插件/某类噪音，而不会一刀切误伤重要提示。
   （可选：勾选后也对 Critical/红色生效。）
2. 一键切换：工具栏按钮 + 快捷键 Ctrl+Shift+M，随时开关屏蔽，无需进设置面板。
3. 三种工作模式：threshold（按级别过滤）/ off（完全隐藏）/ reduce（自动收起）。

被屏蔽的消息默认会同时写入「日志消息」面板，因此信息不会真正丢失。
"""

import os

from qgis.PyQt.QtCore import QSettings, QSize, QTimer, QObject, QEvent
from qgis.PyQt.QtGui import QKeySequence, QIcon, QFont
from qgis.PyQt.QtWidgets import (
    QAction,
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QGroupBox,
    QFrame,
    QCheckBox,
    QComboBox,
    QSpinBox,
    QLabel,
    QDialogButtonBox,
    QPlainTextEdit,
    QPushButton,
    QShortcut,
)
from qgis.core import Qgis, QgsApplication
from qgis.gui import QgsMessageBar, QgsMessageBarItem

# 级别数值（Qgis.MessageLevel）：Info=0, Warning=1, Critical=2, Success=3
LEVEL_INFO = Qgis.Info
LEVEL_WARNING = Qgis.Warning
LEVEL_CRITICAL = Qgis.Critical
LEVEL_SUCCESS = Qgis.Success

MODE_THRESHOLD = "threshold"
MODE_OFF = "off"
MODE_REDUCE = "reduce"

SETTINGS_NS = "messagebartamer"


class MessageBarTamer(QObject):
    def __init__(self, iface):
        super().__init__()
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.message_bar = None
        self.orig_pushMessage = None
        self.orig_pushWidget = None
        self.action_settings = None
        self.action_toggle = None
        self.shortcut = None
        self._handled_ids = set()         # 已处理过的横幅 item id，去重

        # 默认设置（随后用 QSettings 覆盖）
        self.settings = {
            "enabled": True,
            "mode": MODE_THRESHOLD,        # 默认「按级别过滤」：干掉 Info/Warning 黄色噪音
            "min_level": LEVEL_CRITICAL,   # threshold 模式下保留 >= Critical（红色报错）
            "auto_close_sec": 5,
            "log_filtered": True,
            "blocklist": "",               # 关键词，换行/逗号分隔
            "blocklist_all_levels": False,  # 关键词是否也作用于 Critical
        }
        self._load_settings()
        # 解析后的关键词列表（小写）
        self._block_keywords = self._parse_blocklist(self.settings["blocklist"])

    # ------------------------------------------------------------------ #
    # 插件生命周期
    # ------------------------------------------------------------------ #
    def initGui(self):
        self.action_settings = QAction(
            "Message Bar 调节器…", self.iface.mainWindow()
        )
        self.action_settings.triggered.connect(self.show_settings)
        self.iface.addPluginToMenu("&Message Bar Tamer", self.action_settings)

        self.action_toggle = QAction(
            "Message Bar：切换开/关", self.iface.mainWindow()
        )
        self.action_toggle.triggered.connect(self.toggle_enabled)
        self.iface.addPluginToMenu("&Message Bar Tamer", self.action_toggle)
        self.iface.addToolBarIcon(self.action_toggle)

        # 快捷键 Ctrl+Shift+M 一键切换
        self.shortcut = QShortcut(
            QKeySequence("Ctrl+Shift+M"), self.iface.mainWindow()
        )
        self.shortcut.activated.connect(self.toggle_enabled)

        self._install_patch()

    def unload(self):
        # 恢复原始方法，避免污染 QGIS 会话
        if self.message_bar is not None and self.orig_pushMessage is not None:
            try:
                self.message_bar.pushMessage = self.orig_pushMessage
                self.message_bar.pushWidget = self.orig_pushWidget
            except Exception:
                pass
        if self.message_bar is not None:
            try:
                self.message_bar.widgetAdded.disconnect(self._on_widget_added)
            except Exception:
                pass
            try:
                self.message_bar.removeEventFilter(self)
            except Exception:
                pass
        if self.action_settings is not None:
            self.iface.removePluginMenu("&Message Bar Tamer", self.action_settings)
        if self.action_toggle is not None:
            self.iface.removePluginMenu("&Message Bar Tamer", self.action_toggle)
            self.iface.removeToolBarIcon(self.action_toggle)
        if self.shortcut is not None:
            try:
                self.shortcut.deleteLater()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # 打补丁
    # ------------------------------------------------------------------ #
    def _install_patch(self):
        try:
            self.message_bar = self.iface.messageBar()
        except Exception:
            self.message_bar = None
        if self.message_bar is None:
            return
        if self.orig_pushMessage is None:
            self.orig_pushMessage = self.message_bar.pushMessage
            self.orig_pushWidget = self.message_bar.pushWidget
            self.message_bar.pushMessage = self._patched_pushMessage
            self.message_bar.pushWidget = self._patched_pushWidget
            # 信号兜底：拦截 C++ 内核（如瓦片网络超时）直接 push 的横幅。
            # monkey-patch 只拦 Python 侧，C++ 调用走 C++ 层、绕过了补丁，
            # 只能等 widget 出现后用 widgetAdded 信号移除。
            try:
                self.message_bar.widgetAdded.connect(self._on_widget_added)
            except Exception:
                pass
            # 事件过滤器兜底：即便 widgetAdded 信号因版本差异未触发，
            # 也能通过 ChildAdded 事件捕获新加入的横幅（C++ 内核瓦片超时等）。
            try:
                self.message_bar.installEventFilter(self)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # 关键词解析 / 匹配
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_blocklist(raw):
        if not raw:
            return []
        out = []
        for part in raw.replace(",", "\n").split("\n"):
            kw = part.strip().lower()
            if kw:
                out.append(kw)
        return out

    def _blocked(self, title, text, level):
        if not self._block_keywords:
            return False
        # 默认只屏蔽 Info/Warning，避免误伤 Critical（红色报错）
        if not self.settings["blocklist_all_levels"] and int(level) > int(LEVEL_WARNING):
            return False
        haystack = "{} {}".format(title, text).lower()
        for kw in self._block_keywords:
            if kw in haystack:
                return True
        return False

    # ------------------------------------------------------------------ #
    # 包装方法（self 是本插件实例）
    # ------------------------------------------------------------------ #
    def _patched_pushMessage(self, *args, **kwargs):
        if self.orig_pushMessage is None:
            return None

        # 直接传入 QgsMessageBarItem 的情形（如其它插件自定义横幅），原样放行
        if args and isinstance(args[0], QgsMessageBarItem):
            return self.orig_pushMessage(*args, **kwargs)

        # 未启用则完全恢复原行为
        if not self.settings["enabled"]:
            return self.orig_pushMessage(*args, **kwargs)

        title = args[0] if len(args) >= 1 else kwargs.get("title", "")
        text = args[1] if len(args) >= 2 else kwargs.get("text", "")
        level = args[2] if len(args) >= 3 else kwargs.get("level", LEVEL_INFO)
        duration = args[3] if len(args) >= 4 else kwargs.get("duration", 0)

        # 完全隐藏
        if self.settings["mode"] == MODE_OFF:
            self._maybe_log("（已隐藏）", title, text, level)
            return None

        # 关键词屏蔽（精准静音某类横幅）
        if self._blocked(title, text, level):
            self._maybe_log("（命中关键词已隐藏）", title, text, level)
            return None

        # 按级别过滤
        if self.settings["mode"] == MODE_THRESHOLD:
            if int(level) < int(self.settings["min_level"]):
                self._maybe_log("（低于阈值已隐藏）", title, text, level)
                return None

        # 自动收起：把停留时长压到上限内
        if self.settings["mode"] == MODE_REDUCE:
            cap = self.settings["auto_close_sec"]
            if duration == 0 or duration > cap:
                duration = cap

        return self.orig_pushMessage(title, text, level, duration)

    def _patched_pushWidget(self, *args, **kwargs):
        if self.orig_pushWidget is None:
            return None

        if not self.settings["enabled"]:
            return self.orig_pushWidget(*args, **kwargs)

        widget = args[0] if len(args) >= 1 else kwargs.get("widget", None)
        level = args[1] if len(args) >= 2 else kwargs.get("level", LEVEL_INFO)

        if self.settings["mode"] == MODE_OFF:
            self._maybe_log("（已隐藏 widget）", "", str(widget), level)
            return None

        if self._blocked("", str(widget), level):
            self._maybe_log("（命中关键词已隐藏 widget）", "", str(widget), level)
            return None

        if self.settings["mode"] == MODE_THRESHOLD and int(level) < int(self.settings["min_level"]):
            self._maybe_log("（低于阈值已隐藏 widget）", "", str(widget), level)
            return None

        # reduce 模式无法改变 widget 自带停留逻辑，原样放行
        return self.orig_pushWidget(*args, **kwargs)

    # ------------------------------------------------------------------ #
    # C++ 内核横幅兜底：widgetAdded 信号 + 事件过滤器 双保险
    # （monkey-patch 只能拦 Python 侧，C++ 内核瓦片网络超时等走 C++ 层）
    # ------------------------------------------------------------------ #
    def _on_widget_added(self, item):
        """widgetAdded 信号回调：C++ 内核直接 push 的横幅在此出现。"""
        if not isinstance(item, QgsMessageBarItem):
            return
        try:
            level = int(item.level())
            title = item.title() or ""
            text = item.text() or ""
        except Exception:
            level, title, text = LEVEL_INFO, "", ""
        self._handle_item(item, level, title, text)

    def eventFilter(self, obj, event):
        """事件过滤器兜底：捕获消息栏新加入的子控件（含 C++ 内核横幅）。"""
        if event.type() == QEvent.Type.ChildAdded:
            child = event.child()
            if isinstance(child, QgsMessageBarItem):
                try:
                    level = int(child.level())
                    title = child.title() or ""
                    text = child.text() or ""
                except Exception:
                    level, title, text = LEVEL_INFO, "", ""
                self._handle_item(child, level, title, text)
        return super().eventFilter(obj, event)

    def _handle_item(self, item, level, title, text):
        if not self.settings["enabled"] or item is None:
            return
        iid = id(item)
        if iid in self._handled_ids:
            return
        self._handled_ids.add(iid)
        action, note = self._decide(level, title, text)
        if action == "now":
            # 延迟一帧再移除：避免在处理信号/事件期间直接改动消息栏导致重入
            QTimer.singleShot(0, lambda: self._remove_item(item, note, title, text, level))
        elif action == "later":
            cap = self.settings["auto_close_sec"]
            QTimer.singleShot(cap * 1000, lambda: self._remove_item(item, note, title, text, level))
        # action == "keep"：原样保留

    def _decide(self, level, title, text):
        """根据当前模式决定如何处理一条横幅。返回 ('now'|'later'|'keep', 备注)。"""
        if self.settings["mode"] == MODE_OFF:
            return "now", "（已隐藏）"
        if self._blocked(title, text, level):
            return "now", "（命中关键词已隐藏）"
        if self.settings["mode"] == MODE_THRESHOLD and int(level) < int(self.settings["min_level"]):
            return "now", "（低于阈值已隐藏）"
        if self.settings["mode"] == MODE_REDUCE:
            return "later", "（自动收起）"
        return "keep", ""

    def _remove_item(self, item, note, title, text, level):
        if item is None or self.message_bar is None:
            return
        try:
            self._maybe_log(note, title, text, level)
            self.message_bar.popWidget(item)
        except Exception:
            pass
        self._handled_ids.discard(id(item))

    # ------------------------------------------------------------------ #
    # 一键切换
    # ------------------------------------------------------------------ #
    def toggle_enabled(self):
        self.settings["enabled"] = not self.settings["enabled"]
        self._save_settings()
        state = "已启用屏蔽" if self.settings["enabled"] else "已关闭屏蔽（恢复 QGIS 原生）"
        # 用原始方法显示，避免被自己的补丁拦掉
        try:
            self.orig_pushMessage("Message Bar Tamer", state, Qgis.Info, 3)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # 被屏蔽消息的兜底记录
    # ------------------------------------------------------------------ #
    def _maybe_log(self, note, title, text, level):
        if not self.settings["log_filtered"]:
            return
        try:
            from qgis.core import QgsMessageLog

            QgsMessageLog.logMessage(
                "{} {}: {}".format(note, title, text),
                "MessageBarTamer",
                level,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # 设置对话框
    # ------------------------------------------------------------------ #
    def show_settings(self):
        dlg = QDialog(self.iface.mainWindow())
        dlg.setWindowTitle("Message Bar 调节器")
        dlg.resize(500, 500)
        dlg.setMinimumWidth(460)

        # 配色：QGIS 品牌蓝 + 中性灰，适配浅色主题
        ACCENT = "#1f78b4"
        qss = """
            QDialog { background: #f5f7fa; }
            QLabel { color: #2b2b2b; }
            QGroupBox {
                border: 1px solid #d8dce3;
                border-radius: 8px;
                margin-top: 14px;
                padding: 12px 14px 14px 14px;
                background: #ffffff;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
                color: %s;
                font-weight: bold;
                font-size: 12px;
            }
            QCheckBox { spacing: 6px; }
            QComboBox, QSpinBox, QPlainTextEdit {
                border: 1px solid #c8cdd6;
                border-radius: 5px;
                padding: 5px 7px;
                background: #ffffff;
                selection-background-color: %s;
            }
            QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus {
                border: 1px solid %s;
            }
            QPlainTextEdit { line-height: 1.4; }
            QPushButton {
                border: 1px solid #c8cdd6;
                border-radius: 5px;
                padding: 7px 18px;
                background: #ffffff;
                color: #2b2b2b;
            }
            QPushButton:hover { border: 1px solid %s; }
            QPushButton#btn_ok {
                background: %s;
                color: #ffffff;
                font-weight: bold;
                border: 1px solid %s;
            }
            QPushButton#btn_ok:hover { background: #196299; }
        """ % (ACCENT, ACCENT, ACCENT, ACCENT, ACCENT, ACCENT)
        dlg.setStyleSheet(qss)

        main = QVBoxLayout(dlg)
        main.setContentsMargins(16, 16, 16, 16)
        main.setSpacing(12)

        # ---- 标题区 ----
        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        icon_label = QLabel()
        icon_label.setFixedSize(32, 32)
        try:
            icon = QgsApplication.getThemeIcon("mActionOptions.svg")
            if not icon.isNull():
                icon_label.setPixmap(icon.pixmap(QSize(28, 28)))
        except Exception:
            pass
        title_row.addWidget(icon_label)
        title_text = QVBoxLayout()
        title_text.setSpacing(3)
        t1 = QLabel("Message Bar 调节器")
        f = QFont()
        f.setPointSize(15)
        f.setBold(True)
        t1.setFont(f)
        t1.setStyleSheet("color: #1f2d3d;")
        t2 = QLabel("屏蔽 / 减弱地图画布顶部的红色、黄色提示横幅")
        t2.setStyleSheet("color: #6b7785; font-size: 11px;")
        title_text.addWidget(t1)
        title_text.addWidget(t2)
        title_row.addLayout(title_text)
        title_row.addStretch()
        main.addLayout(title_row)

        # ---- 分组：总开关 ----
        gb_enable = QGroupBox("总开关")
        gbe_layout = QVBoxLayout(gb_enable)
        self.chk_enabled = QCheckBox("启用屏蔽（取消勾选即恢复 QGIS 原生消息栏）")
        self.chk_enabled.setChecked(self.settings["enabled"])
        gbe_layout.addWidget(self.chk_enabled)
        main.addWidget(gb_enable)

        # ---- 分组：工作模式 ----
        gb_mode = QGroupBox("工作模式")
        gbm = QVBoxLayout(gb_mode)
        gbm.setSpacing(10)

        self.cmb_mode = QComboBox()
        self.cmb_mode.addItems([
            "按级别过滤（推荐：去掉黄色噪音，保留红色报错）",
            "完全隐藏所有横幅",
            "自动收起（限时显示后自动消失）",
        ])
        mode_idx = {
            MODE_THRESHOLD: 0,
            MODE_OFF: 1,
            MODE_REDUCE: 2,
        }.get(self.settings["mode"], 2)
        self.cmb_mode.setCurrentIndex(mode_idx)
        gbm.addWidget(self.cmb_mode)

        row_levels = QHBoxLayout()
        row_levels.setSpacing(16)
        col_level = QVBoxLayout()
        col_level.setSpacing(4)
        lbl_level = QLabel("保留级别 ≥（仅「按级别过滤」生效）")
        lbl_level.setStyleSheet("color: #6b7785; font-size: 11px;")
        self.cmb_level = QComboBox()
        # userData 统一存 int，与 QSettings 读回的 int 一致（枚举 vs int 严格不等）
        self.cmb_level.addItem("Info（信息）", int(LEVEL_INFO))
        self.cmb_level.addItem("Warning（警告 / 黄）", int(LEVEL_WARNING))
        self.cmb_level.addItem("Critical（严重 / 红）", int(LEVEL_CRITICAL))
        self.cmb_level.addItem("Success（成功 / 绿）", int(LEVEL_SUCCESS))
        idx = self.cmb_level.findData(self.settings["min_level"])
        self.cmb_level.setCurrentIndex(idx if idx >= 0 else 0)
        col_level.addWidget(lbl_level)
        col_level.addWidget(self.cmb_level)
        col_auto = QVBoxLayout()
        col_auto.setSpacing(4)
        lbl_auto = QLabel("自动收起时长（仅「自动收起」生效）")
        lbl_auto.setStyleSheet("color: #6b7785; font-size: 11px;")
        self.spin_auto = QSpinBox()
        self.spin_auto.setRange(1, 60)
        self.spin_auto.setSuffix(" 秒")
        self.spin_auto.setValue(self.settings["auto_close_sec"])
        col_auto.addWidget(lbl_auto)
        col_auto.addWidget(self.spin_auto)
        row_levels.addLayout(col_level)
        row_levels.addLayout(col_auto)
        row_levels.addStretch()
        gbm.addLayout(row_levels)
        main.addWidget(gb_mode)

        # ---- 分组：关键词精准屏蔽 ----
        gb_kw = QGroupBox("关键词精准屏蔽")
        gkw = QVBoxLayout(gb_kw)
        gkw.setSpacing(8)
        hint = QLabel("每行一个关键词，命中标题/正文的横幅将被静音（不区分大小写）")
        hint.setStyleSheet("color: #6b7785; font-size: 11px;")
        gkw.addWidget(hint)
        self.txt_block = QPlainTextEdit()
        self.txt_block.setPlainText(self.settings["blocklist"])
        self.txt_block.setMinimumHeight(72)
        self.txt_block.setPlaceholderText("例如：\nCRS\nlayer\ndeprecated")
        gkw.addWidget(self.txt_block)
        self.chk_block_all = QCheckBox("关键词屏蔽也作用于 Critical（红色）横幅")
        self.chk_block_all.setChecked(self.settings["blocklist_all_levels"])
        gkw.addWidget(self.chk_block_all)
        main.addWidget(gb_kw)

        # ---- 分组：信息兜底 ----
        gb_log = QGroupBox("信息兜底")
        gl = QVBoxLayout(gb_log)
        self.chk_log = QCheckBox("被屏蔽的消息同时写入「日志消息」面板（避免错过报错）")
        self.chk_log.setChecked(self.settings["log_filtered"])
        gl.addWidget(self.chk_log)
        main.addWidget(gb_log)

        # ---- 底部按钮 ----
        main.addStretch()
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(dlg.reject)
        btn_ok = QPushButton("确定")
        btn_ok.setObjectName("btn_ok")
        btn_ok.clicked.connect(dlg.accept)
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_ok)
        main.addLayout(btn_row)

        # PyQt5 用 exec_()、PyQt6 用 exec()，用 hasattr 兼容两套 Qt 绑定
        res = dlg.exec() if hasattr(dlg, "exec") else dlg.exec_()
        if res:
            self._apply_settings()

    def _apply_settings(self):
        self.settings["enabled"] = self.chk_enabled.isChecked()
        self.settings["mode"] = {
            0: MODE_THRESHOLD,
            1: MODE_OFF,
            2: MODE_REDUCE,
        }.get(self.cmb_mode.currentIndex(), MODE_REDUCE)
        self.settings["min_level"] = self.cmb_level.currentData()
        self.settings["auto_close_sec"] = self.spin_auto.value()
        self.settings["blocklist"] = self.txt_block.toPlainText().strip()
        self.settings["blocklist_all_levels"] = self.chk_block_all.isChecked()
        self.settings["log_filtered"] = self.chk_log.isChecked()
        self._block_keywords = self._parse_blocklist(self.settings["blocklist"])
        self._save_settings()

    # ------------------------------------------------------------------ #
    # 设置持久化
    # ------------------------------------------------------------------ #
    def _save_settings(self):
        s = QSettings()
        s.setValue(SETTINGS_NS + "/enabled", self.settings["enabled"])
        s.setValue(SETTINGS_NS + "/mode", self.settings["mode"])
        s.setValue(SETTINGS_NS + "/min_level", int(self.settings["min_level"]))
        s.setValue(SETTINGS_NS + "/auto_close_sec", self.settings["auto_close_sec"])
        s.setValue(SETTINGS_NS + "/log_filtered", self.settings["log_filtered"])
        s.setValue(SETTINGS_NS + "/blocklist", self.settings["blocklist"])
        s.setValue(SETTINGS_NS + "/blocklist_all_levels", self.settings["blocklist_all_levels"])

    def _load_settings(self):
        s = QSettings()
        self.settings["enabled"] = s.value(SETTINGS_NS + "/enabled", True, type=bool)
        self.settings["mode"] = s.value(SETTINGS_NS + "/mode", MODE_REDUCE, type=str)
        self.settings["min_level"] = s.value(
            SETTINGS_NS + "/min_level", LEVEL_CRITICAL, type=int
        )
        self.settings["auto_close_sec"] = s.value(
            SETTINGS_NS + "/auto_close_sec", 5, type=int
        )
        self.settings["log_filtered"] = s.value(
            SETTINGS_NS + "/log_filtered", True, type=bool
        )
        self.settings["blocklist"] = s.value(SETTINGS_NS + "/blocklist", "", type=str)
        self.settings["blocklist_all_levels"] = s.value(
            SETTINGS_NS + "/blocklist_all_levels", False, type=bool
        )

        # ---- 迁移：v0.4 起默认改为「按级别过滤 + 保留 Critical」 ----
        # 旧版本默认「自动收起」对 C++ 内核持续刷新的横幅（如瓦片网络超时）
        # 几乎无效，用户感知为「没屏蔽」。升级后让默认即直接干掉黄色噪音。
        schema = s.value(SETTINGS_NS + "/schema_version", 0, type=int)
        if schema < 2 and self.settings["mode"] == MODE_REDUCE:
            self.settings["mode"] = MODE_THRESHOLD
            self.settings["min_level"] = LEVEL_CRITICAL
            s.setValue(SETTINGS_NS + "/mode", self.settings["mode"])
            s.setValue(SETTINGS_NS + "/min_level", int(self.settings["min_level"]))
        s.setValue(SETTINGS_NS + "/schema_version", 2)
