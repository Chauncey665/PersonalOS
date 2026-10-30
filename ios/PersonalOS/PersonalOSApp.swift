import SwiftUI
import AppIntents
import Security
import Speech
import AVFoundation
import UIKit
import CryptoKit
import UniformTypeIdentifiers

struct TaskRecord: Codable, Identifiable {
    let id: String
    let body: String
    let state: String
    let result: String
    let plan: String
    let due: Double?
    let created: Double?
    var speech: String?
    var businessOutcome: [String: Any]? {
        (try? JSONSerialization.jsonObject(with: Data(plan.utf8)) as? [String: Any])?["outcome"] as? [String: Any]
    }
    var title: String { (try? JSONSerialization.jsonObject(with: Data(body.utf8)) as? [String: Any])?["text"] as? String ?? body }
}

struct ServerEndpoint: Equatable {
    let url: URL
    var key: String { url.absoluteString }

    init(_ value: String) throws {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard var components = URLComponents(string: trimmed),
              components.scheme?.lowercased() == "https",
              let host = components.host?.lowercased(), host.hasSuffix(".ts.net"),
              components.user == nil, components.password == nil,
              components.query == nil, components.fragment == nil,
              components.path.isEmpty || components.path == "/" else {
            throw PersonalOSError.message("请输入可信的 Tailscale HTTPS 地址（.ts.net）")
        }
        components.scheme = "https"; components.host = host; components.path = ""
        guard let url = components.url else { throw PersonalOSError.message("服务器地址无效") }
        self.url = url
    }
}

enum PersonalOSError: LocalizedError {
    case message(String)
    var errorDescription: String? {
        if case let .message(message) = self { return message }
        return nil
    }
}

enum Vault {
    private static func query(_ endpoint: ServerEndpoint) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: "PersonalOS",
         kSecAttrAccount as String: endpoint.key]
    }
    static func read(for endpoint: ServerEndpoint) -> String {
        var q = query(endpoint); q[kSecReturnData as String] = true; q[kSecMatchLimit as String] = kSecMatchLimitOne
        var value: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &value) == errSecSuccess, let data = value as? Data else { return "" }
        return String(data: data, encoding: .utf8) ?? ""
    }
    static func save(_ token: String, for endpoint: ServerEndpoint) throws {
        let attributes: [String: Any] = [kSecValueData as String: Data(token.utf8),
                                       kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly]
        let status = SecItemUpdate(query(endpoint) as CFDictionary, attributes as CFDictionary)
        if status == errSecSuccess { return }
        guard status == errSecItemNotFound else { throw PersonalOSError.message("无法保存设备认证，请解锁手机后重试") }
        var q = query(endpoint); attributes.forEach { q[$0.key] = $0.value }
        guard SecItemAdd(q as CFDictionary, nil) == errSecSuccess else {
            throw PersonalOSError.message("无法保存设备认证，请解锁手机后重试")
        }
    }
}

// A single unresolved submission survives process termination without syncing its
// task text or calendar context to iCloud. Never send before this record is saved.
struct PendingSubmission: Codable {
    let endpoint: String
    let payload: [String: String]
    let requestID: String
    let inputSignature: String
    let pairingFingerprint: String
    let created: Date
    var title: String { payload["text"] ?? "未确认的任务" }

    static func input(text: String, root: String, query: String, due: Date?) -> [String: String] {
        var input = ["text": text.trimmingCharacters(in: .whitespacesAndNewlines)]
        if !root.isEmpty { input["root"] = root }
        if !query.isEmpty { input["query"] = query }
        if let due { input["due"] = ISO8601DateFormatter().string(from: due) }
        return input
    }
    static func signature(_ input: [String: String]) throws -> String {
        let data = try JSONSerialization.data(withJSONObject: input, options: [.sortedKeys])
        return SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }
    static func fingerprint(for endpoint: ServerEndpoint) -> String {
        SHA256.hash(data: Data(Vault.read(for: endpoint).utf8)).map { String(format: "%02x", $0) }.joined()
    }
}

private enum PendingSubmissionStore {
    private static var query: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: "PersonalOS.PendingSubmission",
         kSecAttrAccount as String: "single-unconfirmed-request",
         kSecAttrSynchronizable as String: false]
    }
    static func read() throws -> PendingSubmission? {
        var q = query; q[kSecReturnData as String] = true; q[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        let status = SecItemCopyMatching(q as CFDictionary, &item)
        if status == errSecItemNotFound { return nil }
        if status == errSecMissingEntitlement {
            throw PersonalOSError.message("当前安装版本的安全存储签名权限不完整，请重新安装正确签名的 PersonalOS。为避免重复，暂不发送新任务")
        }
        guard status == errSecSuccess, let data = item as? Data else {
            throw PersonalOSError.message("无法读取未确认提交（安全存储错误 \(status)），请解锁手机后重试；为避免重复，暂不发送新任务")
        }
        do {
            let request = try JSONDecoder().decode(PendingSubmission.self, from: data)
            _ = try ServerEndpoint(request.endpoint)
            guard !request.requestID.isEmpty, request.payload["request_id"] == request.requestID else {
                throw PersonalOSError.message("请求标识不一致")
            }
            return request
        } catch {
            throw PersonalOSError.message("未确认提交记录无法读取：\(error.localizedDescription)。请先核对 Mac 任务历史，再选择丢弃这条本机记录")
        }
    }
    static func save(_ request: PendingSubmission) throws {
        let attributes: [String: Any] = [kSecValueData as String: try JSONEncoder().encode(request),
                                        kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly]
        let status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if status == errSecSuccess { return }
        guard status == errSecItemNotFound else {
            throw PersonalOSError.message("未发送：无法安全保存重试记录（错误 \(status)），请解锁手机后重试")
        }
        var q = query; attributes.forEach { q[$0.key] = $0.value }
        let added = SecItemAdd(q as CFDictionary, nil)
        guard added == errSecSuccess else {
            throw PersonalOSError.message("未发送：无法安全保存重试记录（错误 \(added)），请解锁手机后重试")
        }
    }
    static func clear() throws {
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw PersonalOSError.message("无法清除本机重试记录（安全存储错误 \(status)），请解锁手机后重试")
        }
    }
}

// Keep authentication on the exact paired origin; never follow a server redirect.
final class NoRedirects: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

struct API {
    static let defaultServer = ""
    static func endpoint() throws -> ServerEndpoint {
        try ServerEndpoint(UserDefaults.standard.string(forKey: "server") ?? defaultServer)
    }
    static func isPaired() -> Bool {
        guard let endpoint = try? endpoint() else { return false }
        return !Vault.read(for: endpoint).isEmpty
    }
    static func request(_ path: String, _ body: [String: String]? = nil,
                        endpoint explicitEndpoint: ServerEndpoint? = nil, timeout: TimeInterval = 15) async throws -> Data {
        let endpoint = try explicitEndpoint ?? self.endpoint()
        guard let url = URL(string: endpoint.key + path) else { throw PersonalOSError.message("请求地址无效") }
        var r = URLRequest(url: url); r.timeoutInterval = timeout
        if path != "/pair" {
            let token = Vault.read(for: endpoint)
            guard !token.isEmpty else { throw PersonalOSError.message("请先解锁手机并与此 Mac 配对") }
            r.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        if let body { r.httpMethod = "POST"; r.httpBody = try JSONEncoder().encode(body); r.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.urlCache = nil
        let session = URLSession(configuration: configuration, delegate: NoRedirects(), delegateQueue: nil)
        defer { session.finishTasksAndInvalidate() }
        let (data, response) = try await session.data(for: r)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            let message = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["error"] as? String ?? "连接失败，请检查 Tailscale 与 Mac 服务"
            throw PersonalOSError.message(message)
        }
        return data
    }
}

@MainActor final class Voice: ObservableObject {
    struct TranscriptUpdate: Equatable {
        let sessionID: UUID
        let text: String
        let isFinal: Bool
    }
    @Published var text = ""
    @Published var recording = false
    @Published var starting = false
    @Published var finishing = false
    @Published private(set) var transcriptUpdate: TranscriptUpdate?
    @Published var error = ""
    private let engine = AVAudioEngine()
    private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "zh-CN"))
    private var recognition: SFSpeechRecognitionTask?
    private var buffer: SFSpeechAudioBufferRecognitionRequest?
    private var recordingID: UUID?
    private var hasTap = false
    private var finishTimeout: Task<Void, Never>?
    private let speaker = AVSpeechSynthesizer()
    private var speechTask: Task<Void,Never>?
    private var speechPlayer: AVAudioPlayer?
    var isCapturing: Bool { starting || recording || finishing }
    func speak(_ text: String) {
        stop(); speaker.stopSpeaking(at: .immediate)
        do { try AVAudioSession.sharedInstance().setCategory(.playback, mode: .spokenAudio); try AVAudioSession.sharedInstance().setActive(true) }
        catch { self.error = error.localizedDescription; return }
        speechTask=Task {
            if let endpoint=try? API.endpoint() {
                let id=await PersonalOSConversation.prepareSpeech(text,endpoint:endpoint)
                if !Task.isCancelled, !id.isEmpty,let data=try? await API.request("/assistant/speech/"+id+"/audio",endpoint:endpoint),!Task.isCancelled {
                    if let player=try? AVAudioPlayer(data:data){speechPlayer=player;player.play();return}
                }
            }
            guard !Task.isCancelled else{return}
            let utterance=AVSpeechUtterance(string:text);utterance.voice=AVSpeechSynthesisVoice(language:text.range(of:"[一-龥]",options:.regularExpression) != nil ? "zh-CN":"en-US");speaker.speak(utterance)
        }
    }
    func stop() {
        speechTask?.cancel();speechTask=nil;speechPlayer?.stop();speechPlayer=nil;speaker.stopSpeaking(at:.immediate)
        recordingID = nil
        finishTimeout?.cancel(); finishTimeout = nil
        stopAudioInput()
        buffer?.endAudio(); recognition?.cancel(); recognition = nil; buffer = nil
        recording = false; starting = false; finishing = false; transcriptUpdate = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }
    private func stopAudioInput() {
        engine.stop()
        if hasTap { engine.inputNode.removeTap(onBus: 0); hasTap = false }
    }
    func finish() {
        guard recording, let id = recordingID else { return }
        finishing = true; recording = false
        stopAudioInput()
        buffer?.endAudio()
        finishTimeout?.cancel()
        finishTimeout = Task { @MainActor [weak self] in
            do { try await Task.sleep(nanoseconds: 3_000_000_000) } catch { return }
            guard let self, self.recordingID == id, self.finishing else { return }
            self.complete(id, message: self.text.isEmpty ? "没有识别到清晰内容，请重试" : "已保留识别文字，请检查后发送")
        }
    }
    private func complete(_ id: UUID, message: String? = nil) {
        guard recordingID == id else { return }
        let finalText = text
        stop()
        if let message { error = message }
        else if finalText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { error = "没有识别到清晰内容，请重试" }
        // A session-tagged delivery survives the recording/finishing flags being
        // cleared in this run loop, but cannot replace a newer editor draft.
        transcriptUpdate = TranscriptUpdate(sessionID: id, text: finalText, isFinal: true)
    }
    func start(sessionID id: UUID) async {
        guard !isCapturing else { return }
        starting = true; error = ""
        recordingID = id; transcriptUpdate = nil
        defer { if recordingID == id { starting = false } }
        let speech = await withCheckedContinuation { c in SFSpeechRecognizer.requestAuthorization { c.resume(returning: $0) } }
        guard recordingID == id else { return }
        let mic = await withCheckedContinuation { c in AVAudioApplication.requestRecordPermission { c.resume(returning: $0) } }
        guard recordingID == id else { return }
        guard speech == .authorized, mic else { error = "请在系统设置授权麦克风和语音识别"; stop(); return }
        guard let recognizer, recognizer.isAvailable, recognizer.supportsOnDeviceRecognition else { error = "此设备暂不支持离线中文识别；请使用键盘听写或文字"; stop(); return }
        do {
            speaker.stopSpeaking(at: .immediate); text = ""
            try AVAudioSession.sharedInstance().setCategory(.record, mode: .measurement, options: .duckOthers)
            try AVAudioSession.sharedInstance().setActive(true)
            let request = SFSpeechAudioBufferRecognitionRequest()
            request.requiresOnDeviceRecognition = true; request.shouldReportPartialResults = true; buffer = request
            let input = engine.inputNode; let format = input.outputFormat(forBus: 0)
            guard format.sampleRate > 0, format.channelCount > 0 else { throw PersonalOSError.message("麦克风暂不可用") }
            input.installTap(onBus: 0, bufferSize: 1024, format: format) { audio, _ in request.append(audio) }
            hasTap = true
            recognition = recognizer.recognitionTask(with: request) { result, failure in
                Task { @MainActor in
                    guard self.recordingID == id else { return }
                    if let result { self.text = result.bestTranscription.formattedString }
                    if result?.isFinal == true { self.complete(id) }
                    else if let failure { self.complete(id, message: failure.localizedDescription) }
                    else if result != nil {
                        self.transcriptUpdate = TranscriptUpdate(sessionID: id, text: self.text, isFinal: false)
                    }
                }
            }
            engine.prepare(); try engine.start()
            recording = true
        } catch { self.error = error.localizedDescription; stop() }
    }
}

