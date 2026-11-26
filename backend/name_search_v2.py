"""backend/name_search_v2.py — 按名/抖音号搜索用户 (v2 general/single 协议通道).

2026-11-26 打通 (lab t10, commit 89c867eaf 后续):
- 端点: POST/GET https://aweme.snssdk.com/aweme/v2/search/general/single/ (App 40.6.0 真机同款)
- 2483 登录墙的根因 = 搜索域校验 bd-ticket-guard 头族 (路径绑定+时效);
  e125 真机抓包拿到新鲜完整头组 (logs/e125_parsed_headers.json, 含凭证不入库)
- 重放配方 = e125 新鲜头组 + unidbg 七头 (sessionid 与头组同会话) → sc=0 真卡
- 实测: query=吕德华 第一卡即本尊 (nickname/uid/unique_id/sec_uid 全齐)

头组时效: bd-ticket-guard-client-data 含 timestamp (设备现签); 头组失效后
2483 会回来 — 届时需再抓一次 (e125_fresh_headers.py, 真机窗口)。

对外: search_user(keyword) -> [user dict]  (nickname/uid/sec_uid/unique_id/follower_count)
纪律: 只读搜索。
"""
from __future__ import annotations

import json
import re
import ssl
import time
import urllib.parse
import urllib.request
from pathlib import Path

LAB_ROOT = Path(r"D:\code\android\androidre\douyin")
FRESH_HEADERS_FILE = LAB_ROOT / "logs" / "e125_parsed_headers.json"

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

UA = ("com.ss.android.ugc.aweme/400601 (Linux; U; Android 11; zh_CN; "
      "Pixel 2 XL; Build/RP1A.201005.004.A1)")

_signer = None


