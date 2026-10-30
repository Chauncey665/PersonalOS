# API notes

The native App is the primary client. Base listener: `http://127.0.0.1:8765`; phone clients use your private Tailscale HTTPS origin. Except `POST /pair`, requests require `Authorization: Bearer DEVICE_TOKEN`. Do not paste a real token into documentation, command history or issues.

The current HTTP protocol uses JSON objects whose top-level values are strings. Larger typed payloads use a JSON-encoded string in the `payload` field. Example for changing the routine:

```json
{"payload":"{\"enabled\":true,\"start_minute\":540,\"end_minute\":1380,\"meal_minutes\":45,\"duration_minutes\":45,\"buffer_minutes\":15,\"daily_minutes\":240}"}
```

Send that object to `POST /planning/settings`. Minutes are measured from local midnight. `end_minute=1500` means next-day01:00; `540..1380` means09:00–23:00. Bounds are validated by `AutoPlanner.configure`. A fresh, complete phone snapshot is still required before selecting slots; changing settings is not proof a calendar write occurred.

| Endpoint | Purpose |
| --- | --- |
| `POST /pair` | `{ "code":"SHORT_LIVED_CODE", "name":"My iPhone" }`; returns a token once; server stores its hash |
| `GET /status` | Readiness; does not expose private key material |
| `GET /tasks` / `GET /tasks/TASK_UUID` | Device-owned task history/details |
| `POST /tasks` | At least `text`; optional stable `request_id`, session and timezone |
| `GET /audit` / `GET /tasks/TASK_UUID/audit` | Device-owned task audit; global administrative events remain local |
| `GET /roots` | Mac-owner-authorized shared file roots; not a per-tenant filesystem |
| `POST /assistant/sessions` | Create a device-owned conversation |
| `GET /assistant/sessions/SESSION_UUID` | Current step, brief speech, pending question/confirmation/phone action |
| `GET /planning/status` | Settings, freshness and actual slot/phone-write states |
| `POST /planning/settings` | Typed settings inside a `payload` string |
| `GET /planning/rollover` | Opt-in daily rollover policy |
| `POST /planning/rollover` | `{ "enabled":"true" }` or `"false"`; does not change all users' policies |
| `GET /calendar/entries` | Versioned events awaiting phone synchronization |
| `POST /calendar/entries/ENTRY_UUID/ack` | Current revision + actual `written`, `removed` or `error` result, sent by the real EventKit executor |

Do not fabricate acknowledgments to make a real task appear synced. The smoke test uses synthetic events/receipts in disposable state and explicitly labels that distinction.

Model selection, Canvas origins, private keys, remote forced-command targets and broad file permissions are local-admin configuration, not model-writable API settings. File approval/cancel/undo and document endpoints retain version/owner/input checks; inspect their source/tests before integrating a new client.
