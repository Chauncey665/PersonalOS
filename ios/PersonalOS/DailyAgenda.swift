import Foundation
import Combine
import EventKit
import CryptoKit

struct DailyAgendaItem: Identifiable {
    let id: String
    let title: String
    let start: Date
    let end: Date
    let isAllDay: Bool
    let calendarName: String
    let isCompleted: Bool
    let isReminder: Bool
    var canToggle: Bool = true
    var restriction: String? = nil
    var completionNote: String? = nil
}

// Pure formatting/identity helpers; an event title, not a task's execution state,
// is the durable evidence that the person marked an agenda item complete.
enum DailyAgendaMetadata {
    static let completedPrefix = "已完成 · "
    static let plannedPrefix = "计划 · "
    static func hash(_ value: String) -> String {
        SHA256.hash(data: Data(value.utf8)).map { String(format: "%02x", $0) }.joined()
    }
    static func isCompleted(_ title: String) -> Bool { title.hasPrefix(completedPrefix) }
    static func plainTitle(_ title: String, reminder: Bool = false) -> String {
        var value = title
        if value.hasPrefix(completedPrefix) { value.removeFirst(completedPrefix.count) }
        if reminder && value.hasPrefix(plannedPrefix) { value.removeFirst(plannedPrefix.count) }
        return value.isEmpty ? "未命名事项" : value
    }
    static func completionTitle(_ title: String, completed: Bool, reminder: Bool = false) -> String {
        let plain = plainTitle(title, reminder: reminder)
        return (completed ? completedPrefix : reminder ? plannedPrefix : "") + plain
    }
    static func reminderTitle(_ text: String) -> String {
        // Only remove a reminder command and its preceding time phrase. Do not
        // invent content, translate it, or strip words from the actual activity.
        var value = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if let range = value.range(of: #"^.{0,100}?提醒(?:一下)?(?:我)?[，,:：\s]*"#, options: .regularExpression) {
            value.removeSubrange(range)
        } else if let range = value.range(of: #"^remind\s+me\s+(?:to\s+)?"#, options: [.regularExpression, .caseInsensitive]) {
            value.removeSubrange(range)
        }
        return value.isEmpty ? text : String(value.prefix(120))
    }
    static func marker(origin: String, taskID: String) -> URL? {
        guard UUID(uuidString: taskID) != nil else { return nil }
        return URL(string: "personalos-reminder-progress://event/" + hash(origin) + "/" + taskID.lowercased())
    }
    static func markerParts(_ url: URL?, scheme: String) -> (origin: String, taskID: String)? {
        guard let url, url.scheme == scheme, url.host == "event", url.query == nil, url.fragment == nil,
              url.user == nil, url.password == nil, url.port == nil else { return nil }
        let parts = url.path.split(separator: "/").map(String.init)
        guard parts.count == 2, parts[0].range(of: #"^[0-9a-f]{64}$"#, options: .regularExpression) != nil,
              UUID(uuidString: parts[1]) != nil else { return nil }
        return (parts[0], parts[1].lowercased())
    }
    static func isReminder(_ task: TaskRecord) -> Bool {
        guard task.calendarEntry == nil else { return false }
        return task.title.contains("提醒") || task.title.range(of: #"\bremind(?:er)?\b"#, options: [.regularExpression, .caseInsensitive]) != nil
    }
}

@MainActor final class DailyAgendaModel: ObservableObject {
    static let shared = DailyAgendaModel()
    @Published private(set) var items: [DailyAgendaItem] = []
    @Published private(set) var loading = false
    @Published private(set) var busyIDs: Set<String> = []
    @Published private(set) var error: String?

    private struct AgendaError: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }
    private struct Context: Equatable {
        let origin: ServerEndpoint?
        let pairing: String
        let day: Date
        let end: Date
        let zone: String
        var key: String { (origin?.key ?? "") + "|" + pairing + "|" + String(day.timeIntervalSince1970) + "|" + zone }
    }
    private struct Receipt: Codable {
        let eventID: String
        let calendarID: String
        let start: Date
        let end: Date
    }
    private struct Snapshot {
        let item: DailyAgendaItem
        let eventID: String
        let calendarID: String
        let occurrence: Date
        let fingerprint: String
        let preserved: String
        let url: URL?
        let taskID: String?
        let managed: Bool
        let deadline: Bool
    }
    private let store = EKEventStore()
    private let receiptsKey = "PersonalOS.DailyAgenda.reminderReceipts.v1"
    private var receipts: [String: Receipt] = [:]
    private var snapshots: [String: Snapshot] = [:]
    private var context: Context?
    private var epoch = 0
    private var refreshedAt = Date.distantPast
    private var lastInput = ""
    private var lastTasks: [TaskRecord] = []
    private var cancelledReminders: Set<String> = []
    private var observer: NSObjectProtocol?

    private init() {
        if let data = UserDefaults.standard.data(forKey: receiptsKey),
           let saved = try? JSONDecoder().decode([String: Receipt].self, from: data) { receipts = saved }
        observer = NotificationCenter.default.addObserver(forName: .EKEventStoreChanged, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor [weak self] in self?.refreshedAt = .distantPast }
        }
    }

