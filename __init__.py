# -*- coding: utf-8 -*-
"""
Message Bar Tamer —— QGIS 插件入口
屏蔽 / 减弱地图画布顶部 MessageBar（红色 / 黄色横幅）提示。
"""

def classFactory(iface):
    from .message_bar_tamer import MessageBarTamer
    return MessageBarTamer(iface)
