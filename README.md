# QQ空间相册批量下载工具

图形界面的 QQ 空间照片/视频批量下载工具：扫码登录 → 多选相册（全选/反选）→ 选择保存路径 → 一键批量下载原图与视频。

## 功能特性

- **扫码登录**：手机 QQ 扫码授权，无需输入密码
- **相册选择**：自动拉取全部相册，支持多选、全选、全不选、反选
- **图片 + 视频**：自动识别相册中的图片和视频，图片下载原图，视频获取直链下载
- **批量下载**：实时进度条与日志，文件按 `IMG_0001.jpg` / `VID_0001.mp4` 顺序命名
- **单文件 exe**：开箱即用，无需安装 Python 环境

## 下载使用

到 [Releases](../../releases) 页面下载最新的 `QzonePhotoDownloader.exe`，双击运行即可。

> Windows 首次运行可能弹出 SmartScreen 提示，点击「更多信息」→「仍要运行」。

## 从源码运行

```bash
pip install curl_cffi pillow
python qzone_downloader.py
```

## 打包单文件 exe

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name QzonePhotoDownloader --collect-all curl_cffi qzone_downloader.py
```

## 技术说明

- 使用 `curl_cffi` 模拟 Chrome 浏览器 TLS 指纹，规避网关风控
- 接口参数对齐 QQ 空间网页版真实请求（`fcg_list_album_v3` / `cgi_list_photo` / `cgi_floatview_photo_list_v2`）
- 界面基于 Python 内置 tkinter

## 免责声明

本工具仅供个人备份自己 QQ 空间内容使用，请遵守 QQ 空间服务条款与相关法律法规，勿用于侵犯他人隐私或其他违规用途。
