# -*- coding: utf-8 -*-
"""
QQ空间照片/视频批量下载工具（纯接口版）
功能：扫码登录 -> 选择相册(多选/全选/反选) -> 选择下载路径 -> 批量下载图片与视频
依赖：curl_cffi(模拟浏览器TLS指纹)、Pillow(二维码显示)、tkinter(GUI)

接口参数均为浏览器实际抓包验证（2026-09）：
  相册：fcg_list_album_v3，返回 data.albumListModeSort / albumListModeClass
  媒体：h5.qzone.qq.com 代理的 cgi_list_photo，topicId=<相册UUID>，分页 pageNum=30
  视频：cgi_floatview_photo_list_v2 二次取 video_info.download_url
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from curl_cffi import requests as cffi_requests
from io import BytesIO
from PIL import Image, ImageTk
import threading
import json
import re
import os
import time
import random


# ============================================================
# 工具函数
# ============================================================

def hash33(s, init=0):
    """QQ hash33 算法
    - ptqrtoken(qrsig) 使用 init=0
    - g_tk(p_skey)  使用 init=5381
    """
    h = init
    for ch in s:
        h += (h << 5) + ord(ch)
    return h & 0x7fffffff


def sanitize_filename(name):
    """清理文件名中的非法字符"""
    if not name:
        return "未命名"
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', name)
    name = name.strip().strip('.')
    return name[:80] if name else "未命名"


def parse_jsonp(text):
    """解析 JSONP（如 shine0_Callback({...});）或纯 JSON"""
    if not text:
        return None
    text = str(text).strip()
    if text.startswith("\ufeff"):
        text = text[1:]
    text = text.rstrip().rstrip(";").strip()
    # 剥掉 回调名(...) 外壳
    m = re.match(r"^[\w$.]+\s*\((.*)\)\s*;?\s*$", text, re.DOTALL)
    if m:
        text = m.group(1)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def norm_url(u):
    """补全协议相对 URL"""
    if not isinstance(u, str):
        return ""
    u = u.strip()
    if u.startswith("//"):
        return "https:" + u
    return u


# ============================================================
# QQ 空间客户端
# ============================================================

class QzoneClient:
    """封装 QQ 空间登录、相册列表、媒体列表与下载（纯 HTTP 接口）"""

    APPID = "549000912"
    DAID = "5"
    U1 = "https://qzs.qq.com/qzone/v5/loginsucc.html?para=izone"

    # 抓包验证的接口地址
    ALBUM_API = ("https://user.qzone.qq.com/proxy/domain/"
                 "photo.qzone.qq.com/fcgi-bin/fcg_list_album_v3")
    PHOTO_API = ("https://h5.qzone.qq.com/proxy/domain/"
                 "photo.qzone.qq.com/fcgi-bin/cgi_list_photo")
    FLOATVIEW_API = ("https://h5.qzone.qq.com/proxy/domain/"
                     "photo.qzone.qq.com/fcgi-bin/cgi_floatview_photo_list_v2")

    def __init__(self):
        # curl_cffi 模拟 Chrome 的 TLS/JA3 指纹，绕过网关风控
        self.session = cffi_requests.Session(impersonate="chrome")
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        })
        self.uin = ""          # 纯数字 QQ 号
        self.p_skey = ""
        self.nickname = ""

    # ---------- 扫码登录 ----------

    def _init_session(self):
        """访问 xlogin 建立 pt_login_sig 等 cookie"""
        params = {
            "appid": self.APPID,
            "daid": self.DAID,
            "style": "40",
            "login_text": "登录",
            "hide_title_bar": "1",
            "hide_border": "1",
            "target": "self",
            "s_url": self.U1,
            "pt_3rd_aid": "0",
        }
        self.session.get("https://xui.ptlogin2.qq.com/cgi-bin/xlogin",
                         params=params, timeout=15)

    def get_qrcode(self):
        """返回 (二维码图片字节, qrsig)"""
        self._init_session()
        params = {
            "appid": self.APPID,
            "e": "2", "l": "M", "s": "3", "d": "72", "v": "4",
            "t": str(time.time()),
            "daid": self.DAID,
            "pt_3rd_aid": "0",
        }
        resp = self.session.get("https://ssl.ptlogin2.qq.com/ptqrshow",
                                params=params, timeout=15)
        return resp.content, self.session.cookies.get("qrsig", "")

    def check_login(self, qrsig):
        """轮询扫码状态，返回 (status, msg)
        status: success / waiting / expired / error"""
        ptqrtoken = hash33(qrsig)
        params = {
            "u1": self.U1,
            "ptqrtoken": str(ptqrtoken),
            "ptredirect": "0",
            "h": "1", "t": "1", "g": "1", "from_ui": "1",
            "ptlang": "2052",
            "action": f"0-0-{int(time.time() * 1000)}",
            "js_ver": "24092616",
            "js_type": "1",
            "login_sig": self.session.cookies.get("pt_login_sig", ""),
            "pt_uistyle": "40",
            "aid": self.APPID,
            "daid": self.DAID,
            "pt_js_version": "24092616",
        }
        try:
            resp = self.session.get(
                "https://ssl.ptlogin2.qq.com/ptqrlogin", params=params,
                headers={"Referer": "https://xui.ptlogin2.qq.com/"}, timeout=15)
        except Exception as e:
            return "error", f"网络错误: {e}"

        # ptuiCB('状态码','0','跳转URL','0','提示信息', '昵称')
        m = re.search(r"ptuiCB\((.*)\)", resp.text.strip(), re.DOTALL)
        if not m:
            return "error", "无法解析登录响应"
        parts = re.findall(r"'([^']*)'", m.group(1))
        if len(parts) < 2:
            return "error", "无法解析登录响应"

        code = parts[0]
        jump_url = parts[2] if len(parts) > 2 else ""
        msg = parts[4] if len(parts) > 4 else ""
        nickname = parts[5] if len(parts) > 5 else ""

        if code == "0":
            self._finalize_login(jump_url, nickname)
            return "success", f"登录成功：{self.nickname}"
        elif code == "66":
            return "waiting", "请使用手机QQ扫描二维码"
        elif code == "67":
            return "waiting", "已扫码，请在手机上确认登录"
        elif code in ("68", "65"):
            return "expired", "二维码已过期，请刷新"
        return "error", f"状态码 {code}: {msg}"

    def _finalize_login(self, jump_url, nickname):
        """跟随跳转拿 p_skey，并确定 QQ 号"""
        try:
            self.session.get(jump_url, timeout=15, allow_redirects=True)
        except Exception:
            pass

        # QQ 号优先取跳转 URL 中的 uin（无前置零）
        m = re.search(r"[?&]uin=(\d+)", jump_url)
        if m:
            self.uin = str(int(m.group(1)))
        else:
            digits = re.sub(r"\D", "", self.session.cookies.get("uin", ""))
            self.uin = str(int(digits)) if digits else ""

        # 访问空间主页，确保 .qzone.qq.com 域上的 cookie（p_skey）落地
        if self.uin:
            try:
                self.session.get(f"https://user.qzone.qq.com/{self.uin}", timeout=15)
            except Exception:
                pass

        self.p_skey = self.session.cookies.get("p_skey", "")
        if not self.p_skey:
            for name, value in self.session.cookies.items():
                if "skey" in name.lower() and value:
                    self.p_skey = value
                    break
        self.nickname = nickname or self.uin

    @property
    def gtk(self):
        return hash33(self.p_skey, init=5381)

    # ---------- 通用请求 ----------

    def _base_params(self):
        return {
            "g_tk": str(self.gtk),
            "uin": self.uin,
            "hostUin": self.uin,
            "appid": "4",
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "source": "qzone",
            "plat": "qzone",
        }

    def _headers(self):
        return {
            "Referer": f"https://user.qzone.qq.com/{self.uin}/photo",
            "Origin": "https://user.qzone.qq.com",
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }

    # ---------- 相册列表 ----------

    def get_albums(self):
        """返回 [{id(UUID), name, count}]，参数与浏览器请求完全一致"""
        if not self.uin:
            raise RuntimeError("未获取到QQ号")
        if not self.p_skey:
            raise RuntimeError("未获取到登录凭证(p_skey)，请重新扫码登录")

        params = self._base_params()
        params.update({
            "t": str(random.randint(100000000, 999999999)),
            "format": "jsonp",
            "notice": "0",
            "filter": "1",
            "handset": "4",
            "pageNumModeSort": "40",
            "pageNumModeClass": "15",
            "needUserInfo": "1",
            "idcNum": "4",
            "callbackFun": "shine0",
        })

        resp = self.session.get(self.ALBUM_API, params=params,
                                headers=self._headers(), timeout=20)
        if resp.status_code != 200:
            raise RuntimeError(f"相册接口 HTTP {resp.status_code}")

        data = parse_jsonp(resp.text)
        if not data or data.get("code") != 0:
            raise RuntimeError(f"相册接口返回异常：{resp.text[:200]}")

        d = data.get("data", {})
        # 真实返回：按时间排序 albumListModeSort；按分类 albumListModeClass
        raw_list = list(d.get("albumListModeSort") or [])
        for it in (d.get("albumListModeClass") or []):
            if not any(str(x.get("id")) == str(it.get("id")) for x in raw_list):
                raw_list.append(it)

        albums = []
        for item in raw_list:
            aid = str(item.get("id") or item.get("albumId") or "")
            if not aid:
                continue
            albums.append({
                "id": aid,
                "name": item.get("name") or item.get("albumName") or "未命名相册",
                "count": item.get("total") or item.get("photoNum") or 0,
            })
        return albums

    # ---------- 媒体列表（图片+视频） ----------

    def get_media_list(self, album_id, log=print):
        """分页拉取某相册全部媒体，返回 [{name,url,thumb,is_video,raw}]"""
        result = []
        start = 0
        size = 30  # 与页面一致

        while True:
            params = self._base_params()
            params.update({
                "t": str(random.randint(100000000, 999999999)),
                "mode": "0",
                "idcNum": "4",
                "topicId": album_id,
                "noTopic": "0",
                "pageStart": str(start),
                "pageNum": str(size),
                "skipCmtCount": "0",
                "singleurl": "1",
                "batchId": "",
                "notice": "0",
                "outstyle": "json",
                "format": "jsonp",
                "json_esc": "1",
                "question": "",
                "answer": "",
                "callbackFun": "shine0",
            })
            resp = self.session.get(self.PHOTO_API, params=params,
                                    headers=self._headers(), timeout=20)
            data = parse_jsonp(resp.text)
            if not data or data.get("code") != 0:
                log(f"  媒体接口 code={data.get('code') if data else '?'}")
                break

            d = data.get("data", {})
            photo_list = d.get("photoList") or d.get("photos") or []
            for p in photo_list:
                result.append({
                    "name": str(p.get("name") or f"item_{len(result)+1}"),
                    "url": self._pick_image_url(p),
                    "thumb": norm_url(p.get("sloc") or p.get("url") or ""),
                    "is_video": self._is_video(p),
                    "raw": p,
                })

            try:
                total = int(d.get("totalInAlbum") or d.get("total") or 0)
            except (TypeError, ValueError):
                total = 0
            if total and len(result) >= total:
                break
            if len(photo_list) < size:
                break
            start += size

        return result

    @staticmethod
    def _is_video(p):
        """多信号判定视频，避免依赖单一字段"""
        if p.get("is_video") in (1, "1", True):
            return True
        if isinstance(p.get("video_info") or p.get("videoInfo"), dict):
            return True
        if p.get("videoUrl") or p.get("video_url"):
            return True
        if str(p.get("phototype", "")) == "2":
            return True
        return False

    def _pick_image_url(self, p):
        """原图地址优先级：raw → origin_url → lloc → url → sloc"""
        for key in ("raw", "origin_url", "originUrl", "rawUploadUrl",
                    "lloc", "url", "sloc"):
            v = p.get(key)
            if isinstance(v, str) and v.startswith(("http://", "https://", "//")):
                return norm_url(v)
        return ""

    # ---------- 视频真实地址 ----------

    def resolve_video_url(self, album_id, media):
        """视频取直链：先条目自带，再走 floatview 二次接口"""
        p = media.get("raw", {})

        for obj_key in ("video_info", "videoInfo"):
            vi = p.get(obj_key)
            if isinstance(vi, dict):
                for k in ("download_url", "video_url", "url"):
                    u = vi.get(k)
                    if isinstance(u, str) and u.startswith(("http://", "https://", "//")):
                        return norm_url(u)
        for k in ("videoUrl", "video_url"):
            v = p.get(k)
            if isinstance(v, str) and v.startswith(("http://", "https://", "//")):
                return norm_url(v)

        return self._floatview_url(album_id, p)

    def _floatview_url(self, album_id, p):
        """cgi_floatview_photo_list_v2 → data.photos[*].video_info.download_url"""
        params = self._base_params()
        params.update({
            "t": str(random.randint(100000000, 999999999)),
            "topicId": f"albumid_{album_id}",
            "picKey": str(p.get("sloc") or p.get("lloc") or p.get("picKey") or ""),
            "shootTime": str(p.get("shoottime") or p.get("uploadtime") or ""),
            "cmtLargestOrderid": "",
            "cmtSmallestOrderid": "",
            "fupdate": "1",
            "need_private_comment": "1",
            "cmtNum": "10", "feedNum": "1", "likeNum": "1",
            "freshnum": "0", "offset": "0", "number": "1",
            "format": "jsonp",
            "callbackFun": "shine0",
        })
        try:
            resp = self.session.get(self.FLOATVIEW_API, params=params,
                                    headers=self._headers(), timeout=20)
            data = parse_jsonp(resp.text)
            if not data or data.get("code") != 0:
                return ""
            for one in data.get("data", {}).get("photos") or []:
                vi = one.get("video_info") or {}
                for k in ("download_url", "video_url", "url"):
                    u = vi.get(k)
                    if isinstance(u, str) and u.startswith(("http://", "https://", "//")):
                        return norm_url(u)
        except Exception:
            return ""
        return ""

    # ---------- 下载 ----------

    def download_media(self, url, save_path):
        """下载图片/视频（CDN 直链，带 Referer）"""
        headers = {"Referer": f"https://user.qzone.qq.com/{self.uin}"}
        try:
            resp = self.session.get(url, headers=headers, timeout=180)
            if resp.status_code == 200 and len(resp.content) > 100:
                with open(save_path, "wb") as f:
                    f.write(resp.content)
                return True
        except Exception:
            pass
        return False


def media_ext(url, is_video):
    """根据 URL 推断扩展名"""
    if is_video:
        m = re.search(r"\.(mp4|mov|avi|mkv|flv|webm)", url, re.IGNORECASE)
        return "." + m.group(1).lower() if m else ".mp4"
    m = re.search(r"\.(jpg|jpeg|png|gif|bmp|webp)", url, re.IGNORECASE)
    return "." + m.group(1).lower() if m else ".jpg"


# ============================================================
# 图形界面
# ============================================================

class App:
    def __init__(self, root):
        self.root = root
        self.root.title("QQ空间相册批量下载工具（佳缘科技）")
        self.root.geometry("800x600")
        self.root.minsize(700, 500)

        self.client = QzoneClient()
        self.albums = []
        self.album_vars = []
        self.download_path = ""
        self.is_downloading = False

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        self.main_frame = ttk.Frame(root, padding=10)
        self.main_frame.pack(fill=tk.BOTH, expand=True)
        self.show_login_frame()

    def clear_main(self):
        for w in self.main_frame.winfo_children():
            w.destroy()

    # ==================== 登录 ====================

    def show_login_frame(self):
        self.clear_main()
        frame = ttk.Frame(self.main_frame)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="QQ空间照片批量下载工具",
                  font=("Microsoft YaHei", 18, "bold")).pack(pady=(10, 5))
        ttk.Label(frame, text="请使用手机QQ扫描下方二维码登录",
                  font=("Microsoft YaHei", 10)).pack(pady=(0, 10))

        self.qr_label = ttk.Label(frame, text="正在获取二维码...", relief="solid")
        self.qr_label.pack(pady=10)

        self.status_var = tk.StringVar(value="正在获取二维码...")
        ttk.Label(frame, textvariable=self.status_var,
                  font=("Microsoft YaHei", 10), foreground="blue").pack(pady=5)

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(pady=10)
        self.refresh_btn = ttk.Button(btn_frame, text="刷新二维码",
                                      command=self.refresh_qrcode)
        self.refresh_btn.pack(side=tk.LEFT, padx=5)

        self.refresh_qrcode()

    def refresh_qrcode(self):
        self.refresh_btn.config(state=tk.DISABLED)
        self.status_var.set("正在获取二维码...")
        self.qr_label.config(image="", text="加载中...")

        # 每次刷新使用全新会话，避免旧 qrsig 干扰
        self.client = QzoneClient()

        def task():
            try:
                img_data, qrsig = self.client.get_qrcode()
                self.root.after(0, self._show_qrcode, img_data, qrsig)
            except Exception as e:
                self.root.after(0, self._qrcode_error, str(e))

        threading.Thread(target=task, daemon=True).start()

    def _show_qrcode(self, img_data, qrsig):
        try:
            img = Image.open(BytesIO(img_data)).resize((220, 220), Image.LANCZOS)
            self.qr_img = ImageTk.PhotoImage(img)
            self.qr_label.config(image=self.qr_img, text="")
            self.refresh_btn.config(state=tk.NORMAL)
            self.status_var.set("请使用手机QQ扫描二维码登录")
            threading.Thread(target=self._poll_login, args=(qrsig,),
                             daemon=True).start()
        except Exception as e:
            self._qrcode_error(str(e))

    def _qrcode_error(self, msg):
        self.qr_label.config(image="", text=f"获取二维码失败\n{msg}")
        self.refresh_btn.config(state=tk.NORMAL)
        self.status_var.set("获取失败，请重试")

    def _poll_login(self, qrsig):
        start = time.time()
        while True:
            if time.time() - start > 120:
                self.root.after(0, self.status_var.set, "二维码已过期，请刷新")
                return
            status, msg = self.client.check_login(qrsig)
            self.root.after(0, self.status_var.set, msg)
            if status == "success":
                self.root.after(0, self.show_album_frame)
                return
            if status in ("expired", "error"):
                return
            time.sleep(2)

    # ==================== 相册选择 ====================

    def show_album_frame(self):
        self.clear_main()

        top = ttk.Frame(self.main_frame)
        top.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(top,
                  text=f"已登录：{self.client.nickname} (QQ: {self.client.uin})",
                  font=("Microsoft YaHei", 10, "bold")).pack(side=tk.LEFT)
        ttk.Button(top, text="退出登录", command=self.logout).pack(side=tk.RIGHT)

        bar = ttk.Frame(self.main_frame)
        bar.pack(fill=tk.X, pady=(0, 5))
        ttk.Button(bar, text="全选", command=self.select_all).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="全不选", command=self.select_none).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="反选", command=self.invert_select).pack(side=tk.LEFT, padx=2)
        self.selected_count_var = tk.StringVar(value="已选择 0 个相册")
        ttk.Label(bar, textvariable=self.selected_count_var).pack(side=tk.RIGHT, padx=5)

        list_frame = ttk.Frame(self.main_frame)
        list_frame.pack(fill=tk.BOTH, expand=True)
        self.canvas = tk.Canvas(list_frame, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical",
                                  command=self.canvas.yview)
        self.album_list_frame = ttk.Frame(self.canvas)
        self.album_list_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.album_list_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

        bottom = ttk.Frame(self.main_frame)
        bottom.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(bottom, text="下载路径：").pack(side=tk.LEFT)
        self.path_var = tk.StringVar(
            value=os.path.join(os.path.expanduser("~"), "Desktop", "qzone_photos"))
        ttk.Entry(bottom, textvariable=self.path_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Button(bottom, text="浏览...", command=self.choose_path).pack(side=tk.LEFT, padx=2)
        ttk.Button(bottom, text="开始下载", command=self.start_download).pack(
            side=tk.LEFT, padx=(10, 0))

        self.load_albums()

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def load_albums(self):
        ttk.Label(self.album_list_frame, text="正在加载相册列表...").pack(pady=20)

        def task():
            try:
                albums = self.client.get_albums()
                self.root.after(0, self._display_albums, albums)
            except Exception as e:
                self.root.after(0, self._albums_error, str(e))

        threading.Thread(target=task, daemon=True).start()

    def _display_albums(self, albums):
        for w in self.album_list_frame.winfo_children():
            w.destroy()
        self.albums = albums
        self.album_vars = []

        if not albums:
            ttk.Label(self.album_list_frame, text="未找到相册").pack(pady=20)
            return

        for album in albums:
            var = tk.BooleanVar(value=False)
            self.album_vars.append(var)
            row = ttk.Frame(self.album_list_frame)
            row.pack(fill=tk.X, padx=5, pady=2)
            ttk.Checkbutton(row, variable=var,
                            command=self.update_selected_count).pack(side=tk.LEFT)
            ttk.Label(row,
                      text=f"{sanitize_filename(album['name'])}  ({album['count']} 项)",
                      width=50, anchor="w").pack(side=tk.LEFT, padx=5)
        self.update_selected_count()

    def _albums_error(self, msg):
        for w in self.album_list_frame.winfo_children():
            w.destroy()
        ttk.Label(self.album_list_frame, text=f"加载相册失败：{msg}",
                  foreground="red").pack(pady=20)

    def select_all(self):
        for v in self.album_vars:
            v.set(True)
        self.update_selected_count()

    def select_none(self):
        for v in self.album_vars:
            v.set(False)
        self.update_selected_count()

    def invert_select(self):
        for v in self.album_vars:
            v.set(not v.get())
        self.update_selected_count()

    def update_selected_count(self):
        n = sum(1 for v in self.album_vars if v.get())
        self.selected_count_var.set(f"已选择 {n} / {len(self.album_vars)} 个相册")

    def choose_path(self):
        path = filedialog.askdirectory(title="选择下载保存路径")
        if path:
            self.path_var.set(path)

    def logout(self):
        self.client = QzoneClient()
        self.albums = []
        self.album_vars = []
        self.show_login_frame()

    # ==================== 下载 ====================

    def start_download(self):
        if self.is_downloading:
            return
        selected = [a for a, v in zip(self.albums, self.album_vars) if v.get()]
        if not selected:
            messagebox.showwarning("提示", "请至少选择一个相册")
            return
        self.download_path = self.path_var.get().strip()
        if not self.download_path:
            messagebox.showwarning("提示", "请选择下载路径")
            return
        try:
            os.makedirs(self.download_path, exist_ok=True)
        except Exception as e:
            messagebox.showerror("错误", f"无法创建下载目录：{e}")
            return
        self.show_download_frame(selected)

    def show_download_frame(self, selected_albums):
        self.clear_main()
        self.is_downloading = True

        info_frame = ttk.Frame(self.main_frame)
        info_frame.pack(fill=tk.X, pady=(0, 10))
        self.progress_var = tk.StringVar(value="准备中...")
        ttk.Label(info_frame, textvariable=self.progress_var,
                  font=("Microsoft YaHei", 10)).pack(side=tk.LEFT)

        self.overall_bar = ttk.Progressbar(self.main_frame, mode="determinate")
        self.overall_bar.pack(fill=tk.X, pady=(0, 10))

        cur_frame = ttk.Frame(self.main_frame)
        cur_frame.pack(fill=tk.X)
        ttk.Label(cur_frame, text="当前相册：").pack(side=tk.LEFT)
        self.cur_bar = ttk.Progressbar(cur_frame, mode="determinate")
        self.cur_bar.pack(side=tk.LEFT, fill=tk.X, expand=True)

        log_frame = ttk.LabelFrame(self.main_frame, text="下载日志", padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self.log_text = scrolledtext.ScrolledText(log_frame, height=15,
                                                  state=tk.DISABLED,
                                                  font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)

        threading.Thread(target=self._do_download,
                         args=(selected_albums,), daemon=True).start()

    def log(self, msg):
        def _append():
            self.log_text.config(state=tk.NORMAL)
            self.log_text.insert(tk.END, msg + "\n")
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)
        self.root.after(0, _append)

    def _do_download(self, selected_albums):
        total_albums = len(selected_albums)
        total_ok = 0
        total_fail = 0

        for idx, album in enumerate(selected_albums, 1):
            name = sanitize_filename(album["name"])
            aid = album["id"]
            self.root.after(0, self.progress_var.set,
                            f"[{idx}/{total_albums}] 正在处理：{name}")

            album_dir = os.path.join(self.download_path, name)
            try:
                os.makedirs(album_dir, exist_ok=True)
            except Exception as e:
                self.log(f"[错误] 无法创建目录 {album_dir}: {e}")
                continue

            self.log(f"[{idx}/{total_albums}] 获取相册 [{name}] 媒体列表...")
            try:
                media = self.client.get_media_list(aid, log=self.log)
            except Exception as e:
                self.log(f"  获取媒体列表失败: {e}")
                continue

            n_img = sum(1 for m in media if not m["is_video"])
            n_vid = len(media) - n_img
            self.log(f"  共 {len(media)} 项（图片 {n_img}，视频 {n_vid}）")
            self.root.after(0, self.cur_bar.configure,
                            {"maximum": max(len(media), 1), "value": 0})

            ok = fail = 0
            for i, item in enumerate(media, 1):
                if item["is_video"]:
                    url = self.client.resolve_video_url(aid, item)
                else:
                    url = item.get("url") or item.get("thumb") or ""

                if not url:
                    fail += 1
                    self.log(f"  [跳过] 无下载链接：{item['name']}")
                else:
                    ext = media_ext(url, item["is_video"])
                    prefix = "VID" if item["is_video"] else "IMG"
                    fname = f"{prefix}_{i:04d}{ext}"
                    save_path = os.path.join(album_dir, fname)
                    if os.path.exists(save_path):
                        save_path = os.path.join(album_dir, f"{prefix}_{i:04d}_{int(time.time())%100000}{ext}")
                    if self.client.download_media(url, save_path):
                        ok += 1
                        self.log(f"  [成功] {fname}")
                    else:
                        fail += 1
                        self.log(f"  [失败] {fname}")

                self.root.after(0, self.cur_bar.configure, {"value": i})
                pct = ((idx - 1) + i / max(len(media), 1)) / total_albums * 100
                self.root.after(0, self.overall_bar.configure, {"value": pct})

            total_ok += ok
            total_fail += fail
            self.log(f"  相册 [{name}] 完成：成功 {ok}，失败 {fail}\n")

        self.is_downloading = False
        self.root.after(0, self.progress_var.set,
                        f"全部完成！成功 {total_ok} 项，失败 {total_fail} 项")
        self.root.after(0, self.overall_bar.configure, {"value": 100})
        self.log("=" * 50)
        self.log(f"全部下载完成！成功 {total_ok} 项，失败 {total_fail} 项")
        self.log(f"保存路径：{self.download_path}")

        def done():
            messagebox.showinfo(
                "下载完成",
                f"全部下载完成！\n成功：{total_ok} 项\n失败：{total_fail} 项\n"
                f"路径：{self.download_path}")
            self.show_album_frame()
        self.root.after(0, done)


# ============================================================
# 入口
# ============================================================

def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
