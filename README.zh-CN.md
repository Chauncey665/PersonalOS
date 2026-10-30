# PersonalOS

**把你说的话，变成能执行的日程。**

面向 iPhone 和个人 Mac 的本地优先 AI 助手。

[English](README.md) · [安装教程](docs/INSTALL.zh-CN.md) · [语音入口](docs/VOICE.md) · [安全边界](SECURITY.md)

> 公开 Alpha：这是从真实自用项目整理的开源版本。界面目前以简体中文为主；支持中英文输入和语音，但并不保证每一种自然表达、日期或系统操作都识别正确，也不是 App Store 成品。

“周五前交报告。” → 记下截止日期 → 避开课程安排执行时间 → 同步 Apple 日历 → 没做完的自动顺延 → 简短报告真实调整。

## 助手叫什么，由你决定

**PersonalOS 是项目名，助手的名字可以由每位用户自己修改。** 例如叫 Atlas 或“阿特拉斯”：

```sh
python scripts/personalos.py name Atlas
```

重启 Mac 服务后开始新对话即可生效。称呼保存在自己的私有 `assistant-profile.json`，不会改变工具权限。如果也要改手机桌面的 App 名称，在 Xcode 修改 `CFBundleDisplayName` 后重新构建；快捷指令使用相同 Bundle ID。

## 能做什么

- **语音和文字记任务。** 区分无时间待办、截止日期和明确起止日程；支持项目和子任务。
- **按真实空档排程。** 使用手机授权共享的日历，避开课程、吃饭和缓冲时间；可开启每日自动顺延。执行时间改变不代表提交截止延后。
- **写入 Apple 日历。** 计划、截止、已完成分开显示；任务需要明确勾选完成。只有手机回传当前版本写入成功，才报告日历已同步。
- **让 Mac 处理资料。** 授权目录检索、文件预览、修改副本、确认写回和撤销；可选第二台 Mac 只读连接。
- **连续对话。** 保存会话和分步执行记录，按需加载工具，保留近期上下文，识别重复无进展的调用。
- **可选扩展。** Canvas 待办和订阅、手机定位天气、健康数据、训练草案、远程推送。目前节假日接口仅支持香港公众假期。

## 先跑通最小闭环

准备 Apple Silicon Mac、Python 3.11+ 和 [Ollama](https://ollama.com/download)。进入本项目目录：

```sh
git clone https://github.com/Chauncey665/PersonalOS.git
cd PersonalOS
python3 -m venv .venv
source .venv/bin/activate
python scripts/personalos.py init
python scripts/personalos.py doctor
python scripts/personalos.py serve
```

另开一个终端，激活同一虚拟环境：

```sh
python scripts/personalos.py pair
```

在**这台 Mac** 打开 `http://127.0.0.1:8765`，输入本机显示的五分钟配对码，提交“设备状态”。这会读取真实设备信息，不需要模型。

自然语言规划需要显式下载已配置的模型：

```sh
ollama pull qwen3.5:9b
```

Ollama 需要在 `127.0.0.1:11434` 运行。初始化不会下载模型，`doctor` 会核对完整标签是否安装。[模型页面](https://ollama.com/library/qwen3.5)列有可用规格；参考设备是 32GB Apple Silicon Mac，其他内存配置需自行验证速度与内存。

然后按[完整安装教程](docs/INSTALL.zh-CN.md)配置你自己的 Tailscale、Apple 签名和 iPhone App。

## 安装条件与边界

| 部分 | 说明 |
| --- | --- |
| Mac 服务 | Python 标准库、SQLite；只监听本机 IPv4 loopback |
| 本地推理 | 可配置 Ollama 模型标签、上下文和驻留时间；没有自动云端回退 |
| iPhone | 当前源码需完整 Xcode 和 iOS 26+ SDK 编译；建议 iOS 26+ 使用当前语音流程，不能把较低部署目标当作所有旧设备已经验收 |
| 日历 | 用户亲自在手机授予 EventKit 权限；iOS 后台执行有限制 |
| 语音 | 可选安装 MLX 及独立下载识别模型；基础安装不自动下载 |
| 播报 | 默认系统声音，可选本地 Qwen TTS / Uncle_Fu |
| 健康、推送 | 分别开启对应签名能力和用户权限；基础签名不包含这两项 |
| Canvas | 先在 Mac 配置学校 HTTPS 地址；仅访问指定来源。订阅模式无法判断是否提交作业 |
| 第二台 Mac | 高级可选配置，使用强制命令 SSH 专用钥匙和主机身份验证，远端保持只读 |

默认工作时段为 11:00 至次日 01:00，午晚餐各45分钟，待办默认45分钟、每日预算4小时。排程设置接口允许调整预算和时间；当前手机设置页未提供完整作息编辑器。不要把默认值误当成根据每个人生活习惯自动学习的结果。

这是为**一个人与其可信设备**准备的私人服务，不面向公网或多租户部署。

## 开发与验证

```sh
python scripts/check_release.py
python -m unittest discover -s tests
python -m unittest discover -s air
python scripts/smoke_install.py
```

测试使用临时状态与合成资料；需要真实 Ollama 的旧集成用例默认跳过，可显式设 `PERSONALOS_LIVE_TESTS=1`。干净安装检查会启动独立本机端口、配对两台合成设备、验证隔离、执行真实设备查询，并清除临时状态。

```sh
xcodebuild -project ios/PersonalOS.xcodeproj -scheme PersonalOS \
  -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath build CODE_SIGNING_ALLOWED=NO build
```

已提供 GitHub Actions 配置；实际远程 CI 要在仓库发布后运行，不能把本地通过当作远程 CI 已通过。[验证范围](docs/VERIFICATION.md)区分代码、构建、设备和账号条件。

## 目录与贡献

`server/` 是服务端，`ios/` 是原生客户端，`scripts/` 提供安装和检查，`shortcuts/` 保存无签名工作流，`config/examples/` 保存禁用状态的配置示例，`air/` 是可选受限导出器。服务运行数据默认保存在 `~/Library/Application Support/PersonalOS-OpenSource`，不会进入仓库。

欢迎按 [CONTRIBUTING.md](CONTRIBUTING.md) 提交可复现的问题与改进。不要上传自己的健康导出、日历订阅链接、数据库、配对码或密钥。

本项目采用 [MIT](LICENSE)；模型和可选依赖分别遵循其许可。设计参考与第三方说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
