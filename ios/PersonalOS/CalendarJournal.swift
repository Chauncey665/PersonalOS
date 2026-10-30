import Foundation
import Combine
import EventKit
import CryptoKit
import UIKit

struct CalendarJournalEntry: Decodable, Identifiable {
    let id: String
    let kind: String
    let title: String
    let start: String?
    let end: String?
    let time_zone: String
    let all_day: Bool
    let status: String
    let revision: Int
    let question: String?
    let device_sync: String
    let sync_error: String?
    let original_text: String
    var reminder_at: String? = nil
    var todo_id: String? = nil
    var auto_manual: Bool? = nil
    var startDate: Date? { JournalMetadata.date(start) }
    var endDate: Date? { JournalMetadata.date(end) }
}

// Pure helpers are kept independent of EventKit so marker/date/fingerprint checks
// can run with synthetic fixtures without calendar authorization or calendar writes.
enum JournalMetadata {
    static let separator = "\n\nELLIS_JOURNAL_META:"
    struct Stamp: Codable {
        let version: Int
        let origin: String
        let task: String
        let revision: Int
        let fingerprint: String
    }
    static func hash(_ text: String) -> String {
        SHA256.hash(data: Data(text.utf8)).map { String(format: "%02x", $0) }.joined()
    }
    static func date(_ text: String?) -> Date? {
        guard let text else { return nil }
        let formatter = ISO8601DateFormatter()
        if let value = formatter.date(from: text) { return value }
        formatter.formatOptions.insert(.withFractionalSeconds)
        return formatter.date(from: text)
    }
    static func marker(origin: String, task: String) -> URL? {
        guard UUID(uuidString: task) != nil else { return nil }
        return URL(string: "personalos-journal://event/" + hash(origin) + "/" + task.lowercased())
    }
    static func plainNotes(_ notes: String?) -> String {
        guard let notes else { return "" }
        return notes.components(separatedBy: separator).first ?? notes
    }
    static func stamp(_ notes: String?) -> Stamp? {
        guard let pieces = notes?.components(separatedBy: separator), pieces.count == 2,
              let data = Data(base64Encoded: pieces[1]), data.count <= 2048,
              let value = try? JSONDecoder().decode(Stamp.self, from: data), value.version == 1 else { return nil }
        return value
    }
    static func notes(_ text: String, stamp: Stamp) throws -> String {
        // A user's original words cannot masquerade as our structured footer.
        let plain = text.replacingOccurrences(of: separator, with: "\n\nELLIS_JOURNAL_META（原话）:")
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        return plain + separator + (try encoder.encode(stamp)).base64EncodedString()
    }
    static func fingerprint(_ fields: [String: String]) throws -> String {
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        return SHA256.hash(data: try encoder.encode(fields)).map { String(format: "%02x", $0) }.joined()
    }
    static func completionTitle(kind: String, title: String, override: Bool?, overrideKind: String?) -> String {
        let completed = overrideKind == kind ? (override ?? (kind == "done")) : kind == "done"
        return (completed ? "已完成 · " : "计划 · ") + title
    }
    static func resolutionFields(revision: Int, title: String, start: Date, end: Date,
                                 timeZone: String, allDay: Bool, kind: String) -> [String: String] {
        let formatter = ISO8601DateFormatter(); formatter.timeZone = TimeZone(identifier: timeZone)
        return ["revision": String(revision), "title": title, "start": formatter.string(from: start),
                "end": formatter.string(from: end), "time_zone": timeZone,
                "all_day": allDay ? "true" : "false", "kind": kind]
    }
}

@MainActor final class CalendarJournal: ObservableObject {
    static let shared = CalendarJournal()
    @Published private(set) var enabled: Bool
    @Published private(set) var summary = "尚未开启写入 Apple 日历"
    @Published private(set) var calendarSourceDescription = "尚未选择专属日历账户"
    @Published private(set) var entries: [CalendarJournalEntry] = []
    @Published private(set) var syncing = false
    @Published private(set) var error: String?

    private struct Page: Decodable { let entries: [CalendarJournalEntry]; let next_cursor: String? }
    private struct Receipt: Codable {
        var eventID: String?
        var calendarID: String
        var revision: Int
        var fingerprint: String?
        var state: String // writing / written / removing / removed
        var start: Date
        var end: Date
        // Optional fields preserve decoding of existing v1 receipts. Version 2
        // keeps ownership evidence here; user-visible notes contain no footer.
        var formatVersion: Int? = nil
        var previousFingerprint: String? = nil
        var previousRevision: Int? = nil
        var previousFormatVersion: Int? = nil
        var completionOverride: Bool? = nil
        var completionKind: String? = nil
        var previousCompletionOverride: Bool? = nil
        var previousCompletionKind: String? = nil
        var previousCalendarID: String? = nil
    }
    private struct DiskState: Codable {
        var calendarID: String?
        var sourceDescription: String?
        var creatingCalendar = false
        var receipts: [String: Receipt] = [:]
        var categoryCalendars: [String: String]? = nil
        var creatingCategory: String? = nil
        var baseColorApplied: Bool? = nil
    }
    private struct JournalError: LocalizedError {
        let text: String
        var errorDescription: String? { text }
    }
    private let store = EKEventStore()
    private let defaults = UserDefaults.standard
    private let enabledKey = "PersonalOS.CalendarJournal.enabled.v1"
    private var disk = DiskState()
    private var diskError: String?
    private var entriesOrigin: String?
    private var outcomes: [String: String] = [:]
    private var enabling = false
    private var enableAttempt: UUID?

    private init() {
        enabled = UserDefaults.standard.bool(forKey: "PersonalOS.CalendarJournal.enabled.v1")
        do { try loadDisk() }
        catch { diskError = error.localizedDescription }
        if let source = disk.sourceDescription { calendarSourceDescription = source }
        if enabled { summary = "自动记录已开启；打开后检查专属日历与待同步事项" }
    }

