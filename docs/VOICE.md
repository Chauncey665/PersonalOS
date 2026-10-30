# Voice / 语音与操作按钮

Voice is optional. Complete text pairing first. These setup commands explicitly download packages and model data when you choose to run them; a phone request never triggers installation.

语音是可选功能，请先跑通文字配对。下面命令仅在你主动执行时下载依赖和权重；手机请求不会触发安装。

## Local recognition / 本地识别

On an Apple Silicon Mac, install FFmpeg through your package manager if it is not available (`brew install ffmpeg` when using Homebrew). Activate the project's Python environment, initialize the runtime, then use the pinned Qwen ASR installer:

```sh
python scripts/personalos.py init
python scripts/setup_qwen_asr.py
```

The script creates `speech-qwen-venv`, downloads only approved data files at the pinned revision, verifies publisher hashes, validates the manifest and enables `voice.json` only afterwards. The weight file is approximately 2.46 GB; packages and caches need additional space. The setup may take time and must finish without errors. Restart PersonalOS after enabling it. Use the same `--home` if you chose a custom state directory.

安装器创建独立 `speech-qwen-venv`，仅下载固定版本批准的数据文件，核对哈希与清单，通过后才启用语音。主权重约2.46GB，依赖和缓存另占空间。完成后重启 PersonalOS；自定义目录需传相同 `--home`。不要用“进度100%”代替完整校验。

An alternative, separately pinned Whisper setup is retained:

```sh
python setup_local_speech.py
python server/personal_os.py voice-enable
```

Do not mix engines' virtual environments or manifests. The Qwen setup is the recommended public recipe; a model being installed does not establish recognition accuracy for your accent or mixed-language speech. Dates still need checking in Calendar.

不要混用两种引擎的环境和清单。Qwen 是公开版推荐方案；安装成功不代表所有口音、中英混说和日期都准确，日历仍需检查。

## Action Button / 快捷指令

The repo contains **unsigned XML sources**, with no credentials or personal signing certificate. If you changed the App's bundle ID in Xcode, generate workflows for that same ID and your own ten-character Xcode Team ID (`YOURTEAMID` is a placeholder):

```sh
python scripts/build_shortcuts.py --bundle-id com.yourname.PersonalOS --team-id YOURTEAMID --sign
```

`--sign` invokes Apple's local `shortcuts sign --mode anyone` command and may require your Mac's Apple/Shortcuts setup. This uses your identity. Generated `.shortcut` files go into ignored `build/shortcuts/`; do not commit them as anonymous source.

仓库仅含无签名 XML 源文件。使用与 Xcode 完全相同的 Bundle ID 和你自己的十位 Team ID 生成并在自己的 Mac 签名；该步骤可能需要你的 Apple/快捷指令环境。输出位于已忽略的 `build/shortcuts/`，不要把个人签名文件提交到仓库。

Transfer the locally signed `PersonalOS-Conversation.shortcut` to your iPhone and import it. Its display name is **PersonalOS 连续对话**. Open PersonalOS once after installation, then in Settings → Action Button → Shortcut choose that entry. Allow microphone and required sharing/location permissions in Apple's dialogs. Use Always Allow only when the system offers it and you accept the scope; some prompts have no persistent option.

将自己签名的文件传到 iPhone 导入，显示名为“PersonalOS 连续对话”。安装后先打开一次 App，然后在操作按钮设置选择它。麦克风、分享和定位仍由系统授权；没有“始终允许”的提示不能由 App 绕过。

Start with `请显示设备状态`, stop the recording, and listen for a brief actual result. Later test `找附近的餐厅` → `第二家，公交过去`. Maps may still require choosing a service or tapping Depart. The workflow has bounded polling and recording rounds; if interrupted, check the retained task rather than repeatedly submitting the same file/voice.

先验收设备状态，再测餐厅→第二家公交。Apple 地图可能仍要求选班次或点出发。锁屏、AirPods、Siri 和连续录音需要在你自己的手机验收；公开包的结构检查/构建不能替代这些验证。

Without a locally configured TTS model, the workflow uses system speech. Optional Uncle_Fu setup is described in [INTEGRATIONS.md](INTEGRATIONS.md).