def _signer_mod():
    global _signer
    if _signer is None:
        import importlib.util
        import sys
        sys.path.insert(0, str(LAB_ROOT / "tools"))
        spec = importlib.util.spec_from_file_location(
            "e121_unidbg_client", LAB_ROOT / "tools" / "e121_unidbg_client.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _signer = mod
    return _signer


def headers_fresh() -> bool:
    """新鲜头组文件存在且含 ticket 家族 → 可用."""
    try:
        h = json.load(open(FRESH_HEADERS_FILE, encoding="utf-8"))
        return "bd-ticket-guard-client-data" in h and "X-Tt-Token" in h
    except Exception:
        return False


def _common_q(**extra) -> dict:
    q = dict(iid="1765185923070852", device_id="5967213509056", ac="wifi",
             channel="huawei_1128_64", aid=1128, app_name="aweme",
             version_code=400600, version_name="40.6.0", device_platform="android",
             os="android", ssmix="a", device_type="Pixel 2 XL", device_brand="google",
             language="zh", os_api=30, os_version=11, manifest_version_code=400601,
             resolution="1440*2712", dpi=560, update_version_code=40609900,
             _rticket=int(time.time() * 1000), package="com.ss.android.ugc.aweme",
             first_launch_timestamp=0, last_deeplink_update_version_code=40609900,
             cpu_support64="true", host_abi="arm64-v8a", is_guest_mode=0,
             app_type="normal", minor_status=0, appTheme="light", is_preinstall=0,
             need_personal_recommend=1, is_android_pad=0, is_android_fold=0,
             ts=int(time.time()), cdid="61eafaf6-6a6b-4423-bf00-bfa1b53b1fb5")
    q.update(extra)
    return q


def _extract_users(d: dict) -> list[dict]:
    """从 v2 响应提取**真用户卡** (2026-11-26 修正版).

    卡片语义 (t10/t10c/t10d 实测):
    - type=999 用户卡: data.raw_data 为转义 JSON 字符串, user_list[].user_info
      即目标用户 (a3075588361 → 千逐 实证); 或 fp card_name=doctype_aweme_user,
      fp.user_fp.user_ids 指认 uid, 作者对象在附带的 aweme_list[].author
    - type=1 视频卡: author 只是蹭 #话题 的视频作者, **不是**搜索结果
    - comment_list / relation_label 里的用户全是噪音, 绝不收

    旧版深扫把评论区/关联推荐/蹭话题作者全混进来且无头像 — 已废。
    头像取 avatar_larger.url_list[0]。
    """
    dec = json.JSONDecoder()
    authors: dict[str, dict] = {}  # uid -> author object (for avatar/fans enrichment)

    def collect_authors(o):
        if isinstance(o, dict):
            if o.get("nickname") and o.get("uid") and "avatar_thumb" in o:
                authors.setdefault(str(o["uid"]), o)
            for v in o.values():
                collect_authors(v)
        elif isinstance(o, list):
            for v in o:
                collect_authors(v)

    collect_authors(d)

    def to_user(ui: dict) -> dict:
        av = ""
        al = ui.get("avatar_larger") or ui.get("avatar_300x300") \
            or ui.get("avatar_168x168") or ui.get("avatar_medium") \
            or ui.get("avatar_thumb") or {}
        urls = al.get("url_list") if isinstance(al, dict) else None
        if urls:
            av = urls[0]
        return {
            "nickname": ui.get("nickname"),
            "uid": str(ui.get("uid")) if ui.get("uid") else None,
            "sec_uid": ui.get("sec_uid"),
            "unique_id": ui.get("unique_id") or ui.get("short_id"),
            "follower_count": ui.get("follower_count"),
            "avatar_url": av,
        }

    out: list[dict] = []
    seen: set[str] = set()

    def push(ui: dict):
        if not (ui.get("nickname") and (ui.get("sec_uid") or ui.get("uid"))):
            return
        key = str(ui.get("sec_uid") or ui.get("uid"))
        if key in seen:
            return
        seen.add(key)
        out.append(to_user(ui))

    for card in d.get("business_data") or []:
        data = card.get("data") or {}
        # path 1: raw_data escaped JSON -> user_list[].user_info (type 999 user card)
        rd = data.get("raw_data")
        if isinstance(rd, str):
            try:
                inner = json.loads(rd)
                for ul in inner.get("user_list") or []:
                    ui = ul.get("user_info") or {}
                    if ui:
                        push(ui)
            except Exception:
                pass
        # path 2: fp card_name=doctype_aweme_user -> user_ids resolved via authors map
        fp = data.get("fp")
        if isinstance(fp, str):
            try:
                f, _ = dec.raw_decode(fp)
            except Exception:
                f = None
            if isinstance(f, dict):
                uids = (f.get("user_fp") or {}).get("user_ids") or []
                for u in uids:
                    au = authors.get(str(u))
                    if au:
                        push(au)
    return out


def search_user(keyword: str, count: int = 10) -> list[dict]:
    """按名/抖音号搜索用户卡. 失败/墙/头组缺失 → [] (调用方回落 sug/浏览器链)."""
    if not headers_fresh():
        return []
    fresh = json.load(open(FRESH_HEADERS_FILE, encoding="utf-8"))
    ck = fresh.get("Cookie", "")
    m_sid = re.search(r"sessionid=([^;]+)", ck)
    m_iid = re.search(r"install_id=([^;]+)", ck)
    if not (m_sid and m_iid):
        return []

    params = _common_q(
        query=keyword, count=count, offset=0,
        search_id=f"{int(time.time() * 1000)}7000000000000000000",
        search_channel="aweme_general", enter_from="search_page",
        is_global_rewrite=0, history_vid="", match_user_keyword=1,
        sort_type=0, publish_time=0, filter_type=0)
    url = ("https://aweme.snssdk.com/aweme/v2/search/general/single/?"
           + urllib.parse.urlencode(params))

    try:
        mod = _signer_mod()
        minjar = f"install_id={m_iid.group(1)}\r\nsessionid={m_sid.group(1)}"
        heads = mod.unidbg_sign(url, minjar)
    except Exception:
        return []

    headers = dict(fresh)
    headers["User-Agent"] = UA
    headers.update(heads)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20, context=CTX) as resp:
            d = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return []
    if not isinstance(d, dict) or d.get("status_code") != 0:
        return []
    return _extract_users(d)
