# 抖音直播链接 (`live.douyin.com/<web_rid>`) 可获取信息分析

> 分析对象：`https://live.douyin.com/31126587860`（web_rid = 31126587860）
> 该 web_rid 即直播间分享号，对应 `live.douyin.com/<web_rid>`。
> 方法：用 js-reverse (CDP) 打开直播页，抓首屏 REST 接口 + DOM/XHR + protobuf 长轮询弹幕。
> 原始样本见 `samples/`。

## 0. 与项目现有能力的差异

项目 (`backend/services.py` + `external/douyin-monitor/main.py`) 现在只用
**用户主页接口** `aweme/v1/web/user/profile/other/` 拿 `live_status / room_id /
live_viewers / follower_count / aweme_count`，是「**从主播主页判断在不在直播**」。

直播页 `live.douyin.com/<web_rid>` 是另一套接口体系（`/webcast/...`），能拿到
**直播间内部**的实时状态、弹幕、礼物、榜单、商品、流地址等，粒度和维度都远超主页接口。
本分析补充这块，落地新能力的一个候选起点。

## 1. 关键标识

| 标识 | 值（本样本） | 来源 |
|------|-------------|------|
| `web_rid`（直播间分享号 / URL 末段） | `31126587860` | URL |
| `room_id_str`（房间真实 ID） | `7664058258907188031` | `webcast/room/web/enter` → `data.enter_room_id`；localStorage `playRoom` |
| 主播 `uid` (`owner_user_id_str`) | `95052337920` | enter → `data.user.id_str` |
| 主播 `sec_uid` | `MS4wLjABAAAAMwl8IP1Ewi4CiQDfhKPzQn2ruEKzwkpobVz0YgKEvzw` | enter → `data.user.sec_uid` |
| 当前访客 `webcast_uid` | `7656842983337674274` | localStorage `__live_triple_screen_icon_key_new__` |

- `web_rid` 在 URL，是入口；`room_id_str` 是 enter接口真正用的房间 ID。
- `room_id_str` 也可在 sessionStorage / localStorage 的 `playRoom` 里找到，
  不抓 enter 接口也能拿到。
- 主播的 `sec_uid` / `uid` 直接在 enter 的 `data.user` 里；拿到 sec_uid 后即可复用
  项目现有的 `user/profile/other` 反查主播粉丝/作品等主页维度数据。

## 2. 核心数据接口（首屏一次性加载）

`live.douyin.com/<web_rid>` 打开后，页面会按顺序请求一批 `webcast/*` 接口。
关键几个及可拿到字段如下。所有接口返回 JSON（除弹幕为 protobuf），顶层统一是
`{"data": ..., "extra": ..., "status_code": 0}`。

### 2.1 `GET /webcast/room/web/enter/`  ← 最核心，房间全量入口信息

样本：`samples/dy_enter_3587.json`。签名参数含 `a_bogus` / `msToken`（浏览器自签）。

可获取字段（`data` 顶层）：

- **房间**
  - `data.enter_room_id` / `data.data[0].id_str` = `room_id_str`
  - `data.data[0].status` (=2 表示直播中) / `status_str`
  - `data.data[0].title` ← **直播间标题**（本样本："放老的CD库存，你来点歌"）
  - `data.data[0].user_count_str` ← 观看人数文本（"1000+"）
  - `data.data[0].like_count` ← **本场点赞数**（62610）
  - `data.data[0].stats.total_user_str` ← 累计观看文本（"3万+"）
  - `data.data[0].room_view_stats.display_value` ← 较准的实时观看数（1218）
    + display_short/middle/long 文本
  - `data.data[0].has_commerce_goods` ← 是否带橱窗商品
  - `data.data[0].admin_user_ids_str` ← 房管 uid 列表
  - `data.partition_road_map.partition` ← 直播分区（id/title/type）
  - `data.similar_rooms[]` ← 同分区推荐直播间（含 peer 的 web_rid/title/user_count_str/cover）
  - `data.qrcode_url` ← 直播间二维码图
- **主播**（`data.user` / `data.data[0].owner`）
  - `id_str` / `sec_uid` / `nickname` / `avatar_thumb.url_list[0]`
  - `follow_info.follow_status`（当前访客是否关注该主播）
- **流地址** `data.web_stream_url`
  - `hls_pull_url` / `flv_pull_url` / `default_resolution` / `stream_orientation`
  - ⚠ 本样本里两栏为空（PC flv/hls 经常仅给协议字段，实拉流在 MSE blob 里，
    `video.src=blob:...`）。要稳定拿原始流需在直播清晰度切流量时再观测。
- **连麦** `data.data[0].linker_detail`（linker_map/playmode 等）
- **电商** `data.data[0].ecom_data` / `room_cart`（含 contain_cart/total/flash_total）
- **付费直播** `data.data[0].paid_live_data`
- 当前访客登录态：`data.login_lead.is_login`

### 2.2 `GET /webcast/setting/`  ← 房间功能开关/平台配置（量大，价值低）

样本：`samples/dy_setting_525.json`。`data` 是 526 个键的扁平配置字典
（ experienmant flags、banner、礼物/粉丝团/电商 UI 开关 等）。
对二次开发直接价值低；可作为「某功能是否在该直播间开启」的探测源。

### 2.3 `GET /webcast/ranklist/audience/`  ← 在线观众榜（高价值）

样本：`samples/dy_ranklist_now.json`，约 10 秒轮询一次（`/webcast/ranklist/audience` 是定时刷新）。

