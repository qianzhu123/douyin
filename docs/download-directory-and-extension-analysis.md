# 下载目录与浏览器插件方案分析

日期：2026-07-06

## 结论

可以改。建议分成两个独立但兼容的改造：

1. 下载目录：默认从项目 `output` 改为 `C:\Users\Light\Downloads`，同时在界面提供自定义目录输入和持久化设置。
2. 浏览器插件：可以编写 Chrome/Edge Manifest V3 插件，但插件最好只负责识别当前 Douyin 页面并调用本地后端；实际下载、解析、账户导入仍交给现有 FastAPI 后端。

这样改动最小，也最符合当前项目结构：项目已经有 `/api/downloads`、`/api/downloads/preview`、`/api/users`，浏览器插件不需要重写抖音解析逻辑。

## 当前项目现状

当前默认下载目录在 `backend/config.py`：

```python
DOWNLOAD_OUTPUT_DIR = PROJECT_ROOT / "output"
```

当前前端提交下载时没有传 `output_dir`，所以 `backend/services.py` 会在 `DownloadService.create_job()` 中回落到 `DOWNLOAD_OUTPUT_DIR`：

```python
resolved_output = str(Path(output_dir) if output_dir else DOWNLOAD_OUTPUT_DIR)
```

当前下载器底层的目录策略在 `external/douyin-downloader/downloader.py`：

- 单视频、模式 1：直接放到 base 目录。
- 图集/笔记、模式 1：始终按标题创建子文件夹。
- 模式 3：始终按标题创建子文件夹。
- 模式 2：JSON 直接放 base 目录。

因此“默认不用文件夹包裹”不能只改前端，还需要给底层 `download_douyin()` 增加一个明确参数，例如 `wrap_folder: bool = False`，再由后端和前端传入。否则图集和模式 3 仍会自动建子目录。

## 下载目录改造建议

推荐设计：

- 默认下载根目录改为 Windows 下载目录：`Path.home() / "Downloads"`。
- 后端新增持久化设置文件，例如 `data/settings.json`：

```json
{
  "download_output_dir": "C:\\Users\\Light\\Downloads",
  "wrap_download_folder": false
}
```

- 前端下载面板新增两个控制项：
  - 下载目录输入框，默认显示 `C:\Users\Light\Downloads`。
  - “按作品标题创建文件夹”开关，默认关闭。

建议不要依赖浏览器原生目录选择器来直接给 Python 后端传本地绝对路径。普通网页出于安全限制不能稳定拿到任意本地文件夹真实路径；本项目是本地工具，最简单可靠的是让用户输入/粘贴路径，并由后端校验、创建目录、返回实际使用路径。

后端需要做的校验：

- 空路径时使用默认 Downloads。
- 展开 `~` 和相对路径。
- 创建目录前检查路径是否像文件而不是目录。
- 创建失败时返回清晰错误。
- 下载任务记录最终绝对路径，方便界面展示。

## 文件夹包裹策略

用户澄清后的语义应定义为：

```text
输入: output_dir = D:\Downloads\Douyin
输入: url = 某个视频/图集链接
输入: wrap_folder = false 或 true
```

`output_dir` 永远表示用户传入的“下载根目录”，不是最终作品目录。

- `wrap_download_folder = false`：
  - 不再为当前作品创建标题子文件夹。
  - 单视频直接保存到用户传入的根目录，例如 `D:\Downloads\Douyin\标题.mp4`。
  - 图集/笔记里的多张图片也直接保存到用户传入的根目录。
  - 数据 JSON 也直接保存到用户传入的根目录。

- `wrap_download_folder = true`：
  - 沿用当前下载器逻辑，在用户传入的根目录下按作品标题创建子文件夹。
  - 视频、图片、JSON 都放进该作品文件夹，例如 `D:\Downloads\Douyin\标题\标题.mp4`。

也就是说，开关只控制“是否在传入目录下再包一层作品标题文件夹”，不改变用户传入目录本身。

这比“图集永远建文件夹”更符合用户要求，但需要同步修改下载器文件名生成逻辑，否则图集图片直接落到根目录时会出现 `001.webp` 这类易冲突文件名。

## 需要补充考虑的情况

下面这些情况建议在实现前明确规则：

1. 同名文件如何处理。
   - 如果根目录已有 `标题.mp4` 或 `001.webp`，建议默认跳过已有非空文件，和当前下载器行为保持一致；但图集不包裹时最好使用 `标题_001.webp`，避免不同作品互相覆盖。

2. 图集/笔记不包裹时的文件名。
   - 当前逻辑在文件夹里保存为 `001.webp`、`002.webp`，这是合理的。
   - 如果直接落根目录，建议改成 `标题_001.webp`、`标题_002.webp`，否则根目录会很乱，也容易冲突。

3. 多链接批量下载是否强制总文件夹。
   - 按你的语义，默认不强制总文件夹，所有作品都落到传入根目录。
   - 如果担心混在一起，可以后续再加“批量任务文件夹”，但最小版本不建议加，避免开关语义变复杂。

4. 模式 3“媒体 + 数据”是否也遵守不包裹。
   - 建议遵守。用户选择不包裹时，视频/图片/JSON 都直接落根目录；选择包裹时才进标题文件夹。

