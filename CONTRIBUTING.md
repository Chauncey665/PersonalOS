# Contributing

Start with a small, reproducible issue or pull request. Use synthetic files and events. Describe the trigger, expected result, actual result, OS/Xcode/Python versions, and whether your evidence is source inspection, a mock, a local model, a Simulator or a physical phone.

Run:

```sh
python scripts/check_release.py
python -m unittest discover -s tests
python -m unittest discover -s air
python scripts/smoke_install.py
```

For changes to Swift code, also run the unsigned Simulator build from the README. Tests should exercise behavior and failure/recovery boundaries. Do not add always-on services, broaden permissions, download models, upload user data, change a shared Ollama alias or send external messages as a test side effect.

Never commit databases, model weights, signing identities, APNs keys, `.env` files, calendar feeds, health exports, signed personal shortcuts or screenshots of real user content. Examples belong in `config/examples/` with disabled settings and obvious placeholders. Use the public service label/data directory, not a private deployment's identifiers.

This is an early project. Preserve existing pairing and idempotency behavior; distinguish completing a worker step, receiving an execution receipt, syncing Calendar and a user actually completing a todo. Keep Air read-only and file confirmations bound to the exact action/version.
