# 抖音用户搜索脚本（已验证可用 ✅）

基于 **Playwright 真实浏览器驱动** + **登录态持久化** + **接口拦截**，自动处理 `a_bogus` / `msToken` 等风控签名，无需逆向。

## 验证状态

- 环境已跑通：登录态注入后，`/aweme/v1/web/discover/search/` 接口 200，成功抓到 12 条原始用户记录。
- 实测关键词 `hanghang` 返回包括 HangHang、小 HangHang、北体航航、李佳航MVP 等 12 个用户，含 `room_data` 直播流信息。
- 部分情况下抖音会弹出滑动验证（详见下方"风控"小节），脚本不预置自动通过——需手动滑一次或换关键词/稍后再试。

## 为什么用浏览器方案

抖音 `discover/search` 接口的 `a_bogus`、`msToken`、`verifyFp` 由前端混淆 JS 动态生成。本方案让真实浏览器自动算签名 / Cookie / referer，只拦截响应读 JSON。

## 安装

```powershell
cd C:\Users\Light\Desktop\temp\douyin-user-search
pip install -r requirements.txt
python -m playwright install chromium
```

或直接双击 `start.bat`，首次会自动装依赖。

## 使用流程

### 1. 登录(只做一次)

```powershell
python login.py
```
或 `start.bat` → 选 `1`。浏览器弹出抖音首页，扫码登录后自动检测并保存到 `.\douyin_profile`。

### 2. 搜索(原始数据，不做清洗)

```powershell
python raw.py hanghang                  # 默认可见浏览器 + 登录态，第一页约 12 个
python raw.py hanghang --more           # 滚动加载更多（最多 ~36）
python raw.py hanghang --headless       # 无头模式（登录态已存好后可用）
python raw.py hanghang --no-save        # 不落文件
python raw.py hanghang --no-profile     # 调试用，不走登录态
```

或 `start.bat` → 选 `2` / `3`。

输出：
- Diagnostics（看到多少次 `discover/search` 接口调用、最后状态码）
- 完整原始 `user_list` JSON（不清洗）
- 落存到 `results/raw_<keyword>_<时间戳>.json`

## 字段速览（user_info 内）

| 字段 | 含义 |
|------|------|
| `nickname` | 昵称 |
| `unique_id` | 抖音号 |
| `short_id` | 短号 |
| `sec_uid` | 安全用户 ID（拼主页 URL） |
| `follower_count` / `total_favorited` | 粉丝数 / 总获赞 |
| `room_id` | 直播间 ID（0=未开播） |
| `room_data` | 直播流信息 JSON 字符串（含 FLV/HLS 拉流 URL，有时效） |
| `signature` | 简介 |
| `custom_verify` / `enterprise_verify_reason` | 个人/企业认证 |

## 文件结构

```
douyin-user-search/
├── start.bat          纯英文启动器（登录/搜索/加载更多 菜单）
├── login.py           扫码登录，持久化到 ./douyin_profile（自动检测登录态）
├── raw.py             搜索 + 原始 JSON 输出 + 诊断（无清洗）
├── douyin_search.py   搜索 + 字段提炼版
├── cli.py             提炼版 CLI
├── requirements.txt
├── README.md
├── douyin_profile\    (登录后生成，存登录态)
└── results\           (搜索后生成，存 JSON)
```

## 风控提示

- **滑动验证**：抖音会在某些情况弹出滑动验证码。脚本不会代你通过——请手动在弹出的浏览器里滑一次，或换关键词、稍后重试。频繁触发可加 `--more` 之外别开着脚本刷太快。
- **登录态过期**：若长时间不用或抖音风控刷新，profile 可能失效——重跑 `login.py` 即可。
- **headless 风险**：headless 模式更容易触发风控，建议优先可见浏览器。
- **拉流地址时效**：`room_data` 里的 `auth_key` / `expire` 有时效，过期需重新请求。
- 仅用于学习研究，请遵守抖音用户协议，不要用于商业爬取。
