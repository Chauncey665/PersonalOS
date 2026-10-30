# Security and privacy

PersonalOS is designed for a single person's trusted devices on a private network. It is not a hardened multi-tenant service. Keep its listener on `127.0.0.1`; use authenticated Tailscale Serve rather than public port forwarding or Funnel. Do not share a paired token with another person.

## Boundaries

- A five-minute, single-use pairing code is generated on the Mac. Device tokens are hashed on the server and stored in iOS Keychain. The client accepts a Tailscale HTTPS origin and does not follow redirects with authentication.
- Public-edition task history, task details, audits and notifications are scoped to the owning paired device. Ownerless historical tasks are not inherited by a new phone.
- Files require local authorization. Broad file browsing is a separate local opt-in. Credentials and protected paths are excluded. Writeback requires a current preview/confirmation and supports guarded undo. Remote Mac access uses forced-command keys and strict host-key verification.
- Optional Canvas origins are set by the Mac owner in a private config file. A phone cannot change that origin. Redirects are rejected. Only the selected institution's allowed API/feed paths are read.
- Calendar context and health data are sent to the user's private Mac only when shared. Health permissions and microphone/location prompts remain under iOS control.
- Acknowledging a calendar write in tests is not a physical-device receipt. The App reports actual EventKit success/failure for its current revision.
- Preferences are persisted only after an explicit request to remember. File contents, web pages, history excerpts and model outputs do not grant permissions.

Runtime logs and the database can contain personal task text and paths. Keep the data directory private (setup uses directory mode 0700 and config mode 0600). Do not attach it to GitHub issues. Local model operation does not mean every feature is offline: location weather, maps, web search, Canvas, model installation and APNs contact their configured external services.

## Reporting

Do not file a public issue containing an exploit, private calendar feed, health data or credentials. Before publication, no maintainer security contact has been assigned; contact the repository owner privately. After publication, prefer GitHub Private Vulnerability Reporting if the owner enables it. This project does not claim an independent penetration test.