    func refresh(tasks: [TaskRecord]) async {
        let next = currentContext()
        if context != next {
            epoch += 1; context = next; items = []; snapshots = [:]
            refreshedAt = .distantPast; lastInput = ""; cancelledReminders = []; error = nil
        }
        lastTasks = tasks
        guard !loading else { return }
        let signature = inputSignature(tasks)
        guard signature != lastInput || Date().timeIntervalSince(refreshedAt) >= 5 else { return }
        let generation = epoch
        loading = true
        defer { loading = false }
        do {
            try requireAccess(); try requireCurrent(next, generation)
            try rebuild(tasks: tasks, context: next)
            error = nil
            lastInput = signature; refreshedAt = Date()
            // A completed marker is a durable cancellation intent. A failed
            // cancellation is retried only for that exact paired origin/task.
            for item in items where item.isReminder && item.isCompleted {
                guard let snapshot = snapshots[item.id], let taskID = snapshot.taskID,
                      tasks.contains(where: { $0.id.lowercased() == taskID && $0.state == "scheduled" }),
                      !busyIDs.contains(item.id), !cancelledReminders.contains(item.id) else { continue }
                busyIDs.insert(item.id)
                do { try await cancelNotification(taskID: taskID, itemID: item.id, context: next, generation: generation) }
                catch { self.error = error.localizedDescription }
                busyIDs.remove(item.id)
            }
        } catch {
            if context == next && epoch == generation {
                self.error = error.localizedDescription
                if EKEventStore.authorizationStatus(for: .event) != .fullAccess { items = []; snapshots = [:] }
            }
        }
    }