struct SubmitTaskIntent: AppIntent {
    static var title: LocalizedStringResource = "交给 PersonalOS"
    static var description = IntentDescription("通过私人网络将任务交给 Mac；需先解锁并在 App 内配对。")
    static var openAppWhenRun = true
    @Parameter(title: "任务") var text: String
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let endpoint = try API.endpoint()
        var body = ["text": text, "request_id": UUID().uuidString, "time_zone": TimeZone.current.identifier]
        if let context = await CompanionFeatures.shared.todayCalendarContext() { body["calendar_context"] = context }
        _ = try await API.request("/tasks", body, endpoint: endpoint)
        return .result(dialog: "已交给你的 Mac，打开 App 就能看到进度。")
    }
}

struct PersonalShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: SubmitTaskIntent(), phrases: ["交给 \(.applicationName)"], shortTitle: "交给助手", systemImageName: "person.crop.circle")
        AppShortcut(intent: PlanTodayIntent(), phrases: ["用 \(.applicationName) 安排今天"], shortTitle: "今天先做什么", systemImageName: "sun.max")
        AppShortcut(intent: ContinueProjectTomorrowIntent(), phrases: ["用 \(.applicationName) 明天提醒我"], shortTitle: "明天继续项目", systemImageName: "bell")
        AppShortcut(intent: OpenVoiceEntryIntent(), phrases: ["打开 \(.applicationName) 语音"], shortTitle: "语音交给助手", systemImageName: "waveform")
        AppShortcut(intent: DictatedTaskIntent(), phrases: ["用 \(.applicationName) 后台提交任务"], shortTitle: "后台提交", systemImageName: "waveform.badge.mic")
        AppShortcut(intent: AudioTaskIntent(), phrases: ["用 \(.applicationName) 提交录音"], shortTitle: "录音交给助手", systemImageName: "waveform.circle")
        AppShortcut(intent: LatestPersonalOSResultIntent(), phrases: ["用 \(.applicationName) 获取最新结果"], shortTitle: "获取最新结果", systemImageName: "doc.on.doc")
    }
}

struct AuthorizedRoot: Decodable, Identifiable {
    let path: String
    let name: String
    let read_only: Bool?
    var id: String { path }
    var isReadOnly: Bool { read_only == true || path == "project://air" }
}
struct ServiceStatus: Decodable {
    let service: String?
    let version: String?
    let model: String?
    let push_configured: Bool?
}
struct TaskSelection: Identifiable { let id: String }
struct FileMove: Decodable, Identifiable {
    let src: String
    let dst: String
    var id: String { src }
}
struct FilePlan: Decodable { let root: String; let moves: [FileMove] }
struct FileHit: Decodable, Identifiable {
    let path: String
    let modified: Double?
    let snippet: String?
    var id: String { path }
}
struct SearchResult: Decodable { let hits: [FileHit]; let scope: String?; let truncated: Bool? }
struct DeviceResult: Decodable { let platform: String; let free_disk_gb: Double }
struct AuditRecord: Decodable, Identifiable {
    let id: Int
    let event: String
    let detail: String
    let at: Double
}

extension TaskRecord {
    var needsReminderDetails: Bool {
        state == "needs_details" && calendarEntry == nil && !plan.contains("voice_session") && ReminderResolveValidation.isReminderText(title)
    }
    var calendarEntry: CalendarJournalEntry? {
        struct Envelope: Decodable { let calendar_entry: CalendarJournalEntry? }
        return (try? JSONDecoder().decode(Envelope.self, from: Data(result.utf8)))?.calendar_entry
            ?? (try? JSONDecoder().decode(Envelope.self, from: Data(plan.utf8)))?.calendar_entry
    }
    var filePlan: FilePlan? { try? JSONDecoder().decode(FilePlan.self, from: Data(plan.utf8)) }
    var searchResult: SearchResult? { try? JSONDecoder().decode(SearchResult.self, from: Data(result.utf8)) }
    var deviceResult: DeviceResult? { try? JSONDecoder().decode(DeviceResult.self, from: Data(result.utf8)) }
    var date: Date? { created.map(Date.init(timeIntervalSince1970:)) }
    var isActive: Bool {
        if let pending = businessOutcome?["pending"] as? Bool { return pending }
        if let entry = calendarEntry { return entry.status == "needs_details" || ["pending", "error"].contains(entry.device_sync) }
        return ["queued", "running", "awaiting_confirmation", "scheduled", "needs_details"].contains(state)
    }
    var isFinished: Bool {
        if businessOutcome?["pending"] as? Bool == true { return false }
        if let entry = calendarEntry { return ["written", "removed"].contains(entry.device_sync) && entry.status != "needs_details" }
        return ["completed", "undone"].contains(state)
    }
    var canCancel: Bool { calendarEntry == nil && ["queued", "awaiting_confirmation", "scheduled"].contains(state) }
    var stateTitle: String {
        if let label = businessOutcome?["label"] as? String { return label }
        if let entry = calendarEntry {
            if entry.status == "needs_details" { return "待补充" }
            if entry.device_sync == "error" { return "同步需检查" }
            if entry.status == "cancelled" { return entry.device_sync == "removed" ? "已撤销" : "待撤销" }
            if entry.device_sync == "written" { return entry.kind == "plan" ? "已安排日历" : "已记入日历" }
            return "待写入日历"
        }
        switch state {
        case "queued": return "排队中"
        case "running": return "正在处理"
        case "awaiting_confirmation": return "等你确认"
        case "scheduled": return "已安排提醒"
        case "completed": return "已处理"
        case "failed": return "未能完成"
        case "needs_review": return "需要检查"
        case "needs_details": return "待补充"
        case "cancelled": return "已取消"
        case "undone": return "已撤销"
        default: return "状态更新中"
        }
    }
    var symbol: String {
        if let status = businessOutcome?["status"] as? String {
            if ["waiting_calendar", "unscheduled"].contains(status) { return "calendar.badge.clock" }
            if ["sync_error", "query_failed"].contains(status) { return "exclamationmark.circle" }
            if status == "historical_answer" { return "clock.arrow.circlepath" }
            if status == "recorded" { return "checklist" }
        }
        if let entry = calendarEntry {
            if entry.status == "needs_details" { return "questionmark.circle" }
            if entry.device_sync == "error" { return "exclamationmark.circle" }
            if entry.status == "cancelled" { return "arrow.uturn.backward" }
            return entry.device_sync == "written" ? "calendar.badge.checkmark" : "calendar.badge.clock"
        }
        switch state {
        case "queued": return "clock"
        case "running": return "waveform.path"
        case "awaiting_confirmation": return "hand.raised"
        case "scheduled": return "bell.badge"
        case "completed": return "checkmark.circle.fill"
        case "failed", "needs_review": return "exclamationmark.circle"
        case "needs_details": return "questionmark.circle"
        case "undone": return "arrow.uturn.backward"
        case "cancelled": return "xmark.circle"
        default: return "circle.dashed"
        }
    }
    var stateColor: Color {
        if businessOutcome?["pending"] as? Bool == true { return .orange }
        if let status = businessOutcome?["status"] as? String, ["query_failed", "historical_answer"].contains(status) { return .secondary }
        if let entry = calendarEntry {
            if entry.status == "needs_details" || entry.device_sync == "error" { return .orange }
            return entry.device_sync == "written" ? OSTheme.accent : .secondary
        }
        switch state {
        case "completed": return OSTheme.accent
        case "failed", "needs_review", "awaiting_confirmation", "needs_details": return .orange
        case "running", "scheduled": return .indigo
        default: return .secondary
        }
    }
    var preview: String {
        if let status = businessOutcome?["status"] as? String,
           ["waiting_calendar", "unscheduled", "sync_error", "calendar_synced", "calendar_removed", "historical_answer", "query_failed"].contains(status),
           let detail = businessOutcome?["detail"] as? String { return detail }
        if let entry = calendarEntry {
            if entry.status == "needs_details" { return entry.question ?? "请补充这件事的开始和结束时间" }
            if entry.status == "cancelled" { return entry.device_sync == "removed" ? "这条 PersonalOS 日历记录已撤销" : "已请求撤销，等待手机日历同步" }
            switch entry.device_sync {
            case "written": return entry.kind == "plan" ? "已安排到手机的 PersonalOS 日历" : "已记入手机的 PersonalOS 日历"
            case "error": return entry.sync_error ?? "日历同步需要检查"
            default: return "日程已整理，等待写入手机的 PersonalOS 日历"
            }
        }
        if let files = searchResult { return "找到 \(files.hits.count) 份资料，按更新时间排列" }
        if let device = deviceResult { return "Mac 已返回设备信息 · 可用空间 \(device.free_disk_gb.formatted()) GB" }
        if state == "awaiting_confirmation", let plan = filePlan { return "已准备 \(plan.moves.count) 个文件的整理清单，确认后才移动" }
        if state == "queued" { return "Mac 已收到，正在等待处理" }
        if state == "running" { return "Mac 正在本地处理这件事" }
        if state == "scheduled", let due { return "\(Date(timeIntervalSince1970: due).formatted(date: .abbreviated, time: .shortened)) 提醒你" }
        return result.isEmpty ? stateTitle : result
    }
}

@MainActor final class AssistantModel: ObservableObject {
    @Published var tasks: [TaskRecord] = []
    @Published var roots: [AuthorizedRoot] = []
    @Published var selectedDocuments: [DocumentDescriptor] = []
    @Published var status: ServiceStatus?
    @Published var connection = "未配对"
    @Published var connected = false
    @Published var busy = false
    @Published var message: String?
    @Published var messageIsError = false
    @Published var selectedTask: TaskSelection?
    @Published var speechResult: String?
    @Published var speechRevision = 0
    @Published var selectedTab = UserDefaults.standard.bool(forKey: "personalOS.voiceRequested") ? 0 : 3
    @Published var settingsPresented = false
    @Published private(set) var pendingSubmission: PendingSubmission?
    @Published private(set) var pendingStorageError: String?
    private var lastMetadataRefresh = Date.distantPast
    private var voiceResultIDs = Set<String>()
    private var scheduling = Set<String>()
    private var pendingSettingsPresentation = false
    private var pendingTaskPresentation: String?
    private let pendingRemindersKey = "personalOS.pendingReminderRequests"

    init() {
        if !UserDefaults.standard.bool(forKey: "PersonalOS.briefVoiceEnabled.v0122") {
            UserDefaults.standard.set(true,forKey: "personalOS.autoSpeak")
            UserDefaults.standard.set(true,forKey: "PersonalOS.briefVoiceEnabled.v0122")
        }
        restorePendingSubmission()
    }

    func restorePendingSubmission() {
        do { pendingSubmission = try PendingSubmissionStore.read(); pendingStorageError = nil }
        catch { pendingStorageError = error.localizedDescription }
    }
    func discardPendingSubmission() {
        guard !busy else { return }
        do {
            try PendingSubmissionStore.clear(); pendingSubmission = nil; pendingStorageError = nil
            notice("已丢弃本机重试记录。Mac 上的任务不会被取消；重新提交相同内容可能重复")
        } catch { notice(error.localizedDescription, error: true) }
    }

