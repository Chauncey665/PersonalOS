import Foundation
import AppIntents
import UniformTypeIdentifiers
import CryptoKit

// Add these three entries to the existing, single PersonalShortcuts provider:
// AppShortcut(intent: PlanTodayIntent(), phrases: ["用 \(.applicationName) 安排今天"], shortTitle: "今天先做什么", systemImageName: "sun.max")
// AppShortcut(intent: ContinueProjectTomorrowIntent(), phrases: ["用 \(.applicationName) 提醒继续项目"], shortTitle: "明天继续项目", systemImageName: "bell")
// AppShortcut(intent: OpenVoiceEntryIntent(), phrases: ["打开 \(.applicationName) 语音输入"], shortTitle: "语音输入", systemImageName: "waveform")

struct PlanTodayIntent: AppIntent {
    static var title: LocalizedStringResource = "今天先做什么"
    static var description = IntentDescription("把任务历史与已授权的今天日历交给私人 Mac，返回今日优先计划。")
    static var openAppWhenRun = true
    @MainActor func perform() async throws -> some IntentResult & ProvidesDialog {
        var body = ["text": "今天应该先做什么", "request_id": UUID().uuidString, "time_zone": TimeZone.current.identifier]
        if let context = CompanionFeatures.shared.todayCalendarContext() { body["calendar_context"] = context }
        let data = try await API.request("/tasks", body)
        let response = try JSONDecoder().decode([String: String].self, from: data)
        if let taskID = response["id"] { CompanionFeatures.openTask(taskID) }
        return .result(dialog: "已交给你的 Mac 规划。结果会在 App 内更新。")
    }
}

struct ContinueProjectTomorrowIntent: AppIntent {
    static var title: LocalizedStringResource = "明天提醒继续项目"
    static var description = IntentDescription("指定时间时安排提醒；留空时记录为明天的待办。")
    static var openAppWhenRun = true
    @Parameter(title: "提醒时间", description: "不填写时记录为待办") var due: Date?

    @MainActor func perform() async throws -> some IntentResult & ProvidesDialog {
        guard let date = due else {
            _ = try await API.request("/tasks", ["text":"明天提醒继续项目", "request_id":UUID().uuidString, "time_zone":TimeZone.current.identifier])
            return .result(dialog:"已交给 Mac 记录为明天的待办。")
        }
        let features = CompanionFeatures.shared
        await features.refreshAuthorizationStatus()
        guard features.notificationsEnabled else { throw PersonalOSError.message("请先开启手机通知") }
        guard date.timeIntervalSinceNow > 1 else { throw PersonalOSError.message("请选择未来的提醒时间") }
        let data = try await API.request("/tasks", ["text": "提醒继续项目", "due": ISO8601DateFormatter().string(from: date), "request_id": UUID().uuidString, "time_zone": TimeZone.current.identifier])
        let response = try JSONDecoder().decode([String: String].self, from: data)
        guard let taskID = response["id"] else { throw PersonalOSError.message("Mac 未返回任务标识，请在历史中检查") }
        CompanionFeatures.openTask(taskID)
        do { try await features.scheduleReminder(taskID: taskID, title: "继续项目", due: date) }
        catch { return .result(dialog: "任务已提交到 Mac，但手机提醒未完成。请在 App 中检查通知权限和私人网络，再重新安排本机提醒。") }
        let formatter = DateFormatter(); formatter.dateStyle = .medium; formatter.timeStyle = .short
        let time = formatter.string(from: date)
        return .result(dialog: "已安排手机提醒：\(time)继续项目。")
    }
}

struct OpenVoiceEntryIntent: AppIntent {
    static var title: LocalizedStringResource = "打开语音输入"
    static var description = IntentDescription("打开 PersonalOS 的语音输入入口；麦克风仅在 App 前台由你主动开始录音。")
    static var openAppWhenRun = true
    @MainActor func perform() async throws -> some IntentResult & ProvidesDialog {
        CompanionFeatures.requestVoiceEntry()
        return .result(dialog: "已打开语音输入。请在 App 内点按录音。")
    }
}

/// System Shortcuts owns dictation; this intent receives only its resulting text.
/// Intended workflow: Dictate Text (Chinese, stop after pause) -> this action.
/// Register in the existing single PersonalShortcuts provider, for example:
/// AppShortcut(intent: DictatedTaskIntent(), phrases: ["用 \(.applicationName) 后台提交任务"], shortTitle: "后台提交", systemImageName: "waveform.badge.mic")
/// Never starts the microphone, opens a scene, or lowers Keychain accessibility.
struct DictatedTaskIntent: AppIntent {
    static var title: LocalizedStringResource = "交给 PersonalOS（后台）"
    static var description = IntentDescription("接收系统“听写文本”的结果并交给私人 Mac，不主动打开 PersonalOS。请先在 PersonalOS 配对；锁屏时可能要求解锁。")
    static var openAppWhenRun: Bool { false }
    @available(iOS 26.0, *)
    static var supportedModes: IntentModes { .background }
    static var authenticationPolicy: IntentAuthenticationPolicy { .requiresLocalDeviceAuthentication }

