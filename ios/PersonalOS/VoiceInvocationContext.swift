import Foundation

// Internal identifiers stay in protected app storage, never in a user prompt.
struct VoiceInvocationContext: Codable {
    let endpoint: String
    let pair: String
    let session: String
    let action: String?
    let operation: String?
    let audio: String?
    let expires: Date
    var task: String? = nil

    enum ContextError: LocalizedError {
        case unavailable, changed, wrongAction
        var errorDescription: String? {
            switch self {
            case .unavailable: return "这次对话已结束，请重新按操作按钮。"
            case .changed: return "对话或私人连接已改变，请重新按操作按钮。"
            case .wrongAction: return "手机操作已改变，已停止旧操作，请重新按操作按钮。"
            }
        }
    }
    func sessionID(_ supplied: String?, endpoint: String, pair: String, now: Date = Date()) throws -> String {
        guard expires > now, UUID(uuidString: session) != nil else { throw ContextError.unavailable }
        guard self.endpoint == endpoint, self.pair == pair else { throw ContextError.changed }
        if let supplied, !supplied.isEmpty, supplied != session { throw ContextError.changed }
        return session
    }
    func actionID(_ supplied: String?, allowed: Set<String>) throws -> String {
        guard let action, UUID(uuidString: action) != nil, let operation, allowed.contains(operation) else { throw ContextError.wrongAction }
        if let supplied, !supplied.isEmpty, supplied != action { throw ContextError.wrongAction }
        return action
    }
    func audioID(_ supplied: String?) throws -> String {
        guard let audio, audio.range(of: "^[0-9a-f]{32}$", options: .regularExpression) != nil else { throw ContextError.unavailable }
        if let supplied, !supplied.isEmpty, supplied != audio { throw ContextError.changed }
        return audio
    }
}