    var activeCount: Int { tasks.filter(\.isActive).count }
    var waitingCount: Int { tasks.filter { ["awaiting_confirmation", "needs_details"].contains($0.state) }.count }
    var pendingReminders: Set<String> {
        get { Set(UserDefaults.standard.stringArray(forKey: pendingRemindersKey) ?? []) }
        set { UserDefaults.standard.set(Array(newValue), forKey: pendingRemindersKey) }
    }
    func notice(_ text: String, error: Bool = false) { message = text; messageIsError = error }
    func showSettings() {
        pendingTaskPresentation = nil
        if selectedTask != nil {
            pendingSettingsPresentation = true; selectedTask = nil
        } else { settingsPresented = true }
    }
    func taskSheetDidDismiss() {
        guard pendingSettingsPresentation else { return }
        pendingSettingsPresentation = false; settingsPresented = true
    }
    func openTask(_ id: String) {
        pendingSettingsPresentation = false
        if settingsPresented {
            pendingTaskPresentation = id; settingsPresented = false
        } else { selectedTask = TaskSelection(id: id) }
    }
    func settingsSheetDidDismiss() {
        guard let id = pendingTaskPresentation else { return }
        pendingTaskPresentation = nil; selectedTask = TaskSelection(id: id)
    }
    func showVoiceHome() {
        pendingSettingsPresentation = false; pendingTaskPresentation = nil
        settingsPresented = false; selectedTab = 0
    }
    func resetConnection() {
        tasks = []; roots = []; selectedDocuments = []; status = nil; connected = false
        connection = API.isPaired() ? "待连接" : "未配对"
        message = nil; lastMetadataRefresh = .distantPast
    }
    func refresh(showError: Bool = false) async {
        guard API.isPaired() else { resetConnection(); return }
        do {
            let endpoint = try API.endpoint()
            let data = try await API.request("/tasks", endpoint: endpoint)
            let updated = try JSONDecoder().decode([TaskRecord].self, from: data)
            guard (try? API.endpoint()) == endpoint, !Task.isCancelled else { return }
            tasks = updated; connected = true; connection = "Mac 已连接"
            if Date().timeIntervalSince(lastMetadataRefresh) > 30 {
                lastMetadataRefresh = Date()
                if let data = try? await API.request("/roots", endpoint: endpoint),
                   let value = try? JSONDecoder().decode([AuthorizedRoot].self, from: data), (try? API.endpoint()) == endpoint { roots = value }
                if let data = try? await API.request("/status", endpoint: endpoint),
                   let value = try? JSONDecoder().decode(ServiceStatus.self, from: data), (try? API.endpoint()) == endpoint { status = value }
            }
            for task in updated where task.state == "completed" && voiceResultIDs.contains(task.id) {
                voiceResultIDs.remove(task.id)
                if UserDefaults.standard.bool(forKey: "personalOS.autoSpeak") {
                    speechResult = task.calendarEntry == nil ? (task.speech ?? task.preview) : task.preview; speechRevision += 1
                }
            }
            await settleReminders(updated)
            await CompanionFeatures.shared.reconcileReminders(tasks: updated)
            await CalendarJournal.shared.refreshAndSync(endpoint: endpoint)
            if PersonalOSClipboard.deliverIfRequested(updated,endpoint:endpoint){notice("已复制到手机剪贴板")}
        } catch is CancellationError { return }
        catch {
            connected = false; connection = "暂时无法连接"
            if showError { notice(error.localizedDescription, error: true) }
        }
    }
    func pair(code: String) async {
        guard !busy else { return }; busy = true; defer { busy = false }
        do {
            let endpoint = try API.endpoint()
            let data = try await API.request("/pair", ["code": code.trimmingCharacters(in: .whitespacesAndNewlines), "name": "PersonalOS · iPhone"], endpoint: endpoint)
            let value = try JSONDecoder().decode([String: String].self, from: data)
            guard let token = value["token"], !token.isEmpty else { throw PersonalOSError.message("配对没有完成，请在 Mac 重新生成配对码") }
            try Vault.save(token, for: endpoint)
            notice("已安全连接你的 Mac")
            CompanionFeatures.registerRemoteNotificationsIfPermitted()
            await refresh(showError: true)
        } catch { notice(error.localizedDescription, error: true) }
    }
    func submit(text: String, root: String, query: String, due: Date?) async -> Bool {
        guard !busy else { return false }
        guard API.isPaired() else { showSettings(); notice("请先连接 Mac"); return false }
        let requestText = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !requestText.isEmpty else { notice("写一句你想做的事", error: true); return false }
        busy = true; defer { busy = false }
        do {
            let endpoint = try API.endpoint()
            if selectedDocuments.isEmpty {try await WeatherLocation.shared.prepareIfNeeded(requestText)}
            var input = PendingSubmission.input(text: requestText, root: root, query: query, due: due)
            if !selectedDocuments.isEmpty {input["document_ids"]=String(decoding:try JSONEncoder().encode(selectedDocuments.map(\.id)),as:UTF8.self)}
            let signature = try PendingSubmission.signature(input)
            let existing = try PendingSubmissionStore.read()
            pendingSubmission = existing; pendingStorageError = nil
            let request: PendingSubmission
            if let existing {
                guard existing.endpoint == endpoint.key, existing.inputSignature == signature else {
                    notice("还有一条未确认提交。请先重试或查看历史；若要改交新任务，请明确丢弃原重试记录", error: true)
                    selectedTab = 0
                    return false
                }
                request = existing
            } else {
                var payload = input
                payload["time_zone"] = TimeZone.current.identifier
                if let context = CompanionFeatures.shared.todayCalendarContext(), due == nil, root.isEmpty { payload["calendar_context"] = context }
                if root.isEmpty && due == nil {
                    let conversation=try await PersonalOSConversation.current(endpoint)
                    guard conversation.status != "processing" else{throw PersonalOSError.message("上一轮仍在处理，请稍候")}
                    payload["session_id"]=conversation.id;payload["reply_to"]=conversation.task_id
                }
                let requestID = UUID().uuidString
                payload["request_id"] = requestID
                request = PendingSubmission(endpoint: endpoint.key, payload: payload, requestID: requestID,
                                            inputSignature: signature, pairingFingerprint: PendingSubmission.fingerprint(for: endpoint), created: Date())
                try PendingSubmissionStore.save(request)
                pendingSubmission = request
            }
            return try await sendPendingSubmission(request)
        } catch {
            restorePendingSubmission()
            notice(error.localizedDescription, error: true)
            return false
        }
    }
    func retryPendingSubmission() async -> Bool {
        guard !busy else { return false }; busy = true; defer { busy = false }
        do {
            guard let request = try PendingSubmissionStore.read() else {
                pendingSubmission = nil; pendingStorageError = nil
                notice("当前没有未确认提交")
                return false
            }
            pendingSubmission = request; pendingStorageError = nil
            return try await sendPendingSubmission(request)
        } catch {
            restorePendingSubmission()
            notice(error.localizedDescription, error: true)
            return false
        }
    }
    private func sendPendingSubmission(_ request: PendingSubmission) async throws -> Bool {
        let endpoint = try ServerEndpoint(request.endpoint)
        guard try API.endpoint() == endpoint else {
            throw PersonalOSError.message("这条未确认提交属于之前的 Mac。请在设置恢复原服务器地址，再重试；不会发送到当前 Mac")
        }
        guard !Vault.read(for: endpoint).isEmpty,
              PendingSubmission.fingerprint(for: endpoint) == request.pairingFingerprint else {
            throw PersonalOSError.message("原配对身份暂不可用或已改变。请先解锁并查看 Mac 任务历史；为避免重复，不会用新配对重发旧请求")
        }
        let data = try await API.request("/tasks", request.payload, endpoint: endpoint)
        let response = try JSONDecoder().decode([String: String].self, from: data)
        guard let id = response["id"], !id.isEmpty else { throw PersonalOSError.message("Mac 没有返回任务记录。原提交已保留，请使用“重试未确认提交”") }
        voiceResultIDs.insert(id)
        if request.payload["due"] != nil || ReminderResolveValidation.isReminderText(request.payload["text"] ?? "") {
            var pending = pendingReminders; pending.insert(id); pendingReminders = pending
        }
        do { try PendingSubmissionStore.clear() }
        catch {
            throw PersonalOSError.message("Mac 已确认接收，但本机重试记录未能清除：\(error.localizedDescription)。可先查看任务历史；保留的原请求仍可安全重试")
        }
        pendingSubmission = nil; pendingStorageError = nil
        selectedDocuments = []
        notice(request.payload["due"] == nil ? "已提交" : "提醒已提交")
        await refresh(showError: true)
        if (try? API.endpoint()) == endpoint { openTask(id) }
        return true
    }
    private func settleReminders(_ updated: [TaskRecord]) async {
        for task in updated where task.state=="scheduled" && task.calendarEntry?.reminder_at != nil {
            let key="PersonalOS.calendarReminder."+JournalMetadata.hash((try? API.endpoint().key) ?? "")+task.id+String(task.due ?? 0)
            if !UserDefaults.standard.bool(forKey:key){pendingReminders.insert(task.id)}
        }
        for task in updated where pendingReminders.contains(task.id) && !scheduling.contains(task.id) {
            if (task.calendarEntry != nil && task.calendarEntry?.reminder_at == nil) || ["failed", "cancelled", "needs_review", "completed", "undone"].contains(task.state) {
                var pending = pendingReminders; pending.remove(task.id); pendingReminders = pending; continue
            }
            guard task.state == "scheduled", let due = task.due else { continue }
            guard CompanionFeatures.shared.notificationsEnabled else {
                notice("提醒已存在 Mac 上。请在设置开启通知，才能收到手机锁屏提醒")
                continue
            }
            scheduling.insert(task.id)
            do {
                try await CompanionFeatures.shared.scheduleReminder(taskID: task.id, title: task.title, due: Date(timeIntervalSince1970: due))
                if task.calendarEntry?.reminder_at != nil {UserDefaults.standard.set(true,forKey:"PersonalOS.calendarReminder."+JournalMetadata.hash((try? API.endpoint().key) ?? "")+task.id+String(task.due ?? 0))}
                var pending = pendingReminders; pending.remove(task.id); pendingReminders = pending
                notice("提醒已加入这台 iPhone，到时会通知你")
            } catch { notice("Mac 已安排提醒，但手机通知未完成：\(error.localizedDescription)", error: true) }
            scheduling.remove(task.id)
        }
    }
    func resolveReminder(_ task: TaskRecord, due: Date, timeZone: String, endpoint: ServerEndpoint) async throws {
        guard !busy else { throw PersonalOSError.message("正在处理，请稍后重试") }
        guard task.needsReminderDetails else { throw PersonalOSError.message("这条提醒已改变，请重新打开任务查看") }
        guard (try? API.endpoint()) == endpoint else {
            throw PersonalOSError.message("Mac 连接已改变，请关闭此页并重新打开对应任务")
        }
        guard due > Date(), let zone = TimeZone(identifier: timeZone) else {
            throw PersonalOSError.message("请选择未来的提醒日期和时间")
        }
        busy = true; defer { busy = false }
        let formatter = ISO8601DateFormatter(); formatter.timeZone = zone
        let payload = ["due": formatter.string(from: due), "time_zone": timeZone]
        let data = try await API.request("/tasks/\(task.id)/reminder/resolve", payload, endpoint: endpoint)
        let saved = try JSONDecoder().decode(TaskRecord.self, from: data)
        guard ReminderResolveValidation.matches(saved, expectedID: task.id, targetDue: due) else {
            throw PersonalOSError.message("Mac 尚未确认所选时间，请保留当前时间重试")
        }
        guard (try? API.endpoint()) == endpoint else {
            throw PersonalOSError.message("原 Mac 已保存提醒，但当前连接已改变；请恢复原连接后查看")
        }
        // Keep this task eligible for local notifications if the confirmation
        // fetch fails after the Mac has already saved the chosen instant.
        var pending = pendingReminders; pending.insert(task.id); pendingReminders = pending
        let refreshed = try JSONDecoder().decode(TaskRecord.self, from: await API.request("/tasks/\(task.id)", endpoint: endpoint))
        guard ReminderResolveValidation.matches(refreshed, expectedID: task.id, targetDue: due) else {
            throw PersonalOSError.message("提醒状态或时间已改变，请刷新记录后查看")
        }
        guard (try? API.endpoint()) == endpoint else {
            throw PersonalOSError.message("Mac 连接已改变，请恢复原连接后查看提醒")
        }
        if let index = tasks.firstIndex(where: { $0.id == refreshed.id }) { tasks[index] = refreshed }
        else { tasks.insert(refreshed, at: 0) }
        connected = true; connection = "Mac 已连接"
        notice("提醒时间已保存")
        await settleReminders([refreshed])
    }
    func act(_ task: TaskRecord, action: String) async {
        guard !busy else { return }; busy = true; defer { busy = false }
        do {
            _ = try await API.request("/tasks/\(task.id)/\(action)", [:])
            if action == "cancel" { CompanionFeatures.shared.cancelReminder(taskID: task.id); var pending = pendingReminders; pending.remove(task.id); pendingReminders = pending }
            notice(["approve": "整理已完成，仍可在详情中撤销", "undo": "文件已恢复原位", "cancel": "任务已取消"][action] ?? "已更新")
            await refresh(showError: true)
        } catch { notice(error.localizedDescription, error: true) }
    }
    func openPendingTask() {
        guard let id = UserDefaults.standard.string(forKey: "personalOS.pendingTaskID") else { return }
        UserDefaults.standard.removeObject(forKey: "personalOS.pendingTaskID")
        selectedTab = 1; openTask(id)
    }
}