5. 自定义目录输入是否持久化。
   - 建议持久化到 `data/settings.json`，这样浏览器插件和网页都能复用同一默认下载目录。

6. 前端目录选择体验。
   - 普通网页不能可靠读取用户选择文件夹后的真实绝对路径。最可靠的方式仍是文本输入/粘贴路径；如果后续要做原生目录选择，需要桌面外壳或 native messaging，复杂度会上升。

## 浏览器插件是否可行

可以。推荐插件定位为“Douyin 页面到本地项目的快捷入口”，而不是独立下载器。

官方 Chrome 扩展平台当前以 Manifest V3 为主；扩展可以使用 host permissions 访问指定站点，也可以通过权限访问本地后端或使用下载 API。相关文档：

- Manifest V3：https://developer.chrome.com/docs/extensions/develop/migrate/what-is-mv3
- Host permissions：https://developer.chrome.com/docs/extensions/develop/concepts/declare-permissions
- downloads API：https://developer.chrome.com/docs/extensions/reference/api/downloads
- Native messaging：https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging

本项目不需要 native messaging。因为已有本地后端 `http://127.0.0.1:8000`，插件只要通过 service worker 调用本地 API 即可。这样安装和维护都更简单。

## 插件功能边界

### 打开视频或图集页时直接下载

插件读取当前 tab URL：

- `https://www.douyin.com/video/<aweme_id>`
- `https://www.douyin.com/note/<aweme_id>`
- `https://v.douyin.com/...`
- 带 `modal_id` 的聚合页 URL

然后调用：

```http
POST http://127.0.0.1:8000/api/downloads
Content-Type: application/json
```

请求体：

```json
{
  "text": "当前页面 URL",
  "mode": 1,
  "output_dir": "C:\\Users\\Light\\Downloads",
  "comments": false,
  "wrap_folder": false
}
```

后端已经支持多种 URL 解析，插件不应该硬编码复杂页面选择器。

### 打开个人主页时导入到本地

插件识别：

```text
https://www.douyin.com/user/<sec_uid>
```

最小实现可以直接调用：

```http
POST http://127.0.0.1:8000/api/users
Content-Type: application/json
```

请求体：

```json
{
  "label": "页面标题或 sec_uid",
  "sec_uid": "<sec_uid>",
  "homepage_url": "当前页面 URL"
}
```

更好的实现是内容脚本从页面 SSR 数据或 DOM 中提取昵称、抖音号、签名、头像等字段，再提交给 `/api/users`。如果提取不到，仍然用 `sec_uid` 兜底保存，后续通过现有检测功能补齐资料。

## 插件目录建议

建议放在：

```text
tools/douyin_browser_extension/
```

建议文件：

```text
tools/douyin_browser_extension/
  manifest.json
  popup.html
  popup.js
  service_worker.js
  content.js
  README.md
```

权限建议：

```json
{
  "permissions": ["activeTab", "scripting", "storage"],
  "host_permissions": [
    "https://www.douyin.com/*",
    "https://v.douyin.com/*",
    "http://127.0.0.1:8000/*"
  ]
}
```

如果请求从 service worker 发起，优先走 `http://127.0.0.1:8000/*` host permission。后端如需兼容扩展页面的 Origin，可以把扩展 ID 对应的 `chrome-extension://<id>` 加进 CORS，或者仅让 service worker 请求本地 API。

## 推荐实施顺序

1. 先改下载目录和文件夹包裹逻辑。
   - 这是用户当前最直接的痛点。
   - 也是浏览器插件会复用的后端能力。

2. 再加后端设置接口。
   - `GET /api/settings`
   - `POST /api/settings`
   - 设置保存到 `data/settings.json`

3. 再改前端下载面板。
   - 显示默认目录。
   - 支持自定义目录。
   - 支持“按作品标题创建文件夹”开关，默认关闭。

4. 最后写浏览器插件。
   - 插件先只支持两个按钮：下载当前作品、导入当前主页。
   - 后续再加“打开本地控制台”“查看最近任务”等增强项。

## 需要注意的限制

- 下载抖音内容应只用于自己有权限保存的内容，避免侵犯版权或违反平台规则。
- 插件不能绕过登录、风控、验证码或平台限制；如果页面或后端下载器需要登录态，仍需要用户正常登录。
- Douyin 页面结构经常变化，所以插件应尽量少依赖 DOM 选择器，优先传 URL 给本地后端。
- 自定义目录是本机路径，插件不应保存敏感目录；建议默认只保存用户显式输入的路径。

## 最小可行方案

最小可行版本只需要做这些：

- 后端默认 `DOWNLOAD_OUTPUT_DIR` 改为 `Path.home() / "Downloads"`。
- `DownloadRequest` 增加 `wrap_folder: bool = False`。
- `DownloadService.create_job()` 把 `wrap_folder` 传给下载器。
- 下载器 `download_douyin()` 增加 `wrap_folder` 参数，并调整图集/笔记直接落根目录时的文件名。
- 前端下载面板增加路径输入和文件夹开关。
- 插件提供“下载当前作品”和“导入当前主页”两个按钮，调用本地 API。

这个方案不改变现有项目核心架构，风险最低。