    // Only the explicit settings action calls this method. No sync/intent path
    // requests permissions or creates a calendar behind an authorization prompt.
    func enable() async {
        guard !enabling else { return }
        let attempt = UUID(); enableAttempt = attempt
        enabling = true; error = nil
        defer { enabling = false; if enableAttempt == attempt { enableAttempt = nil } }
        do {
            if EKEventStore.authorizationStatus(for: .event) != .fullAccess {
                guard try await store.requestFullAccessToEvents() else {
                    throw JournalError(text: "未获日历完整访问权限。请在系统设置允许后再开启；尚未写入事项。")
                }
            }
            guard enableAttempt == attempt else { return }
            try loadDisk()
            _ = try ownedCalendar(create: true)
            enabled = true; defaults.set(true, forKey: enabledKey)
            summary = "已开启 · " + calendarSourceDescription + "；仅写入本功能创建的 PersonalOS 日历"
            await refreshAndSync()
        } catch { self.error = error.localizedDescription; summary = "开启未完成；" + error.localizedDescription }
    }

    func disable() {
        enableAttempt = nil
        enabled = false; defaults.set(false, forKey: enabledKey)
        summary = "自动写入已关闭；已有日历事项保留，可单独确认撤销"
    }

    // A read-only ownership query for the Today view. It never requests access
    // or adopts/creates a same-name calendar.
    func progressCalendarIdentifier() throws -> String {
        if diskError != nil { try loadDisk() }
        return try ownedCalendar(create: false).calendarIdentifier
    }

    // Keep the original account and ledger. A calendar with the same title is
    // never adopted. Category creation is authorized by the color feature;
    // there are no new permission prompts or account changes here.
    func managedCalendarIdentifiers() throws -> Set<String> {
        let base = try ownedCalendar(create: false)
        var result: Set<String> = [base.calendarIdentifier]
        for identifier in (disk.categoryCalendars ?? [:]).values {
            guard let calendar = store.calendar(withIdentifier: identifier), calendar.allowsContentModifications,
                  calendar.source.sourceIdentifier == base.source.sourceIdentifier else {
                throw JournalError(text: "PersonalOS 分类日历已删除、未同步或账户改变，请先在 Apple 日历检查")
            }
            result.insert(identifier)
        }
        return result
    }

    func categoryCalendarIdentifier(completed: Bool, deadline: Bool = false, allowExplicitCompletion: Bool = false) throws -> String {
        guard enabled || allowExplicitCompletion else { throw JournalError(text: "请先开启 PersonalOS 自动记入日历") }
        let base = try ownedCalendar(create: false)
        if disk.baseColorApplied != true {
            base.title = "PersonalOS · 待办"
            base.cgColor = UIColor(red: 0.10, green: 0.54, blue: 0.61, alpha: 1).cgColor
            try store.saveCalendar(base, commit: true)
            disk.baseColorApplied = true; try persist()
        }
        guard completed || deadline else { return base.calendarIdentifier }
        let key = completed ? "completed" : "deadline"
        if let identifier = disk.categoryCalendars?[key] {
            guard let calendar = store.calendar(withIdentifier: identifier), calendar.allowsContentModifications,
                  calendar.source.sourceIdentifier == base.source.sourceIdentifier else {
                throw JournalError(text: "原 PersonalOS 分类日历不可用，不会接管同名日历或换账户")
            }
            return identifier
        }
        guard disk.creatingCategory == nil else { throw JournalError(text: "上次创建分类日历未能确认，请先检查 Apple 日历；不会重复创建") }
        disk.creatingCategory = key; try persist()
        let calendar = EKCalendar(for: .event, eventStore: store)
        calendar.title = completed ? "PersonalOS · 已完成" : "PersonalOS · 截止"
        calendar.source = base.source
        calendar.cgColor = completed ? UIColor(red: 0.25, green: 0.61, blue: 0.40, alpha: 1).cgColor
                                     : UIColor(red: 0.91, green: 0.53, blue: 0.17, alpha: 1).cgColor
        try store.saveCalendar(calendar, commit: true)
        var categories = disk.categoryCalendars ?? [:]; categories[key] = calendar.calendarIdentifier
        disk.categoryCalendars = categories; disk.creatingCategory = nil; try persist()
        return calendar.calendarIdentifier
    }

    func completionState(taskID: String) -> Bool? {
        guard let origin = try? API.endpoint(), entriesOrigin == origin.key,
              let entry = entries.first(where: { $0.id == taskID && $0.status == "ready" }),
              let calendar = try? ownedCalendar(create: false),
              let event = try? findEvent(entry, origin: origin, calendar: calendar),
              let verified = try? assertUnchanged(event, entry: entry, origin: origin, receipt: disk.receipts[receiptKey(origin, taskID)]),
              verified.revision == entry.revision else { return nil }
        if event.title == "已完成 · " + entry.title { return true }
        if event.title == "计划 · " + entry.title { return false }
        return nil
    }

