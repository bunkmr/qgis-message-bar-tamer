# MessageBarTamer

> 屏蔽 / 减弱 QGIS 地图画布顶部 **MessageBar**（红色 / 黄色横幅报错）的插件。
> Tame or suppress the noisy QGIS MessageBar banners.

QGIS 原生没有全局开关能彻底关闭 MessageBar。本插件在消息上屏前「打补丁」拦截
`QgsMessageBar.pushMessage / pushWidget`，按规则处理——是目前最干净、不改 QGIS 源码的做法。

## 功能

- **三种工作模式**
  - 按级别过滤（默认推荐）：只显示 ≥ 设定级别的消息，干掉黄色噪音、保留红色报错。
  - 完全隐藏：所有横幅一律不出现，画布顶部彻底干净。
  - 自动收起：仍显示，但限时（默认 5 秒）后自动消失。
- **关键词精准屏蔽**：多行文本框，每行一个关键词；标题/正文命中即静音。默认只作用于 Info/Warning，避免误伤 Critical。
- **一键切换**：工具栏按钮 + 快捷键 `Ctrl+Shift+M` 随时开关（切换提示走原始通道，确保你看得到反馈）。
- **日志兜底**：被屏蔽的消息同时写入「日志消息」面板，关掉横幅 ≠ 错过报错。
- **C++ 内核横幅全覆盖**：除了 monkey-patch Python 侧推送，还连接 `QgsMessageBar.widgetAdded` 信号，拦截 C++ 内核直接弹出的横幅（如**瓦片网络超时**、部分 CRS 提示）——这类走 C++ 层、绕过了 Python 补丁，旧版会漏网，v0.3 起已被覆盖。

## 安装

### 方式一：手动安装
1. 把本仓库 `MessageBarTamer/` 目录复制到 QGIS 用户插件目录：
   - **QGIS 4.x**：`~/Library/Application Support/QGIS/QGIS4/profiles/default/python/plugins/MessageBarTamer/`
   - **QGIS 3.x**：`~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/MessageBarTamer/`
2. 重启 QGIS → 插件 ▸ 管理并安装插件 ▸ 已安装 → 勾选 **Message Bar Tamer**。

### 方式二：从发布包安装
下载 `MessageBarTamer.0.3.zip`，解压到上述插件目录即可。

## 使用
- 菜单 **插件 ▸ Message Bar Tamer ▸ Message Bar 调节器…** 调整模式与关键词，设置自动保存。
- 取消「启用屏蔽」即恢复 QGIS 原生行为；卸载插件会还原被改的方法，无残留。

## 兼容性
- 同时支持 **QGIS 3.x（PyQt5）** 与 **QGIS 4.x（PyQt6）**。`metadata.txt` 声明范围 `3.0 – 4.99`。

## 已知局限
- 极少数横幅若在 `widgetAdded` 信号触发前已被 C++ 同步绘制，可能极短暂闪现一帧后才被移除（移除动作延迟一帧执行以避免在信号回调中直接改动消息栏导致重入）。
- 关键词屏蔽默认不作用于 Critical（红色报错），如需连红色也静音，请在设置里勾选「关键词屏蔽也作用于 Critical」。

## License
MIT — 见 [LICENSE](LICENSE)。