enum OSTheme {
    static let ink = Color(uiColor: UIColor { traits in
        traits.userInterfaceStyle == .dark ? UIColor(red: 0.86, green: 0.89, blue: 0.91, alpha: 1) : UIColor(red: 0.13, green: 0.16, blue: 0.19, alpha: 1)
    })
    static let mint = Color(red: 0.66, green: 0.85, blue: 0.91)
    static let accent = Color(uiColor: UIColor { traits in
        traits.userInterfaceStyle == .dark ? UIColor(red: 0.49, green: 0.80, blue: 0.90, alpha: 1) : UIColor(red: 0.12, green: 0.43, blue: 0.53, alpha: 1)
    })
    static let background = Color(uiColor: UIColor { traits in
        traits.userInterfaceStyle == .dark ? UIColor(red: 0.07, green: 0.08, blue: 0.09, alpha: 1) : UIColor(red: 0.96, green: 0.97, blue: 0.98, alpha: 1)
    })
    static let surface = Color(uiColor: .secondarySystemGroupedBackground)
    static let onInk = Color(uiColor: UIColor { traits in
        traits.userInterfaceStyle == .dark ? UIColor(red: 0.10, green: 0.12, blue: 0.14, alpha: 1) : .white
    })
}
private struct SurfaceCard<Content: View>: View {
    let content: Content
    init(@ViewBuilder content: () -> Content) { self.content = content() }
    var body: some View {
        content.padding(18).frame(maxWidth: .infinity, alignment: .leading)
            .background(OSTheme.surface, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
    }
}
private struct StatusPill: View {
    let task: TaskRecord
    var body: some View {
        Label(task.stateTitle, systemImage: task.symbol)
            .font(.caption.weight(.semibold)).foregroundStyle(task.stateColor)
            .padding(.horizontal, 10).padding(.vertical, 6)
            .background(task.stateColor.opacity(0.10), in: Capsule())
            .accessibilityLabel("任务状态：\(task.stateTitle)")
    }
}
private struct ConnectionPill: View {
    @ObservedObject var model: AssistantModel
    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(model.connected ? OSTheme.accent : .secondary).frame(width: 5, height: 5)
            Text(model.connected ? "在线" : API.isPaired() ? "未连接" : "未配对")
        }.font(.caption.weight(.medium)).foregroundStyle(model.connected ? OSTheme.accent : .secondary)
            .accessibilityLabel(model.connection)
    }
}
private struct NoticeBanner: View {
    @ObservedObject var model: AssistantModel
    var body: some View {
        if let message = model.message {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: model.messageIsError ? "exclamationmark.circle.fill" : "checkmark.circle.fill")
                    .foregroundStyle(model.messageIsError ? .orange : OSTheme.accent)
                Text(message).font(.subheadline).frame(maxWidth: .infinity, alignment: .leading)
                Button { model.message = nil } label: { Image(systemName: "xmark").font(.caption.bold()).frame(width: 44, height: 44) }
                    .buttonStyle(.plain).accessibilityLabel("关闭提示")
            }.padding(14).background(OSTheme.surface, in: RoundedRectangle(cornerRadius: 16))
        }
    }
}
private struct EmptyState: View {
    let symbol: String
    let title: String
    let detail: String
    var body: some View {
        VStack(spacing: 14) {
            Image(systemName: symbol).font(.system(size: 32, weight: .light)).foregroundStyle(OSTheme.accent)
                .frame(width: 68, height: 68).background(OSTheme.accent.opacity(0.08), in: RoundedRectangle(cornerRadius: 22))
            Text(title).font(.headline)
            Text(detail).font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
        }.frame(maxWidth: .infinity).padding(.vertical, 24)
    }
}

@MainActor struct ContentView: View {
    @AppStorage("server") private var server = API.defaultServer
    @AppStorage("personalOS.autoSpeak") private var autoSpeak = false
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var model = AssistantModel()
    @StateObject private var voice = Voice()
    @StateObject private var features = CompanionFeatures.shared
    var body: some View {
        TabView(selection: $model.selectedTab) {
            TodayScreen(agenda: .shared, tasks: model.tasks, openSettings: { model.showSettings() }, refresh: {
                await model.refresh(showError: true)
                await DailyAgendaModel.shared.refresh(tasks: model.tasks)
            })
                .tabItem { Label("今天", systemImage: "circle.dotted.circle") }.tag(3)
            AssistantHome(model: model, voice: voice, features: features)
                .tabItem { Label("PersonalOS", systemImage: "waveform") }.tag(0)
            TaskLibrary(model: model)
                .tabItem { Label("记录", systemImage: "clock.arrow.circlepath") }.tag(1)
                .badge(model.waitingCount)
            FileBrowserScreen()
                .tabItem { Label("文件", systemImage: "folder") }.tag(2)
        }
        .tint(OSTheme.accent)
        .sheet(item: $model.selectedTask, onDismiss: { model.taskSheetDidDismiss() }) { selection in
            TaskDetailScreen(id: selection.id, model: model, voice: voice)
        }
        .sheet(isPresented: $model.settingsPresented, onDismiss: { model.settingsSheetDidDismiss() }) {
            SettingsScreen(model: model, features: features)
        }
        .task(id: "\(server)|\(scenePhase)") {
            guard scenePhase == .active else { return }
            model.restorePendingSubmission()
            await features.refreshAuthorizationStatus()
            await DailyAgendaModel.shared.refresh(tasks: model.tasks)
            if UserDefaults.standard.bool(forKey: "personalOS.voiceRequested") { model.showVoiceHome() }
            else { model.openPendingTask() }
            while !Task.isCancelled {
                if !model.busy { await model.refresh() }
                if !Task.isCancelled { await TodoModel.shared.refresh() }
                if !Task.isCancelled { await AutoScheduleModel.shared.refresh() }
                if !Task.isCancelled { await DailyAgendaModel.shared.refresh(tasks: model.tasks) }
                do { try await Task.sleep(nanoseconds: 2_000_000_000) } catch { return }
            }
        }
        .task(id: "health|\(server)|\(scenePhase)") {
            guard scenePhase == .active else { return }
            await HealthFitness.shared.sync()
        }
        .onChange(of: model.tasks.map { "\($0.id)|\($0.state)|\($0.due ?? 0)|\($0.calendarEntry?.revision ?? 0)" }) { _, _ in
            guard scenePhase == .active else { return }
            Task { await DailyAgendaModel.shared.refresh(tasks: model.tasks) }
        }
        .onChange(of: server) { _, _ in model.resetConnection() }
        .onChange(of: scenePhase) { _, phase in if phase == .background { voice.stop() } }
        .onChange(of: model.selectedTab) { _, tab in if tab != 0 { voice.stop() } }
        .onChange(of: model.settingsPresented) { _, presented in if presented { voice.stop() } }
        .onChange(of: model.selectedTask?.id) { _, id in if id != nil { voice.stop() } }
        .onChange(of: model.speechRevision) { _, _ in if autoSpeak, let result = model.speechResult { voice.speak(result) } }
        .onReceive(NotificationCenter.default.publisher(for: .personalOSTaskRequested)) { _ in model.openPendingTask() }
        .onReceive(NotificationCenter.default.publisher(for: .personalOSVoiceRequested)) { _ in model.showVoiceHome() }
    }
}