    func toggle(_ item: DailyAgendaItem, tasks: [TaskRecord]) async {
        guard !busyIDs.contains(item.id), let captured = context else { return }
        guard item.canToggle else { error = item.restriction ?? "这项日历不能修改。"; return }
        guard let current = items.first(where: { $0.id == item.id }), current.isCompleted == item.isCompleted else {
            error = "事项已经更新，请查看最新状态后再操作。"; return
        }
        let generation = epoch, target = !current.isCompleted
        var calendarSaved = false
        var verifiedIdentity: Receipt?
        busyIDs.insert(item.id); error = nil
        defer { busyIDs.remove(item.id) }
        do {
            try requireAccess(); try requireCurrent(captured, generation)
            if let snapshot = snapshots[item.id] {
                let fresh = try freshEvent(snapshot)
                try requireWritable(fresh)
                guard fingerprint(fresh) == snapshot.fingerprint else { throw AgendaError(message: "日历事项已被修改，请刷新后再勾选，未覆盖你的修改。") }
                if snapshot.deadline {
                    guard let todoID = snapshot.taskID else { throw AgendaError(message: "截止事项归属无法确认。") }
                    try await TodoModel.shared.setCompleted(id: todoID, completed: target, requireCalendar: true)
                    try requireCurrent(captured, generation)
                    store.reset()
                    let verified = try movedManagedEvent(snapshot)
                    guard DailyAgendaMetadata.isCompleted(verified.title ?? "") == target else {
                        throw AgendaError(message: "待办已保存，日历完成标记尚未确认，请刷新。")
                    }
                    verifiedIdentity = identity(verified)
                } else if snapshot.managed {
                    guard let taskID = snapshot.taskID else { throw AgendaError(message: "日历记录归属无法确认。") }
                    try await CalendarJournal.shared.setCompletion(taskID: taskID, completed: target)
                    try requireCurrent(captured, generation)
                    store.reset()
                    let verified = try movedManagedEvent(snapshot)
                    guard DailyAgendaMetadata.isCompleted(verified.title ?? "") == target else {
                        throw AgendaError(message: "日历尚未确认保存，未更改完成状态。")
                    }
                    verifiedIdentity = identity(verified)
                } else {
                    let title = DailyAgendaMetadata.completionTitle(fresh.title ?? "", completed: target, reminder: item.isReminder)
                    verifiedIdentity = try saveTitle(fresh, title: title, snapshot: snapshot)
                }
            } else if item.isReminder {
                guard let task = tasks.first(where: { reminderID($0.id, captured) == item.id }),
                      task.state == "scheduled" || task.state == "completed",
                      let due = task.due, Date(timeIntervalSince1970: due) == item.start else {
                    throw AgendaError(message: "提醒状态或时间已改变，请刷新后再操作。")
                }
                try createReminderMarker(task, completed: target, context: captured)
            } else { throw AgendaError(message: "未找到这次日历事项，请刷新后重试。") }
            calendarSaved = true
            try requireCurrent(captured, generation)
            try rebuild(tasks: tasks, context: captured)
            let saved: DailyAgendaItem?
            if let identity = verifiedIdentity {
                // Saving one recurring occurrence may detach it and change its
                // external ID. Follow the actual verified post-save identity,
                // never assume the pre-save row ID survived that operation.
                let matches = snapshots.values.filter {
                    $0.eventID == identity.eventID && $0.calendarID == identity.calendarID &&
                    $0.item.start == identity.start && $0.item.end == identity.end
                }
                saved = matches.count == 1 ? matches.first?.item : nil
            } else { saved = items.first(where: { $0.id == item.id }) }
            guard let saved, saved.isCompleted == target else {
                throw AgendaError(message: "写入后未能核实完成状态，请检查 Apple 日历并刷新。")
            }
            refreshedAt = Date(); lastInput = inputSignature(tasks)
            if item.isReminder && target {
                let taskID = snapshots[item.id]?.taskID
                if let taskID {
                    CompanionFeatures.shared.cancelReminder(taskID: taskID)
                    if tasks.contains(where: { $0.id.lowercased() == taskID && $0.state == "scheduled" }) {
                        try await cancelNotification(taskID: taskID, itemID: item.id, context: captured, generation: generation)
                    }
                }
            }
        } catch {
            if context == captured && epoch == generation {
                self.error = error.localizedDescription
                // Re-read actual saved state even if a later notification cancel
                // failed. Never pretend a saved completion was rolled back.
                if calendarSaved { try? rebuild(tasks: tasks, context: captured) }
                refreshedAt = .distantPast
            }
        }
    }

