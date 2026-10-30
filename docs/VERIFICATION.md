# Verification and troubleshooting / 验证与排错

A release needs several kinds of evidence. They are deliberately separate:

| Check | What it proves | What it does not prove |
| --- | --- | --- |
| Source/privacy gate | Included paths, common secret markers, empty default server, no personal signing team, resolvable local documentation links | A complete independent security audit |
| Python tests | Behavior under isolated fixtures, including auth, owner scope, task/calendar state and recovery | Every natural-language utterance or real provider |
| Clean-copy smoke | A checkout under a different directory can initialize, serve, pair, isolate devices, move/undo a test file and process synthetic calendar acknowledgments | A physical EventKit write or an LLM's understanding |
| Unsigned Simulator build | Public Swift sources compile without the original owner's signing identity | Your Apple account/profile, microphone, Siri or real device permissions |
| Local model test | A selected installed model returns an answer/uses tools for a particular case | Broad intelligence, all accents or every date expression |
| Optional connector test | Only the exact configured account/service and actual receipt verified | A different school's Canvas, Huawei sync or universal APNs delivery |

Local release results are recorded in `release-checks.json` after preparation. CI execution on GitHub, a fresh optional-model download and a new person's phone installation are not claimed before they happen.

## Common issues

- **Connection fails:** verify Mac foreground service, `doctor`, Tailscale on both devices and your own `serve status` URL. The App requires HTTPS `.ts.net`; `localhost` on iPhone points to the phone itself. Pairing does not create network access.
- **Port occupied:** use another port or stop only the service you own. The installer does not kill another listener.
- **Model missing:** run Ollama and explicitly install the exact configured tag. Do not infer installation from a download percentage; `doctor` checks installed tags.
- **Calendar pending:** open PersonalOS with Tailscale connected. Check selected calendars, EventKit permission and fresh complete snapshots. Do not repeatedly re-submit the same event.
- **Canvas fails:** set the real origin on the Mac, restart, then connect in the App. Never post a private feed URL; provide a redacted error and whether using API or feed.
- **Health data empty:** confirm the source app writes Apple Health, user consent, and an actual sample. A sync success can still contain missing fields.
- **Voice unavailable:** finish the explicit setup, check interpreter/model files and FFmpeg, then restart. Package installation and model verification are independent phases. Do not mix Qwen and Whisper environments/configs.
- **Shortcut opens a missing action:** the workflow bundle ID must match the App you built. Regenerate/sign using your Mac; open the App once before adding shortcuts.
- **Signing failure:** use your own team and unique bundle ID. Begin with base entitlements; add optional capabilities only after provisioning. Never borrow another person's signing files.

排错时先确认最后真正成功的一步。准备了计划、生成了文件副本、Apple接受了推送、手机实际同步，是不同状态。分享问题时仅附脱敏错误，不上传数据库、健康记录、学校日历链接、密钥或录音。


For an optional clean-copy test with an **already installed** model, run `python scripts/smoke_install.py --model qwen3.5:9b` (or your exact installed tag). It does not download a model or alter aliases. It additionally requires an actual local model tool call and a verified device-status receipt. This is one acceptance case, not a broad model benchmark.
