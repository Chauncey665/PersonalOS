# Install PersonalOS

[中文](INSTALL.zh-CN.md) · [README](../README.md)

Start with the Mac web interface, then connect a phone. No Apple signing, health permission or push key is needed for the first device-status test.

## 1. Requirements and source

- Apple Silicon macOS; a 32 GB Mac is the reference host, not a universal minimum.
- Python 3.11+ recommended. Core code is standard-library Python. Optional MLX environments are separate.
- [Ollama](https://ollama.com/download) for natural-language reasoning.
- [Tailscale](https://tailscale.com/download) on Mac and iPhone, signed into your own tailnet.
- Full Xcode with iOS 26+ SDK for the phone build; simulator compilation does not install or sign a phone app.

Download the repository source ZIP, extract it to a permanent directory, then open Terminal in that directory. The public repository is https://github.com/Chauncey665/PersonalOS; clone it or use GitHub’s Download ZIP.

```sh
python3 -m venv .venv
source .venv/bin/activate
python scripts/personalos.py init
python scripts/personalos.py doctor
```

Data/configs live under `~/Library/Application Support/PersonalOS-OpenSource` with private permissions. Existing configs are retained on repeated `init`. This public edition has its own service label and bundle ID. It does not migrate a separate personal installation automatically.

Explicitly install the model, with Ollama running:

```sh
ollama pull qwen3.5:9b
python scripts/personalos.py doctor
```

If you choose another tool-capable local model, initialize a new state directory with `init --model TAG`, or edit your private `local-model.json` and restart. Do not rename a shared model alias to make setup pass. A missing model remains a setup issue; there is no cloud fallback.

## 2. Test on the Mac

```sh
python scripts/personalos.py serve
```

Keep that terminal running. In another terminal in the same checkout, activate `.venv` and run:

```sh
python scripts/personalos.py pair
```

Open `http://127.0.0.1:8765` on the Mac, enter the code, submit `设备状态` and refresh history. You should see actual platform and disk data. A code is single-use and expires after five minutes. Generate a new code when pairing the phone.

If port 8765 is occupied, do not stop an unrelated service. Use `python scripts/personalos.py --port 18765 serve` and point Serve at that port. Global `--home` and `--port` options go **before** the subcommand. Custom state example:

```sh
python scripts/personalos.py --home "$HOME/Library/Application Support/PersonalOS-OpenSource Test" init
```

Use the same `--home` with every subsequent command for that installation.

## 3. Connect over your private tailnet

Log in and approve Tailscale's system extension on both devices yourself. Enable MagicDNS / HTTPS for your tailnet if prompted. Use the installed CLI; on macOS the app executable can be used when `tailscale` is not on PATH:

```sh
/Applications/Tailscale.app/Contents/MacOS/Tailscale serve --bg --https=8443 http://127.0.0.1:8765
/Applications/Tailscale.app/Contents/MacOS/Tailscale serve status
```

Use the **actual HTTPS URL shown by your own command**. It must end in `.ts.net`, with the port if one is shown. No shared server is provided. Serve should remain private to the tailnet: do not enable Funnel or router port forwarding. [Tailscale Serve documentation](https://tailscale.com/docs/reference/tailscale-cli/serve) covers account authorization and command differences.

On the iPhone, connect Tailscale and open that URL in Safari to verify reachability before diagnosing the App. Pairing is still required even with network access.

## 4. Build and sign your iPhone App

1. Open `ios/PersonalOS.xcodeproj` in Xcode. Choose the `PersonalOS` scheme; the display name is PersonalOS.
2. Under **Signing & Capabilities**, select your Apple account/team and Automatic Signing.
3. Set a unique bundle ID, e.g. `com.yourname.PersonalOS`. The repository's `org.personalosassistant.app` is a source default, not an identity you own.
4. Keep `PersonalOS/PersonalOS.entitlements` for the base build. It is empty so optional HealthKit/APNs signing does not block the core installation.
5. Attach your iPhone, unlock it, trust the Mac, enable Developer Mode when iOS requests it, and choose it as the Run destination. Follow [Apple's device-running guide](https://developer.apple.com/documentation/xcode/running-your-app-on-simulated-or-physical-devices).
6. Run from Xcode. Account terms, verification codes and system permissions are completed by you in Apple's UI.
7. In PersonalOS settings, enter your own Tailscale HTTPS URL. The public App deliberately starts with an empty server field. Generate a fresh `python scripts/personalos.py pair` code on the Mac and enter it on the phone.
8. Submit `设备状态` and confirm the returned result. Then test a natural-language question with the local model running.

Your signing/profile conditions determine how long a development installation remains usable. An IPA built for another person's devices is not a universal installer. This repository does not include one.

## 5. Verify Calendar and scheduling

Enable Calendar access in PersonalOS and choose the permissions you want in iOS. Allow full access if you want reading availability, completion updates and undo; write-only access cannot provide a complete schedule snapshot. An iCloud Calendar account is needed for iCloud sync across devices.

Submit a synthetic event such as `明天15:00到16:00，安排日历验收`. Open Apple Calendar, enable the PersonalOS calendars, and check the date/title/time. Return to PersonalOS to sync. A Mac-created pending event is not a successful phone write until its current revision is acknowledged.

Enable **自动安排待办** in the scheduling settings if desired. The initial routine is 11:00–next-day 01:00, two 45-minute meal reservations, 15-minute course buffers, 45-minute default tasks and a four-hour daily work budget. The planning settings API can change these values; a complete routine editor is not yet in the App.

Say/type `开启每天自动重排` to opt into daily unfinished-task rollover, or `关闭每天自动重排` to stop it. It checks after the workday ends, at 01:05 in the configured calendar timezone; a changed-plan digest is not pushed before 10:30. It waits for fresh context and actual phone sync. An offline phone may need you to reopen PersonalOS.

## 6. Keep the Mac service running

Once foreground testing works, stop it with Control-C, then optionally install the per-user login service:

```sh
python scripts/personalos.py service-install --dry-run
python scripts/personalos.py service-install
```

The label is `org.personalosassistant.agent`. It starts after this user logs in, not before an encrypted Mac is unlocked. Keep the selected Python interpreter/venv available. Errors are in the private data directory's `service-error.log`; read locally and redact before sharing.

```sh
python scripts/personalos.py service-uninstall
```

Uninstall removes the login service, preserving data. `revoke` invalidates all paired devices for the selected state directory. Do not delete a data directory to troubleshoot an unrelated signing issue.

## Optional features

- [Voice and Action Button](VOICE.md): explicit model setup and locally signed shortcuts.
- [Canvas, file access, HealthKit, APNs, TTS and a second Mac](INTEGRATIONS.md).
- [Troubleshooting and evidence](VERIFICATION.md).


Planning settings payloads and authentication: [API notes / 排程接口说明](API.md).
