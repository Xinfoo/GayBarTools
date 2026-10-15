# GayBarTools

按 [构建需求](docs/HOW_AGENT_TO_BUILD.md) 编写的 Python 桌面章节助手，支持 Linux 和 Windows。使用独立 Chromium/Chrome/Edge 会话，手动登录和进入课程后，识别视频或静态文档、等待正常播放或滚动阅读、识别学习检测并调用 OpenAI 兼容接口作答、提交后进入下一节。

当前已实现可运行版本和本地演示。**真实学习通课程及真实 API 尚未验收**：录屏无法提供实际 DOM 或服务端规则，可能需要针对课程页面调整选择器。详见 [录屏分析和验证范围](docs/VIDEO_NOTES.md)。

## 安装与启动

需要 Python 3.11+，桌面界面使用 Python 的 Tkinter。Linux 的 Python 发行包需包含 Tk，并需已有可用的 `bubblewrap`（`bwrap`）来限制浏览器写入；Windows 官方 Python 安装可包含 Tcl/Tk。程序不会安装系统包，也不会修改 shell 或用户浏览器设置。

在项目目录执行：

```sh
python3 scripts/bootstrap.py
./run.sh
```

Windows：

```bat
py scripts\bootstrap.py
run.bat
```

依赖仅安装到 `.venv`，锁定版本见 `requirements.lock`。自动查找系统 Chromium、Edge，Windows 也查找 Chrome；也可以在界面指定可执行文件。没有浏览器时下载到项目目录：

```sh
.venv/bin/python -B -m gaybar_tools --install-browser
```

Windows 使用 `.venv\Scripts\python.exe` 替代 `.venv/bin/python`。也可安装时直接使用 `python3 scripts/bootstrap.py --browser`。浏览器下载需要网络；系统动态库缺失时需由用户准备可运行的浏览器环境，脚本不会执行 `install-deps` 或修改系统包。

## 使用

1. 填写服务商的 API 地址（例如 `https://api.openai.com/v1`）、模型名和密钥。支持完整 `/chat/completions` 地址，也支持无需密钥的本地兼容服务。模型需支持图片输入，或启用本机 OCR 配合文本模型。可先“测试 API”；这会发送一条很小的文本请求。
2. 点击“打开浏览器”，在新窗口手动登录学习通，打开任意课程的任意章节。登录 Cookie 每隔约 5 秒自动保存，关闭软件时也会保存；登录后可点击“立即保存登录状态”，无需先配置 API。下次打开浏览器会自动恢复，包括会话 Cookie。网站主动退出或登录过期后仍需重新登录。“开始刷课”在空闲时始终可点击，不再等待后台章节检测；执行任务和关闭窗口时会临时禁用。
3. 点击开始后，程序检查最近打开的可用页面，根据实际内容识别视频、文档或检测。请让要处理的课程章节成为最近打开的页面。没有打开浏览器、需要登录或页面无法识别时会显示原因并停止。视频按页面正常播放并等待结束；静态文档使用鼠标滚动内部阅读区域。随后进入“学习检测”，提取单选、多选和判断题，将题目及启用的截图发送给配置的 API。
4. 检查答案格式和选项映射；检查失败、返回空答案或输出被截断时，重新发送同批题目及本次原有的截图、OCR 内容，最多额外重试 3 次（含首次共 4 次请求）。任一次通过后模拟点击选项，并按“自动提交并继续下一节”设置执行：开启时自动提交，确认提交成功后进入下一节；关闭时仅填答，留待手动检查和提交。API 网络或鉴权错误会直接报告，不触发格式重试；点击“停止”会终止后续重试。
5. “停止”中断后续操作，保留当前浏览器；已经发出的 API 请求会等待返回或超时。关闭窗口会结束软件启动的浏览器进程。

程序使用有间隔的鼠标移动、点击和滚动，减少机械式操作；不保证规避平台检测。不会修改播放进度、播放倍速、网站完成标志或调用课程完成接口。遇到验证码、登录失效、无法识别的控件、答案格式重试耗尽或不明确的提交结果会停止并保留当前页面，保存截图供排查。

文档阅读会重新定位嵌套页面中可见的正文区域，再在该区域发送鼠标滚轮，检查实际阅读容器的滚动位置和内容高度。外层空壳、右侧课程目录或不可读取进度的插件不作为“阅读到底”的依据。日志会记录正文鼠标坐标和滚动进度；如果仍无法滚动，可用这些信息配合诊断截图排查页面差异。

