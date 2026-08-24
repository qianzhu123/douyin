# Douyin 直播间人数悬浮窗

本地 Chrome/Edge Manifest V3 插件,打开抖音直播间页面后右上角自动显示圆形悬浮按钮,实时显示观看人数,点击展开详情面板。

## 功能

- 打开任意 `live.douyin.com/<web_rid>` 直播间后,右上角自动出现圆形悬浮按钮(默认 `top:96px right:24px`),显示当前观看人数。
- 点击按钮展开详情面板:房间标题、主播昵称、当前观看、累计观看、点赞数、跳转本地控制台。
- 每 30 秒自动刷新一次,数字异常时显示 `--`。
- 悬浮按钮可拖动调整位置,设置保存在 `localStorage`。

## 本地安装

1. 启动本地前端与后端。
2. Chrome/Edge 扩展管理 → 启用开发者模式 → 加载已解压的扩展程序。
3. 选择本目录:`tools/douyin_extensions/live-overlay`。

## 兼容性

- 与 `tools/douyin_extensions/downloader` 互不冲突,可同时启用。
- 仅匹配 `https://live.douyin.com/*`,不会注入其他站点。