enum ComposerMode: String, CaseIterable {
    case general = "输入", search = "查找资料", organize = "整理文件", reminder = "提醒"
    var symbol: String {
        switch self { case .general: return "person.crop.circle"; case .search: return "doc.text.magnifyingglass"; case .organize: return "folder.badge.gearshape"; case .reminder: return "bell" }
    }
    func reminderDue(_ selectedDate: Date) -> Date? { self == .reminder ? selectedDate : nil }
}
@MainActor private struct AssistantHome: View {
    @ObservedObject var model: AssistantModel
    @ObservedObject var voice: Voice
    @ObservedObject var features: CompanionFeatures
    @Environment(\.scenePhase) private var scenePhase
    @State private var draft = ""
    @State private var voiceDraftSession: UUID?
    @State private var mode = ComposerMode.general
    @State private var root = ""
    @State private var query = "PROJECT"
    @State private var confirmingDiscard = false
    @State private var showingActions=false
    @State private var importingDocument = false
    @State private var uploadingDocument = false
    @State private var reminderDate = Calendar.current.date(bySettingHour: 9, minute: 0, second: 0, of: Date().addingTimeInterval(86400)) ?? Date().addingTimeInterval(86400)
    @FocusState private var draftFocused: Bool
    private var voiceDraftPending: Bool { voice.isCapturing || voiceDraftSession != nil }
    private var isCalendarStatement: Bool {
        // This only hides irrelevant file options. The Mac owns intent parsing,
        // date extraction and whether a calendar entry is safe to propose.
        let future = draft.range(of: #"明天|后天|下周|\btomorrow\b|\bnext\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"#, options: [.regularExpression, .caseInsensitive]) != nil
        if future && !isReminder && !ReminderResolveValidation.isReminderText(draft) { return true }
        return ["我做了", "我完成了", "我已完成", "帮我安排", "安排", "记入日历", "记到日历", "日历记录", "我计划", "我准备", "我要", "我会", "打算", "i did", "i completed", "i finished", "i have completed", "i have finished", "i will", "i plan to", "schedule"].contains { draft.lowercased().contains($0) }
    }
    private var isTodoStatement: Bool {
        draft.range(of: #"待办|todo|canvas|ddl|deadline|截止|拆分|拆成|分解|子任务|小任务|大任务|项目任务|要做的事|(?:新增|记录|添加|记下).{0,30}(?:任务|项目)"#,
                    options: [.regularExpression, .caseInsensitive]) != nil
    }
    private var isInternetTask:Bool{draft.range(of:#"联网|网上|互联网|天气|下雨|节假日|餐厅|饭店|附近|导航|路线|搜索.*资料|网页|剪贴板|\bweb\b|weather|holiday|internet"#,options:[.regularExpression,.caseInsensitive]) != nil}
    private var isFileTask: Bool { model.selectedDocuments.isEmpty && !isInternetTask && (mode == .search || mode == .organize || (mode == .general && !isTodoStatement && !ReminderResolveValidation.isReminderText(draft) && !isCalendarStatement && (draft.contains("整理") || draft.contains("查找") || draft.contains("检索")))) }
    private var isOrganizeTask: Bool { mode == .organize || draft.contains("整理") }
    private var isSearchTask: Bool { isFileTask && !isOrganizeTask && (mode == .search || draft.contains("查找") || draft.contains("检索")) }
    private var availableRoots: [AuthorizedRoot] { model.roots.filter { !isOrganizeTask || !$0.isReadOnly } }
    private var isReminder: Bool { mode == .reminder }
    private var draftSignature: String? {
        try? PendingSubmission.signature(PendingSubmission.input(text: draft, root: isFileTask ? root : "", query: isSearchTask ? query : "", due: mode.reminderDue(reminderDate)))
    }
    private var visibleTasks: [TaskRecord] {
        let active = model.tasks.filter(\.isActive)
        return Array((active.isEmpty ? model.tasks : active).prefix(3))
    }
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    topLine
                    NoticeBanner(model: model)
                    if !API.isPaired() { pairingNudge }
                    if model.pendingSubmission != nil || model.pendingStorageError != nil { pendingSubmissionCard }
                    composer
                    recentTasks
                }.padding(.horizontal, 22).padding(.top, 12).padding(.bottom, 28)
            }
            .background(OSTheme.background).toolbar(.hidden, for: .navigationBar)
            .scrollDismissesKeyboard(.interactively)
            .refreshable {
                await model.refresh(showError: true)
                await DailyAgendaModel.shared.refresh(tasks: model.tasks)
            }
            .onChange(of: voice.transcriptUpdate) { _, update in
                guard let update else {
                    if !voice.isCapturing { voiceDraftSession = nil }
                    return
                }
                guard voiceDraftSession == update.sessionID else { return }
                draft = update.text
                if update.isFinal { voiceDraftSession = nil }
            }
            .onChange(of: voice.isCapturing) { _, active in
                if !active && voice.transcriptUpdate == nil { voiceDraftSession = nil }
            }
            .onChange(of: isOrganizeTask) { _, _ in
                if !root.isEmpty && !availableRoots.contains(where: { $0.path == root }) { root = "" }
            }
            .onChange(of: scenePhase) { _, phase in if phase == .active { resumeVoiceDraft() } }
            .onAppear { resumeVoiceDraft() }
            .sheet(isPresented:$showingActions){PhoneActionsScreen()}
            .fileImporter(isPresented:$importingDocument,allowedContentTypes:[.plainText,UTType(filenameExtension:"md") ?? .plainText,UTType(filenameExtension:"docx") ?? .data]) { result in
                switch result {
                case .success(let url):Task{uploadingDocument=true;defer{uploadingDocument=false};do{model.selectedDocuments=[try await DocumentClient.shared.upload(url)];mode = .general;root=""}catch{model.notice(error.localizedDescription,error:true)}}
                case .failure(let error):model.notice(error.localizedDescription,error:true)
                }
            }
            .onReceive(NotificationCenter.default.publisher(for: .personalOSVoiceRequested)) { _ in consumeVoiceEntry() }
            .confirmationDialog("丢弃这条重试记录？", isPresented: $confirmingDiscard, titleVisibility: .visible) {
                Button("已核对记录，丢弃", role: .destructive) { model.discardPendingSubmission() }
            } message: { Text("Mac 可能已收到。丢弃不会取消任务，再次提交可能重复。") }
        }
    }
    private var topLine: some View {
        HStack(spacing: 12) {
            Text("PersonalOS").font(.system(size: 29, weight: .semibold, design: .rounded)).tracking(-0.7)
            ConnectionPill(model: model)
            Spacer(minLength: 4)
            Button { model.showSettings() } label: {
                Image(systemName: "gearshape").font(.system(size: 20, weight: .regular)).foregroundStyle(.secondary)
                    .frame(width: 44, height: 44)
            }.buttonStyle(.plain).accessibilityLabel("设置")
        }
    }
    private var pairingNudge: some View {
        Button { model.showSettings() } label: {
            HStack {
                Label("连接 Mac", systemImage: "link")
                Spacer()
                Image(systemName: "chevron.right").font(.caption)
            }.font(.subheadline.weight(.medium)).padding(.vertical, 12)
        }.buttonStyle(.plain).foregroundStyle(OSTheme.accent)
    }
    private var pendingSubmissionCard: some View {
        SurfaceCard {
            VStack(alignment: .leading, spacing: 12) {
                Label(model.pendingSubmission == nil ? "安全存储暂不可用" : "提交尚未确认", systemImage: "exclamationmark.circle").font(.subheadline.weight(.semibold))
                if let pending = model.pendingSubmission {
                    Text(pending.title).font(.subheadline).lineLimit(3)
                    Text("重试会沿用原请求，不重复新建。") .font(.caption).foregroundStyle(.secondary)
                    Button {
                        guard !voiceDraftPending else { return }
                        Task { if await model.retryPendingSubmission(), draftSignature == pending.inputSignature { draft = ""; mode = .general } }
                    } label: { Label(model.busy ? "确认中…" : "重试提交", systemImage: "arrow.clockwise").frame(minHeight: 44) }
                        .buttonStyle(.borderedProminent).disabled(model.busy || voiceDraftPending)
                }
                if let error = model.pendingStorageError {
                    Text(error).font(.caption).foregroundStyle(.orange)
                    Button("重新检查") { model.restorePendingSubmission() }.disabled(model.busy)
                }
                HStack(spacing: 20) {
                    Button("查看记录") { model.selectedTab = 1; Task { await model.refresh(showError: true) } }
                    if model.pendingSubmission != nil {
                        Button("丢弃", role: .destructive) { confirmingDiscard = true }.disabled(model.busy)
                    }
                }.font(.subheadline).frame(minHeight: 44)
            }
        }
    }
    private var composer: some View {
        VStack(alignment: .leading, spacing: 18) {
            Button {
                draftFocused = false
                if voice.recording { voice.finish() }
                else if !voiceDraftPending {
                    let id = UUID(); voiceDraftSession = id
                    Task {
                        await voice.start(sessionID: id)
                        if voiceDraftSession == id, !voice.isCapturing, voice.transcriptUpdate?.sessionID != id {
                            voiceDraftSession = nil
                        }
                    }
                }
            } label: {
                HStack(spacing: 15) {
                    ZStack {
                        Circle().fill(OSTheme.accent.opacity(voice.recording ? 0.18 : 0.08)).frame(width: 64, height: 64)
                        if voice.starting || voice.finishing { ProgressView().tint(OSTheme.accent) }
                        else { Image(systemName: voice.recording ? "stop.fill" : "waveform").font(.system(size: 25, weight: .medium)).foregroundStyle(OSTheme.accent) }
                    }
                    Text(voice.recording ? "正在听" : voice.finishing ? "正在识别…" : voice.starting ? "准备录音…" : "说一句")
                        .font(.system(.title2, design: .rounded).weight(.medium)).foregroundStyle(.primary)
                    Spacer(minLength: 0)
                    if voice.recording { Text("结束").font(.subheadline.weight(.medium)).foregroundStyle(OSTheme.accent) }
                }.frame(minHeight: 68).contentShape(Rectangle())
            }.buttonStyle(.plain).disabled(!voice.recording && (voiceDraftPending || model.busy))
                .accessibilityLabel(voice.recording ? "结束录音" : voice.finishing ? "正在识别" : "开始语音输入")
            Divider().overlay(.primary.opacity(0.02))
            TextField("或输入一句话…", text: Binding(get: { draft }, set: { voiceDraftSession = nil; draft = $0 }), axis: .vertical)
                .font(.body).lineLimit(2...6).focused($draftFocused).padding(.vertical, 2)
                .disabled(voiceDraftPending)
                .accessibilityLabel("任务内容")
            if isFileTask { fileOptions.disabled(voiceDraftPending) }
            if isReminder { reminderOptions.disabled(voiceDraftPending) }
            ForEach(model.selectedDocuments){document in
                HStack{Label(document.name,systemImage:"doc.text").font(.caption).lineLimit(1);Spacer();Button{model.selectedDocuments.removeAll{$0.id==document.id}}label:{Image(systemName:"xmark.circle.fill")}.accessibilityLabel("移除附件")}.foregroundStyle(.secondary)
            }
            if uploadingDocument {ProgressView("正在添加文档").font(.caption)}
            if !voice.error.isEmpty { Text(voice.error).font(.caption).foregroundStyle(.orange) }
            HStack(spacing: 14) {
                toolsMenu
                Spacer(minLength: 0)
                Button { submitDraft() } label: {
                    HStack(spacing: 8) {
                        if model.busy { ProgressView().tint(OSTheme.onInk) }
                        else { Text("发送"); Image(systemName: "arrow.up") }
                    }.font(.subheadline.weight(.semibold)).foregroundStyle(OSTheme.onInk)
                        .frame(minWidth: 90, minHeight: 44).background(OSTheme.ink, in: Capsule())
                }.buttonStyle(.plain)
                    .disabled(model.busy || uploadingDocument || voiceDraftPending || draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .opacity(draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? 0.35 : 1)
                    .accessibilityLabel("发送任务")
            }
        }.padding(22).background(OSTheme.surface, in: RoundedRectangle(cornerRadius: 26, style: .continuous))
    }
    private var toolsMenu: some View {
        Menu {
            Button{showingActions=true}label:{Label("手机与Mac操作",systemImage:"iphone.and.arrow.forward")}
            Button{importingDocument=true}label:{Label("添加文档",systemImage:"paperclip")}.disabled(uploadingDocument || model.busy)
            Button{selectTool(.general,text:"联网搜索 ")}label:{Label("联网搜索",systemImage:"globe")}
            Button { selectTool(.general, text: "今天应该先做什么") } label: { Label("今日计划", systemImage: "sun.max") }
            Button { selectTool(.search, text: "查找 PROJECT 最新资料") } label: { Label("查找资料", systemImage: "doc.text.magnifyingglass") }
            Button { selectTool(.organize, text: "整理选定文件") } label: { Label("整理文件", systemImage: "folder") }
            Button { selectTool(.reminder, text: "明天提醒继续项目") } label: { Label("设置提醒", systemImage: "bell") }
            if mode != .general { Divider(); Button("普通输入") { mode = .general } }
        } label: {
            Label(mode == .general ? "工具" : mode.rawValue, systemImage: "plus.circle")
                .font(.subheadline).foregroundStyle(OSTheme.accent).frame(minHeight: 44)
        }.disabled(voiceDraftPending).accessibilityLabel("任务工具")
    }
    private func selectTool(_ newMode: ComposerMode, text: String) {
        guard !voiceDraftPending else { return }
        voiceDraftSession = nil; voice.stop(); draft = text; mode = newMode
        if newMode == .search {
            query = "PROJECT"
            if let project = model.roots.first(where: { $0.path == "project://air" }) { root = project.path }
        }
        if newMode == .organize && !model.roots.contains(where: { $0.path == root && !$0.isReadOnly }) { root = "" }
    }
    private func submitDraft() {
        guard !voiceDraftPending else { return }
        voiceDraftSession = nil; draftFocused = false; voice.stop()
        let submittedSignature = draftSignature
        Task {
            if isFileTask && !availableRoots.contains(where: { $0.path == root }) {
                model.notice(isOrganizeTask ? "请选择可整理目录" : "请选择资料来源", error: true); return
            }
            if await model.submit(text: draft, root: isFileTask ? root : "", query: isSearchTask ? query : "", due: mode.reminderDue(reminderDate)) {
                if draftSignature == submittedSignature { draft = ""; mode = .general }
            }
        }
    }
    private var fileOptions: some View {
        VStack(alignment: .leading, spacing: 10) {
            Divider()
            if availableRoots.isEmpty {
                Text(isOrganizeTask ? "暂无可整理目录" : "暂无资料来源").font(.subheadline)
                Button("查看授权目录") { model.showSettings() }.font(.subheadline)
            } else {
                Picker(isOrganizeTask ? "整理目录" : "资料来源", selection: $root) {
                    Text("请选择").tag("")
                    ForEach(availableRoots) { item in Text(item.name).tag(item.path) }
                }.pickerStyle(.menu)
                if let selected = availableRoots.first(where: { $0.path == root }) {
                    Text(selected.path == "project://air" ? "MacBook Air · PROJECT（只读）" : selected.path)
                        .font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                }
            }
            if isSearchTask { TextField("关键词", text: $query).textFieldStyle(.roundedBorder).accessibilityLabel("检索关键词") }
            if isOrganizeTask { Text("先预览清单，确认后移动。") .font(.caption).foregroundStyle(.secondary) }
        }
    }
    private var reminderOptions: some View {
        VStack(alignment: .leading, spacing: 10) {
            Divider()
            DatePicker("提醒时间", selection: $reminderDate, in: Date()..., displayedComponents: [.date, .hourAndMinute]).font(.subheadline)
            if !features.notificationsEnabled {
                Button("开启手机通知") { Task { await features.enableNotifications() } }.font(.subheadline)
            }
        }
    }
    private var recentTasks: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(model.activeCount > 0 ? "当前" : "最近").font(.subheadline.weight(.semibold)).foregroundStyle(.secondary)
                Spacer()
                Button("全部") { model.selectedTab = 1 }.font(.subheadline).frame(minHeight: 36)
            }
            if visibleTasks.isEmpty {
                Text("暂无记录").font(.subheadline).foregroundStyle(.tertiary).padding(.vertical, 22)
            } else {
                ForEach(visibleTasks) { task in
                    Button { model.openTask(task.id) } label: { TaskSummaryCard(task: task) }.buttonStyle(.plain)
                    if task.id != visibleTasks.last?.id { Divider() }
                }
            }
        }
    }
    private func resumeVoiceDraft() {
        if !voice.isCapturing && voice.transcriptUpdate == nil { voiceDraftSession = nil }
        consumeVoiceEntry()
    }
    private func consumeVoiceEntry() {
        guard UserDefaults.standard.bool(forKey: "personalOS.voiceRequested") else { return }
        UserDefaults.standard.removeObject(forKey: "personalOS.voiceRequested")
        voiceDraftSession = nil; voice.stop()
        model.showVoiceHome(); mode = .general; draftFocused = false
        model.notice("点“说一句”开始录音")
    }
}