    private func currentContext() -> Context {
        var calendar = Calendar(identifier: .gregorian); calendar.timeZone = .current
        let start = calendar.startOfDay(for: Date())
        let endpoint = try? API.endpoint()
        let pairing = endpoint.map { PendingSubmission.fingerprint(for: $0) } ?? ""
        return Context(origin: endpoint, pairing: pairing, day: start,
                       end: calendar.date(byAdding: .day, value: 1, to: start)!, zone: calendar.timeZone.identifier)
    }
    private func requireCurrent(_ expected: Context, _ generation: Int) throws {
        guard generation == epoch, context == expected, currentContext() == expected, !Task.isCancelled else {
            throw AgendaError(message: "连接、日期或时区已改变，请刷新今日事项后重试。")
        }
    }
    private func requireAccess() throws {
        guard EKEventStore.authorizationStatus(for: .event) == .fullAccess else {
            throw AgendaError(message: "请在 iPhone 设置允许 PersonalOS 完整访问日历，才能读取今天的课程和事项。")
        }
    }
    private func requireWritable(_ event: EKEvent) throws {
        guard event.calendar.allowsContentModifications else { throw AgendaError(message: "这个日历只读，不能在 Apple 日历中修改完成状态。") }
        guard !event.hasAttendees, event.attendees?.isEmpty ?? true else {
            throw AgendaError(message: "这项活动含参与者或邀请，PersonalOS 不会修改它或发送日程更新。")
        }
    }
    private func reminderID(_ taskID: String, _ context: Context) -> String {
        "reminder:" + DailyAgendaMetadata.hash(context.origin?.key ?? "") + ":" + taskID.lowercased()
    }
    private func inputSignature(_ tasks: [TaskRecord]) -> String {
        DailyAgendaMetadata.hash(tasks.map { $0.id + "|" + $0.state + "|" + String($0.due ?? 0) + "|" + $0.body }.joined(separator: "\n"))
    }
    private func events(start: Date, end: Date, calendars: [EKCalendar]? = nil) -> [EKEvent] {
        store.events(matching: store.predicateForEvents(withStart: start, end: end, calendars: calendars))
    }
    private func fingerprint(_ event: EKEvent, preserveOnly: Bool = false, excludeCalendar: Bool = false) -> String {
        var fields = [event.calendar.calendarIdentifier, String(event.startDate.timeIntervalSince1970), String(event.endDate.timeIntervalSince1970),
                      String(event.isAllDay), event.timeZone?.identifier ?? "", event.notes ?? "", event.location ?? "", event.url?.absoluteString ?? "",
                      String(event.availability.rawValue), String(event.hasAttendees), String(event.status.rawValue)]
        if excludeCalendar { fields.removeFirst() }
        for alarm in event.alarms ?? [] {
            let offset = String(alarm.relativeOffset)
            let absolute = String(alarm.absoluteDate?.timeIntervalSince1970 ?? 0)
            let location = alarm.structuredLocation?.title ?? ""
            let proximity = String(alarm.proximity.rawValue)
            fields.append([offset, absolute, location, proximity].joined(separator: ":"))
        }
        if !preserveOnly {
            fields += [event.title ?? "", String(event.lastModifiedDate?.timeIntervalSince1970 ?? 0), String(event.isDetached)]
            for rule in event.recurrenceRules ?? [] {
                fields += [rule.calendarIdentifier, String(rule.frequency.rawValue), String(rule.interval), String(rule.firstDayOfTheWeek),
                           String(rule.recurrenceEnd?.endDate?.timeIntervalSince1970 ?? 0), String(rule.recurrenceEnd?.occurrenceCount ?? 0)]
                fields.append((rule.daysOfTheWeek ?? []).map { String($0.dayOfTheWeek.rawValue) + ":" + String($0.weekNumber) }.joined(separator: ","))
                for numbers in [rule.daysOfTheMonth, rule.daysOfTheYear, rule.weeksOfTheYear, rule.monthsOfTheYear, rule.setPositions] {
                    fields.append((numbers ?? []).map { $0.stringValue }.joined(separator: ","))
                }
            }
        }
        return DailyAgendaMetadata.hash(fields.joined(separator: "\u{1f}"))
    }
    private func snapshot(_ event: EKEvent, item: DailyAgendaItem, taskID: String?, managed: Bool, deadline: Bool = false) -> Snapshot {
        Snapshot(item: item, eventID: event.eventIdentifier ?? "", calendarID: event.calendar.calendarIdentifier,
                 occurrence: event.occurrenceDate ?? event.startDate, fingerprint: fingerprint(event), preserved: fingerprint(event, preserveOnly: true),
                 url: event.url, taskID: taskID, managed: managed, deadline: deadline)
    }
    private func freshEvent(_ snapshot: Snapshot) throws -> EKEvent {
        store.reset()
        guard let calendar = store.calendar(withIdentifier: snapshot.calendarID) else { throw AgendaError(message: "原日历已不可用，请刷新。") }
        let found = events(start: snapshot.item.start.addingTimeInterval(-1), end: snapshot.item.end.addingTimeInterval(1), calendars: [calendar]).filter {
            $0.eventIdentifier == snapshot.eventID && ($0.occurrenceDate ?? $0.startDate) == snapshot.occurrence && $0.startDate == snapshot.item.start
        }
        guard found.count == 1, let event = found.first else { throw AgendaError(message: "这次日历事项已移动、删除或无法唯一定位，请刷新后重试。") }
        return event
    }
    private func identity(_ event: EKEvent) -> Receipt {
        Receipt(eventID: event.eventIdentifier ?? "", calendarID: event.calendar.calendarIdentifier,
                start: event.startDate, end: event.endDate)
    }
    // Called only after the owning writer has verified and saved its receipt.
    // An ID may change when EventKit moves the same item between calendars.
    private func movedManagedEvent(_ snapshot: Snapshot) throws -> EKEvent {
        guard let url = snapshot.url else { throw AgendaError(message: "缺少日历归属标识") }
        let calendars = try CalendarJournal.shared.managedCalendarIdentifiers().compactMap { store.calendar(withIdentifier: $0) }
        let matches = events(start: snapshot.item.start.addingTimeInterval(-1), end: snapshot.item.end.addingTimeInterval(1), calendars: calendars).filter {
            $0.url == url && $0.startDate == snapshot.item.start && $0.endDate == snapshot.item.end
        }
        guard matches.count == 1 else { throw AgendaError(message: "日历分类移动后的记录尚未唯一确认，请刷新") }
        return matches[0]
    }
    private func saveTitle(_ event: EKEvent, title: String, snapshot: Snapshot) throws -> Receipt {
        if snapshot.url?.scheme == "personalos-reminder-progress" {
            let targetID = try CalendarJournal.shared.categoryCalendarIdentifier(completed: DailyAgendaMetadata.isCompleted(title), allowExplicitCompletion: true)
            guard let target = store.calendar(withIdentifier: targetID) else { throw AgendaError(message: "提醒分类日历不可用") }
            event.calendar = target
        }
        let targetID = event.calendar.calendarIdentifier
        let preserved = fingerprint(event, preserveOnly: true)
        event.title = title
        try store.save(event, span: .thisEvent, commit: true)
        let savedID = event.eventIdentifier
        store.reset()
        guard let calendar = store.calendar(withIdentifier: targetID) else { throw AgendaError(message: "保存后原日历不可用，请在 Apple 日历检查。") }
        let found = events(start: snapshot.item.start.addingTimeInterval(-1), end: snapshot.item.end.addingTimeInterval(1), calendars: [calendar]).filter {
            $0.eventIdentifier == savedID && $0.startDate == snapshot.item.start && $0.endDate == snapshot.item.end
        }
        guard found.count == 1, let saved = found.first, let verifiedID = saved.eventIdentifier, !verifiedID.isEmpty, saved.title == title,
              fingerprint(saved, preserveOnly: true) == preserved else {
            throw AgendaError(message: "保存后未能核实原事项的其他信息，刷新前不会假定操作成功。")
        }
        if let url = snapshot.url, url.scheme == "personalos-reminder-progress" { remember(saved, marker: url) }
        return Receipt(eventID: verifiedID, calendarID: saved.calendar.calendarIdentifier, start: saved.startDate, end: saved.endDate)
    }

