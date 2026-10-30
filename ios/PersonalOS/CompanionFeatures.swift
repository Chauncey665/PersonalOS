import Foundation
import Combine
import UIKit
import UserNotifications
import EventKit

enum CalendarContextEncoding {
    static func representsPlanningOccupiedTime(_ event: EKEvent, completedAutoIDs: Set<String>, origin: String) -> Bool {
        guard representsOccupiedTime(event), event.availability != .free, event.calendar.type != .birthday else {return false}
        if event.calendar.isSubscribed && ["節日","节日","holidays"].contains(where:{event.calendar.title.lowercased().contains($0)}) {return false}
        if let marker=DailyAgendaMetadata.markerParts(event.url,scheme:"personalos-journal"),
           marker.origin==JournalMetadata.hash(origin),completedAutoIDs.contains(marker.taskID) {return false}
        return true
    }
    static func representsOccupiedTime(_ event: EKEvent) -> Bool {
        event.status != .canceled && event.url?.scheme != "personalos-todo-deadline"
    }
    static func metadata(for day: DateInterval, timeZone: TimeZone) -> [String: Any] {
        let date = DateFormatter()
        date.calendar = Calendar(identifier: .gregorian)
        date.locale = Locale(identifier: "en_US_POSIX")
        date.timeZone = timeZone
        date.dateFormat = "yyyy-MM-dd"
        let timestamp = ISO8601DateFormatter()
        return ["date": date.string(from: day.start), "timeZone": timeZone.identifier,
                "range_start": timestamp.string(from: day.start), "range_end": timestamp.string(from: day.end)]
    }
}

// Integration checklist (the project owner adds these):
// - Add this file and IntentEntryPoints.swift to the app target's Sources phase.
// - Info.plist: NSCalendarsFullAccessUsageDescription with the user's calendar-sharing purpose.
// - Info.plist: PersonalOSAPNSEnvironment = sandbox for this development build.
// - Signed entitlement: aps-environment = development (requires the matching provisioning profile).
// - PersonalOSApp: @UIApplicationDelegateAdaptor(PersonalOSAppDelegate.self) var appDelegate.
// - Pair success: CompanionFeatures.registerRemoteNotificationsIfPermitted().
// - UI explicitly enables calendar sharing; only then may it send todayCalendarContext()
//   in the calendar_context request field to the paired private Mac.
// - No background microphone, calendar writes, iMessage, or WeChat sending occurs here.

extension Notification.Name {
    static let personalOSTaskRequested = Notification.Name("PersonalOS.taskRequested")
    static let personalOSVoiceRequested = Notification.Name("PersonalOS.voiceRequested")
}

@MainActor final class CompanionFeatures: ObservableObject {
    static let shared = CompanionFeatures()
    static let pendingTaskKey = "personalOS.pendingTaskID"
    static let voiceRequestedKey = "personalOS.voiceRequested"

    @Published private(set) var notificationSummary = "尚未开启手机通知"
    @Published private(set) var calendarSummary = "今日系统日历尚未共享"
    @Published private(set) var pushSummary = "远程推送尚未登记"
    @Published private(set) var notificationsEnabled = false
    @Published private(set) var calendarSharingEnabled: Bool

    private struct LocalReminder: Codable {
        let taskID: String
        var title: String
        var due: Date
        let endpoint: String
    }
    private let notificationCenter = UNUserNotificationCenter.current()
    private let eventStore = EKEventStore()
    private let defaults = UserDefaults.standard
    private let ledgerKey = "personalOS.localReminders.v1"
    private let calendarKey = "personalOS.calendarSharingEnabled"
    private var reminders: [String: LocalReminder]
    private var operations: [String: UUID] = [:]
    private var cancelledOperations = Set<String>()
    private var isReconciling = false
    private var calendarRequestInProgress = false
    private var calendarRequestID: UUID?

    private init() {
        calendarSharingEnabled = UserDefaults.standard.bool(forKey: "personalOS.calendarSharingEnabled")
        if let data = UserDefaults.standard.data(forKey: "personalOS.localReminders.v1"),
           let saved = try? JSONDecoder().decode([String: LocalReminder].self, from: data) {
            reminders = saved
        } else { reminders = [:] }
        if calendarSharingEnabled { calendarSummary = "已选择共享今天日历；等待检查系统授权" }
    }

