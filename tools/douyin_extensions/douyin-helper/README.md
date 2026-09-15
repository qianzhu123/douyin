# Douyin Helper

本地 Chrome/Edge Manifest V3 单扩展，整合了原来的 `downloader` 和 `live-overlay`：
打开任意抖音 web 页面后，右上角会自动出现圆形悬浮按钮。
插件会按当前 URL 自动判定场景，弹出对应操作面板。

## 支持的页面场景

| 场景 | URL 模板 | 面板动作 |
| --- | --- | --- |
| 视频详情 | `/video/<aweme_id>` | 下载当前作品 |
| 图集笔记 | `/note/<aweme_id>` | 下载当前作品 |
| 短链 | `v.douyin.com/...` | 下载当前作品（交给后端 302 落地） |
| 推荐流 / 搜索 / 喜欢 / 关注 / 个人主页 modal | `...?modal_id=<id>` | 下载当前作品（规约到 /video/） |
| 个人主页 | `/user/<sec_uid>` | 一键导入到本地账户列表 |
| 直播间 | `live.douyin.com/<web_rid>` | 就地检测展示：房间 / 主播 / 当前观看 / 累计观看 / 本场点赞 |

所有页面都有两个共用动作：**打开本地控制台** 和 **重启本地后端**。

## 直播间就地检测（v0.5.0 起）

打开直播间后点悬浮按钮，面板直接展示当前直播间数据，并每 3 秒自动刷新：
房间标题、主播昵称、当前观看、累计观看、本场点赞。

数据源分两层：

1. **页面接口捕获（主）**：`live-capture.js` 以 MAIN world 在页面加载最早期
   hook 页面自身的 `webcast/room/web/enter/` 响应。累计观看
   （`stats.total_user_str`）和本场点赞根本不在页面 DOM 里，只有接口里有，
   这也是旧版"完全检测不到"的原因。
2. **DOM 兜底（辅）**：仅覆盖标题 / 主播 / 当前观看，选择器随抖音改版可能
   失效；接口未捕获时（例如装完扩展没刷新页面）面板会提示刷新。

**10万+ 封顶展示**：累计观看达到 10万 后（抖音页面本身即只显示 "10万+"），
面板永远展示 `10万+`，不再展示具体数字；同一标签页会话内按 web_rid 记忆封顶
状态（sessionStorage）。亿级（如 `3.4亿`）照抄页面文案。

主按钮为 **重新检测**（补拉一次接口捕获缓存并即时重渲染）。原"在控制台查看
该直播间"（POST /api/live-room + 跳转）已舍弃；需要控制台时用共用按钮
**打开本地控制台**。

本地纯函数测试（中文数量解析 / 封顶规则 / 真实抓包样本归一）：

```bash
node tools/douyin_extensions/douyin-helper/live_core.test.js
```

## 安装

1. 启动本地后端与前端（`python -m uvicorn backend.app:app --reload` + `npm run dev`）。
2. Chrome / Edge 扩展管理 → 启用开发者模式 → 加载已解压的扩展程序。
3. 选择本目录：`tools/douyin_extensions/douyin-helper`。

## 自定义后端地址

默认调用 `http://127.0.0.1:8000`。如需修改，在浏览器控制台执行：

```js
localStorage.setItem('dy_dlh_api_base', 'http://your-host:port');
```

## 重启后端

点击面板里的 "重启本地后端" 按钮，插件会调 `/api/admin/restart-backend`，
由后端异步启动 `scripts/start-douyin.ps1`，幂等重启 8000 端口的后端进程。
预计 1-3 秒后端会短暂失联，刷新页面即可。

## 兼容性

- 替代了原 `downloader/` 和 `live-overlay/`，请勿同时加载这三个扩展，否则悬浮按钮会重叠。
- 仅匹配 `https://www.douyin.com/*`、`https://live.douyin.com/*`、`https://v.douyin.com/*`，
  不会注入其它站点。
