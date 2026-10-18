#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
爱云动（微信小程序）马拉松水印照片批量下载工具
================================================

适用平台：爱云动 / 悦动拍（runnerbar）—— 微信小程序「爱云动」及 H5 站 m.yundong.runnerbar.com
适用场景：参加完马拉松 / 越野跑等赛事后，按「参赛号码」批量下载平台收录的本人照片

工作原理（详见 docs/原理说明.md）：
    1. 赛事发现：从平台门户接口拉取赛事列表，按名称关键词 + 年份匹配
    2. 参赛号搜索：调用平台 V2 搜索接口，返回号码被标注的照片（免费最高清水印版）
    3. 人脸检测：把搜到的照片上传到平台 OSS，调用人脸检测接口拿到人脸 ID
    4. 人脸搜索：用人脸 ID 再搜一遍，补回「有人在照片里、但号码没被标注」的照片
    5. 收敛过滤：只有被 >=2 个不同角度的本人人脸同时确认的照片才保留（过滤误匹配）
    6. 批量下载 + 完整性校验 + 缩略图总览

使用前你需要知道三件事：
    1. 你参加的是哪一年的哪场比赛（用于匹配赛事，如：2026 西青区半程马拉松）
    2. 你的参赛号码（如 B10426）
    3. 照片是「带水印的最高清免费版」，原图需在平台付费购买

依赖：Python 3.8+，仅标准库（Pillow 可选，仅用于生成缩略图总览和深度校验）

用法示例：
    # 最常用：按赛事关键词 + 参赛号下载
    python aiyundong_downloader.py --event "西青半程马拉松" --year 2026 --bib B10426

    # 先看看有哪些匹配的赛事（不下载）
    python aiyundong_downloader.py --event "西青" --year 2026 --bib B10426 --list-events

    # 已知赛事 ID，直接下载
    python aiyundong_downloader.py --event-id 36231 --bib B10426

    # 只要参赛号搜到的照片，跳过人脸搜索（快，但会漏照片）
    python aiyundong_downloader.py --event "西青半程马拉松" --bib B10426 --bib-only