    @Parameter(title: "任务文字", description: "选择上一步“听写文本”的结果")
    var text: String

    static var parameterSummary: some ParameterSummary {
        Summary("把 \(\.$text) 交给 PersonalOS")
    }

    func perform() async throws -> some IntentResult & ProvidesDialog & ReturnsValue<String> {
        let taskText = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !taskText.isEmpty else { throw PersonalOSError.message("没有听到任务，请重新运行听写") }
        guard taskText.count <= 4_000 else { throw PersonalOSError.message("任务太长，请分成几句话再提交") }
        let endpoint = try API.endpoint()
        var body = ["text": taskText, "request_id": UUID().uuidString, "time_zone": TimeZone.current.identifier]
        // This accessor returns nil unless the person explicitly enabled calendar
        // sharing and the operating system has already granted EventKit access.
        if let context = await MainActor.run(body: { CompanionFeatures.shared.todayCalendarContext() }) {
            body["calendar_context"] = context
        }
        let data: Data
        do { data = try await API.request("/tasks", body, endpoint: endpoint) }
        catch let error as URLError {
            switch error.code {
            case .notConnectedToInternet, .cannotConnectToHost, .cannotFindHost, .timedOut, .networkConnectionLost:
                throw PersonalOSError.message("暂时连不上私人 Mac。请确认 Tailscale 已连接且 Mac 在线；可在 PersonalOS 历史中检查任务是否已收到。")
            default:
                throw PersonalOSError.message("私人连接验证未完成，请在 PersonalOS 中检查连接设置后重试")
            }
        }
        struct Submission: Decodable { let id: String }
        let submission = try JSONDecoder().decode(Submission.self, from: data)
        guard !submission.id.isEmpty else { throw PersonalOSError.message("Mac 没有返回任务编号，请在 PersonalOS 历史中检查") }
        // No openTask(), openURL(), foreground continuation, or microphone API.
        let calendarEnabled = await CalendarJournal.shared.enabled
        let calendarReply = calendarEnabled ? await CalendarJournal.shared.waitForTaskAndSync(taskID: submission.id, endpoint: endpoint) : nil
        return .result(value: submission.id, dialog: IntentDialog(stringLiteral: calendarReply ?? "已交给 PersonalOS"))
    }
}

/// The system Record Audio action owns recording and its visible controls. This
/// intent only uploads that explicitly supplied recording to the paired private Mac.
struct AudioTaskIntent: AppIntent {
    static var title: LocalizedStringResource = "录音交给 PersonalOS（后台）"
    static var description = IntentDescription("接收系统录音，由私人 Mac 本地识别中文或英文并执行任务；不主动打开 PersonalOS。最长 90 秒、最大 8 MiB，锁屏时可能要求解锁。")
    static var openAppWhenRun: Bool { false }
    @available(iOS 26.0, *)
    static var supportedModes: IntentModes { .background }
    static var authenticationPolicy: IntentAuthenticationPolicy { .requiresLocalDeviceAuthentication }

    @Parameter(title: "录音", description: "选择上一步系统“录制音频”的结果；支持 M4A 或 WAV")
    var audio: IntentFile

    static var parameterSummary: some ParameterSummary {
        Summary("把 \(\.$audio) 交给 PersonalOS 识别")
    }

