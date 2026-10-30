# Optional integrations / 可选连接

All examples are placeholders or disabled. Real configs belong in your private PersonalOS data directory, not the checkout. Copy with mode 0600 and supply your own values; do not paste tokens or calendar-feed URLs into GitHub issues.

所有示例均为占位或禁用状态。真实配置写在自己的 PersonalOS 数据目录，权限0600；不要提交令牌或私人日历订阅链接。

## Canvas

Configure your institution on the Mac, then restart the service:

```sh
python scripts/personalos.py configure-canvas https://canvas.YOUR-SCHOOL.edu
```

Replace the example with the actual HTTPS origin; no path, port, query or credentials. In PersonalOS → Today → Todos → Canvas, paste a personal API token **if your institution permits it**, or the Calendar Feed URL copied from Canvas's Calendar page. The existing token field accepts a feed URL too. Secrets are saved privately on the Mac; requests do not follow redirects or arbitrary origins. Do not bypass an administrator restriction on API tokens.

先配置学校真实 HTTPS 地址、重启，再在 App 的 Canvas 页面连接。学校禁止创建令牌时，优先使用它提供的 Calendar Feed。订阅只有截止记录，没有可靠提交状态；PersonalOS不会将作业从 feed 消失当作已提交。

The current parser imports dated assignment events, not every quiz or personal appointment. Both server and App use the configured origin. Institution-specific SSO/redirect requirements may be incompatible; record the sanitized error rather than posting the feed URL.

## Local files and Mac applications

For a small test folder:

```sh
mkdir -p "$HOME/Documents/PersonalOS Demo"
python scripts/personalos.py grant "$HOME/Documents/PersonalOS Demo"
python scripts/personalos.py init --build-helper
```

The grant applies to that exact directory. Broad Mac file browsing is a separate opt-in:

```sh
python scripts/personalos.py files-enable
# To disable later:
python scripts/personalos.py files-disable
```

This uses ordinary macOS file permissions and still excludes credential/session paths. Full Disk Access, if needed for your chosen directories, must be granted by you in System Settings to the actual interpreter/tool macOS attributes the access to. PersonalOS does not change TCC or bypass the sandbox. Start with a non-sensitive folder; inspect real errors instead of granting everything preemptively.

目录授权、广域只读访问和文件写回是不同权限。文件整理与覆盖前需要看清预览、确认当前版本；原件备份和撤销不是任意删除功能。不要为了普通目录测试先给所有工具完全磁盘权限。

The native helper permits a small allowlist of Mac actions. It does not give the model a general shell. iPhone maps/sharing use system interfaces; final message sending remains with the user.

## HealthKit and APNs

Base builds use the empty `PersonalOS.entitlements`. For these optional features, configure your own Apple developer account and provisioning:

1. In Xcode add the desired capability to your **own** App ID/team. Confirm any account agreements yourself.
2. Use `PersonalOS/PersonalOSOptional.entitlements` only when the corresponding HealthKit/APNs capabilities are provisioned. If enabling only one, make a local entitlement file containing only that capability.
3. HealthKit: after provisioning the entitlement, set `PersonalOSHealthKitEnabled` to Boolean YES in `ios/PersonalOS/Info.plist`. Base builds keep it NO and do not call HealthKit authorization. Then grant selected data types on the phone. Enable sharing to your private Mac inside PersonalOS. Absence of a sample is not proof permission was denied, nor proof no activity occurred.
4. APNs: use your own key and bundle topic. Store the key outside the repo, copy `config/examples/push.json` into the private data directory as `push.json`, set mode0600, adapt the values and enable it. The topic must exactly match the phone build. Use the matching sandbox/production environment.

签名能力、健康授权和通知授权分别由 Apple/用户控制。不要分发自己的推送密钥或描述文件。基础版不需要先开启这些能力；配置存在、Apple接受请求、手机实际收到通知是三个不同的验证结果。

Huawei Health data is available to PersonalOS only if it is actually written into Apple Health and the user shares it. PersonalOS does not promise a Huawei account API or scraping connector. Training drafts use supplied data and declared background; body weight, nutrition and training load are not inferred from heart rate alone.

## Local male speech (optional)

System speech works without local TTS. To use Uncle_Fu, create an isolated environment:

```sh
python3 -m venv "$HOME/Library/Application Support/PersonalOS-OpenSource/tts-venv"
"$HOME/Library/Application Support/PersonalOS-OpenSource/tts-venv/bin/python" -m pip install -r tts-requirements.txt
```

Separately download the data files of [Qwen3-TTS CustomVoice 8bit](https://huggingface.co/mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit), honoring its model card and license. Preserve the model's tokenizer assets and directory structure. No weights are bundled. Copy `config/examples/tts.json` privately, set its interpreter/model to the real absolute paths, mode0600 and `enabled=true`; restart PersonalOS and select Uncle_Fu in settings. If the model cannot load, keep system speech as the fallback. This optional fresh dependency/model installation must be verified on your hardware.

可选男声需自己下载模型与独立依赖、填写真实路径，再在手机选择 Uncle_Fu。不要把下载完成等同于已成功播报，先听一段中英文日期时间再使用。

## A second Mac (advanced)

This release ships generic restricted agents, **not an automatically trusted remote computer or pinned personal host key**. The account's Remote Login must be enabled by its owner. Use a dedicated key with no general shell, forwarding or agent access. Verify the remote host fingerprint through a trusted path; do not disable host-key checking or blindly trust key-scan output.

For file access, copy `air/file_agent.py` and `server/file_access.py` to a private directory on the remote Mac. Install only that dedicated public key in its `~/.ssh/authorized_keys` with this shape (replace every placeholder):

```text
restrict,command="/usr/bin/python3 /Users/REMOTE_USER/.local/share/personalos-files/file_agent.py" ssh-ed25519 YOUR_DEDICATED_PUBLIC_KEY personalos-files-readonly
```

On the main Mac, copy the disabled `air-files-source.json` example to the private state directory, fill your remote host/user/home and private key/pinned known-hosts paths, set mode0600 and enable only after a read-only test. The configured host must be private or a `.ts.net`/`.local` name. Root accounts are rejected. Remote requests still enforce read-only paths and exclude credentials.

For a single project exporter instead, use `air/project_readonly.py` and a private adjacent `project-root.txt` containing the exact authorized absolute directory, with a separate forced-command key. Use the `project-source.json` example with the same root. Never reuse a normal login key as if it were restricted.

These are advanced manual procedures. The exporters and restricted installers have isolated tests; a new user's host-key provisioning, file permissions, OS Python and live SSH link require their own verification. Do not run the installer modules accidentally on the main Mac: they are intended to execute only under the authorized remote user's account.

第二台 Mac 需要其本人开启远程登录、核对主机身份、安装专用强制命令公钥。公开包没有你的设备地址、已信任主机密钥，也不自动扩大读取权限。远端保持只读。