    func refreshAuthorizationStatus() async {
        let settings = await notificationCenter.notificationSettings()
        notificationsEnabled = Self.permitsNotifications(settings.authorizationStatus)
        switch settings.authorizationStatus {
        case .authorized: notificationSummary = "手机通知已开启"
        case .provisional: notificationSummary = "已允许安静通知；横幅与声音可在系统设置调整"
        case .ephemeral: notificationSummary = "临时通知已开启"
        case .denied: notificationSummary = "系统已关闭通知，请在 iPhone 设置中开启"
        case .notDetermined: notificationSummary = "尚未开启手机通知"
        @unknown default: notificationSummary = "请检查 iPhone 的通知设置"
        }
        if calendarSharingEnabled {
            if EKEventStore.authorizationStatus(for: .event) == .fullAccess {
                calendarSummary = "已开启：仅在提交规划时共享今天最多 20 个事件"
            } else {
                calendarSharingEnabled = false; defaults.set(false, forKey: calendarKey)
                calendarSummary = "系统日历授权不可用，已停止共享"
            }
        }
    }

    func enableNotifications() async {
        do {
            _ = try await notificationCenter.requestAuthorization(options: [.alert, .sound, .badge])
            await refreshAuthorizationStatus()
            if notificationsEnabled { Self.registerRemoteNotificationsIfPermitted() }
        } catch { notificationSummary = "无法申请通知权限，请在系统设置检查后重试" }
    }

    func scheduleReminder(taskID: String, title: String, due: Date) async throws {
        guard !taskID.isEmpty, taskID.count <= 200 else { throw PersonalOSError.message("提醒任务标识无效") }
        guard due.timeIntervalSinceNow > 1 else { throw PersonalOSError.message("提醒时间必须在未来") }
        guard operations[taskID] == nil else { throw PersonalOSError.message("此提醒正在安排，请稍后再试") }
        let endpoint = try API.endpoint()
        let operation = UUID(); operations[taskID] = operation
        defer {
            if operations[taskID] == operation { operations.removeValue(forKey: taskID) }
            cancelledOperations.remove(taskID)
        }
        let settings = await notificationCenter.notificationSettings()
        notificationsEnabled = Self.permitsNotifications(settings.authorizationStatus)
        guard notificationsEnabled else { throw PersonalOSError.message("请先在 App 设置中开启手机通知") }
        guard !cancelledOperations.contains(taskID) else { throw PersonalOSError.message("提醒操作已取消") }
        guard reminders[taskID] != nil || reminders.count < 50 else {
            throw PersonalOSError.message("本机最多保留 50 条未来提醒，请先取消不再需要的提醒")
        }
        let reminder = LocalReminder(taskID: taskID, title: String(title.prefix(200)), due: due, endpoint: endpoint.key)
        let request = makeRequest(reminder)
        do {
            try await notificationCenter.add(request)
            guard !cancelledOperations.contains(taskID) else { throw PersonalOSError.message("提醒操作已取消") }
            // Confirm only after iOS has accepted the local schedule. The Mac uses this
            // per-device confirmation to avoid sending a duplicate reminder through APNs.
            _ = try await API.request("/tasks/\(taskID)/local-reminder", ["scheduled": "true"], endpoint: endpoint)
            guard !cancelledOperations.contains(taskID) else { throw PersonalOSError.message("提醒操作已取消") }
            reminders[taskID] = reminder; persistReminders()
            notificationSummary = "手机提醒已安排；即使 App 关闭也由系统负责提醒"
        } catch {
            notificationCenter.removePendingNotificationRequests(withIdentifiers: [request.identifier])
            reminders.removeValue(forKey: taskID); persistReminders()
            // If a successful server acknowledgement was lost in transit, attempt to clear
            // its marker as well. Delivery after a network interruption still needs checking.
            _ = try? await API.request("/tasks/\(taskID)/local-reminder", ["scheduled": "false"], endpoint: endpoint)
            throw PersonalOSError.message("未完成手机提醒确认，已取消本机排程。Mac 任务仍可在历史查看；请保持私人网络连接后重试。")
        }
    }

    func cancelReminder(taskID: String) {
        if operations[taskID] != nil { cancelledOperations.insert(taskID) }
        let previous = reminders.removeValue(forKey: taskID)
        let identifier = Self.reminderIdentifier(taskID)
        notificationCenter.removePendingNotificationRequests(withIdentifiers: [identifier])
        notificationCenter.removeDeliveredNotifications(withIdentifiers: [identifier])
        persistReminders()
        notificationSummary = "本机提醒已取消"
        if let previous, let endpoint = try? ServerEndpoint(previous.endpoint) {
            Task {
                // A server-side task cancellation also clears the marker; this is best effort
                // for the case where only the local reminder is removed.
                _ = try? await API.request("/tasks/\(taskID)/local-reminder", ["scheduled": "false"], endpoint: endpoint)
            }
        }
    }

