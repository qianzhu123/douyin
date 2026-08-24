# Douyin Downloader Helper

本地 Chrome/Edge Manifest V3 插件,打开抖音页面后右上角自动显示圆形悬浮按钮,点击展开下载/导入面板。

## 功能

- 在抖音任意页面(视频详情 / 笔记 / 短链 / 推荐 / 搜索 / 喜欢列表 / 关注列表 / 个人主页 modal 弹窗)右上角显示悬浮按钮。
- 点击按钮展开详情面板,可一键下载当前作品或导入个人主页到本地面板。
- 不下载媒体本身,只调用本地后端 `http://127.0.0.1:8000` 的 `/api/downloads` 和 `/api/users`。
- 复用面板的下载目录设置(`data/settings.json`),与网页面板行为一致。

## 支持的页面场景

| 场景 | URL 模板 | 行为 |
| --- | --- | --- |
| 视频详情 | `/video/<aweme_id>` | 可下载 |
| 图集笔记 | `/note/<aweme_id>` | 可下载 |
| 短链 | `v.douyin.com/...` | 可下载(交给后端 302 跳转) |
| 推荐流 | `/jingxuan?modal_id=<id>` | 可下载 |
| 搜索结果 | `/jingxuan/search/...&modal_id=<id>` | 可下载 |
| 喜欢列表 | `/user/self?...&modal_id=<id>` | 可下载 |
| 关注列表 | `/user/following?...&modal_id=<id>` | 可下载 |
| 个人主页 modal | `/user/<sec_uid>?from_tab_name=main&modal_id=<id>` | 可下载 + 可导入主页 |
| 个人主页 | `/user/<sec_uid>` | 可导入主页 |

## 本地安装

1. 启动本地后端与前端(`python -m uvicorn backend.app:app --reload --host 127.0.0.1 --port 8000` 和 `npm run dev`)。
2. 打开 Chrome / Edge 扩展管理页。
3. 启用"开发者模式"。
4. 选择"加载已解压的扩展程序"。
5. 选择本目录:`tools/douyin_extensions/downloader`。

## 自定义后端地址

默认调用 `http://127.0.0.1:8000`。如需修改,在浏览器控制台执行:

```js
localStorage.setItem('dy_dlh_api_base', 'http://your-host:port');
```

## 测试

```bash
node tools/douyin_extensions/downloader/popup_core.test.js
```

覆盖多场景 URL 解析、modal_id 提取、详情页规约、可下载判定等核心逻辑。