    // An explicit completion action changes only this verified event's title.
    // Its original plan kind and time range remain on the Mac unchanged.
    func setCompletion(taskID: String, completed: Bool) async throws {
        if let origin=try? API.endpoint(), entriesOrigin==origin.key,
           let entry=entries.first(where:{$0.id==taskID && $0.status=="ready"}), let todoID=entry.todo_id {
            guard !syncing else { throw JournalError(text:"日历正在同步，请稍后再勾选") }
            try await TodoModel.shared.setCompleted(id:todoID,completed:completed)
            await refreshAndSync(endpoint:origin)
            guard completionState(taskID:taskID)==completed else { throw JournalError(text:"待办已保存，执行时段的完成颜色仍待手机同步，请刷新") }
            return
        }
        guard UUID(uuidString: taskID) != nil, !syncing else {
            throw JournalError(text: "日历正在同步，请稍后再勾选")
        }
        let origin = try API.endpoint()
        let pairing = PendingSubmission.fingerprint(for: origin)
        guard API.isPaired() else { throw JournalError(text: "请先解锁手机并连接原 Mac") }
        let key = receiptKey(origin, taskID)
        let current = entries.first(where: { $0.id == taskID })
        guard entriesOrigin == origin.key, current?.status == "ready",
              disk.receipts[key]?.revision == current?.revision, disk.receipts[key]?.state == "written" else {
            throw JournalError(text: "日历正在更新，请刷新后再勾选")
        }
        try requireCurrentOrigin(origin)
        guard PendingSubmission.fingerprint(for: origin) == pairing, API.isPaired() else {
            throw JournalError(text: "配对身份已改变，请刷新后再勾选")
        }
        guard !syncing, entriesOrigin == origin.key,
              let entry = entries.first(where: { $0.id == taskID && $0.status == "ready" }) else {
            throw JournalError(text: "这条日历记录尚未同步或已撤销，请刷新后查看")
        }
        syncing = true; defer { syncing = false }
        if diskError != nil { try loadDisk() }
        let calendar = try ownedCalendar(create: false)
        guard let event = try findEvent(entry, origin: origin, calendar: calendar) else {
            throw JournalError(text: "原日历事项已删除或暂未同步，不会重新创建")
        }
        let original = disk.receipts[key]
        let verified = try assertUnchanged(event, entry: entry, origin: origin, receipt: original)
        guard verified.revision == entry.revision, !event.hasAttendees, event.attendees?.isEmpty != false else {
            throw JournalError(text: "日历版本尚未同步或包含邀请，不会修改这条事项")
        }
        let previous = completionMetadata(original, previous: verified.previous, kind: entry.kind)
        let previousFingerprint = try fingerprint(event, formatVersion: verified.formatVersion)
        let oldReceipt = makeReceipt(event, revision: entry.revision, state: "written", formatVersion: verified.formatVersion,
            completionOverride: previous.override, completionKind: previous.kind)
        let desiredTitle = JournalMetadata.completionTitle(kind: entry.kind, title: entry.title,
            override: completed, overrideKind: entry.kind)
        let targetID = try categoryCalendarIdentifier(completed: completed, allowExplicitCompletion: true)
        guard let target = store.calendar(withIdentifier: targetID) else { throw JournalError(text: "完成分类日历尚未同步") }
        if event.title == desiredTitle && event.calendar.calendarIdentifier == targetID {
            disk.receipts[key] = makeReceipt(event, revision: entry.revision, state: "written", formatVersion: verified.formatVersion,
                completionOverride: completed, completionKind: entry.kind)
            try persist(); return
        }
        let previousCalendarID = event.calendar.calendarIdentifier
        event.title = desiredTitle; event.calendar = target
        let desiredFingerprint = try fingerprint(event, formatVersion: 2)
        disk.receipts[key] = Receipt(eventID: event.eventIdentifier, calendarID: targetID,
            revision: entry.revision, fingerprint: desiredFingerprint, state: "writing", start: event.startDate, end: event.endDate,
            formatVersion: 2, previousFingerprint: previousFingerprint, previousRevision: verified.revision,
            previousFormatVersion: verified.formatVersion, completionOverride: completed, completionKind: entry.kind,
            previousCompletionOverride: previous.override, previousCompletionKind: previous.kind,
            previousCalendarID: previousCalendarID)
        do { try persist() }
        catch { store.reset(); disk.receipts[key] = original; throw error }
        do {
            try requireCurrentOrigin(origin)
            guard PendingSubmission.fingerprint(for: origin) == pairing,
                  EKEventStore.authorizationStatus(for: .event) == .fullAccess else {
                throw JournalError(text: "配对或日历权限已改变，尚未修改完成状态")
            }
            try store.save(event, span: .thisEvent, commit: true)
        } catch {
            store.reset()
            // If EventKit definitely retained the old state, restore that exact
            // receipt too. An ambiguous success keeps both fingerprints for recovery.
            if let actual = try? findEvent(entry, origin: origin, calendar: calendar),
               (try? fingerprint(actual, formatVersion: verified.formatVersion)) == previousFingerprint {
                disk.receipts[key] = oldReceipt
                try? persist()
            }
            throw JournalError(text: "未确认日历完成状态，请刷新后重试：" + error.localizedDescription)
        }
        store.reset()
        guard let actual = try findEvent(entry, origin: origin, calendar: calendar),
              actual.title == desiredTitle, try fingerprint(actual, formatVersion: 2) == desiredFingerprint else {
            throw JournalError(text: "日历保存后的状态未能核实，请刷新后查看；不会重复创建")
        }
        disk.receipts[key] = makeReceipt(actual, revision: entry.revision, state: "written", formatVersion: 2,
            completionOverride: completed, completionKind: entry.kind)
        try persist()
    }

    func refreshAndSync(endpoint: ServerEndpoint? = nil) async {
        do { await synchronize(origin: try endpoint ?? API.endpoint(), deadline: Date().addingTimeInterval(25)) }
        catch { self.error = error.localizedDescription }
    }

    func resolve(entry: CalendarJournalEntry, title: String, start: Date, end: Date,
                 timeZone: String, allDay: Bool, kind: String) async {
        error = nil
        do {
            guard !syncing else { throw JournalError(text: "正在同步，请稍后再保存时间") }
            guard !title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, end > start,
                  TimeZone(identifier: timeZone) != nil, ["done", "plan"].contains(kind) else {
                throw JournalError(text: "请填写标题、有效时区以及晚于开始的结束时间")
            }
            let origin = try API.endpoint(); try requireEntryOrigin(origin)
            _ = try await API.request("/calendar/entries/" + entry.id + "/resolve",
                JournalMetadata.resolutionFields(revision: entry.revision, title: title, start: start, end: end,
                    timeZone: timeZone, allDay: allDay, kind: kind), endpoint: origin)
            await synchronize(origin: origin, deadline: Date().addingTimeInterval(25))
        } catch { self.error = error.localizedDescription }
    }

