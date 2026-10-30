import Foundation
import EventKit

@MainActor final class TodoDeadlineSync {
    static let shared = TodoDeadlineSync()
    private let store = EKEventStore()
    private let storageKey = "PersonalOS.TodoDeadline.receipts.v1"
    private struct Receipt: Codable {
        var eventID: String?
        let calendarID: String
        let start: Date
        let end: Date
        var fingerprint: String
        var previous: String?
        var phase: String
        var previousCalendarID: String? = nil
    }
    private var receipts: [String: Receipt] = [:]
    private var syncing = false
    private var acknowledged = Set<String>()
    private init() {
        if let data = UserDefaults.standard.data(forKey: storageKey),
           let saved = try? JSONDecoder().decode([String: Receipt].self, from: data) { receipts = saved }
    }
    static func marker(origin: String, id: String) -> URL? {
        guard UUID(uuidString: id) != nil else { return nil }
        return URL(string: "personalos-todo-deadline://event/" + JournalMetadata.hash(origin) + "/" + id.lowercased())
    }
    private func persist() throws {
        UserDefaults.standard.set(try JSONEncoder().encode(receipts), forKey: storageKey)
    }
    private func fingerprint(_ e: EKEvent) -> String {
        var fields: [String] = []
        fields.append(e.title ?? "")
        fields.append(String(e.startDate.timeIntervalSince1970))
        fields.append(String(e.endDate.timeIntervalSince1970))
        fields.append(String(e.isAllDay))
        fields.append(e.timeZone?.identifier ?? "")
        fields.append(e.calendar.calendarIdentifier)
        fields.append(e.notes ?? "")
        fields.append(e.url?.absoluteString ?? "")
        fields.append(e.location ?? "")
        fields.append(String(e.availability.rawValue))
        fields.append(String(e.hasAttendees))
        fields.append(String(e.attendees?.count ?? 0))
        fields.append(String(e.recurrenceRules?.count ?? 0))
        fields.append(e.structuredLocation?.title ?? "")
        fields.append(String(e.structuredLocation?.geoLocation?.coordinate.latitude ?? 0))
        fields.append(String(e.structuredLocation?.geoLocation?.coordinate.longitude ?? 0))
        let alarms: [String] = (e.alarms ?? []).map { alarm in
            let offset = String(alarm.relativeOffset)
            let absolute = String(alarm.absoluteDate?.timeIntervalSince1970 ?? 0)
            return offset + ":" + absolute
        }
        fields.append(alarms.joined(separator: ","))
        return JournalMetadata.hash(fields.joined(separator: "\u{1f}"))
    }
    private func find(url: URL, calendar: EKCalendar, start: Date, end: Date, receipt: Receipt?) throws -> EKEvent? {
        let lower = min(start, receipt?.start ?? start).addingTimeInterval(-86400)
        let upper = max(end, receipt?.end ?? end).addingTimeInterval(86400)
        let calendars = try CalendarJournal.shared.managedCalendarIdentifiers().compactMap { store.calendar(withIdentifier: $0) }
        let matches = store.events(matching: store.predicateForEvents(withStart: lower, end: upper, calendars: calendars)).filter { $0.url == url }
        guard matches.count <= 1 else { throw PersonalOSError.message("发现重复的截止标记，请在 Apple 日历检查") }
        return matches.first
    }
    func synchronize(items: [TodoItem], endpoint: ServerEndpoint) async throws -> String? {
        guard !syncing else { return nil }
        guard items.contains(where: { $0.deadline != nil }) || !receipts.isEmpty else { return nil }
        guard CalendarJournal.shared.enabled else { return "开启 PersonalOS 自动记入日历后，同步截止日期" }
        guard EKEventStore.authorizationStatus(for: .event) == .fullAccess else { return "请允许 PersonalOS 完整访问日历以同步截止日期" }
        let pairing = PendingSubmission.fingerprint(for: endpoint)
        guard API.isPaired(), try API.endpoint().key == endpoint.key else { throw PersonalOSError.message("配对连接已改变，截止日期尚未同步") }
        syncing = true; defer { syncing = false }
        let calendarID = try CalendarJournal.shared.progressCalendarIdentifier()
        store.reset()
        guard let calendar = store.calendar(withIdentifier: calendarID), calendar.allowsContentModifications else {
            throw PersonalOSError.message("个人 PersonalOS 日历不可写，截止日期仍保留在待办中")
        }
        let ownerKey = JournalMetadata.hash(endpoint.key + "|" + pairing) + ":"
        for item in items {
            guard try API.endpoint().key == endpoint.key, PendingSubmission.fingerprint(for: endpoint) == pairing else {
                throw PersonalOSError.message("连接已改变，截止日期同步已停止")
            }
            guard let url = Self.marker(origin: endpoint.key, id: item.id) else { continue }
            store.reset()
            let targetID = item.deadline == nil ? calendarID : try CalendarJournal.shared.categoryCalendarIdentifier(completed: item.completed, deadline: true)
            guard let calendar = store.calendar(withIdentifier: targetID) else { throw PersonalOSError.message("原截止日历不可用") }
            let key = ownerKey + item.id
            let old = receipts[key]
            if item.deadline == nil {
                if let old, old.phase != "removed" {
                    guard let event = try find(url: url, calendar: calendar, start: old.start, end: old.end, receipt: old) else {
                        var removed = old; removed.phase = "removed"; receipts[key] = removed; try persist(); continue
                    }
                    guard event.calendar.calendarIdentifier == old.calendarID, fingerprint(event) == old.fingerprint,
                          !event.hasAttendees else { throw PersonalOSError.message("截止标记被手动修改，未自动移除：" + item.title) }
                    var removing = old; removing.phase = "removing"; receipts[key] = removing; try persist()
                    try store.remove(event, span: .thisEvent, commit: true)
                    guard try find(url: url, calendar: calendar, start: old.start, end: old.end, receipt: old) == nil else {
                        throw PersonalOSError.message("截止标记移除尚未确认")
                    }
                    removing.phase = "removed"; receipts[key] = removing; try persist()
                }
                continue
            }
            guard let start = item.deadlineDate else { throw PersonalOSError.message("截止日期格式无法识别：" + item.title) }
            let allDay = item.deadline?.count == 10
            // Timed deadlines are one-minute markers, not allocated work time.
            let end = allDay ? Calendar.current.date(byAdding: .day, value: 1, to: start)! : start.addingTimeInterval(60)
            let expectedTitle = (item.completed ? "已完成 · " : "") + "截止 · " + item.title
            let expectedNotes = item.notes + (item.notes.isEmpty ? "" : "\n\n") + "截止标记，不代表安排执行时间。"
            let existing = try find(url: url, calendar: calendar, start: start, end: end, receipt: old)
            if let event = existing {
                guard let old, (old.calendarID == event.calendar.calendarIdentifier || (old.phase == "writing" && old.previousCalendarID == event.calendar.calendarIdentifier)), !event.hasAttendees else { throw PersonalOSError.message("截止标记归属无法确认，未覆盖：" + item.title) }
                let actual = fingerprint(event)
                guard actual == old.fingerprint || actual == old.previous else { throw PersonalOSError.message("截止标记被手动修改，未覆盖：" + item.title) }
                if event.title == expectedTitle, event.notes == expectedNotes, event.startDate == start,
                   event.endDate == end, event.isAllDay == allDay, event.calendar.calendarIdentifier == targetID {
                    receipts[key] = Receipt(eventID: event.eventIdentifier, calendarID: targetID, start: start, end: end,
                                            fingerprint: actual, previous: nil, phase: "written")
                    try persist(); continue
                }
            } else if let old, old.phase != "removed" {
                throw PersonalOSError.message("原截止标记已移除或暂未同步，不会重复新建：" + item.title)
            }
            let event = existing ?? EKEvent(eventStore: store)
            let previous = existing.map { fingerprint($0) }
            let previousCalendarID = existing?.calendar.calendarIdentifier
            event.calendar = calendar; event.url = url; event.title = expectedTitle; event.notes = expectedNotes
            event.timeZone = .current
            event.startDate = start; event.endDate = end; event.isAllDay = allDay
            event.location = nil; event.alarms = []; event.availability = .free
            let desired = fingerprint(event)
            receipts[key] = Receipt(eventID: event.eventIdentifier, calendarID: targetID, start: start, end: end,
                                    fingerprint: desired, previous: previous, phase: existing == nil ? "creating" : "writing", previousCalendarID: previousCalendarID)
            try persist()
            do { try store.save(event, span: .thisEvent, commit: true) }
            catch { throw PersonalOSError.message("截止日期保存未确认，请刷新后检查：" + item.title) }
            store.reset()
            guard let verifiedCalendar = store.calendar(withIdentifier: targetID),
                  let actual = try find(url: url, calendar: verifiedCalendar, start: start, end: end, receipt: receipts[key]),
                  fingerprint(actual) == desired else { throw PersonalOSError.message("截止日期保存后的状态未确认：" + item.title) }
            receipts[key] = Receipt(eventID: actual.eventIdentifier, calendarID: targetID, start: start, end: end,
                                    fingerprint: fingerprint(actual), previous: nil, phase: "written")
            try persist()
        }
        // Only acknowledge the exact content that EventKit has just verified.
        // Project completion may change without the parent's own revision.
        for item in items {
            guard try API.endpoint().key == endpoint.key,
                  PendingSubmission.fingerprint(for: endpoint) == pairing else {
                throw PersonalOSError.message("连接已改变，日历回执尚未发送")
            }
            let fingerprint = JournalMetadata.hash([item.id, item.title, item.notes, item.deadline ?? "", item.completed ? "1" : "0"].joined(separator: "\u{1f}"))
            let receiptKey = ownerKey + item.id + ":" + String(item.revision) + ":" + fingerprint
            if acknowledged.contains(receiptKey) { continue }
            let state = item.deadline != nil ? "written" : (receipts[ownerKey + item.id]?.phase == "removed" ? "removed" : "not_required")
            let payload: [String: Any] = ["revision": item.revision, "fingerprint": fingerprint, "status": state]
            let encoded = try JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
            _ = try await API.request("/todos/" + item.id + "/calendar-ack",
                ["payload": String(decoding: encoded, as: UTF8.self)], endpoint: endpoint)
            acknowledged.insert(receiptKey)
        }
        if acknowledged.count > 1000 { acknowledged.removeAll() }
        return nil
    }
}
