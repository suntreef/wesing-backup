# 🎤 全民K歌备份工具（WeSing Backup）

**把你的全民K歌作品一键备份到本地** —— 歌曲、封面、MV、上传时间、介绍文案、评论、评分与全部详情，
并生成可直接试听的本地 HTML 歌单 + Excel 目录。完全本地运行，不收集任何数据。

| 主界面 | 备份完成 |
|---|---|
| ![主界面](docs/ui-home.png) | ![备份完成](docs/ui-done.png) |

## ✨ 特性

- **一键备份**：粘贴个人主页链接（或 uid）即可，无需命令行
- **最高音质**：保存服务端原始 AAC 流，不转码、不二次压缩
- **全部详情**：标题、上传时间、介绍文案、评分、播放/点赞/评论/鲜花/礼物数、合唱搭档、录制设备、完整接口 JSON
- **本地歌单**：生成 `index.html`（可试听/搜索/按时间·热度·评分排序，自动适配深色模式）+ `index.csv`（Excel 直开）
- **增量备份**：重跑自动跳过已备份作品，新歌/失败项自动补齐；误删文件自动修复
- **音频标签**：标题/歌手/日期/文案/封面自动写进 m4a（无损 `-c copy`，需系统安装 [ffmpeg](https://www.gyan.dev/ffmpeg/builds/)，可选）
- **MP3 副本 / ZIP 打包 / MV 视频**：一键可选
- **私密作品**：登录 Cookie 可连同"仅自己可见"的作品一起备份
- **代理支持**：`--proxy http://127.0.0.1:7890`

## 📥 下载使用（Windows）

1. 到 [Releases](../../releases) 下载 `KG-Backup.exe`（约 10MB，免安装）
2. 双击运行，浏览器会自动打开操作页面
3. 粘贴主页链接 → 点「开始备份」，完事

> macOS / Linux 用户：`pip install requests && python kg_app.py`（或 `python kg_backup.py <主页链接>` 用命令行）

## 🚀 使用方法

**图形界面**：双击 `KG-Backup.exe`（或 `python kg_app.py`）

**命令行**：

```bash
pip install requests

# 备份全部公开作品
python kg_backup.py "https://kg.qq.com/node/personal?uid=xxxxxxxxxxxxxxxx"

# 含私密作品（Cookie 获取方法见下）
python kg_backup.py <uid> --cookie-file cookie.txt

# 常用组合
python kg_backup.py <uid> --proxy http://127.0.0.1:7890     # 走本地代理
python kg_backup.py <uid> --since 2019-01-01 --until 2021-12-31
python kg_backup.py <uid> --mp3 --zip                       # MP3副本 + 打包
python kg_backup.py <uid> --list-only                       # 只看清单不下载
```

<details>
<summary><b>🔑 如何获取 Cookie（备份私密作品时才需要）</b></summary>

1. 电脑 Chrome/Edge 打开 https://kg.qq.com 并登录 QQ
2. `F12` → Network（网络）→ 刷新页面
3. 点任意发往 `kg.qq.com` 的请求 → Headers（标头）→ 复制 `Cookie:` 后面的**完整**内容
4. 保存为一行文本到 `cookie.txt`，运行时加 `--cookie-file cookie.txt`（图形界面直接粘贴进 Cookie 输入框）

Cookie 只保存在本机、只发往腾讯服务器，本工具不含任何上传行为。
</details>

## 📤 输出结构

```
kg_backup_昵称/
├── index.html     ← 双击打开：可试听/搜索/排序的本地歌单
├── index.csv      ← Excel 总目录（全部详细信息）
├── index.json     ← 增量备份索引
├── audio/         ← 歌曲：序号_日期_歌名.m4a（原始码率不转码）
├── covers/        ← 封面图 + 介绍配图
└── meta/          ← 每首完整详情 JSON + 评论
```

## ❓ FAQ

<details>
<summary><b>音质是什么水平？能拿到无损吗？</b></summary>
全民K歌服务端存储的就是 AAC 音频（约 80–130kbps，越新的作品码率越高），工具按原始流原样保存、绝不转码，这就是公开渠道能拿到的最高音质；服务端没有无损音源，转出 FLAC 是假无损。
</details>

<details>
<summary><b>为什么列表数量比账号显示的少？</b></summary>
差值一般是「仅自己可见」的私密作品，需要登录 Cookie 才能取到。
</details>

<details>
<summary><b>能导出全部历史评论吗？</b></summary>
详情页自带的最新一页评论（含总数）会完整导出；全量历史评论的接口未公开，暂不支持。
</details>

<details>
<summary><b>下载会弄脏我的账号吗？</b></summary>
工具只读取公开页面数据，带轻微限速与失败重试，默认并发 3。请勿调高并发狂拉他人主页。
</details>

## ⚠️ 使用须知

本项目仅用于**备份本人或经授权账号**的作品、留存个人记忆，请勿用于批量抓取他人内容、商业用途或任何违反全民K歌用户协议的场景。歌词与音乐版权归原权利方所有。

## 🛠 开发

```
core.py      备份核心（CLI / GUI 共用，支持进度回调与取消）
kg_app.py    图形界面：本地服务器 + 自动打开浏览器
kg_backup.py 命令行入口
static/      前端单页（原生 HTML/CSS/JS，无构建）
```

本地构建 exe：`pip install pyinstaller && pyinstaller --onefile --noconsole --name KG-Backup --add-data "static;static" kg_app.py`

## License

[MIT](LICENSE)