    // UI confirms this action first. With auto-write disabled, this explicit
    // cancellation may still remove the exact unchanged event created by us.
    func cancel(entry: CalendarJournalEntry) async {
        error = nil
        do {
            guard !syncing else { throw JournalError(text: "正在同步，请稍后再撤销") }
            let origin = try API.endpoint(); try requireEntryOrigin(origin)
            _ = try await API.request("/calendar/entries/" + entry.id + "/cancel",
                                      ["revision": String(entry.revision)], endpoint: origin)
            await synchronize(origin: origin, deadline: Date().addingTimeInterval(25), explicitCancellation: entry.id)
        } catch { self.error = error.localizedDescription }
    }

    // Bounded network waits; no long-lived background work or permission dialogs.
    // A successful queue submission is never described as a completed calendar write.
    func waitForTaskAndSync(taskID: String, endpoint: ServerEndpoint? = nil) async -> String? {
        let deadline = Date().addingTimeInterval(18)
        guard UUID(uuidString: taskID) != nil else { return nil }
        do {
            let origin = try endpoint ?? API.endpoint()
            while Date() < deadline && !Task.isCancelled {
                try requireCurrentOrigin(origin)
                await synchronize(origin: origin, deadline: deadline)
                if let entry = entries.first(where: { $0.id == taskID }), entriesOrigin == origin.key {
                    if entry.status == "needs_details" {
                        return entry.question?.isEmpty == false ? entry.question : "已收到，请打开 PersonalOS 补充开始和结束时间，尚未写入日历。"
                    }
                    if let result = outcomes[outcomeKey(origin, entry)] { return result }
                    if !enabled { return "事项已接收，但自动写入日历尚未开启。请在 PersonalOS 设置中开启后同步。" }
                    if entry.device_sync == "error" { return "事项已接收，日历同步需处理：" + (entry.sync_error ?? "请打开 PersonalOS 查看") }
                }
                let remaining = deadline.timeIntervalSinceNow
                guard remaining > 1 else { break }
                let data = try await API.request("/tasks/" + taskID, endpoint: origin, timeout: min(3, remaining))
                let task = try JSONDecoder().decode(TaskRecord.self, from: data)
                if task.state == "failed" || task.state == "cancelled" { return nil }
                if ["completed", "scheduled", "awaiting_confirmation", "needs_review"].contains(task.state),
                   !entries.contains(where: { $0.id == taskID }) { return nil }
                try await Task.sleep(nanoseconds: 500_000_000)
            }
        } catch { /* The submitted task remains on the Mac; foreground sync can retry. */ }
        return "任务已接收。若需记入日历，请稍后打开 PersonalOS 完成同步；目前尚未确认写入。"
    }

    private func synchronize(origin: ServerEndpoint, deadline: Date, explicitCancellation: String? = nil) async {
        guard !syncing else { return }
        syncing = true; error = nil
        defer { syncing = false }
        do {
            try requireCurrentOrigin(origin)
            if entriesOrigin != origin.key { entries = []; outcomes = [:]; entriesOrigin = origin.key }
            // Fetch pages before ack changes pending/written sorting. This avoids
            // skipping older pending entries when a page's cursor order changes.
            var fetched: [CalendarJournalEntry] = []; var cursor: String?; var seen = Set<String>()
            repeat {
                try checkBudget(deadline)
                var route = "/calendar/entries"
                if let cursor {
                    var components = URLComponents(); components.queryItems = [URLQueryItem(name: "cursor", value: cursor)]
                    route += "?" + (components.percentEncodedQuery ?? "")
                }
                let data = try await API.request(route, endpoint: origin, timeout: min(5, max(0.2, deadline.timeIntervalSinceNow)))
                try requireCurrentOrigin(origin)
                let page = try JSONDecoder().decode(Page.self, from: data)
                fetched.append(contentsOf: page.entries.filter { seen.insert($0.id).inserted })
                cursor = page.next_cursor
                if cursor?.isEmpty == true { cursor = nil }
            } while cursor != nil
            entries = fetched
            guard enabled || explicitCancellation != nil else {
                summary = "自动写入已关闭；待补充和待同步事项仍可在此查看"; return
            }
            guard EKEventStore.authorizationStatus(for: .event) == .fullAccess else {
                throw JournalError(text: "日历完整访问未授权，请在 PersonalOS 设置中点开启；后台不会自动弹出授权")
            }
            if diskError != nil { try loadDisk() }
            let calendar = try ownedCalendar(create: false)
            for entry in fetched {
                try checkBudget(deadline); try requireCurrentOrigin(origin)
                guard enabled || (entry.id == explicitCancellation && entry.status == "cancelled") else { continue }
                guard ["ready", "cancelled"].contains(entry.status) else { continue }
                let saved = disk.receipts[receiptKey(origin, entry.id)]
                let completion = completionMetadata(saved, previous: false, kind: entry.kind)
                let done = completion.override ?? (entry.kind == "done")
                let desiredCalendarID = entry.status == "ready" ? try categoryCalendarIdentifier(completed: done) : nil
                if entry.status == "ready", entry.device_sync == "written", saved?.state == "written", saved?.revision == entry.revision,
                   saved?.calendarID == desiredCalendarID {
                    // Historical confirmed writes are not continuously re-saved or
                    // policed. A later resolve/cancel still checks the live fingerprint.
                    outcomes[outcomeKey(origin, entry)] = "已写入 Apple 日历的 PersonalOS 日历。"
                    continue
                }
                var applied: String?
                do {
                    let disposition = try apply(entry, origin: origin, calendar: calendar,
                        allowExplicitRemoval: entry.id == explicitCancellation && entry.status == "cancelled")
                    applied = disposition
                    outcomes[outcomeKey(origin, entry)] = disposition == "written" ? "已写入 Apple 日历的 PersonalOS 日历。" : "本次 PersonalOS 日历记录已撤销。"
                    if entry.device_sync != disposition {
                        try await acknowledge(entry, origin: origin, status: disposition, deadline: deadline)
                    }
                } catch {
                    let message = error.localizedDescription
                    self.error = message
                    let key = outcomeKey(origin, entry)
                    if applied == "written" {
                        outcomes[key] = "已写入 Apple 日历，私人 Mac 的确认尚未完成；下次打开 PersonalOS 会继续同步。"
                    } else if applied == "removed" {
                        outcomes[key] = "已从 Apple 日历撤销本次记录，私人 Mac 的确认尚未完成；下次打开 PersonalOS 会继续同步。"
                    } else { outcomes[key] = "事项已接收，但尚未完成日历同步：" + message }
                    if entry.device_sync != "error" || entry.sync_error != message {
                        try? await acknowledge(entry, origin: origin, status: "error", message: message, deadline: deadline)
                    }
                }
            }
            if enabled { summary = "已开启 · " + calendarSourceDescription + "；仅处理本功能的 PersonalOS 日历" }
        } catch {
            self.error = error.localizedDescription
            if enabled { summary = "日历同步待完成：" + error.localizedDescription }
        }
    }