    private func rebuild(tasks: [TaskRecord], context: Context) throws {
        try requireAccess(); store.reset()
        try recolorReminderMarkers(context)
        let today = events(start: context.day, end: context.end)
        let personalID = try? CalendarJournal.shared.progressCalendarIdentifier()
        let personalIDs = (try? CalendarJournal.shared.managedCalendarIdentifiers()) ?? []
        let originHash = context.origin.map { DailyAgendaMetadata.hash($0.key) }
        let relevant = tasks.filter { task in
            guard DailyAgendaMetadata.isReminder(task), let due = task.due, due.isFinite,
                  ["scheduled", "completed", "cancelled"].contains(task.state) else { return false }
            return due >= context.day.timeIntervalSince1970 && due < context.end.timeIntervalSince1970
        }
        let taskByID = Dictionary(relevant.map { ($0.id.lowercased(), $0) }, uniquingKeysWith: { first, _ in first })
        var result: [DailyAgendaItem] = [], rebuilt: [String: Snapshot] = [:], consumed = Set<String>()
        let markerCounts = Dictionary(today.compactMap { event -> (String, Int)? in
            guard let marker = DailyAgendaMetadata.markerParts(event.url, scheme: "personalos-reminder-progress"), marker.origin == originHash,
                  personalIDs.contains(event.calendar.calendarIdentifier) else { return nil }
            return (marker.taskID, 1)
        }, uniquingKeysWith: +)
        for event in today {
            guard let start = event.startDate, let end = event.endDate, end >= start else { continue }
            let marker = DailyAgendaMetadata.markerParts(event.url, scheme: "personalos-reminder-progress")
            let journal = DailyAgendaMetadata.markerParts(event.url, scheme: "personalos-journal")
            let deadline = DailyAgendaMetadata.markerParts(event.url, scheme: "personalos-todo-deadline")
            if deadline != nil && (start < context.day || start >= context.end) { continue }
            let matchingMarker = marker?.origin == originHash && personalIDs.contains(event.calendar.calendarIdentifier)
            let task = matchingMarker ? marker.flatMap { taskByID[$0.taskID] } : nil
            let id = task.map { reminderID($0.id, context) } ?? "event:" + DailyAgendaMetadata.hash(event.calendar.calendarIdentifier + "|" + (event.calendarItemExternalIdentifier ?? event.calendarItemIdentifier) + "|" + String((event.occurrenceDate ?? start).timeIntervalSince1970))
            // Duplicate marker events must not be merged into an apparently safe write.
            let duplicate = matchingMarker && (markerCounts[marker!.taskID] ?? 0) > 1
            var restriction: String?
            if !event.calendar.allowsContentModifications { restriction = "此日历只读，不能修改原事项。" }
            else if event.hasAttendees || !(event.attendees?.isEmpty ?? true) { restriction = "含参与者或邀请，不会发送日程修改。" }
            else if duplicate { restriction = "发现重复的提醒日历标记，请先在 Apple 日历检查。" }
            else if event.url?.scheme == "personalos-journal" && journal == nil { restriction = "PersonalOS 日历标记无效，无法安全更新。" }
            else if event.url?.scheme == "personalos-reminder-progress" && marker == nil { restriction = "提醒日历标记无效，无法安全更新。" }
            else if journal != nil && journal?.origin != originHash { restriction = "这条 PersonalOS 日历记录属于另一连接。" }
            else if marker != nil && !matchingMarker { restriction = "无法确认这条提醒日历标记的归属。" }
            else if event.url?.scheme == "personalos-todo-deadline" && (deadline == nil || deadline?.origin != originHash || !personalIDs.contains(event.calendar.calendarIdentifier)) { restriction = "截止标记不属于当前 PersonalOS 连接。" }
            let done = DailyAgendaMetadata.isCompleted(event.title ?? "")
            let reminder = matchingMarker
            var note: String?
            if reminder && !done { note = "通知不会自动恢复，需另设提醒。" }
            if reminder && done && task?.state == "scheduled" && !cancelledReminders.contains(id) { note = "已完成；正在取消原提醒通知。" }
            var item = DailyAgendaItem(id: duplicate ? id + ":" + (event.eventIdentifier ?? UUID().uuidString) : id,
                                       title: DailyAgendaMetadata.plainTitle(event.title ?? "未命名事项", reminder: reminder),
                                       start: start, end: end, isAllDay: event.isAllDay, calendarName: event.calendar.title,
                                       isCompleted: done, isReminder: reminder, canToggle: restriction == nil, restriction: restriction, completionNote: note)
            if matchingMarker, let marker {
                consumed.insert(marker.taskID)
                if !duplicate, let url = event.url { remember(event, marker: url) }
            }
            if rebuilt[item.id] != nil { item = DailyAgendaItem(id: item.id + ":duplicate", title: item.title, start: start, end: end, isAllDay: item.isAllDay, calendarName: item.calendarName, isCompleted: done, isReminder: reminder, canToggle: false, restriction: "无法唯一定位这次日历事项。") }
            result.append(item)
            rebuilt[item.id] = snapshot(event, item: item, taskID: deadline?.taskID ?? journal?.taskID ?? (matchingMarker ? marker?.taskID : nil),
                                       managed: journal != nil, deadline: deadline != nil)
        }
        for task in relevant where !consumed.contains(task.id.lowercased()) && task.state != "cancelled" {
            let start = Date(timeIntervalSince1970: task.due!)
            let id = reminderID(task.id, context)
            var restriction: String?
            if context.origin == nil || !API.isPaired() { restriction = "请先连接对应 Mac，才能写入这条提醒的完成记录。" }
            else if personalID == nil { restriction = "请先在设置开启个人 PersonalOS 日历，再记录提醒完成状态。" }
            if let origin = context.origin, let marker = DailyAgendaMetadata.marker(origin: origin.key, taskID: task.id), receipts[marker.absoluteString] != nil {
                // Previously saved but no longer in today's event query: never
                // silently recreate a manually moved/deleted event.
                restriction = "原提醒日历记录已移动或删除，请先在 Apple 日历检查。"
            }
            result.append(DailyAgendaItem(id: id, title: DailyAgendaMetadata.reminderTitle(task.title), start: start,
                                          end: start.addingTimeInterval(900), isAllDay: false, calendarName: "PersonalOS 提醒",
                                          isCompleted: false, isReminder: true, canToggle: restriction == nil, restriction: restriction))
        }
        result.sort { a, b in a.isAllDay != b.isAllDay ? a.isAllDay : a.start != b.start ? a.start < b.start : a.id < b.id }
        items = result; snapshots = rebuilt
    }