    func perform() async throws -> some IntentResult & ProvidesDialog & ReturnsValue<String> {
        let endpoint = try API.endpoint()
        let recording = try await AudioIntentPayload.load(audio)
        try Task.checkCancellation()
        var body = ["audio_base64": recording.data.base64EncodedString(),
                    "mime_type": recording.mimeType, "request_id": AudioIntentPayload.requestID(for: recording.data),
                    "time_zone": TimeZone.current.identifier]
        if let context = await MainActor.run(body: { CompanionFeatures.shared.todayCalendarContext() }) {
            body["calendar_context"] = context
        }
        let data: Data
        do { data = try await API.request("/voice", body, endpoint: endpoint, timeout: 30) }
        catch let error as URLError {
            switch error.code {
            case .notConnectedToInternet, .cannotConnectToHost, .cannotFindHost, .timedOut, .networkConnectionLost:
                throw PersonalOSError.message("录音提交未确认。请检查 Tailscale 与 Mac 是否在线，并先在 PersonalOS 历史中确认是否已收到，再决定是否重试。")
            default:
                throw PersonalOSError.message("私人连接验证未完成，请在 PersonalOS 检查连接设置后重试")
            }
        }
        struct Submission: Decodable { let id: String }
        guard let submission = try? JSONDecoder().decode(Submission.self, from: data), !submission.id.isEmpty else {
            throw PersonalOSError.message("Mac 没有返回录音任务编号，请在 PersonalOS 历史中检查是否已收到")
        }
        let calendarEnabled = await CalendarJournal.shared.enabled
        let calendarReply = calendarEnabled ? await CalendarJournal.shared.waitForTaskAndSync(taskID: submission.id, endpoint: endpoint) : nil
        return .result(value: submission.id, dialog: IntentDialog(stringLiteral: calendarReply ?? "录音已交给 PersonalOS，结果会在 App 和通知中返回。"))
    }
}

enum AudioIntentPayload {
    static let maximumBytes = 8 * 1024 * 1024
    struct Recording { let data: Data; let mimeType: String }

    static func requestID(for data: Data) -> String {
        // Replaying the exact recording is a retry, not a new task. The server
        // scopes this identity to the paired device and rejects changed context.
        "voice-" + SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    static func load(_ file: IntentFile) async throws -> Recording {
        let data: Data
        if let url = file.fileURL {
            data = try readBounded(url)
        } else if #available(iOS 18.0, *),
                  let type = file.availableContentTypes.first(where: { $0.conforms(to: .mpeg4Audio) || $0.conforms(to: .wav) }) {
            data = try await file.withFile(contentType: type, allowOpenInPlace: true) { url, _ in try readBounded(url) }
        } else {
            data = file.data
        }
        return try validate(data)
    }

    static func readBounded(_ url: URL) throws -> Data {
        guard url.isFileURL else { throw PersonalOSError.message("请选择快捷指令提供的本机录音文件") }
        let access = url.startAccessingSecurityScopedResource()
        defer { if access { url.stopAccessingSecurityScopedResource() } }
        let values = try url.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey])
        guard values.isRegularFile == true else { throw PersonalOSError.message("录音必须是普通 M4A 或 WAV 文件") }
        if let size = values.fileSize, size > maximumBytes { throw tooLarge() }
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var data = Data()
        while data.count <= maximumBytes {
            let chunk = try handle.read(upToCount: min(65_536, maximumBytes + 1 - data.count)) ?? Data()
            if chunk.isEmpty { break }
            data.append(chunk)
        }
        guard data.count <= maximumBytes else { throw tooLarge() }
        return data
    }

    static func validate(_ data: Data) throws -> Recording {
        guard !data.isEmpty else { throw PersonalOSError.message("录音为空，请重新录制后提交") }
        guard data.count <= maximumBytes else { throw tooLarge() }
        let header = Array(data.prefix(12))
        if header.count == 12, Array(header[0..<4]) == Array("RIFF".utf8), Array(header[8..<12]) == Array("WAVE".utf8) {
            return Recording(data: data, mimeType: "audio/wav")
        }
        if header.count == 12, Array(header[4..<8]) == Array("ftyp".utf8) {
            return Recording(data: data, mimeType: "audio/mp4")
        }
        throw PersonalOSError.message("录音格式不支持，请使用系统“录制音频”的 M4A 或 WAV 结果")
    }
    private static func tooLarge() -> PersonalOSError {
        .message("录音超过 8 MiB，请重新录制不超过 90 秒的简短任务")
    }
}

struct LatestPersonalOSResultIntent:AppIntent {
    static var title:LocalizedStringResource="获取 PersonalOS 最新结果"
    static var authenticationPolicy:IntentAuthenticationPolicy{.requiresLocalDeviceAuthentication}
    @available(iOS 26.0, *)
    static var supportedModes:IntentModes{.background}
    static var description=IntentDescription("取得已完成结果文本，可在快捷指令后接系统的“复制到剪贴板”；不打开 PersonalOS。")
    static var openAppWhenRun=false
    @MainActor func perform()async throws->some IntentResult & ReturnsValue<String> & ProvidesDialog {
        struct Result:Decodable{let id:String;let result:String}
        let data=try await API.request("/assistant/latest-result")
        let reply=try JSONDecoder().decode(Result.self,from:data)
        guard !reply.id.isEmpty else{throw PersonalOSError.message("暂无已完成结果")}
        return .result(value:reply.result,dialog:"已获取最新完成的结果")
    }
}
