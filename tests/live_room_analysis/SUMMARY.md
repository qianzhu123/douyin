# 直播间可获取信息 — 归一摘要（基于 samples/ 真实数据）

主播房间：`https://live.douyin.com/31126587860`
主播名片：**歌手刘筝**  uid=`95052337920`  sec_uid=`MS4wLjABAAAAMwl8IP1Ewi4CiQDfhKPzQn2ruEKzwkpobVz0YgKEvzw`
房间 status=2（直播中）, 分区=音乐现场(id=10000), 带橱窗(has_commerce_goods=true)

| 类别 | 字段 | 值/来源接口 |
|------|------|------|
| 房间 | web_rid | 31126587860（URL） |
| 房间 | room_id_str | 7664058258907188031（enter.data.enter_room_id / localStorage playRoom） |
| 房间 | 直播间标题 | enter.data.data[0].title = "放老的CD库存，你来点歌" |
| 房间 | 观看人数(准确) | room_view_stats.display_value = 1218 |
| 房间 | 观看人数(文本) | user_count_str = "1000+" |
| 房间 | 累计观看 | stats.total_user_str = "3万+" |
| 房间 | 本场点赞 | like_count = 62610 |
| 房间 | 房管 uid | admin_user_ids_str = [122734099308622, ...] |
| 房间 | 同分区推荐 | similar_rooms[]（12 个，含 peer web_rid/title/人数） |
| 主播 | nickname/sec_uid/uid/avatar | enter.data.user + owner |
| 流 | hls_pull_url / flv_pull_url / default_resolution | enter.data.web_stream_url（本样本为空，实流在 MSE blob） |
| 电商 | has_ecom / cart total / flash_total / show_cart | enter.data.data[0].ecom_data + room_cart |
| 观众榜 | Top200：rank/score/sec_uid/nickname/display_id/gender/pay_grade_level/fans_club_level/fans_club.anchor_id | ranklist/audience, ~10s 轮询 |
| 观众榜 | 自己在榜位置 | ranklist.data.self_info |
| 心愿单 | anchor_name / common_wish_info.wish_list | wish/list |
| 礼物面板 | 分组/优惠/粉丝团礼物id/快捷礼物id | gift/list（单条完整礼物字段在面板展开时才拉） |
| 弹幕/消息 | **protobuf** 长轮询 im/fetch（非 WebSocket） | WebcastChatMessage 等 method，需 proto 解码 |
| 当前访客 | webcast_uid / 登录态 | localStorage __live_triple_screen_icon_key_new__ / enter.login_lead.is_login |
| 服务器时间 | 对齐弹幕时间 | webcast/time_stamp |
| DOM 兜底 | nickname/infoBar/在线观看/meta description | data-e2e 选择器 |

## 验证
`extract_live_room._summarize` 离线三测全 PASS（见 `test_extract_live_room.py`，
`python -c` 内联运行 3 断言通过；`python -m pytest` 在本机环境启动阶段挂起，
属环境问题非脚本问题）。

## 关键发现
1. 直播间以 **HTTP `webcast/*` JSON 接口集** 暴露数据，最核心是
   `room/web/enter`，一次拿到房间+主播+流+电商+推荐房。
2. 弹幕走 **HTTP im/fetch protobuf 长轮询**，不是 WebSocket
   （抓取全程 `get_websocket_messages` 空，响应首字节即 protobuf `WebcastChatMessage`）。
3. 所有接口带 `a_bogus`+`msToken` 签名，外层直发会被风控；复用项目现有
   headless chromium + response 拦截套路（同 monitor `fetch_profile`）可绕过自签。
