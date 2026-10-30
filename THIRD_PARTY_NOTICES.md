# Third-party notes

PersonalOS source is MIT licensed. This source distribution does not include Python, Apple SDKs, Ollama, MLX, downloaded model weights or their environments.

Optional components retain their own licenses and requirements. Consult the exact versions you install:

- [Ollama](https://github.com/ollama/ollama) and [Qwen model library](https://ollama.com/library/qwen3.5).
- [MLX](https://github.com/ml-explore/mlx), [MLX Audio](https://github.com/Blaizzy/mlx-audio), [mlx-whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper).
- [Qwen3 ASR model card](https://huggingface.co/mlx-community/Qwen3-ASR-1.7B-8bit), [Whisper model card](https://huggingface.co/mlx-community/whisper-large-v3-turbo-8bit), [Qwen3 TTS model card](https://huggingface.co/mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit).
- [OpenCC](https://github.com/yichen0831/opencc-python), [SoundFile](https://github.com/bastibe/python-soundfile). FFmpeg is a separate system dependency; its license depends on the build.
- Apple frameworks and signing services are provided by Apple, not redistributed by this repository. Tailscale is installed and authorized separately.

## Design references

The PersonalOS Python runtime is independently implemented. No implementation files from these projects are incorporated in this distribution:

- [Familiar ToolLoader](https://github.com/IsaacHuo/Familiar/blob/main/Familiar/Agent/FamiliarToolLoader.swift): scoped, lazy exposure of tool schemas. Reviewed blob `64ef6d328d05d6dfffe04d38ab45cc8b82b6f8d0`.
- [LocalAssist ConversationMemory](https://github.com/Saithej2k/LocalAssist/blob/main/Sources/LocalAssistCore/ConversationMemory.swift): bounded rolling conversation context. Reviewed blob `3145510c0ea5edeb7e804462bd2cb9cdf001d05d`.
- [OpenClaw loop detection](https://github.com/openclaw/openclaw/blob/main/docs/tools/loop-detection.md): repetitive/no-progress tool calls. Reviewed blob `4c45c2b34ebed6ad57436f68eb75714af627d702`.

The App icon was generated for PersonalOS; no personal photo is bundled. Exercise tutorial links point to their publishers; their videos and photos are not included or relicensed here. Test identities, keys, health values, course examples and calendar events are synthetic fixtures, not a user-data export. The original personal deployment's reports, screenshots and runtime files are excluded.