private struct TaskSummaryCard: View {
    let task: TaskRecord
    var body: some View {
        HStack(alignment: .top, spacing: 13) {
            Image(systemName: task.symbol).font(.system(size: 19, weight: .regular)).foregroundStyle(task.stateColor)
                .frame(width: 24).padding(.top, 2)
            VStack(alignment: .leading, spacing: 6) {
                Text(task.calendarEntry?.title ?? task.title).font(.subheadline.weight(.semibold)).foregroundStyle(.primary).lineLimit(2)
                Text(task.preview).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                HStack(spacing: 8) {
                    Text(task.stateTitle).foregroundStyle(task.stateColor)
                    if let date = task.date { Text(date, format: .dateTime.month().day().hour().minute()).foregroundStyle(.tertiary) }
                }.font(.caption2)
            }.frame(maxWidth: .infinity, alignment: .leading)
            Image(systemName: "chevron.right").font(.caption2.weight(.medium)).foregroundStyle(.tertiary).padding(.top, 4)
        }.padding(.vertical, 16).contentShape(Rectangle()).accessibilityElement(children: .combine)
    }
}
@MainActor private struct TaskLibrary: View {
    @ObservedObject var model: AssistantModel
    @State private var filter = "全部"
    @State private var search = ""
    private let filters = ["全部", "进行中", "待确认", "待补充", "已处理"]
    private var filtered: [TaskRecord] {
        model.tasks.filter { task in
            let matchesFilter = filter == "全部" || (filter == "进行中" && task.isActive) || (filter == "待确认" && task.state == "awaiting_confirmation") || (filter == "待补充" && task.state == "needs_details") || (filter == "已处理" && task.isFinished)
            return matchesFilter && (search.isEmpty || task.title.localizedCaseInsensitiveContains(search) || task.result.localizedCaseInsensitiveContains(search))
        }
    }
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 6) {
                            ForEach(filters, id: \.self) { option in
                                Button { filter = option } label: {
                                    Text(option).font(.caption.weight(.semibold)).padding(.horizontal, 13).frame(minHeight: 40)
                                        .foregroundStyle(filter == option ? OSTheme.accent : .secondary)
                                        .background(filter == option ? OSTheme.accent.opacity(0.09) : .clear, in: Capsule())
                                }.buttonStyle(.plain).accessibilityAddTraits(filter == option ? .isSelected : [])
                            }
                        }
                    }
                    NoticeBanner(model: model)
                    if filtered.isEmpty {
                        Text(search.isEmpty ? "暂无记录" : "没有匹配记录").font(.subheadline).foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity).padding(.vertical, 48)
                    } else {
                        LazyVStack(spacing: 0) {
                            ForEach(filtered) { task in
                                Button { model.openTask(task.id) } label: { TaskSummaryCard(task: task) }.buttonStyle(.plain)
                                if task.id != filtered.last?.id { Divider() }
                            }
                        }
                    }
                }.padding(.horizontal, 22).padding(.bottom, 24)
            }.background(OSTheme.background).navigationTitle("记录")
                .searchable(text: $search, prompt: "搜索记录")
                .refreshable { await model.refresh(showError: true) }
                .toolbar {
                    ToolbarItemGroup(placement: .topBarTrailing) {
                        Button { Task { await model.refresh(showError: true) } } label: { Image(systemName: "arrow.clockwise") }.accessibilityLabel("刷新记录")
                        Button { model.showSettings() } label: { Image(systemName: "gearshape") }.accessibilityLabel("设置")
                    }
                }
        }
    }
}