    func reconcileReminders(tasks: [TaskRecord]) async {
        guard !isReconciling, let endpoint = try? API.endpoint() else { return }
        isReconciling = true
        defer { isReconciling = false }
        let records = Dictionary(tasks.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        let pending = await notificationCenter.pendingNotificationRequests()
        let pendingIdentifiers = Set(pending.map(\.identifier))
        let settings = await notificationCenter.notificationSettings()
        notificationsEnabled = Self.permitsNotifications(settings.authorizationStatus)
        // Reconcile only this app's previously scheduled reminders. A task created on a
        // different device is never silently converted into a local iPhone notification.
        for (taskID, reminder) in Array(reminders) where reminder.endpoint == endpoint.key {
            guard operations[taskID] == nil, let task = records[taskID] else { continue }
            if ["cancelled", "failed", "needs_review", "undone"].contains(task.state) {
                cancelReminder(taskID: taskID)
                continue
            }
            if task.state == "completed" || reminder.due <= Date() {
                // Do not schedule or re-deliver completed notifications. Retain an already
                // delivered banner so tapping it can still open the task history.
                reminders.removeValue(forKey: taskID); persistReminders()
                continue
            }
            guard task.state == "scheduled", let dueValue = task.due else { continue }
            let currentDue = Date(timeIntervalSince1970: dueValue)
            if !notificationsEnabled {
                notificationCenter.removePendingNotificationRequests(withIdentifiers: [Self.reminderIdentifier(taskID)])
                _ = try? await API.request("/tasks/\(taskID)/local-reminder", ["scheduled": "false"], endpoint: endpoint)
                continue
            }
            if abs(currentDue.timeIntervalSince(reminder.due)) > 1 || !pendingIdentifiers.contains(Self.reminderIdentifier(taskID)) {
                do { try await scheduleReminder(taskID: taskID, title: task.title, due: currentDue) }
                catch { notificationSummary = error.localizedDescription }
            }
        }
    }

    func testNotification() async throws {
        let settings = await notificationCenter.notificationSettings()
        guard Self.permitsNotifications(settings.authorizationStatus) else {
            throw PersonalOSError.message("请先开启手机通知，再发送测试")
        }
        let content = UNMutableNotificationContent()
        content.title = "PersonalOS 通知测试"
        content.body = "手机本地通知已送达。此测试不代表远程 APNs 已连通。"
        content.sound = .default
        let trigger = UNTimeIntervalNotificationTrigger(timeInterval: 10, repeats: false)
        let request = UNNotificationRequest(identifier: "personalOS.notification-test", content: content, trigger: trigger)
        try await notificationCenter.add(request)
        notificationSummary = "已安排 10 秒后的本地通知测试"
    }

    func connectCalendar() async {
        guard !calendarRequestInProgress else { return }
        calendarRequestInProgress = true
        let requestID = UUID(); calendarRequestID = requestID
        defer { calendarRequestInProgress = false }
        do {
            let granted: Bool
            if EKEventStore.authorizationStatus(for: .event) == .fullAccess { granted = true }
            else { granted = try await eventStore.requestFullAccessToEvents() }
            guard calendarRequestID == requestID else { return }
            calendarSharingEnabled = granted
            defaults.set(granted, forKey: calendarKey)
            calendarSummary = granted
                ? "已开启：仅在提交规划时共享今天最多 20 个事件"
                : "未授权系统日历，继续仅依据任务历史规划"
        } catch {
            calendarSharingEnabled = false; defaults.set(false, forKey: calendarKey)
            calendarSummary = "日历授权未完成，请在系统设置检查"
        }
    }

    func setCalendarSharingEnabled(_ enabled: Bool) async {
        if enabled { await connectCalendar() }
        else {
            calendarRequestID = nil
            calendarSharingEnabled = false; defaults.set(false, forKey: calendarKey)
            calendarSummary = "今日系统日历共享已关闭"
        }
    }

    func todayCalendarContext() -> String? {
        let timeZone = TimeZone.current
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timeZone
        guard calendarSharingEnabled,
              EKEventStore.authorizationStatus(for: .event) == .fullAccess,
              let today = calendar.dateInterval(of: .day, for: Date()) else { return nil }
        let predicate = eventStore.predicateForEvents(withStart: today.start, end: today.end, calendars: nil)
        let events = eventStore.events(matching: predicate)
            .filter { CalendarContextEncoding.representsOccupiedTime($0) }
            .sorted { $0.startDate < $1.startDate }
        let formatter = ISO8601DateFormatter()
        var items: [[String: Any]] = events.prefix(20).map { event in
            ["title": String((event.title ?? "无标题事件").prefix(120)),
             "start": formatter.string(from: event.startDate),
             "end": formatter.string(from: event.endDate),
             "isAllDay": event.isAllDay]
        }
        // Keep the UTF-8 payload within the private server's 8 KB cap, including CJK titles.
        while true {
            var payload = CalendarContextEncoding.metadata(for: today, timeZone: timeZone)
            payload["events"] = items
            payload["truncated"] = items.count < events.count
            if let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]), data.count <= 8_000 {
                calendarSummary = "已提供今天 \(items.count) 个事件，仅用于本次私人规划"
                return String(data: data, encoding: .utf8)
            }
            guard !items.isEmpty else { return nil }
            items.removeLast()
        }
    }

    static func registerRemoteNotificationsIfPermitted() {
        Task { @MainActor in
            let settings = await UNUserNotificationCenter.current().notificationSettings()
            guard permitsNotifications(settings.authorizationStatus) else { return }
            UIApplication.shared.registerForRemoteNotifications()
        }
    }

    func registerPushToken(_ token: Data) async {
        guard API.isPaired() else {
            pushSummary = "手机通知已授权，配对 Mac 后再登记远程推送"
            return
        }
        let value = token.map { String(format: "%02x", $0) }.joined()
        let environment = (Bundle.main.object(forInfoDictionaryKey: "PersonalOSAPNSEnvironment") as? String) == "production" ? "production" : "sandbox"
        do {
            let data = try await API.request("/devices/push", ["token": value, "environment": environment])
            let response = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            if (response?["apns_configured"] as? Bool ?? response?["push_configured"] as? Bool) == false {
                pushSummary = "设备已登记；Mac 远程推送凭据尚未配置"
            } else {
                pushSummary = "设备已登记；远程通知送达仍需实际测试"
            }
        } catch { pushSummary = "远程推送登记未完成；请连接私人网络并确认 Mac 服务后重试" }
    }

    func pushRegistrationFailed(_ error: Error) {
        // The OS message explains missing entitlements or connectivity without logging a token.
        pushSummary = "远程推送注册未完成：\(error.localizedDescription)"
    }

    static func openTask(_ taskID: String) {
        guard !taskID.isEmpty, taskID.count <= 200 else { return }
        UserDefaults.standard.set(taskID, forKey: pendingTaskKey)
        NotificationCenter.default.post(name: .personalOSTaskRequested, object: nil, userInfo: ["taskID": taskID])
    }

    static func requestVoiceEntry() {
        UserDefaults.standard.set(true, forKey: voiceRequestedKey)
        NotificationCenter.default.post(name: .personalOSVoiceRequested, object: nil)
    }

    private static func permitsNotifications(_ status: UNAuthorizationStatus) -> Bool {
        status == .authorized || status == .provisional || status == .ephemeral
    }
    private static func reminderIdentifier(_ taskID: String) -> String { "personalOS.reminder.\(taskID)" }
    private func persistReminders() {
        if let data = try? JSONEncoder().encode(reminders) { defaults.set(data, forKey: ledgerKey) }
    }
    private func makeRequest(_ reminder: LocalReminder) -> UNNotificationRequest {
        let content = UNMutableNotificationContent()
        content.title = "PersonalOS 提醒"
        content.body = reminder.title
        content.sound = .default
        content.threadIdentifier = "PersonalOS.reminders"
        content.userInfo = ["taskID": reminder.taskID]
        var calendar = Calendar(identifier: .gregorian); calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        var components = calendar.dateComponents([.year, .month, .day, .hour, .minute, .second], from: reminder.due)
        components.timeZone = calendar.timeZone
        let trigger = UNCalendarNotificationTrigger(dateMatching: components, repeats: false)
        return UNNotificationRequest(identifier: Self.reminderIdentifier(reminder.taskID), content: content, trigger: trigger)
    }
}

@MainActor final class PersonalOSAppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        Task { await CompanionFeatures.shared.refreshAuthorizationStatus() }
        CompanionFeatures.registerRemoteNotificationsIfPermitted()
        return true
    }
    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        Task { await CompanionFeatures.shared.registerPushToken(deviceToken) }
    }
    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        CompanionFeatures.shared.pushRegistrationFailed(error)
    }
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter,
                                           willPresent notification: UNNotification,
                                           withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .list, .sound])
    }
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter,
                                           didReceive response: UNNotificationResponse,
                                           withCompletionHandler completionHandler: @escaping () -> Void) {
        let info = response.notification.request.content.userInfo
        let taskID = info["taskID"] as? String ?? info["task_id"] as? String
        Task { @MainActor in
            if let taskID { CompanionFeatures.openTask(taskID) }
            completionHandler()
        }
    }
}