    private func recolorReminderMarkers(_ context: Context) throws {
        guard let origin = context.origin, API.isPaired(), CalendarJournal.shared.enabled else { return }
        let owned = try CalendarJournal.shared.managedCalendarIdentifiers()
        let calendars = owned.compactMap { store.calendar(withIdentifier: $0) }
        let candidates = events(start: context.day, end: context.end, calendars: calendars).filter {
            DailyAgendaMetadata.markerParts($0.url, scheme: "personalos-reminder-progress")?.origin == DailyAgendaMetadata.hash(origin.key)
        }
        // reset() invalidates EventKit objects; retain only value URLs and
        // refetch each item so a first move cannot invalidate the next one.
        let urls = candidates.compactMap(\.url)
        for url in urls where urls.filter({ $0 == url }).count == 1 {
            let currentCalendars = try CalendarJournal.shared.managedCalendarIdentifiers().compactMap { store.calendar(withIdentifier: $0) }
            let current = events(start: context.day, end: context.end, calendars: currentCalendars).filter { $0.url == url }
            guard current.count == 1 else { continue }
            let event = current[0]
            guard
                  !event.hasAttendees, !event.hasRecurrenceRules else { continue }
            let targetID = try CalendarJournal.shared.categoryCalendarIdentifier(completed: DailyAgendaMetadata.isCompleted(event.title ?? ""))
            guard targetID != event.calendar.calendarIdentifier else { continue }
            guard let target = store.calendar(withIdentifier: targetID) else { throw AgendaError(message: "提醒分类日历尚未同步") }
            let preserved = fingerprint(event, preserveOnly: true, excludeCalendar: true), title = event.title
            let start = event.startDate!, end = event.endDate!
            event.calendar = target
            try store.save(event, span: .thisEvent, commit: true)
            store.reset()
            let all = try CalendarJournal.shared.managedCalendarIdentifiers().compactMap { store.calendar(withIdentifier: $0) }
            let actual = events(start: start.addingTimeInterval(-1), end: end.addingTimeInterval(1), calendars: all).filter { $0.url == url }
            guard actual.count == 1, actual[0].title == title,
                  actual[0].calendar.calendarIdentifier == targetID,
                  fingerprint(actual[0], preserveOnly: true, excludeCalendar: true) == preserved else {
                throw AgendaError(message: "提醒颜色分类尚未确认，请检查 Apple 日历；不会重复创建")
            }
            remember(actual[0], marker: url)
        }
    }

