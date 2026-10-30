# Publishing this source

This folder is a public-source candidate. Review `LICENSE`, the feature boundaries and `release-checks.json` before publishing. No remote repository or signing identity is configured for you.

1. Run the checks in the README from a fresh checkout/extraction.
2. Review the source/privacy findings, local documentation links, and your chosen license. `check_release.py` is a useful gate, not an independent security audit.
3. Commit with the public identity/email you choose. Do not include a `.git` directory copied from an older private project or import its commit history.
4. Create your own GitHub repository, set its remote, then push. Use GitHub's UI or authenticated tooling; do not paste a token into a command, issue or chat.
5. Enable Actions and review its actual results. There is no claim of GitHub CI success before execution.
6. Enable private vulnerability reporting if available and replace the pre-publication contact guidance in SECURITY.md with a contact you control.
7. Add only synthetic screenshots/a short demo and an explicit compatibility matrix. Do not post personal calendars, health records, paired-server URLs or school feeds.
8. Release source first. Binaries, TestFlight/App Store distribution and shared shortcut downloads require their own signing, privacy and hardware acceptance work.

Suggested repository description:

> Local-first iPhone + Mac assistant: voice-to-tasks, Apple Calendar planning, daily rollover and guarded file actions.

Suggested topics: `ios`, `swiftui`, `macos`, `local-first`, `ollama`, `personal-assistant`, `calendar`, `self-hosted`.