    private func acknowledge(_ entry: CalendarJournalEntry, origin: ServerEndpoint, status: String,
                             message: String? = nil, deadline: Date) async throws {
        try checkBudget(deadline); try requireCurrentOrigin(origin)
        var body = ["revision": String(entry.revision), "status": status]
        if let message { body["message"] = String(message.prefix(400)) }
        _ = try await API.request("/calendar/entries/" + entry.id + "/ack", body, endpoint: origin,
                                  timeout: min(4, max(0.2, deadline.timeIntervalSinceNow)))
    }

    private func apply(_ entry: CalendarJournalEntry, origin: ServerEndpoint, calendar: EKCalendar,
                       allowExplicitRemoval: Bool = false) throws -> String {
        try requireMutationAllowed(origin, allowExplicitRemoval: allowExplicitRemoval)
        guard UUID(uuidString: entry.id) != nil, entry.revision > 0 else { throw JournalError(text: "日历事项标识或版本无效") }
        let key = receiptKey(origin, entry.id)
        let existing = try findEvent(entry, origin: origin, calendar: calendar)
        let receipt = disk.receipts[key]
        if entry.status == "cancelled" {
            if let existing {
                let verified = try assertUnchanged(existing, entry: entry, origin: origin, receipt: receipt)
                let completion = completionMetadata(receipt, previous: verified.previous, kind: entry.kind)
                disk.receipts[key] = makeReceipt(existing, revision: entry.revision, state: "removing", formatVersion: verified.formatVersion,
                    completionOverride: completion.override, completionKind: completion.kind)
                try persist()
                try requireMutationAllowed(origin, allowExplicitRemoval: allowExplicitRemoval)
                try store.remove(existing, span: .thisEvent, commit: true)
            } else if let receipt, !["removed", "removing"].contains(receipt.state) {
                throw JournalError(text: "原事项已被手动删除、移动或暂未同步，无法确认撤销；不会改动其他日历")
            }
            if var value = disk.receipts[key] { value.state = "removed"; value.revision = entry.revision; disk.receipts[key] = value; try persist() }
            return "removed"
        }
        guard let start = entry.startDate, let end = entry.endDate, end > start,
              let zone = TimeZone(identifier: entry.time_zone), ["done", "plan"].contains(entry.kind),
              !entry.title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw JournalError(text: "日历事项时间不完整，请先在 PersonalOS 补充，不会猜测时间")
        }
        var previousFingerprint: String?
        var previousRevision: Int?
        var previousFormatVersion: Int?
        var previousCompletionOverride: Bool?
        var previousCompletionKind: String?
        var completionOverride: Bool?
        var completionKind: String?
        if let existing {
            let verified = try assertUnchanged(existing, entry: entry, origin: origin, receipt: receipt)
            let completion = completionMetadata(receipt, previous: verified.previous, kind: entry.kind)
            completionOverride = completion.override; completionKind = completion.kind
            if verified.revision == entry.revision {
                let targetID = try categoryCalendarIdentifier(completed: completionOverride ?? (entry.kind == "done"))
                if existing.calendar.calendarIdentifier != targetID {
                    try moveCategory(existing, entry: entry, origin: origin, targetID: targetID,
                        formatVersion: verified.formatVersion, completionOverride: completionOverride, completionKind: completionKind)
                    return "written"
                }
                disk.receipts[key] = makeReceipt(existing, revision: entry.revision, state: "written", formatVersion: verified.formatVersion,
                    completionOverride: completionOverride, completionKind: completionKind); try persist()
                return "written" // Also recovers save-success / ledger-or-ack-interruption.
            }
            guard verified.revision < entry.revision else { throw JournalError(text: "日历记录版本较新，请刷新后重试") }
            previousFingerprint = try fingerprint(existing, formatVersion: verified.formatVersion)
            previousRevision = verified.revision; previousFormatVersion = verified.formatVersion
            // Recovery must describe the actual old event, even when this new
            // revision changes kind and intentionally clears its override.
            previousCompletionOverride = verified.previous ? receipt?.previousCompletionOverride : receipt?.completionOverride
            previousCompletionKind = verified.previous ? receipt?.previousCompletionKind : receipt?.completionKind
        } else if let receipt, !(receipt.state == "removed" && entry.revision > receipt.revision) {
            throw JournalError(text: "原事项已被手动删除、移动，或上次写入中断；为避免重复不会自动重建，请在 Apple 日历检查")
        }
        let event = existing ?? EKEvent(eventStore: store)
        if entry.todo_id != nil, entry.kind=="plan", entry.auto_manual != true,
           existing == nil || existing?.startDate != start || existing?.endDate != end {
            try validateAutoSlot(entry, origin: origin)
        }
        event.title = JournalMetadata.completionTitle(kind: entry.kind, title: entry.title,
            override: completionOverride, overrideKind: completionKind)
        let previousCalendarID = existing?.calendar.calendarIdentifier
        let targetID = try categoryCalendarIdentifier(completed: event.title.hasPrefix("已完成 · "))
        guard let target = store.calendar(withIdentifier: targetID) else { throw JournalError(text: "分类日历尚未同步") }
        event.calendar = target
        event.startDate = start; event.endDate = end; event.isAllDay = entry.all_day; event.timeZone = zone
        event.url = JournalMetadata.marker(origin: origin.key, task: entry.id)
        event.alarms = nil; event.recurrenceRules = nil
        if entry.todo_id != nil { event.availability = event.title.hasPrefix("已完成 · ") ? .free : .busy }
        let entered = ISO8601DateFormatter().string(from: Date())
        let notes = "由 PersonalOS 从本人私人 Mac 创建\n类型：" + (entry.kind == "done" ? "已完成" : "未来安排") + "\n原话：" + entry.original_text + "\n录入时间：" + entered
        event.notes = notes
        // Persist both the exact intended representation and the previously
        // verified representation BEFORE saving. A recovered update may match
        // either side; anything else is preserved for manual inspection.
        disk.receipts[key] = Receipt(eventID: existing?.eventIdentifier, calendarID: targetID,
            revision: entry.revision, fingerprint: try fingerprint(event, formatVersion: 2), state: "writing", start: start, end: end,
            formatVersion: 2, previousFingerprint: previousFingerprint, previousRevision: previousRevision,
            previousFormatVersion: previousFormatVersion, completionOverride: completionOverride, completionKind: completionKind,
            previousCompletionOverride: previousCompletionOverride, previousCompletionKind: previousCompletionKind,
            previousCalendarID: previousCalendarID)
        do { try persist() }
        catch { store.reset(); throw error } // Discard unsaved cached EventKit edits.
        do {
            try requireMutationAllowed(origin, allowExplicitRemoval: false)
            try store.save(event, span: .thisEvent, commit: true)
        }
        catch {
            store.reset()
            throw JournalError(text: "Apple 日历保存未能确认。已保留防重复记录，请先在日历核对该事项；不会自动重试或重复创建。系统信息：" + error.localizedDescription)
        }
        // EventKit may normalize fields. Keep the actual post-save fingerprint
        // locally. If a crash prevents this receipt, only an exact pre-save
        // match is adopted; normalization uncertainty never causes a new event.
        disk.receipts[key] = makeReceipt(event, revision: entry.revision, state: "written", formatVersion: 2,
            completionOverride: completionOverride, completionKind: completionKind)
        try persist()
        return "written"
    }

    private func moveCategory(_ event: EKEvent, entry: CalendarJournalEntry, origin: ServerEndpoint, targetID: String,
                              formatVersion: Int, completionOverride: Bool?, completionKind: String?) throws {
        guard !event.hasAttendees, !event.hasRecurrenceRules,
              let target = store.calendar(withIdentifier: targetID) else {
            throw JournalError(text: "这条记录包含邀请或重复规则，未自动移动分类")
        }
        let key = receiptKey(origin, entry.id)
        let original = disk.receipts[key]
        let previous = try fingerprint(event, formatVersion: formatVersion)
        let previousCalendar = event.calendar.calendarIdentifier
        event.calendar = target
        let desired = try fingerprint(event, formatVersion: 2)
        disk.receipts[key] = Receipt(eventID: event.eventIdentifier, calendarID: targetID, revision: entry.revision,
            fingerprint: desired, state: "writing", start: event.startDate, end: event.endDate, formatVersion: 2,
            previousFingerprint: previous, previousRevision: entry.revision, previousFormatVersion: formatVersion,
            completionOverride: completionOverride, completionKind: completionKind,
            previousCompletionOverride: completionOverride, previousCompletionKind: completionKind,
            previousCalendarID: previousCalendar)
        do { try persist() } catch { store.reset(); disk.receipts[key] = original; throw error }
        do { try requireMutationAllowed(origin, allowExplicitRemoval: false); try store.save(event, span: .thisEvent, commit: true) }
        catch { store.reset(); throw JournalError(text: "日历分类移动尚未确认，保留原归属记录，请刷新后检查") }
        store.reset()
        guard let base = try? ownedCalendar(create: false), let actual = try findEvent(entry, origin: origin, calendar: base),
              actual.calendar.calendarIdentifier == targetID, try fingerprint(actual, formatVersion: 2) == desired else {
            throw JournalError(text: "分类保存后的状态未能核实，不会重复创建")
        }
        disk.receipts[key] = makeReceipt(actual, revision: entry.revision, state: "written", formatVersion: 2,
            completionOverride: completionOverride, completionKind: completionKind)
        try persist()
    }

    private func validateAutoSlot(_ entry: CalendarJournalEntry, origin: ServerEndpoint) throws {
        guard let start=entry.startDate, let end=entry.endDate, start>Date().addingTimeInterval(60) else {
            throw JournalError(text:"自动排程冲突：安排时间已过，请刷新排程")
        }
        let ids=Set(entries.filter{$0.todo_id != nil}.map(\.id))
        let completed=Set(entries.filter{$0.todo_id != nil && $0.kind=="done"}.map(\.id))
        let predicate=store.predicateForEvents(withStart:start.addingTimeInterval(-900),end:end.addingTimeInterval(900),calendars:nil)
        for other in store.events(matching:predicate) {
            if other.url==JournalMetadata.marker(origin:origin.key,task:entry.id) { continue }
            guard CalendarContextEncoding.representsPlanningOccupiedTime(other,completedAutoIDs:completed,origin:origin.key) else {continue}
            let marker=DailyAgendaMetadata.markerParts(other.url,scheme:"personalos-journal")
            let ownAuto=marker?.origin==JournalMetadata.hash(origin.key) && marker.map{ids.contains($0.taskID)}==true
            let buffer:TimeInterval=ownAuto ? 600 : 900
            if start<other.endDate.addingTimeInterval(buffer) && end>other.startDate.addingTimeInterval(-buffer) {
                throw JournalError(text:"自动排程冲突：手机已有日程，请刷新排程；没有覆盖原事项")
            }
        }
    }

    private func findEvent(_ entry: CalendarJournalEntry, origin: ServerEndpoint, calendar: EKCalendar) throws -> EKEvent? {
        let receipt = disk.receipts[receiptKey(origin, entry.id)]
        let identifiers = try managedCalendarIdentifiers()
        let calendars = identifiers.compactMap { store.calendar(withIdentifier: $0) }
        var matches: [String: EKEvent] = [:]
        if let id = receipt?.eventID, let event = store.event(withIdentifier: id) {
            guard identifiers.contains(event.calendar.calendarIdentifier),
                  event.url == JournalMetadata.marker(origin: origin.key, task: entry.id) else {
                throw JournalError(text: "原事项的日历或来源已被手动改变；不会覆盖或删除")
            }
            matches[event.eventIdentifier ?? event.calendarItemIdentifier] = event
        }
        var ranges: [DateInterval] = []
        if let start = entry.startDate, let end = entry.endDate, end >= start { ranges.append(DateInterval(start: start, end: end)) }
        if let receipt { ranges.append(DateInterval(start: receipt.start, end: max(receipt.start, receipt.end))) }
        for range in ranges {
            let predicate = store.predicateForEvents(withStart: range.start.addingTimeInterval(-86_400),
                end: range.end.addingTimeInterval(86_400), calendars: calendars)
            for event in store.events(matching: predicate) where event.url == JournalMetadata.marker(origin: origin.key, task: entry.id) {
                matches[event.eventIdentifier ?? event.calendarItemIdentifier] = event
            }
        }
        guard matches.count <= 1 else { throw JournalError(text: "发现多个相同来源的日历记录，请先在 Apple 日历核对；不会自动覆盖或删除") }
        return matches.values.first
    }

    @discardableResult
    private func assertUnchanged(_ event: EKEvent, entry: CalendarJournalEntry, origin: ServerEndpoint, receipt: Receipt?) throws -> (revision: Int, formatVersion: Int, previous: Bool) {
        guard event.url == JournalMetadata.marker(origin: origin.key, task: entry.id),
              try managedCalendarIdentifiers().contains(event.calendar.calendarIdentifier),
              receipt == nil || receipt?.calendarID == event.calendar.calendarIdentifier ||
                  (receipt?.state == "writing" && receipt?.previousCalendarID == event.calendar.calendarIdentifier) else {
            throw JournalError(text: "这条事项已在 Apple 日历中被手动修改；为保留你的修改，PersonalOS 不会覆盖或删除")
        }
        if let receipt, receipt.formatVersion == 2 {
            let actual = try fingerprint(event, formatVersion: 2)
            if ["written", "removing", "writing"].contains(receipt.state), receipt.fingerprint == actual {
                return (receipt.revision, 2, false)
            }
            if receipt.state == "writing", let previous = receipt.previousFingerprint,
               let revision = receipt.previousRevision, let format = receipt.previousFormatVersion,
               [1, 2].contains(format), previous == (try fingerprint(event, formatVersion: format)) {
                if format == 1 { guard try legacyRevision(event, entry: entry, origin: origin) == revision else {
                    throw JournalError(text: "旧日历记录版本已改变，请先检查；不会覆盖或删除")
                } }
                return (revision, format, true)
            }
            throw JournalError(text: "这条事项已在 Apple 日历中被手动修改，或写入状态无法确认；为保留你的修改，PersonalOS 不会覆盖、删除或重复创建")
        }
        // Existing v1 events retain their authenticated footer semantics. They
        // are upgraded only on a later explicit revision, never by a bulk scan.
        let revision = try legacyRevision(event, entry: entry, origin: origin)
        let actual = try fingerprint(event, formatVersion: 1)
        if let receipt, let previous = receipt.fingerprint, receipt.state == "written", previous != actual {
            throw JournalError(text: "日历记录与本机写入回执不一致，请先检查；不会覆盖手工修改")
        }
        return (revision, 1, false)
    }
    private func legacyRevision(_ event: EKEvent, entry: CalendarJournalEntry, origin: ServerEndpoint) throws -> Int {
        let actual = try fingerprint(event, formatVersion: 1)
        guard let stamp = JournalMetadata.stamp(event.notes), stamp.origin == JournalMetadata.hash(origin.key),
              stamp.task == entry.id, stamp.fingerprint == actual else {
            throw JournalError(text: "缺少有效日历归属回执，或事项已被手动修改；不会覆盖、删除或重复创建")
        }
        return stamp.revision
    }
    private func fingerprint(_ event: EKEvent, formatVersion: Int = 1) throws -> String {
        try JournalMetadata.fingerprint([
            "calendar": event.calendar.calendarIdentifier, "title": event.title ?? "",
            "start": String(event.startDate.timeIntervalSince1970.rounded()), "end": String(event.endDate.timeIntervalSince1970.rounded()),
            "allDay": String(event.isAllDay), "timeZone": event.timeZone?.identifier ?? "",
            "notes": formatVersion == 2 ? (event.notes ?? "") : JournalMetadata.plainNotes(event.notes), "url": event.url?.absoluteString ?? "",
            "location": event.location ?? "", "structuredLocation": event.structuredLocation?.title ?? "",
            "hasStructuredLocation": String(event.structuredLocation != nil),
            "alarms": String(event.alarms?.count ?? 0), "recurrence": String(event.hasRecurrenceRules),
            "attendees": String(event.attendees?.count ?? 0), "availability": String(event.availability.rawValue)
        ])
    }
    private func completionMetadata(_ receipt: Receipt?, previous: Bool, kind: String) -> (override: Bool?, kind: String?) {
        let value = previous ? receipt?.previousCompletionOverride : receipt?.completionOverride
        let recordedKind = previous ? receipt?.previousCompletionKind : receipt?.completionKind
        guard recordedKind == kind, value != nil else { return (nil, nil) }
        return (value, recordedKind)
    }
    private func makeReceipt(_ event: EKEvent, revision: Int, state: String, formatVersion: Int = 1,
                             completionOverride: Bool? = nil, completionKind: String? = nil) -> Receipt {
        Receipt(eventID: event.eventIdentifier, calendarID: event.calendar.calendarIdentifier, revision: revision,
                fingerprint: try? fingerprint(event, formatVersion: formatVersion), state: state, start: event.startDate, end: event.endDate,
                formatVersion: formatVersion, completionOverride: completionOverride, completionKind: completionKind)
    }

    private func ownedCalendar(create: Bool) throws -> EKCalendar {
        guard EKEventStore.authorizationStatus(for: .event) == .fullAccess else { throw JournalError(text: "请先在设置中授权日历完整访问") }
        if let identifier = disk.calendarID {
            guard let calendar = store.calendar(withIdentifier: identifier), calendar.allowsContentModifications else {
                throw JournalError(text: "原专属 PersonalOS 日历已删除、未同步或不可写；不会接管同名日历或换账户，请在系统日历检查")
            }
            calendarSourceDescription = sourceDescription(calendar.source)
            return calendar
        }
        guard create else { throw JournalError(text: "请先在 PersonalOS 设置中开启专属日历；后台不会创建日历或请求授权") }
        guard !disk.creatingCalendar else { throw JournalError(text: "上次创建专属日历未能确认。请先在 Apple 日历核对；为避免重复不会新建或接管同名日历") }
        let writable = store.calendars(for: .event).filter(\.allowsContentModifications)
        let sources = store.sources
        let isCloud: (EKSource) -> Bool = { source in
            source.sourceType == .calDAV && source.title.trimmingCharacters(in: .whitespacesAndNewlines).caseInsensitiveCompare("iCloud") == .orderedSame
                && writable.contains(where: { $0.source.sourceIdentifier == source.sourceIdentifier })
        }
        let cloud = sources.filter(isCloud)
        let source: EKSource
        if let normal = store.defaultCalendarForNewEvents?.source, isCloud(normal) { source = normal }
        else if cloud.count == 1 { source = cloud[0] }
        else if cloud.count > 1 { throw JournalError(text: "检测到多个可写 iCloud 来源，请先在系统日历设置默认 iCloud 日历") }
        else if let local = sources.first(where: { $0.sourceType == .local }) { source = local }
        else { throw JournalError(text: "没有可用的 iCloud 或本机日历。请先在系统开启 iCloud 日历；不会写入 Google、Exchange 或公司账户") }
        disk.creatingCalendar = true; try persist()
        let calendar = EKCalendar(for: .event, eventStore: store)
        calendar.title = "PersonalOS"; calendar.source = source
        do { try store.saveCalendar(calendar, commit: true) }
        catch {
            // Do not try a second account after a failure, and do not assume a
            // same-name calendar is ours after an ambiguous save interruption.
            throw JournalError(text: "无法在所选来源创建 PersonalOS 日历（" + source.title + "）：" + error.localizedDescription)
        }
        disk.calendarID = calendar.calendarIdentifier; disk.sourceDescription = sourceDescription(source); disk.creatingCalendar = false
        try persist(); calendarSourceDescription = disk.sourceDescription ?? source.title
        return calendar
    }
    private func sourceDescription(_ source: EKSource) -> String {
        if source.sourceType == .local { return source.title + " · 仅此设备；跨设备同步需先开启系统 iCloud 日历" }
        if source.sourceType == .calDAV && source.title.caseInsensitiveCompare("iCloud") == .orderedSame {
            return source.title + " · iCloud 日历账户；跨设备状态由系统同步决定"
        }
        return source.title + " · 已有日历账户；PersonalOS 不迁移账户，也不宣称 iCloud 同步"
    }
    private func receiptKey(_ origin: ServerEndpoint, _ task: String) -> String { JournalMetadata.hash(origin.key) + ":" + task }
    private func outcomeKey(_ origin: ServerEndpoint, _ entry: CalendarJournalEntry) -> String {
        receiptKey(origin, entry.id) + ":" + String(entry.revision)
    }
    private func requireCurrentOrigin(_ origin: ServerEndpoint) throws {
        guard try API.endpoint() == origin else { throw JournalError(text: "私人 Mac 连接已切换，请返回当前连接重新同步") }
    }
    private func requireEntryOrigin(_ origin: ServerEndpoint) throws {
        if let entriesOrigin, entriesOrigin != origin.key { throw JournalError(text: "事项来自此前的 Mac 连接，请刷新后再操作") }
    }
    private func requireMutationAllowed(_ origin: ServerEndpoint, allowExplicitRemoval: Bool) throws {
        try requireCurrentOrigin(origin)
        guard enabled || allowExplicitRemoval else { throw JournalError(text: "自动写入已关闭；事项保留在待同步列表") }
        guard EKEventStore.authorizationStatus(for: .event) == .fullAccess else { throw JournalError(text: "日历完整访问权限已关闭，尚未继续修改") }
    }
    private func checkBudget(_ deadline: Date) throws {
        try Task.checkCancellation()
        guard deadline.timeIntervalSinceNow > 0.2 else { throw JournalError(text: "本次同步时间已到，打开 PersonalOS 后会继续同步") }
    }
    private func stateURL() throws -> URL {
        try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
            .appendingPathComponent("PersonalOSCalendarJournal", isDirectory: true).appendingPathComponent("ownership-v1.json")
    }
    private func loadDisk() throws {
        do {
            let url = try stateURL()
            disk = FileManager.default.fileExists(atPath: url.path) ? try JSONDecoder().decode(DiskState.self, from: Data(contentsOf: url)) : DiskState()
            diskError = nil
        } catch {
            diskError = "无法读取日历归属记录，请解锁后重试；为避免重复不会写入日历"
            throw JournalError(text: diskError!)
        }
    }
    private func persist() throws {
        guard diskError == nil else { throw JournalError(text: diskError!) }
        let url = try stateURL(); let folder = url.deletingLastPathComponent()
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true,
            attributes: [.protectionKey: FileProtectionType.complete, .posixPermissions: 0o700])
        var directory = folder; var values = URLResourceValues(); values.isExcludedFromBackup = true
        try directory.setResourceValues(values)
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        try encoder.encode(disk).write(to: url, options: [.atomic, .completeFileProtection])
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
    }
}