    private func remember(_ event: EKEvent, marker: URL) {
        guard let eventID = event.eventIdentifier, !eventID.isEmpty else { return }
        let receipt = Receipt(eventID: eventID, calendarID: event.calendar.calendarIdentifier, start: event.startDate, end: event.endDate)
        receipts[marker.absoluteString] = receipt
        if let data = try? JSONEncoder().encode(receipts) { UserDefaults.standard.set(data, forKey: receiptsKey) }
    }
    private func createReminderMarker(_ task: TaskRecord, completed: Bool, context: Context) throws {
        guard let origin = context.origin, let due = task.due,
              let url = DailyAgendaMetadata.marker(origin: origin.key, taskID: task.id) else { throw AgendaError(message: "提醒标识无效。") }
        let calendarID = try CalendarJournal.shared.categoryCalendarIdentifier(completed: completed, allowExplicitCompletion: true)
        store.reset()
        guard let calendar = store.calendar(withIdentifier: calendarID), calendar.allowsContentModifications else {
            throw AgendaError(message: "个人 PersonalOS 日历不可写，尚未记录完成状态。")
        }
        guard receipts[url.absoluteString] == nil,
              !events(start: context.day, end: context.end, calendars: [calendar]).contains(where: { $0.url == url }) else {
            throw AgendaError(message: "这条提醒已有日历记录，请刷新后操作。")
        }
        let event = EKEvent(eventStore: store)
        event.calendar = calendar; event.startDate = Date(timeIntervalSince1970: due); event.endDate = event.startDate.addingTimeInterval(900)
        event.timeZone = TimeZone(identifier: context.zone); event.isAllDay = false; event.url = url
        event.title = DailyAgendaMetadata.completionTitle(DailyAgendaMetadata.reminderTitle(task.title), completed: completed, reminder: true)
        event.alarms = []; event.availability = .free
        let expectedTitle = event.title
        try store.save(event, span: .thisEvent, commit: true)
        remember(event, marker: url)
        store.reset()
        guard let verificationCalendar = store.calendar(withIdentifier: calendarID) else { throw AgendaError(message: "保存后个人 PersonalOS 日历不可用，请检查 Apple 日历。") }
        let found = events(start: context.day, end: context.end, calendars: [verificationCalendar]).filter { $0.url == url }
        guard found.count == 1, let saved = found.first, saved.title == expectedTitle, saved.startDate == Date(timeIntervalSince1970: due),
              saved.endDate == Date(timeIntervalSince1970: due + 900), !saved.hasAttendees, saved.alarms?.isEmpty ?? true else {
            throw AgendaError(message: "未能核实提醒完成记录，请刷新并检查 Apple 日历；不会重复新建。")
        }
        remember(saved, marker: url)
    }
    private func cancelNotification(taskID: String, itemID: String, context: Context, generation: Int) async throws {
        try requireCurrent(context, generation)
        CompanionFeatures.shared.cancelReminder(taskID: taskID)
        guard let origin = context.origin else { throw AgendaError(message: "已记录完成，但 Mac 连接不可用，原提醒取消待重试。") }
        do {
            _ = try await API.request("/tasks/" + taskID + "/cancel", [:], endpoint: origin, timeout: 6)
            try requireCurrent(context, generation)
            cancelledReminders.insert(itemID)
            items = items.map { value in var updated = value; if value.id == itemID { updated.completionNote = nil }; return updated }
        } catch {
            throw AgendaError(message: "已记录完成并清除本机通知，但 Mac 提醒取消尚未确认，稍后会重试。" + error.localizedDescription)
        }
    }
}