OCR 为可选项：需已有 `tesseract` 和 `chi_sim`、`eng` 语言数据。默认使用 `chi_sim+eng`；可在配置文件修改 `ocr_languages`。OCR 通过标准输入/输出处理截图，未安装时可使用视觉模型。

## 本地演示与检查

无需学习通账号或 API 密钥：

```sh
./run.sh --demo
./run.sh --demo-headless
./run.sh --doctor
.venv/bin/python -B -m unittest discover -s tests -v
```

图形演示中点击“打开浏览器”和“开始刷课”。命令行演示自动完成两节：真实 3 秒视频 → 单选/多选/判断检测 → 内部滚动文档 → 检测 → 课程结束。演示答案只允许用于本次启动的本地演示源站，不会发送给真实课程。

测试使用本地 HTTP 服务及独立临时配置；浏览器集成测试需要可运行的 Chromium，不会登录真实站点或调用付费 API。

## 数据与目录

持久运行数据位于项目的 `.runtime/`，已加入 `.gitignore`；Linux 浏览器 socket 使用短 `/tmp/gbt-*` 临时目录，关闭会话后清理，避免较长项目路径触发系统 socket 长度限制：

| 路径 | 用途 |
| --- | --- |
| `.venv/` | 项目专用 Python 依赖 |
| `.runtime/browser-profile/` | 独立浏览器登录、Cookie、历史、缓存 |
| `.runtime/browser-profile/cookies.json` | 自动保存的 Cookie 快照，启动时恢复会话登录 |
| `.runtime/settings.json` | API、识别和运行设置 |
| `.runtime/logs/` | 运行日志，不记录 API Key 或请求正文 |
| `.runtime/screenshots/` | 出错时的诊断截图，可能包含课程或账号内容 |
| `.runtime/browsers/` | 按需下载的 Chromium |
| `.runtime/cache/`、`tmp/` 等 | 子进程缓存、下载和临时数据 |

默认不持久保存 API Key；可通过 `GAYBARTOOLS_API_KEY` 或 `OPENAI_API_KEY` 提供。勾选“记住密钥”会明文写入本地配置，支持的平台上设置为仅当前用户可读写。取消勾选并保存即可移除已保存的密钥。

Cookie 快照保存在项目内，以原子替换方式更新；Linux 下文件权限为仅当前用户可读写，日志不记录 Cookie 值。网站退出登录后，新的 Cookie 状态也会覆盖旧快照；即使全部清空也会保存。浏览器拒绝恢复快照时会保留原文件，请手动登录后点击“立即保存登录状态”再恢复自动保存。删除 `.runtime/browser-profile/` 可清除软件的全部登录状态；请先关闭浏览器。图形演示使用单独的 `.runtime/demo-browser-profile/`，命令行演示使用项目内临时会话，两者都不会复用真实登录配置。

进程和浏览器子进程的 XDG 缓存、配置、数据和临时路径均指向项目目录。Linux 额外通过 bubblewrap 将宿主普通文件系统设为只读，持久文件只允许写入项目目录和 `/tmp`，避免 Chromium 访问旧 NSS 证书库时写入用户目录。私有 `/dev` 中会单独保留现有的 DRM/NVIDIA 显卡字符设备，供 Wayland/EGL 和视频渲染使用；不映射整棵宿主 `/dev`，也不把设备目录变成可写的宿主文件目录。隔离不可用时停止启动，不会退回未隔离模式。不会修改 `HOME` 或接管当前浏览器配置。Windows 使用独立会话和项目内缓存路径，尚未做 Windows 实机验证。

截图仅在启用视觉模式并开始作答时发送给配置的 API；启用 OCR 时在本机识别，识别文本仍会发送给 API。全页截图受 iframe 视口裁剪限制，内部屏幕外的纯图片题可能无法完整识别，需要人工处理；DOM 能读取的题干与选项会同时提供给模型。

主要代码：`ui.py` 桌面界面，`worker.py` 浏览器线程，`browser.py` 页面适配，`cookies.py` 登录 Cookie 快照，`workflow.py` 章节状态机，`llm.py` API 与答案校验，`config.py`/`paths.py` 配置和隔离目录。页面结构变化时，应先调整 `browser.py` 并运行本地回归，再验证实际课程。

API 图片消息采用官方的 `image_url` / Base64 data URL 格式，参考 [OpenAI 图片与视觉文档](https://developers.openai.com/api/docs/guides/images-vision)。
