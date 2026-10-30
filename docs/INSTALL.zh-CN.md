# 安装 PersonalOS

[English](INSTALL.md) · [项目说明](../README.zh-CN.md)

先跑通 Mac 网页，再连接手机。第一次设备状态验收不需要 Apple 签名、健康权限或推送密钥。

## 1. 准备环境

需要 Apple Silicon Mac、建议 Python 3.11+，以及用于自然语言推理的 [Ollama](https://ollama.com/download)。参考机器是32GB内存，不表示所有模型在较小内存机器上都能流畅运行。可选 MLX 语音环境与基础服务分开安装。

手机连接需要两端安装 [Tailscale](https://tailscale.com/download) 并登录你自己的网络；编译 App 需要完整 Xcode 和 iOS 26+ SDK。

下载仓库源码 ZIP，解压到长期保留的目录，在该目录打开终端：

```sh
python3 -m venv .venv
source .venv/bin/activate
python scripts/personalos.py init
python scripts/personalos.py doctor
```

状态和配置位于 `~/Library/Application Support/PersonalOS-OpenSource`。初始化采用私有目录和配置权限，再次执行不会覆盖现有配置。公开版使用独立服务标识、App Bundle ID；不会自动迁移其他私人部署。

确认 Ollama 正在运行，再显式下载模型：

```sh
ollama pull qwen3.5:9b
python scripts/personalos.py doctor
```

也可新建状态目录并用 `init --model TAG` 选择已经安装的本地工具模型，或编辑私有 `local-model.json` 后重启。不要为了安装修改其他程序共用的模型别名。模型缺失时需要继续配置，不会转到云端。

## 2. Mac 上验收

```sh
python scripts/personalos.py serve
```

保持该终端运行，另开终端、激活同一虚拟环境：

```sh
python scripts/personalos.py pair
```

在这台 Mac 打开 `http://127.0.0.1:8765`，输入本机显示的配对码，提交“设备状态”并刷新历史。应返回真实系统和磁盘信息。配对码五分钟有效、只能用一次；手机需要另外生成一枚。

端口被占用时不要停止不相关的服务，可用 `python scripts/personalos.py --port 18765 serve`，并让 Tailscale 转发相同端口。`--home`、`--port` 全局参数放在子命令前。自定义状态目录示例：

```sh
python scripts/personalos.py --home "$HOME/Library/Application Support/PersonalOS-OpenSource Test" init
```

该安装的后续命令都要使用相同 `--home`。

## 3. 建立私人手机连接

在 Mac 和 iPhone 登录 Tailscale，亲自确认系统网络扩展。如果网络尚未启用 MagicDNS/HTTPS，按 Tailscale 页面完成授权。Mac 找不到 `tailscale` 命令时可用 App 内的程序：

```sh
/Applications/Tailscale.app/Contents/MacOS/Tailscale serve --bg --https=8443 http://127.0.0.1:8765
/Applications/Tailscale.app/Contents/MacOS/Tailscale serve status
```

以你自己命令输出的 HTTPS 地址为准，域名应以 `.ts.net` 结尾，显示端口就一并填写。本项目没有公共服务器。保持 Serve 仅用于私人网络，不开启 Funnel 或路由器公网转发。账号授权及命令差异参考 [Tailscale 官方说明](https://tailscale.com/docs/reference/tailscale-cli/serve)。

iPhone 连接 Tailscale 后，先用 Safari 打开自己的地址，验证网络可达；可达仍不代表已完成 PersonalOS 配对。

## 4. 编译安装 iPhone App

1. 在 Xcode 打开 `ios/PersonalOS.xcodeproj`，选择 `PersonalOS` scheme，App 显示名为 PersonalOS。
2. 在 Signing & Capabilities 选择你自己的 Apple 账号/Team 和自动签名。
3. 改成独属于你的 Bundle ID，例如 `com.yourname.PersonalOS`。源码中的 `org.personalosassistant.app` 只是通用默认值。
4. 基础版使用空的 `PersonalOS/PersonalOS.entitlements`，健康入口保持关闭，避免可选健康和推送能力阻塞普通安装。需要健康功能时，按扩展文档配置签名及 `PersonalOSHealthKitEnabled`。
5. 连接并解锁 iPhone，在手机确认信任，并按系统提示开启开发者模式。在 Xcode 选择这台手机作为运行目标，参考 [Apple 官方教程](https://developer.apple.com/documentation/xcode/running-your-app-on-simulated-or-physical-devices)。
6. 点击 Run；Apple 账号协议、验证码和系统授权由你在对应界面完成。
7. 在 PersonalOS 设置里填写你自己的 Tailscale HTTPS 地址。公开版不会预填别人的服务器。Mac 再运行一次 `python scripts/personalos.py pair`，将新配对码填到手机。
8. 提交“设备状态”验证结果，再在模型运行时测试自然语言。

开发签名有效期取决于你的账号和描述文件。别人的 IPA 不能当作面向所有人的通用安装包；本仓库不分发该类私有包。

## 5. 日历与自动排程验收

在 PersonalOS 开启日历权限，并在 iOS 系统页面选择愿意授权的项目。读取空档、完成状态和撤销通常需要完整访问；仅写入权限不能提供完整课表。跨设备 iCloud 同步需要你本人的 iCloud 日历账户。

提交“明天15:00到16:00，安排日历验收”，在 Apple 日历勾选 PersonalOS 对应日历，检查日期、标题和时段；回到 PersonalOS 完成同步。Mac 创建 pending 记录只代表待同步，只有手机确认当前版本后才算写入成功。

需要排程时，在设置开启“自动安排待办”。初始作息是11:00至次日01:00，午晚餐各45分钟，课程缓冲15分钟，待办默认45分钟，每日预算4小时；完整作息编辑器尚未在手机提供，可通过排程设置 API 修改。

说或输入“开启每天自动重排”可启用未完成任务顺延；“关闭每天自动重排”可停止。检查时间为日历时区凌晨01:05，调整摘要最早10:30发送。课表过旧或手机未同步时会等待，离线手机可能需要你重新打开 App。

## 6. 登录后常驻

前台验收完成后，Control-C 停止前台服务，再按需安装登录启动项：

```sh
python scripts/personalos.py service-install --dry-run
python scripts/personalos.py service-install
```

服务标识为 `org.personalosassistant.agent`，在该用户登录后启动，不能越过加密磁盘解锁。保留当前 Python/虚拟环境；错误记录在私有数据目录中的 `service-error.log`，分享前先脱敏。

```sh
python scripts/personalos.py service-uninstall
```

卸载启动项会保留数据；`revoke` 会撤销所选状态目录的全部手机令牌。不要为了排查签名删除个人数据。

语音、操作按钮见 [VOICE.md](VOICE.md)；Canvas、文件、健康、推送、第二台 Mac 见 [INTEGRATIONS.md](INTEGRATIONS.md)。


Planning settings payloads and authentication: [API notes / 排程接口说明](API.md).