- `data.ranks[]` ← **Top 200 上榜观众**，每条含：
  - `rank` / `score` / `delta` / `gap_description`（与上名次差距文案）
  - `user.id_str` / `sec_uid` / `nickname` / `display_id` / `gender`(1男2女0未知) / `city`
  - `user.follow_info.follower_count` / `following_count`（多数榜单里为 0，被隐藏）
  - `user.pay_grade.level` ← **付费等级**
  - `user.fans_club.data.level` / `anchor_id` ← **粉丝团等级+(归属主播)uid**
  - `user.top_vip_no` / `is_hidden`
- `data.total` / `user_count_desc` / `invisible_total` / `has_more`
- `data.self_info` ← 当前访客在榜的位置（未上榜则全 0）

### 2.4 `GET /webcast/im/fetch/`  ← 弹幕 / 房间消息（protobuf 长轮询）

样本：`samples/dy_imfetch_3486.json`（**二进制 protobuf，非 JSON**）。

- 顶层 message type 可见 `WebcastChatMessage`（弹幕）、连礼、进房、点赞等。
- 这是抖音直播**消息通道**：PC 端用的是 **HTTP 长轮询 im/fetch**（不是 WebSocket），
  body 为 protobuf，须按 `.proto` 解码。
- 通用 wrapper：每条 message = `{common:{method,msg_type,...}, payload(bytes)}`，
  `method` 决定 payload 的 protobuf 类型（WebcastChatMessage=弹幕，WebcastGiftMessage=礼物，
  WebcastMemberMessage=进房/退房，WebcastLikeMessage=点赞，WebcastRoomUserSeqMessage=在线序列…）。
- 对接需：proto 定义（社区有 webcast im proto）+ proto 解码；否则只能做正则/二进制扫描。
- ⚠ 在抓取网络里**没有出现 WebSocket 连接**（`get_websocket_messages` 返回空），
  印证 web 端弹幕走 HTTP im/fetch 轮询，非 wss。

### 2.5 其它首屏接口（已确认存在，按需扩展）

| 接口 | 用途 | 样本 |
|------|------|------|
| `/webcast/gift/list/` | 礼物面板元数据：分组 / 优惠 / 粉丝团礼物 id 等（单条礼物完整 diamond/icon 多在面板展开时才拉） | `dy_giftlist_3487.json` |
| `/webcast/wish/list/` | 主播心愿单：`anchor_name` / `common_wish_info.wish_list[]`（wish_name/type） | `dy_wish_3599.json` |
| `/webcast/user/me/` | 当前访客状态 | — |
| `/webcast/room/interaction/info/` | 房间互动配置 | — |
| `/webcast/privilege/subscribe/info/` | 主播订阅/特权 | — |
| `/webcast/ranklist/hour_entrance/` | 小时榜入口 | — |
| `/webcast/luckybox/box/list/` & `/lottery/melon/lottery_info/` | 福袋 / 抽奖 | — |
| `/webcast/time_stamp` | 服务器时间戳（用于对齐弹幕时间） | — |
| `/webcast/diamond/` | 用户钻石余额(付费账户) | — |
| `/aweme/v1/web/hot/search/list/` | 热搜 | — |

> 观众榜、时间戳等接口会被定时轮询，可直接复用做轮询监控。

## 3. DOM 维度（不需接口即可拿的轻量信息）

- 主播昵称：`[data-e2e="live-room-nickname"]` → "歌手刘筝"
- 顶部信息条：`[data-e2e="rooom-info-bar-anchor"]` → "歌手刘筝5.4万本场点赞"
- 在线观看：`[data-e2e="anchor-watching-count"]`（类目）
- `<meta name="description">` → "欢迎来到歌手刘筝的抖音直播间，歌手刘筝与大家一起记录美好生活 - 抖音直播"
- 资源全场廛etc:📁 其他 e2e: living-container / live-avatar / live-followbutton / quality / gifts-container / danmaku-setting-icon / live-room-audience 等。

## 4. 签名与可调用性

- 所有 `live.douyin.com/webcast/...` 接口查询串尾带 `a_bogus`、
  常带 `msToken`（cookie 同源票据），后端直发大概率被风控拒。
- **可行路线**：用 Playwright/浏览器（同项目 monitor 现有 headless chromium 模式）
  打开 `live.douyin.com/<web_rid>`，监听 `response` 事件拦 `webcast/*` 各接口，
  直接读 `response.json()`——与现有 `fetch_profile` 拦 `user/profile/other` 同套路。
  此路线**不需自己算 a_bogus**，复用浏览器签好的请求。

## 5. 建议落地形态（与项目现有架构对齐）

- 在 `external/` 下新增 `douyin-live-room`（参照 monitor）暴露：
  `async fetch_room(web_rid) -> dict`（拦 enter 接口，归一化字段）。
- 在 `backend/services.py` 增加 `LiveRoomService.fetch_overview(web_rid)`，
  返回统一结构（见本目录 `extract_live_room.py` 的 `_summarize`）。
- 可选：`LiveRoomService.fetch_audience_rank(web_rid)` 复用观众榜。
- 弹幕接入需 proto 解码，单独立项（`im/fetch` protobuf + 增量 cursor）。

## 6. 采样注意

- 本样本日期 2026-07-19，主播 "歌手刘筝"、直播分区 "音乐现场"、带货 `has_commerce_goods=true`。
- 字段名/结构可能随抖音前端版本漂移，生产代码应做 `None` 兜底与多键兼容。
- 昵称/标题在导出的 JSON 里因编码呈现乱码是导出工具显示问题，浏览器直读 `response.json()`
  拿到的是正确 UTF-8。