完整参数见 README.md 或 python aiyundong_downloader.py --help
"""

import argparse
import concurrent.futures
import glob
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter

# ---------------------------------------------------------------- 常量

API_BASE = "https://apiface.store.runnerbar.com"   # 生产环境（测试环境 api.corp.yundongpai.net 返回的是内部测试数据，不要用）
PORTAL_API = "/yundong/yd_portal/getPortalActivityV3.json"
ALBUM_INFO_API = "/yundong/yd_album/getAlbumInfo_V2.json"
BIB_SEARCH_API = "/yundong/faceSearch/getFaceAndGameNumSearchPhotoV2.json"
FACE_DETECT_API = "/yundong/faceSearch/saveSearchFaces.json"
FACE_SEARCH_API = "/yundong/faceSearch/getFaceAndGameNumSearchPhotoV2.json"
OSS_SIGN_API = "/yundong/yd/upload/generalOssRequestMap.json"
OSS_SIGN_SECRET = "iyd"                            # 平台前端写死的签名密钥
MEDIA_TYPE_PHOTO = 1                               # media_show_type=1 真实照片；=2 是广告占位
MIN_FILE_BYTES = 20 * 1024                         # 小于 20KB 视为下载失败
DEFAULT_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")


# ---------------------------------------------------------------- 基础 HTTP


def log(msg):
    print(msg, flush=True)


def http_request(url, method="GET", data=None, headers=None, timeout=90, retries=3):
    """带重试的 HTTP 请求，返回 (status, body_bytes)"""
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, method=method,
                                         headers=headers or {"User-Agent": DEFAULT_USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            body = b""
            try:
                body = e.read()
            except Exception:
                pass
            # 4xx 一般是参数问题，重试无意义
            if 400 <= e.code < 500 and e.code != 429:
                raise RuntimeError("HTTP %s %s | %s" % (e.code, url, body[:200]))
            last_err = "HTTP %s %s" % (e.code, body[:200])
        except Exception as e:
            last_err = str(e)
        if attempt < retries - 1:
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("请求失败(%s): %s" % (url, last_err))


def api_post(path, params, retries=3):
    """平台 API：参数走 query string，POST 空 body（与前端行为一致）"""
    url = API_BASE + path + "?" + urllib.parse.urlencode(params)
    _, body = http_request(url, method="POST", data=b"", retries=retries)
    return json.loads(body.decode("utf-8"))


# ---------------------------------------------------------------- 平台客户端


class AiyunDong:
    """爱云动平台 API 客户端（复刻小程序/H5 的调用方式）"""

    def __init__(self, delay=0.3, verbose=True):
        self.delay = delay
        self.verbose = verbose

    def _pause(self):
        if self.delay > 0:
            time.sleep(self.delay)

    # ---- 赛事发现 ----

    def list_events(self, pages=5, page_size=100):
        """拉取门户赛事列表（默认前 500 条，按发布时间排序）"""
        events = []
        for index in range(0, pages * page_size, page_size):
            j = api_post(PORTAL_API, {"index": index, "size": page_size})
            pl = (j.get("result") or {}).get("portalList") or []
            events.extend(pl)
            if len(pl) < page_size:
                break
            self._pause()
        return events

    @staticmethod
    def match_events(events, keyword, year=None):
        """按关键词 + 年份匹配赛事。

        匹配规则：把关键词按非中文/字母数字切成词元，所有词元都要出现在赛事名里；
        年份优先匹配标题中的年份，其次看开赛时间。
        返回 [(score, event)]，score 越高越匹配。
        """
        tokens = [t for t in re.split(r"[^\u4e00-\u9fa5A-Za-z0-9]+", keyword) if t]
        if not tokens:
            return []
        hits = []
        for e in events:
            title = e.get("title") or ""
            if all(t in title for t in tokens):
                score = sum(title.count(t) * len(t) for t in tokens)
                start = e.get("start_time") or 0
                ev_year = None
                m = re.search(r"(20\d{2})", title)
                if m:
                    ev_year = int(m.group(1))
                elif start:
                    ev_year = time.gmtime(start / 1000).tm_year
                if year:
                    if ev_year != year:
                        continue
                    score += 100          # 年份完全匹配的大幅加分
                hits.append((score, start, e))
        hits.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [(s, e) for s, _, e in hits]

    def album_info(self, activity_id):
        """赛事专辑信息（照片总数、是否有人脸搜索、视频数等）"""
        j = api_post(ALBUM_INFO_API, {"activity_id": activity_id})
        return (j.get("result") or {})

    # ---- 参赛号搜索 ----

    def search_by_bib(self, activity_id, bib, photo_num=200):
        """V2 参赛号搜索，返回真实照片列表（剔除广告占位）"""
        j = api_post(BIB_SEARCH_API, {
            "uid": "0", "activity_id": activity_id, "game_number": bib,
            "photoNum": photo_num, "source": "h5"})
        res = j.get("result") or {}
        items = [p for p in (res.get("topicInfoList") or [])
                 if p.get("media_show_type") == MEDIA_TYPE_PHOTO]
        return items, res

    # ---- OSS 上传（人脸检测的前置步骤） ----

    def upload_image(self, img_path):
        """复刻前端 UploadAli：签名 -> 取 OSS 凭证 -> 直传阿里云 OSS。

        关键点：sign 串里的 uid 不能为空，空字符串会被服务端判定为 sign 错误(-100)。
        返回 OSS object key。
        """
        data = open(img_path, "rb").read()
        file_md5 = hashlib.md5(data).hexdigest()
        ts = str(int(time.time() * 1000))
        uid = "0"
        sign = hashlib.md5(
            ("function=faceSearch&md5=%s&timestamp=%s&uid=%s&secret=%s"
             % (file_md5, ts, uid, OSS_SIGN_SECRET)).encode("utf-8")
        ).hexdigest()
        j = api_post(OSS_SIGN_API, {
            "function": "faceSearch", "timestamp": ts,
            "sign": sign, "md5": file_md5, "uid": uid})
        if j.get("status") != 0:
            raise RuntimeError("OSS 签名失败: status=%s msg=%s" % (j.get("status"), j.get("msg")))
        rm = j["result"]["respMap"]
        # key 格式必须与前端一致：dir + 时间戳 + "-" + uuid(带横杠) + 文件大小 + ".jpg"
        key = rm["dir"] + ts + "-" + str(uuid.uuid4()) + str(len(data)) + ".jpg"

        boundary = "----ayd" + uuid.uuid4().hex
        parts = []
        for k, v in [("OSSAccessKeyId", rm["accessid"]), ("policy", rm["policy"]),
                     ("callback", rm["callback"]), ("Signature", rm["signature"]),
                     ("key", key), ("success_action_status", "200"), ("md5", file_md5)]:
            parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                          % (boundary, k, v)).encode("utf-8"))
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; "
                      "filename=\"photo.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n"
                      % boundary).encode("utf-8"))
        parts.append(data)
        parts.append(b"\r\n")
        parts.append(("--%s--\r\n" % boundary).encode("utf-8"))
        _, body = http_request(rm["host"], method="POST", data=b"".join(parts),
                               headers={"Content-Type":
                                        "multipart/form-data; boundary=%s" % boundary})
        if b"OK" not in body:
            raise RuntimeError("OSS 上传失败: %s" % body[:200])
        return key

    # ---- 人脸检测 / 搜索 ----

    def detect_faces(self, oss_key, activity_id):
        """检测上传照片中的所有人脸，返回 [{id, url}, ...]"""
        j = api_post(FACE_DETECT_API, {
            "uid": "0", "activity_id": activity_id, "face_url": oss_key})
        return j.get("list") or []

    def search_by_face(self, activity_id, face_id, photo_num=200):
        """用人脸 ID 搜索照片，返回真实照片列表"""
        j = api_post(FACE_SEARCH_API, {
            "uid": "0", "activity_id": activity_id, "face_id": face_id,
            "photoNum": photo_num, "source": "h5"})
        res = j.get("result") or {}
        return [p for p in (res.get("topicInfoList") or [])
                if p.get("media_show_type") == MEDIA_TYPE_PHOTO]


# ---------------------------------------------------------------- 下载 / 总览图


def download_one(url, out_path):
    """下载单张照片（带重试），返回字节数；失败返回 0"""
    for attempt in range(3):
        try:
            _, body = http_request(url, timeout=90, retries=2)
            if len(body) > MIN_FILE_BYTES:
                with open(out_path, "wb") as f:
                    f.write(body)
                return len(body)
        except Exception:
            pass
        time.sleep(1.0 * (attempt + 1))
    return 0


def verify_image(path):
    """校验 JPEG 完整性；Pillow 可用时做深度校验，否则只查文件头"""
    try:
        with open(path, "rb") as f:
            head = f.read(3)
        if head != b"\xff\xd8\xff":
            return False
    except Exception:
        return False
    try:
        from PIL import Image
        with Image.open(path) as im:
            im.verify()
        return True
    except ImportError:
        return True          # 没有 Pillow，文件头校验通过即认为有效
    except Exception:
        return False


def make_contact_sheet(files, out_path, cols=10, cell=(200, 280)):
    """生成缩略图总览图（需要 Pillow，没有则跳过）"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        log("[提示] 未安装 Pillow，跳过缩略图总览（pip install Pillow 后可生成）")
        return None
    cell_w, cell_h = cell
    label_h = 22
    rows = (len(files) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell_w, rows * (cell_h + label_h)), (245, 245, 245))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("msyh.ttc", 13)
    except Exception:
        font = ImageFont.load_default()
    for i, fp in enumerate(files):
        r, c = divmod(i, cols)
        x0, y0 = c * cell_w, r * (cell_h + label_h)
        try:
            with Image.open(fp) as im:
                im = im.convert("RGB")
                im.thumbnail((cell_w - 6, cell_h - 6))
                sheet.paste(im, (x0 + (cell_w - im.width) // 2,
                                 y0 + (cell_h - im.height) // 2))
        except Exception:
            continue
        no = os.path.basename(fp).split("_")[0]
        draw.text((x0 + 6, y0 + cell_h + 3), "#%s" % no, fill=(60, 60, 60), font=font)
    sheet.save(out_path, optimize=True)
    return out_path


# ---------------------------------------------------------------- 主流程


def parse_existing_ids(out_dir):
    """解析输出目录里已下载的照片 ID（用于增量下载）"""
    ids = set()
    for fp in glob.glob(os.path.join(out_dir, "*.jpg")):
        m = re.match(r"\d+_(\d+)\.jpg$", os.path.basename(fp))
        if m:
            ids.add(m.group(1))
    return ids


def choose_event(client, keyword, year, event_id, list_only):
    """赛事发现 / 匹配，返回 event dict 或 None"""
    if event_id:
        log("[赛事] 使用指定赛事 ID: %s" % event_id)
        info = client.album_info(event_id)
        album = info.get("album") or {}
        title = album.get("title") or ("赛事 %s" % event_id)
        return {"id": int(event_id), "title": title,
                "activity_photo_count": album.get("pic_count"),
                "is_faceSearch": album.get("is_faceSearch"),
                "video": info.get("video")}

    log("[赛事] 正在拉取平台赛事列表并匹配「%s%s」..." %
        (keyword, (" / %s年" % year) if year else ""))
    events = client.list_events()
    hits = client.match_events(events, keyword, year)
    if not hits:
        log("[赛事] 没有找到匹配的赛事。可能原因：\n"
            "  1. 关键词写法不对（试试更短的关键词，如只写城市名或「马拉松」）\n"
            "  2. 赛事太老，已不在门户前 %d 条列表里（可加大 --pages）\n"
            "  3. 该平台没有收录这场比赛（换个平台，如爱云动只收录部分赛事）" % len(events))
        return None

    log("[赛事] 找到 %d 个候选：" % len(hits))
    for i, (score, e) in enumerate(hits[:10], 1):
        log("    %d. [ID %s] %s（约 %s 张照片）"
            % (i, e.get("id"), e.get("title"),
               e.get("activity_photo_count") or "?"))
    if list_only:
        return None
    if len(hits) == 1:
        log("[赛事] 唯一匹配，自动选择")
        return hits[0][1]
    log("[赛事] 有多个候选，请用 --event-id <ID> 指定后重跑，"
        "或先用 --list-events 查看完整列表")
    return None


def run(args):
    client = AiyunDong(delay=args.delay)

    # ---------- 1. 赛事发现 ----------
    event = choose_event(client, args.event, args.year, args.event_id, args.list_events)
    if not event:
        return 0 if args.list_events else 1
    activity_id = event["id"]
    bib = args.bib.strip()
    log("[赛事] %s（ID %s，约 %s 张照片，人脸搜索%s）"
        % (event.get("title"), activity_id, event.get("activity_photo_count") or "?",
           "开启" if event.get("is_faceSearch") else "关闭/未知"))

    try:
        info = client.album_info(activity_id)
        video_cnt = info.get("video")
        log("[赛事] 平台档案：共 %s 张照片，视频 %s 个"
            % ((info.get("album") or {}).get("pic_count"), video_cnt if video_cnt is not None else "未知"))
        if video_cnt == 0:
            log("[赛事] 本赛事没有视频，所有「影像」均为照片")
    except Exception as e:
        log("[赛事] 专辑信息获取失败（不影响主流程）：%s" % e)

    # ---------- 2. 输出目录 ----------
    safe_name = re.sub(r"[\\/:*?\"<>|]", "_", event.get("title") or "赛事")
    out_dir = args.output or os.path.join(
        os.getcwd(), "马拉松照片_%s_%s" % (safe_name[:30], bib))
    os.makedirs(out_dir, exist_ok=True)
    log("[输出] %s" % out_dir)

    # ---------- 3. 参赛号搜索 ----------
    log("[号码搜索] 参赛号 %s ..." % bib)
    bib_items, bib_res = client.search_by_bib(activity_id, bib)
    bib_ids = set(p["id"] for p in bib_items)
    log("[号码搜索] 命中 %d 张照片（平台原始计数 %s，其余为广告占位）"
        % (len(bib_items), bib_res.get("count")))
    if not bib_items:
        log("[号码搜索] 没有搜到照片。请检查：\n"
            "  1. 参赛号是否正确（注意字母大小写、数字 0/O、1/I）\n"
            "  2. 比赛是否已结束并开始上传照片（赛后几小时内可能还没有）\n"
            "  3. 赛事 ID 是否选对")
        return 1

    final_items = {p["id"]: dict(p, _source="bib") for p in bib_items}

    # ---------- 4. 人脸检测 + 人脸搜索 ----------
    face_sets = {}        # face_id -> {"photo": 来源照片, "ids": [...]}
    tmp_by_id = {}        # 人脸检测阶段下载的临时文件（下载阶段复用）
    if args.bib_only:
        log("[人脸搜索] 已指定 --bib-only，跳过")
    else:
        # 先下载号码搜到的照片（人脸检测需要平台能访问到图片，上传本地文件即可）
        todo = [p for p in bib_items if p["id"] not in parse_existing_ids(out_dir)]
        log("[下载] 先下载号码搜索命中的 %d 张（用于人脸检测）..." % len(todo))
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(download_one, p.get("url_hq") or p.get("url"),
                                os.path.join(out_dir, "tmp_%s.jpg" % p["id"])): p["id"]
                    for p in todo}
            done = 0
            for fut in concurrent.futures.as_completed(futs):
                done += 1
                if done % 10 == 0 or done == len(todo):
                    log("[下载] %d/%d" % (done, len(todo)))

        # 临时文件保留到下载阶段复用（避免重复下载），全部处理完再统一清理
        all_tmp = sorted(glob.glob(os.path.join(out_dir, "tmp_*.jpg")))
        for fp in all_tmp:
            m = re.search(r"tmp_(\d+)\.jpg$", fp)
            if m:
                tmp_by_id[m.group(1)] = fp
        bib_files = all_tmp[: args.limit_faces] if args.limit_faces else all_tmp
        log("[人脸检测] 开始处理 %d 张照片（上传→检测人脸→搜索）..." % len(bib_files))
        for i, fp in enumerate(bib_files, 1):
            photo_id = re.search(r"tmp_(\d+)\.jpg$", fp).group(1)
            try:
                key = client.upload_image(fp)
                faces = client.detect_faces(key, activity_id)
            except Exception as e:
                log("[人脸] %d/%d %s 上传/检测失败: %s" % (i, len(bib_files), photo_id, e))
                continue
            if not faces:
                log("[人脸] %d/%d %s 未检测到人脸" % (i, len(bib_files), photo_id))
                continue
            found_runner = False
            for f in faces:
                fid = f.get("id")
                if not fid:
                    continue
                try:
                    items = client.search_by_face(activity_id, fid)
                except Exception as e:
                    log("[人脸] face %s 搜索失败: %s" % (fid, e))
                    continue
                ids = set(p["id"] for p in items)
                overlap = len(ids & bib_ids)
                is_runner = overlap >= args.overlap_threshold
                if is_runner:
                    found_runner = True
                    face_sets[fid] = {"photo": photo_id, "ids": sorted(ids)}
                    for p in items:
                        final_items.setdefault(p["id"], dict(p, _source="face"))
                log("[人脸] %d/%d %s face=%s 结果=%d 与号码重合=%d %s"
                    % (i, len(bib_files), photo_id, fid, len(ids), overlap,
                       "→ 本人" if is_runner else ""))
                if found_runner:
                    break        # 已找到本人脸，跳过该照片其余人脸（都是其他跑者）
            client._pause()

        # 临时文件保留到下载阶段复用（避免重复下载），全部处理完再统一清理
        tmp_by_id = {}
        for fp in bib_files:
            m = re.search(r"tmp_(\d+)\.jpg$", fp)
            if m:
                tmp_by_id[m.group(1)] = fp

    # ---------- 5. 收敛过滤 ----------
    if not args.bib_only and not args.all_faces:
        cnt = Counter()
        for fs in face_sets.values():
            for pid in fs["ids"]:
                cnt[pid] += 1
        confident = set(p for p, c in cnt.items() if c >= args.min_faces)
        face_only_conf = confident - bib_ids
        dropped = [p for p, src in final_items.items()
                   if src["_source"] == "face" and p not in confident]
        for p in dropped:
            final_items.pop(p)
        log("[收敛过滤] %d 个本人人脸共同确认 %d 张照片；"
            "保留人脸新增 %d 张，过滤低置信匹配 %d 张（--all-faces 可不过滤）"
            % (len(face_sets), len(confident & bib_ids | face_only_conf),
               len(face_only_conf), len(dropped)))
    elif args.all_faces:
        log("[收敛过滤] 已指定 --all-faces，保留全部人脸匹配结果")

    # ---------- 6. 汇总 ----------
    n_bib = sum(1 for p in final_items.values() if p["_source"] == "bib")
    n_face = sum(1 for p in final_items.values() if p["_source"] == "face")
    log("=" * 56)
    log("[汇总] 参赛号命中 %d 张 + 人脸新增 %d 张 = 共 %d 张" % (n_bib, n_face, len(final_items)))

    # ---------- 7. 批量下载 ----------
    existing = parse_existing_ids(out_dir)
    todo = [p for pid, p in final_items.items() if pid not in existing]
    log("[下载] 新增 %d 张（已有 %d 张，跳过）..." % (len(todo), len(existing)))
    max_no = 0
    for fp in glob.glob(os.path.join(out_dir, "*.jpg")):
        m = re.match(r"(\d+)_", os.path.basename(fp))
        if m:
            max_no = max(max_no, int(m.group(1)))

    results = {}
    reuse, need_dl = [], []
    for i, p in enumerate(todo, 1):
        out_path = os.path.join(out_dir, "%03d_%s.jpg" % (max_no + i, p["id"]))
        if p["id"] in tmp_by_id and os.path.getsize(tmp_by_id[p["id"]]) > MIN_FILE_BYTES:
            reuse.append((p["id"], tmp_by_id[p["id"]], out_path))
        else:
            need_dl.append((p["id"], p.get("url_hq") or p.get("url"), out_path))
    if reuse:
        log("[下载] 复用已下载的临时文件 %d 张" % len(reuse))
    for pid, src, dst in reuse:
        try:
            os.replace(src, dst)
            results[pid] = os.path.getsize(dst)
        except Exception:
            need_dl.append((pid, None, dst))

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {}
        for pid, url, out_path in need_dl:
            if not url:
                results[pid] = 0
                continue
            futs[pool.submit(download_one, url, out_path)] = (pid, out_path)
        done = 0
        for fut in concurrent.futures.as_completed(futs):
            pid, out_path = futs[fut]
            results[pid] = fut.result()
            done += 1
            if done % 10 == 0 or done == len(futs):
                log("[下载] %d/%d" % (done, len(futs)))

    # ---------- 8. 校验 + 重命名单张失败的 ----------
    ok_ids = set(existing)
    bad = []
    for pid, sz in results.items():
        fp = None
        for cand in glob.glob(os.path.join(out_dir, "*_%s.jpg" % pid)):
            fp = cand
            break
        if sz > MIN_FILE_BYTES and fp and verify_image(fp):
            ok_ids.add(pid)
        else:
            bad.append(pid)
            if fp and os.path.exists(fp):
                os.remove(fp)
    log("[校验] 有效 %d 张，失败 %d 张" % (len(ok_ids), len(bad)))
    if bad:
        log("[校验] 失败 ID（可重跑自动补下）: %s" % ", ".join(bad[:20]))

    # 清理残留的临时文件
    for fp in glob.glob(os.path.join(out_dir, "tmp_*.jpg")):
        try:
            os.remove(fp)
        except Exception:
            pass

    # ---------- 9. 清单 + 总览图 ----------
    manifest = []
    for pid in sorted(ok_ids):
        p = final_items.get(pid) or {"id": pid}
        manifest.append({"id": pid, "source": p.get("_source", "?"),
                         "faces_confirmed": cnt.get(pid, 0) if not args.bib_only else None,
                         "url_hq": p.get("url_hq"), "url": p.get("url"),
                         "width": p.get("image_width"), "height": p.get("image_height")})
    json.dump(manifest, open(os.path.join(out_dir, "final_photos.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    summary = {
        "event": {"id": activity_id, "title": event.get("title")},
        "bib": bib,
        "bib_photos": n_bib, "face_only_photos": n_face,
        "total_photos": len(ok_ids), "failed": len(bad),
        "min_faces": args.min_faces,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    json.dump(summary, open(os.path.join(out_dir, "search_summary.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    if not args.no_sheet:
        files = sorted(glob.glob(os.path.join(out_dir, "[0-9][0-9][0-9]_*.jpg")))
        sheet = make_contact_sheet(files, os.path.join(out_dir, "缩略图总览.png"))
        if sheet:
            log("[总览] 已生成 %s" % sheet)

    log("=" * 56)
    log("[完成] 共 %d 张照片 -> %s" % (len(ok_ids), out_dir))
    log("[完成] 说明：下载的是平台免费最高清水印版；想要无水印原图需在平台付费购买")
    return 0


# ---------------------------------------------------------------- 入口


def build_parser():
    p = argparse.ArgumentParser(
        description="爱云动（微信小程序）马拉松水印照片批量下载工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python aiyundong_downloader.py --event \"西青半程马拉松\" --year 2026 --bib B10426\n"
               "  python aiyundong_downloader.py --event \"西青\" --year 2026 --bib B10426 --list-events\n"
               "  python aiyundong_downloader.py --event-id 36231 --bib B10426 --bib-only\n")
    p.add_argument("--event", help="赛事名称关键词，如 \"西青半程马拉松\"、\"杭州马拉松\"")
    p.add_argument("--year", type=int, help="赛事年份，如 2026（建议提供，避免同名赛事混淆）")
    p.add_argument("--bib", required=True, help="你的参赛号码，如 B10426")
    p.add_argument("--event-id", type=int, help="已知赛事 ID 时直接指定（跳过搜索）")
    p.add_argument("--output", help="输出目录（默认: ./马拉松照片_<赛事>_<参赛号>）")
    p.add_argument("--list-events", action="store_true", help="只列出匹配的赛事，不下载")
    p.add_argument("--pages", type=int, default=5, help="赛事列表拉取页数，每页 100 条（默认 5）")
    p.add_argument("--bib-only", action="store_true",
                   help="只按参赛号搜索，跳过人脸搜索（快，但会漏掉号码未标注的照片）")
    p.add_argument("--all-faces", action="store_true",
                   help="不过滤人脸匹配结果（会包含其他跑者的误匹配照片）")
    p.add_argument("--min-faces", type=int, default=2,
                   help="收敛阈值：照片至少被几个本人人脸同时确认才保留（默认 2）")
    p.add_argument("--overlap-threshold", type=int, default=15,
                   help="人脸归属判定：与参赛号结果的重合数达到该值才认为是本人（默认 15）")
    p.add_argument("--limit-faces", type=int, default=0,
                   help="只对前 N 张号码照片做人脸检测（调试用，0=全部）")
    p.add_argument("--workers", type=int, default=8, help="下载并发数（默认 8）")
    p.add_argument("--delay", type=float, default=0.3, help="API 调用间隔秒数（默认 0.3）")
    p.add_argument("--no-sheet", action="store_true", help="不生成缩略图总览")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not args.event and not args.event_id:
        build_parser().error("必须提供 --event（赛事关键词）或 --event-id（赛事 ID）之一")
    try:
        return run(args)
    except KeyboardInterrupt:
        log("\n已中断。已下载的照片保留在输出目录，重跑可增量续传。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