@MainActor private struct TaskDetailScreen: View {
    let id: String
    @ObservedObject var model: AssistantModel
    @ObservedObject var voice: Voice
    @ObservedObject private var journal = CalendarJournal.shared
    @Environment(\.dismiss) private var dismiss
    @State private var action: String?
    @State private var audit: [AuditRecord] = []
    @State private var auditError: String?
    @State private var loadingAudit = false
    @State private var showCalendarEditor = false
    @State private var reminderToEdit: TaskRecord?
    private var task: TaskRecord? { model.tasks.first { $0.id == id } }
    private var calendarEntry: CalendarJournalEntry? { journal.entries.first { $0.id == id } ?? task?.calendarEntry }
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    if let task {
                        header(task)
                        NoticeBanner(model: model)
                        if calendarEntry == nil { resultCard(task) }
                        if let data = task.plan.data(using:.utf8), let plan = try? JSONSerialization.jsonObject(with:data) as? [String:Any], let exercise = plan["exercise_id"] as? String, ["lateral_raise","wall_pushup","sit_to_stand"].contains(exercise) {
                            NavigationLink("播放动作示意"){ExerciseDemoView(kind:exercise)}.buttonStyle(.borderedProminent)
                        }
                        if let entry = calendarEntry { calendarEntryCard(entry) }
                        if task.macPlan != nil{MacActionCard(task:task){action=$0}}
                        if let plan = task.filePlan { filePlanCard(plan, task: task) }
                        actions(task)
                        auditCard
                    } else {
                        SurfaceCard { EmptyState(symbol: "clock.arrow.circlepath", title: "正在读取这件事", detail: "如果这是较早的通知，任务可能已不在最近记录中。") }
                    }
                }.padding(20)
            }.background(OSTheme.background).navigationTitle("任务详情").navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } }
                    ToolbarItem(placement: .topBarTrailing) {
                        if let task, let entry = calendarEntry {
                            Menu {
                                Button { voice.speak(task.preview) } label: { Label("朗读", systemImage: "speaker.wave.2") }
                                ShareLink(item: entry.title + "\n\n" + task.preview) { Label("分享", systemImage: "square.and.arrow.up") }
                            } label: { Image(systemName: "ellipsis").frame(minWidth: 44, minHeight: 44) }
                                .accessibilityLabel("记录选项")
                        }
                    }
                }
                .sheet(isPresented: $showCalendarEditor) {
                    if let entry = calendarEntry { CalendarEntryEditor(entry: entry, model: model) }
                }
                .sheet(item: $reminderToEdit) { task in
                    ReminderTimeEditor(task: task, model: model)
                }
                .confirmationDialog(actionTitle, isPresented: Binding(get: { action != nil }, set: { if !$0 { action = nil } }), titleVisibility: .visible) {
                    if let action, let task {
                        Button(action == "approve" ? (task.macPlan != nil ? "确认执行" : "确认移动") : action == "undo" ? (task.macPlan != nil ? "撤销写回" : "撤销整理") : action == "calendar_cancel" ? "撤销这条日历记录" : "取消任务", role: action == "cancel" || action == "calendar_cancel" ? .destructive : nil) {
                            self.action = nil
                            Task {
                                if action == "calendar_cancel", let entry = calendarEntry { await journal.cancel(entry: entry); await model.refresh() }
                                else { await model.act(task, action: action) }
                            }
                        }
                    }
                } message: { Text(confirmationMessage) }
        }
    }
    private var actionTitle: String { if task?.macPlan != nil{return action=="undo" ? "恢复写回前的原件？" : "在Mac执行这项操作？"};return action == "approve" ? "按这份清单整理文件？" : action == "undo" ? "恢复整理前的位置？" : action == "calendar_cancel" ? "撤销这条 PersonalOS 日历记录？" : "取消这件事？" }
    private var confirmationMessage: String {
        if task?.macPlan != nil{return action=="undo" ? "原文件有新的修改时会停止；原版备份保留。" : "以已展示的操作清单为准，确认后在Mac执行。"}
        switch action {
        case "approve": return "移动清单中的 \(task?.filePlan?.moves.count ?? 0) 个文件；遇到变化或同名冲突会停止。"
        case "undo": return "恢复这次整理的文件到原位置；文件已改变时停止。"
        case "calendar_cancel": return "撤销“\(calendarEntry?.title ?? "这条记录")”。手动修改过的事件会保留。"
        default: return "取消“\(task?.title ?? "这条任务")”，保留历史记录。"
        }
    }
    private func calendarEntryCard(_ entry: CalendarJournalEntry) -> some View {
        SurfaceCard {
            VStack(alignment: .leading, spacing: 14) {
                HStack(alignment: .top) {
                    Label("Apple 日历 · PersonalOS", systemImage: "calendar").font(.headline)
                    Spacer()
                    Text(calendarStatus(entry)).font(.caption.weight(.semibold))
                        .foregroundStyle(entry.device_sync == "written" ? OSTheme.accent : .secondary)
                }
                Label(entry.kind == "done" ? "已经做过" : "接下来安排", systemImage: entry.kind == "done" ? "checkmark.circle" : "clock").font(.caption).foregroundStyle(.secondary)
                if let start = entry.startDate, let end = entry.endDate {
                    Text(calendarRange(start: start, end: end, entry: entry)).font(.subheadline).foregroundStyle(.secondary)
                    Text((TimeZone(identifier: entry.time_zone)?.localizedName(for: .generic, locale: Locale(identifier: "zh_Hans_CN")) ?? entry.time_zone) + (entry.all_day ? " · 全天" : "")).font(.caption).foregroundStyle(.secondary)
                }
                if let question = entry.question, !question.isEmpty { Text(question).font(.subheadline).foregroundStyle(.orange) }
                if let error = entry.sync_error, !error.isEmpty { Text(error).font(.caption).foregroundStyle(.orange) }
                if let error = journal.error { Text(error).font(.caption).foregroundStyle(.orange) }
                if entry.status == "needs_details" || entry.status == "ready" {
                    Button { showCalendarEditor = true } label: {
                        Label(entry.status == "needs_details" ? "补充" : "编辑", systemImage: "pencil").frame(minHeight: 44)
                    }.buttonStyle(.bordered).disabled(journal.syncing)
                }
                if !journal.enabled && entry.status != "cancelled" {
                    Button("开启日历记录") { model.showSettings() }.font(.subheadline).frame(minHeight: 44)
                } else if entry.status == "ready" && (entry.device_sync == "pending" || entry.device_sync == "error") {
                    Button { Task { await journal.refreshAndSync(); await model.refresh() } } label: {
                        if journal.syncing { ProgressView() } else { Label("重新同步", systemImage: "arrow.clockwise") }
                    }.disabled(journal.syncing)
                }
                if entry.status != "cancelled" || entry.device_sync != "removed" {
                    Button(entry.status == "cancelled" ? "重试撤销" : "撤销记录", role: .destructive) { action = "calendar_cancel" }.font(.subheadline).frame(minHeight: 44).disabled(journal.syncing)
                }
                DisclosureGroup("原话") {
                    Text(entry.original_text).font(.subheadline).foregroundStyle(.secondary).textSelection(.enabled)
                }.font(.subheadline)
            }
        }
    }
    private func calendarStatus(_ entry: CalendarJournalEntry) -> String {
        if entry.status == "needs_details" { return "待补充" }
        if entry.device_sync == "error" { return (entry.sync_error ?? "").contains("冲突") ? "冲突" : "同步需检查" }
        if entry.status == "cancelled" { return entry.device_sync == "removed" ? "已撤销" : "待撤销同步" }
        return entry.device_sync == "written" ? "已写入" : "待写入"
    }
    private func calendarRange(start: Date, end: Date, entry: CalendarJournalEntry) -> String {
        let formatter = DateFormatter(); formatter.locale = Locale(identifier: "zh_CN")
        formatter.timeZone = TimeZone(identifier: entry.time_zone) ?? .current
        formatter.dateStyle = .medium; formatter.timeStyle = entry.all_day ? .none : .short
        var calendar = Calendar(identifier: .gregorian); calendar.timeZone = formatter.timeZone
        let displayEnd = entry.all_day ? (calendar.date(byAdding: .day, value: -1, to: end) ?? end) : end
        if calendar.isDate(start, inSameDayAs: displayEnd) {
            let first = formatter.string(from: start)
            if entry.all_day { return first }
            formatter.dateStyle = .none
            return first + " — " + formatter.string(from: end)
        }
        return formatter.string(from: start) + " — " + formatter.string(from: displayEnd)
    }
    private func header(_ task: TaskRecord) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            StatusPill(task: task)
            Text(calendarEntry?.title ?? task.title).font(.system(.title2, design: .rounded).weight(.bold)).textSelection(.enabled)
            if let date = task.date { Text(date, format: .dateTime.year().month().day().hour().minute()).font(.caption).foregroundStyle(.secondary) }
        }
    }
    private func resultCard(_ task: TaskRecord) -> some View {
        SurfaceCard {
            VStack(alignment: .leading, spacing: 16) {
                Label(task.isActive ? "进展" : "结果", systemImage: task.isActive ? "waveform.path" : "text.bubble").font(.headline)
                if !task.isActive && !task.result.isEmpty {Button{PersonalOSClipboard.copy(task.result);model.notice("已复制到手机剪贴板")}label:{Label("复制",systemImage:"doc.on.doc")}.font(.subheadline)}
                ForEach(task.documentVersions){document in DocumentResultRow(document:document)}
                if let phone=task.phonePlan{PhoneActionResult(plan:phone)}
                if task.state == "queued" || task.state == "running" {
                    HStack(spacing: 12) { ProgressView(); Text(task.preview).font(.subheadline).foregroundStyle(.secondary) }
                } else if let result = task.deviceResult {
                    Label("Mac 已返回设备信息", systemImage: "desktopcomputer").font(.headline)
                    HStack(alignment: .firstTextBaseline) { Text(result.free_disk_gb.formatted()).font(.system(.largeTitle, design: .rounded).weight(.bold)); Text("GB 可用空间").font(.subheadline).foregroundStyle(.secondary) }
                    Text(result.platform.replacingOccurrences(of: "-", with: " · ")).font(.caption).foregroundStyle(.secondary)
                } else if let result = task.searchResult {
                    searchResults(result)
                } else {
                    Text(task.preview).font(.body).lineSpacing(5).textSelection(.enabled)
                }
                if task.needsReminderDetails {
                    Button { reminderToEdit = task } label: {
                        Label("补充时间", systemImage: "clock.badge.questionmark")
                            .foregroundStyle(OSTheme.onInk)
                            .frame(maxWidth: .infinity, minHeight: 44)
                    }.buttonStyle(.borderedProminent).disabled(model.busy)
                }
                if task.state == "scheduled", let due = task.due {
                    Label(Date(timeIntervalSince1970: due).formatted(date: .complete, time: .shortened), systemImage: "calendar").font(.subheadline)
                }
                if !task.result.isEmpty && !task.isActive {
                    Divider()
                    HStack(spacing: 18) {
                        Button { voice.speak(task.preview) } label: { Label("朗读", systemImage: "speaker.wave.2") }
                        ShareLink(item: task.title + "\n\n" + task.preview) { Label("分享", systemImage: "square.and.arrow.up") }
                    }.font(.subheadline.weight(.medium)).frame(minHeight: 44)
                }
            }
        }
    }
    private func searchResults(_ result: SearchResult) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(result.hits.isEmpty ? "这个范围内没有找到匹配资料" : "找到 \(result.hits.count) 份资料").font(.subheadline.weight(.semibold))
            ForEach(result.hits) { hit in
                VStack(alignment: .leading, spacing: 6) {
                    Label(URL(fileURLWithPath: hit.path).lastPathComponent, systemImage: "doc.text").font(.subheadline.weight(.semibold))
                    Text(hit.path).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                    if let modified = hit.modified { Text("更新于 " + Date(timeIntervalSince1970: modified).formatted(date: .abbreviated, time: .shortened)).font(.caption2).foregroundStyle(.secondary) }
                    if let snippet = hit.snippet, !snippet.isEmpty { Text(snippet).font(.subheadline).lineLimit(6).textSelection(.enabled) }
                }.padding(.vertical, 6)
                if hit.id != result.hits.last?.id { Divider() }
            }
            if let scope = result.scope { Text(scope).font(.caption).foregroundStyle(.secondary) }
            if result.truncated == true { Label("结果较多，试试更具体的关键词", systemImage: "info.circle").font(.caption).foregroundStyle(.secondary) }
        }
    }
    private func filePlanCard(_ plan: FilePlan, task: TaskRecord) -> some View {
        SurfaceCard {
            VStack(alignment: .leading, spacing: 16) {
                HStack { Label("整理清单", systemImage: "folder").font(.headline); Spacer(); Text("\(plan.moves.count) 个文件").font(.caption).foregroundStyle(.secondary) }
                if plan.moves.isEmpty { Text("这个目录没有需要移动的文件。") .font(.subheadline).foregroundStyle(.secondary) }
                ForEach(plan.moves) { move in
                    VStack(alignment: .leading, spacing: 7) {
                        Text(URL(fileURLWithPath: move.src).lastPathComponent).font(.subheadline.weight(.semibold))
                        Label(relative(move.src, root: plan.root), systemImage: "doc").font(.caption).foregroundStyle(.secondary)
                        Label(relative(move.dst, root: plan.root), systemImage: "arrow.turn.down.right").font(.caption).foregroundStyle(OSTheme.accent)
                    }.textSelection(.enabled)
                    if move.id != plan.moves.last?.id { Divider() }
                }
                Text("仅处理 Mac 已授权的目录；保留原路径与撤销记录。") .font(.caption).foregroundStyle(.secondary)
            }
        }
    }
    private func relative(_ path: String, root: String) -> String { path.hasPrefix(root + "/") ? String(path.dropFirst(root.count + 1)) : path }
    private func actions(_ task: TaskRecord) -> some View {
        VStack(spacing: 12) {
            if task.state == "awaiting_confirmation", !(task.filePlan?.moves.isEmpty ?? true) {
                Button { action = "approve" } label: { Label("确认清单并整理", systemImage: "checkmark.shield").frame(maxWidth: .infinity, minHeight: 44) }
                    .buttonStyle(.borderedProminent)
            }
            if task.state == "completed", task.filePlan != nil {
                Button { action = "undo" } label: { Label("撤销这次整理", systemImage: "arrow.uturn.backward").frame(maxWidth: .infinity, minHeight: 44) }.buttonStyle(.bordered)
            }
            if task.canCancel {
                Button("取消任务", role: .destructive) { action = "cancel" }.frame(minHeight: 44)
            }
        }.disabled(model.busy)
    }
    private var auditCard: some View {
        SurfaceCard {
            DisclosureGroup {
                VStack(alignment: .leading, spacing: 14) {
                    if audit.isEmpty {
                        Button { loadAudit() } label: { if loadingAudit { ProgressView() } else { Label("读取执行记录", systemImage: "arrow.clockwise") } }.disabled(loadingAudit)
                    }
                    if let auditError { Text(auditError).font(.caption).foregroundStyle(.orange) }
                    ForEach(audit) { event in
                        HStack(alignment: .top, spacing: 12) {
                            Image(systemName: "checkmark.circle").foregroundStyle(OSTheme.accent)
                            VStack(alignment: .leading, spacing: 3) {
                                Text(auditTitle(event.event)).font(.subheadline.weight(.medium))
                                Text(Date(timeIntervalSince1970: event.at), format: .dateTime.hour().minute().second()).font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                }.padding(.top, 16)
            } label: { Label("执行记录", systemImage: "list.bullet.rectangle").font(.subheadline.weight(.semibold)) }
        }
    }
    private func loadAudit() {
        loadingAudit = true
        Task {
            defer { loadingAudit = false }
            do { audit = try JSONDecoder().decode([AuditRecord].self, from: await API.request("/tasks/\(id)/audit")) }
            catch { auditError = error.localizedDescription }
        }
    }
    private func auditTitle(_ event: String) -> String {
        ["submitted": "Mac 已收到任务", "planning": "开始处理", "local_model": "本地模型完成规划", "completed": "任务完成", "failed": "执行未完成", "preview": "已生成整理清单", "approve": "你已确认执行", "move_intent": "准备移动文件", "move": "文件已移动", "undo_move": "文件已恢复", "undo": "你已请求撤销", "cancel": "你已取消任务", "reminder_scheduled": "提醒已安排", "reminder_resolved": "你已补充提醒时间", "reminder_due": "已到提醒时间", "action_failed": "操作需要检查", "calendar_proposed": "已整理日历事项", "calendar_resolved": "你已补充日历时间", "calendar_cancelled": "你已请求撤销日历记录", "calendar_sync_ack": "手机已回传日历同步状态"][event] ?? event.replacingOccurrences(of: "_", with: " ")
    }
}

enum ReminderResolveValidation {
    static func isReminderText(_ text: String) -> Bool {
        text.range(of: #"提醒|\bremind(?:er|ers|ing|ed|s)?\b"#, options: [.regularExpression, .caseInsensitive]) != nil
    }
    static func matches(_ saved: TaskRecord, expectedID: String, targetDue: Date) -> Bool {
        guard saved.id == expectedID, saved.state == "scheduled", saved.calendarEntry == nil,
              let due = saved.due, due.isFinite else { return false }
        return abs(due - targetDue.timeIntervalSince1970) < 1
    }
}

@MainActor private struct ReminderTimeEditor: View {
    let task: TaskRecord
    @ObservedObject var model: AssistantModel
    @Environment(\.dismiss) private var dismiss
    @State private var due: Date
    @State private var timeSelected = false
    @State private var saving = false
    @State private var error: String?
    @State private var entryEndpoint = try? API.endpoint()
    private let timeZone: TimeZone

    init(task: TaskRecord, model: AssistantModel) {
        self.task = task; self.model = model
        timeZone = .current
        // An initial picker value is not a chosen reminder time. Save stays
        // disabled until the user explicitly changes the date or time.
        let nextMinute = (floor(Date().timeIntervalSince1970 / 60) + 1) * 60
        _due = State(initialValue: Date(timeIntervalSince1970: nextMinute))
    }
    private var canSave: Bool { timeSelected && due > Date() }
    var body: some View {
        NavigationStack {
            Form {
                Section("提醒事项") {
                    Text(task.title).font(.body).textSelection(.enabled)
                }
                Section {
                    DatePicker("日期和时间", selection: $due, in: Date()..., displayedComponents: [.date, .hourAndMinute])
                        .datePickerStyle(.graphical)
                        .onChange(of: due) { _, _ in timeSelected = true; error = nil }
                    if timeSelected {
                        Text(due, format: .dateTime.year().month().day().hour().minute())
                            .font(.subheadline.weight(.medium)).foregroundStyle(OSTheme.accent)
                    } else {
                        Text("请选择提醒日期和时间").font(.subheadline).foregroundStyle(.secondary)
                    }
                } header: { Text("提醒时间") } footer: {
                    Text(timeZone.localizedName(for: .generic, locale: Locale(identifier: "zh_Hans_CN")) ?? timeZone.identifier)
                }
                if let error { Section { Text(error).font(.subheadline).foregroundStyle(.orange).textSelection(.enabled) } }
            }
            .disabled(saving)
            .environment(\.timeZone, timeZone)
            .navigationTitle("补充提醒时间").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() }.disabled(saving) }
                ToolbarItem(placement: .confirmationAction) {
                    Button(saving ? "保存中…" : error == nil ? "保存" : "重试") { save() }
                        .disabled(!canSave || saving || model.busy)
                }
            }
            .interactiveDismissDisabled(saving)
        }
    }
    private func save() {
        guard canSave, !saving, !model.busy else { return }
        guard let endpoint = entryEndpoint, (try? API.endpoint()) == endpoint else {
            error = "Mac 连接已改变，请关闭此页并重新打开对应任务。"; return
        }
        let selectedDue = due, selectedTimeZone = timeZone.identifier
        saving = true; error = nil
        Task {
            defer { saving = false }
            do {
                try await model.resolveReminder(task, due: selectedDue, timeZone: selectedTimeZone, endpoint: endpoint)
                dismiss()
            } catch {
                self.error = "尚未确认保存：" + error.localizedDescription
            }
        }
    }
}

enum CalendarEditValidation {
    static func matches(_ saved: CalendarJournalEntry, original: CalendarJournalEntry,
                        title: String, start: Date, end: Date, allDay: Bool, kind: String) -> Bool {
        guard saved.id == original.id, saved.revision > original.revision, saved.status == "ready",
              saved.title == title, saved.kind == kind, saved.all_day == allDay,
              saved.time_zone == original.time_zone, let savedStart = saved.startDate,
              let savedEnd = saved.endDate else { return false }
        // The wire format has second precision; DatePicker can retain subsecond values.
        return abs(savedStart.timeIntervalSince(start)) < 1 && abs(savedEnd.timeIntervalSince(end)) < 1
    }
}

@MainActor private struct CalendarEntryEditor: View {
    let entry: CalendarJournalEntry
    @ObservedObject var model: AssistantModel
    @ObservedObject private var journal = CalendarJournal.shared
    @Environment(\.dismiss) private var dismiss
    @State private var title: String
    @State private var kind: String
    @State private var start: Date
    @State private var end: Date
    @State private var allDay: Bool
    @State private var startConfirmed: Bool
    @State private var endConfirmed: Bool
    @State private var saving = false
    @State private var error: String?
    @State private var entryEndpoint = try? API.endpoint()

