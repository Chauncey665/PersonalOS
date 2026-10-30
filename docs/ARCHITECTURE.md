# Architecture

The iPhone owns system capabilities: microphone recording, MapKit/navigation handoff, EventKit writes, HealthKit reads, location and sharing. The Mac owns the persistent queue, planner, local-model requests, read-only source connectors and guarded file executor. Tailscale transports authenticated requests over private HTTPS.

SQLite is the source of truth for task state, revisions, sessions, workflows and execution receipts. A worker finishing does not mark a todo complete. File approvals bind to the exact device/plan/version and expire. A phone acknowledges each calendar entry's current revision. Retry keys prevent duplicate submissions.

`agent_runtime.py` reduces tool schema exposure and retains bounded dialogue excerpts. It is not an authorization engine: `assistant_brain.py` and the individual executors still validate each action against the current request, selected inputs and local permissions. Loading a tool never authorizes it.

Public release-specific boundaries:

- Empty initial iPhone server address; no pre-paired state or shared server.
- Independent `org.personalosassistant.agent` login service and `~/Library/Application Support/PersonalOS-OpenSource` state.
- Generic configurable Canvas origin and restricted remote targets.
- Per-device task, task-audit and notification scope, including formerly shared legacy task types.
- Base signing excludes optional APNs/HealthKit entitlements.
- No screenshot, historical verification report, host key, signed shortcut, private runtime or model weight is copied from the original personal deployment.

This release remains a single-person/private-network system. Shared authorized file roots are a Mac-owner capability, not a multi-tenant per-user filesystem sandbox.