    init(entry: CalendarJournalEntry, model: AssistantModel) {
        self.entry = entry; self.model = model
        _title = State(initialValue: entry.title); _kind = State(initialValue: entry.kind)
        _allDay = State(initialValue: entry.all_day)
        let initial = Date()
        var calendar = Calendar(identifier: .gregorian); calendar.timeZone = TimeZone(identifier: entry.time_zone) ?? .current
        _start = State(initialValue: entry.startDate ?? initial)
        let displayEnd = entry.all_day ? entry.endDate.flatMap { calendar.date(byAdding: .day, value: -1, to: $0) } : entry.endDate
        _end = State(initialValue: displayEnd ?? entry.startDate ?? initial)
        _startConfirmed = State(initialValue: entry.startDate != nil)
        _endConfirmed = State(initialValue: entry.endDate != nil)
    }
    private var calendar: Calendar {
        var value = Calendar(identifier: .gregorian); value.timeZone = TimeZone(identifier: entry.time_zone) ?? .current
        return value
    }
    private var resolvedStart: Date { allDay ? calendar.startOfDay(for: start) : start }
    private var resolvedEnd: Date {
        allDay ? (calendar.date(byAdding: .day, value: 1, to: calendar.startOfDay(for: end)) ?? end) : end
    }
    private var valid: Bool { !title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && startConfirmed && endConfirmed && resolvedEnd > resolvedStart }
    private var editingExisting: Bool { entry.status == "ready" }
    private var hasChanges: Bool {
        guard let originalStart = entry.startDate, let originalEnd = entry.endDate else { return true }
        return title.trimmingCharacters(in: .whitespacesAndNewlines) != entry.title || kind != entry.kind || allDay != entry.all_day ||
            abs(originalStart.timeIntervalSince(resolvedStart)) >= 1 || abs(originalEnd.timeIntervalSince(resolvedEnd)) >= 1
    }
    private var canSave: Bool { valid && (!editingExisting || hasChanges) }
    var body: some View {
        NavigationStack {
            Form {
                Section("这件事") {
                    TextField("标题", text: $title, axis: .vertical).lineLimit(2...4)
                    Picker("记录类型", selection: $kind) {
                        Text("已经做过").tag("done")
                        Text("接下来安排").tag("plan")
                    }
                    if let question = entry.question, !question.isEmpty { Text(question).font(.caption).foregroundStyle(.secondary) }
                }
                Section {
                    Toggle("全天", isOn: $allDay)
                    DatePicker(allDay ? "开始日期" : "开始时间", selection: $start, displayedComponents: allDay ? [.date] : [.date, .hourAndMinute])
                        .onChange(of: start) { _, _ in startConfirmed = true }
                    if !editingExisting || !startConfirmed {
                        Toggle(allDay ? "确认开始日期" : "确认开始时间", isOn: $startConfirmed)
                    }
                    DatePicker(allDay ? "最后一天（含当天）" : "结束时间", selection: $end, displayedComponents: allDay ? [.date] : [.date, .hourAndMinute])
                        .onChange(of: end) { _, _ in endConfirmed = true }
                    if !editingExisting || !endConfirmed {
                        Toggle(allDay ? "确认最后一天" : "确认结束时间", isOn: $endConfirmed)
                    }
                    if startConfirmed && endConfirmed && resolvedEnd <= resolvedStart {
                        Text("结束必须晚于开始，请确认完整时间范围。") .font(.caption).foregroundStyle(.orange)
                    }
                } header: { Text("时间") } footer: {
                    Text((TimeZone(identifier: entry.time_zone)?.localizedName(for: .generic, locale: Locale(identifier: "zh_Hans_CN")) ?? entry.time_zone) + (allDay ? " · 包含结束日期" : ""))
                }
                if let error { Section { Text(error).font(.subheadline).foregroundStyle(.orange) } }
                Section {
                    Text(journal.enabled ? "保存后写入独立 PersonalOS 日历。" : "保存后保留为待写入；在设置开启日历记录后同步。") .font(.caption).foregroundStyle(.secondary)
                }
            }
            .disabled(saving)
            .environment(\.timeZone, calendar.timeZone)
            .navigationTitle(editingExisting ? "编辑日历记录" : "补充日历记录").navigationBarTitleDisplayMode(.inline)
            .onChange(of: allDay) { _, _ in startConfirmed = false; endConfirmed = false }
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() }.disabled(saving) }
                ToolbarItem(placement: .confirmationAction) {
                    Button(saving ? "保存中…" : "保存") { save() }.disabled(!canSave || saving || journal.syncing)
                }
            }
            .interactiveDismissDisabled(saving)
        }
    }
    private func save() {
        guard canSave, !saving else { return }
        guard entryEndpoint != nil, (try? API.endpoint()) == entryEndpoint else {
            error = "私人 Mac 连接已改变，请关闭此页并重新打开对应任务。"; return
        }
        saving = true; error = nil
        let savedTitle = title.trimmingCharacters(in: .whitespacesAndNewlines)
        let savedStart = resolvedStart, savedEnd = resolvedEnd, savedAllDay = allDay, savedKind = kind
        Task {
            defer { saving = false }
            await journal.resolve(entry: entry, title: savedTitle, start: savedStart, end: savedEnd,
                                  timeZone: entry.time_zone, allDay: savedAllDay, kind: savedKind)
            guard journal.entries.contains(where: {
                CalendarEditValidation.matches($0, original: entry, title: savedTitle,
                    start: savedStart, end: savedEnd, allDay: savedAllDay, kind: savedKind)
            }) else {
                error = journal.error ?? "尚未确认保存，请检查连接后重试。"; return
            }
            let syncError = journal.error
            await model.refresh()
            if let syncError { model.notice("记录已保存，日历同步待重试：" + syncError, error: true) }
            dismiss()
        }
    }
}

@MainActor private struct SettingsScreen: View {
    @ObservedObject var model: AssistantModel
    @ObservedObject var features: CompanionFeatures
    @ObservedObject private var journal = CalendarJournal.shared
    @ObservedObject private var weather = WeatherLocation.shared
    @ObservedObject private var planner = AutoScheduleModel.shared
    @Environment(\.dismiss) private var dismiss
    @AppStorage("server") private var server = API.defaultServer
    @AppStorage("personalOS.autoSpeak") private var autoSpeak = false
    @State private var code = ""
    @State private var testingNotification = false
    var body: some View {
        NavigationStack {
            Form {
                if model.message != nil { Section { NoticeBanner(model: model).listRowInsets(EdgeInsets()) } }
                Section("连接") {
                    HStack { Label("Mac", systemImage: "desktopcomputer"); Spacer(); ConnectionPill(model: model) }
                    if API.isPaired() {
                        DisclosureGroup("重新配对") { pairingFields }
                    } else { pairingFields }
                    DisclosureGroup("连接详情") {
                        TextField("Tailscale HTTPS 地址", text: $server)
                            .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled().font(.footnote)
                        if let version = model.status?.version { LabeledContent("服务版本", value: version).font(.caption) }
                        if let name = model.status?.model { LabeledContent("本地模型", value: name).font(.caption) }
                    }
                }
                Section("日历") {
                    Toggle("自动记入日历", isOn: Binding(get: { journal.enabled }, set: { enabled in
                        if enabled { Task { await journal.enable() } } else { journal.disable() }
                    }))
                    if let error = journal.error { Text(error).font(.caption).foregroundStyle(.orange) }
                    DisclosureGroup("同步与来源") {
                        Text(journal.summary).font(.caption).foregroundStyle(.secondary)
                        Text("关闭自动记录会保留已有事件。") .font(.caption).foregroundStyle(.secondary)
                        if journal.enabled {
                            Button { Task { await journal.refreshAndSync() } } label: {
                                if journal.syncing { ProgressView() } else { Label("同步日历", systemImage: "arrow.clockwise") }
                            }.disabled(journal.syncing)
                        }
                    }
                    Toggle("共享今天日历", isOn: Binding(get: { features.calendarSharingEnabled }, set: { enabled in
                        Task { await features.setCalendarSharingEnabled(enabled) }
                    }))
                    DisclosureGroup("共享范围") {
                        Text("仅将今天最多20个日程的标题和时间交给私人 Mac 规划。") .font(.caption).foregroundStyle(.secondary)
                        Text(features.calendarSummary).font(.caption).foregroundStyle(.secondary)
                    }
                }
                Section("排程") {
                    Toggle("自动安排待办",isOn:Binding(get:{planner.status?.settings.enabled ?? false},set:{value in Task{await planner.enabled(value)}}))
                    Text("11:00–次日01:00 · 午晚餐各45分钟").font(.caption).foregroundStyle(.secondary)
                    Text("仅将未来8天的占用时段交给私人 Mac 排程，不含日程标题。").font(.caption).foregroundStyle(.secondary)
                }
                Section { NavigationLink { HealthFitnessView() } label: { Label("健康与训练", systemImage:"heart.text.clipboard") } }
                Section("天气") {
                    Toggle("使用手机所在地区",isOn:Binding(get:{weather.enabled},set:{value in if value{weather.enable()}else{weather.disable()}}))
                    if weather.enabled {Button(weather.busy ? "正在定位…" : "更新大致位置"){weather.enable()}.disabled(weather.busy);Text(weather.status).font(.caption).foregroundStyle(.secondary)}
                    Text("粗略坐标用于私人 Mac 和 Open-Meteo 天气查询。").font(.caption).foregroundStyle(.secondary)
                }
                Section("反馈") {
                    Toggle("语音结果简短播报", isOn: $autoSpeak)
                    PersonalOSVoiceSettings()
                    DisclosureGroup {
                        Text(features.notificationSummary).font(.caption).foregroundStyle(.secondary)
                        if features.notificationsEnabled {
                            Button {
                                testingNotification = true
                                Task {
                                    defer { testingNotification = false }
                                    do { try await features.testNotification(); model.notice("测试提醒将在10秒后到达") }
                                    catch { model.notice(error.localizedDescription, error: true) }
                                }
                            } label: { Label("测试通知", systemImage: "bell.badge") }.disabled(testingNotification)
                        } else {
                            Button("开启通知") { Task { await features.enableNotifications(); await model.refresh() } }
                        }
                        Text(features.pushSummary).font(.caption).foregroundStyle(.secondary)
                        Button("系统通知设置") {
                            if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
                        }
                    } label: {
                        HStack { Text("通知"); Spacer(); Text(features.notificationsEnabled ? "已开启" : "未开启").foregroundStyle(.secondary) }
                    }
                }
                Section {
                    DisclosureGroup("授权目录") {
                        if model.roots.isEmpty { Text("暂无目录，请在 Mac 添加授权。") .font(.caption).foregroundStyle(.secondary) }
                        ForEach(model.roots) { root in
                            VStack(alignment: .leading, spacing: 4) {
                                HStack {
                                    Text(root.name).font(.subheadline)
                                    if root.isReadOnly { Text("只读").font(.caption2).foregroundStyle(.secondary) }
                                }
                                Text(root.path == "project://air" ? "MacBook Air · PROJECT" : root.path)
                                    .font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                            }.padding(.vertical, 4)
                        }
                    }
                    DisclosureGroup("快捷指令") {
                        Text("操作按钮：和PersonalOS说话") .font(.subheadline)
                        Link(destination: URL(string: "shortcuts://")!) { Label("打开快捷指令", systemImage: "arrow.up.forward.app") }
                    }
                }
            }
            .scrollContentBackground(.hidden).background(OSTheme.background)
            .navigationTitle("设置").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } } }
            .task { await features.refreshAuthorizationStatus() }
        }
    }
    private var pairingFields: some View {
        Group {
            SecureField("Mac 配对码", text: $code).textInputAutocapitalization(.never).autocorrectionDisabled()
            Button {
                Task { await model.pair(code: code); if model.connected { code = "" } }
            } label: { Label("配对", systemImage: "link").frame(minHeight: 32) }
                .disabled(code.isEmpty || model.busy)
        }
    }
}

@main struct PersonalOSApp: App {
    @UIApplicationDelegateAdaptor(PersonalOSAppDelegate.self) private var appDelegate
    var body: some Scene { WindowGroup {
        #if DEBUG
        if ProcessInfo.processInfo.arguments.contains("--fitness-preview") {NavigationStack {HealthFitnessView()}}
        else if ProcessInfo.processInfo.arguments.contains("--exercise-preview") {NavigationStack {ExerciseDemoView(kind:"lateral_raise")}}
        else {ContentView()}
        #else
        ContentView()
        #endif
    } }
}
