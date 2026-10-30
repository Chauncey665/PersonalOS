import SwiftUI

struct TodoItem: Codable, Identifiable, Equatable {
    let id: String
    let parentID: String?
    let kind: String
    let title: String
    let notes: String
    let deadline: String?
    let completed: Bool
    let revision: Int
    let created: Double
    let updated: Double
    let completedCount: Int?
    let totalCount: Int?
    let source: String?
    let sourceURL: String?
    enum CodingKeys: String, CodingKey {
        case id, kind, title, notes, deadline, completed, revision, created, updated, source
        case parentID = "parent_id", completedCount = "completed_count", totalCount = "total_count", sourceURL = "source_url"
    }
    var deadlineDate: Date? {
        guard let deadline else { return nil }
        if deadline.count == 10 {
            let f = DateFormatter(); f.locale = Locale(identifier: "en_US_POSIX")
            f.calendar = Calendar(identifier: .gregorian); f.timeZone = .current; f.dateFormat = "yyyy-MM-dd"; return f.date(from: deadline)
        }
        return JournalMetadata.date(deadline)
    }
    var deadlineLabel: String? {
        guard let date = deadlineDate else { return nil }
        return date.formatted(.dateTime.month().day()) + (deadline?.count == 10 ? "" : " " + date.formatted(date: .omitted, time: .shortened)) + " 截止"
    }
    var overdue: Bool {
        guard !completed, let date = deadlineDate else { return false }
        if deadline?.count == 10 { return Calendar.current.startOfDay(for: date) < Calendar.current.startOfDay(for: Date()) }
        return date < Date()
    }
}

struct CanvasConnectionStatus: Codable {
    let configured: Bool
    let host: String?
    let mode: String?
    let syncing: Bool?
    let lastSync: Double?
    let error: String?
    enum CodingKeys: String, CodingKey { case configured, host, mode, syncing, error; case lastSync = "last_sync" }
}

@MainActor final class TodoModel: ObservableObject {
    static let shared = TodoModel()
    @Published private(set) var items: [TodoItem] = []
    @Published private(set) var loading = false
    @Published private(set) var busyIDs: Set<String> = []
    @Published var error: String?
    @Published private(set) var calendarNote: String?
    @Published private(set) var canvas: CanvasConnectionStatus?
    private var connection = ""
    private var lastRefresh = Date.distantPast
    private struct Page: Decodable { let items: [TodoItem]; let next_cursor: String? }

    private func context() throws -> (ServerEndpoint, String) {
        let endpoint = try API.endpoint()
        guard API.isPaired() else { throw PersonalOSError.message("请先连接 Mac") }
        return (endpoint, endpoint.key + "|" + PendingSubmission.fingerprint(for: endpoint))
    }
    private func check(_ key: String) throws {
        guard try context().1 == key, !Task.isCancelled else { throw PersonalOSError.message("连接已改变，请重新打开待办") }
    }
    func refresh(force: Bool = false) async {
        do {
            let (endpoint, key) = try context()
            if connection != key { connection = key; items = []; canvas = nil; lastRefresh = .distantPast; error = nil }
            guard !loading else { return }
            guard force || Date().timeIntervalSince(lastRefresh) > 5 else { return }
            loading = true; defer { loading = false }
            var next: String? = "0", result: [TodoItem] = [], seen = Set<String>()
            while let cursor = next {
                guard seen.insert(cursor).inserted, seen.count <= 25 else { throw PersonalOSError.message("待办数量较多，请稍后重试；未用不完整列表覆盖日历") }
                let data = try await API.request("/todos?cursor=" + cursor, endpoint: endpoint)
                try check(key)
                let page = try JSONDecoder().decode(Page.self, from: data)
                result += page.items; next = page.next_cursor
            }
            guard Set(result.map(\.id)).count == result.count else { throw PersonalOSError.message("待办列表重复，请刷新") }
            items = result; lastRefresh = Date(); error = nil
            if let data = try? await API.request("/canvas/status", endpoint: endpoint) {
                try check(key); canvas = try? JSONDecoder().decode(CanvasConnectionStatus.self, from: data)
            }
            do { calendarNote = try await TodoDeadlineSync.shared.synchronize(items: items, endpoint: endpoint) }
            catch { calendarNote = error.localizedDescription }
        } catch {
            self.error = error.localizedDescription
        }
    }
    private func post(_ path: String, value: [String: Any], endpoint: ServerEndpoint) async throws -> Data {
        let bytes = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
        return try await API.request(path, ["payload": String(decoding: bytes, as: UTF8.self)], endpoint: endpoint)
    }
    func create(title: String, notes: String, deadline: String?, project: Bool, parentID: String?, requestID: String) async throws {
        let (endpoint, key) = try context()
        let item: [String: Any] = ["title": title, "notes": notes, "deadline": deadline as Any? ?? NSNull(), "kind": project ? "project" : "task"]
        var body: [String: Any] = ["items": [item], "request_id": requestID]
        if let parentID { body["parent_id"] = parentID }
        let data = try await post("/todos", value: body, endpoint: endpoint)
        try check(key)
        struct Created: Decodable { let items: [TodoItem] }
        let response = try JSONDecoder().decode(Created.self, from: data)
        guard response.items.contains(where: { $0.title == title.trimmingCharacters(in: .whitespacesAndNewlines) && $0.parentID == parentID }) else { throw PersonalOSError.message("保存后未能核对任务，输入已保留") }
        await refresh(force: true)
    }
    func edit(_ item: TodoItem, title: String, notes: String, deadline: String?) async throws {
        let (endpoint, key) = try context()
        let data = try await post("/todos/" + item.id + "/update",
            value: ["revision": item.revision, "title": title, "notes": notes, "deadline": deadline as Any? ?? NSNull()], endpoint: endpoint)
        try check(key)
        let updated = try JSONDecoder().decode(TodoItem.self, from: data)
        guard updated.id == item.id, updated.revision > item.revision,
              updated.title == title.trimmingCharacters(in: .whitespacesAndNewlines),
              updated.notes == notes.trimmingCharacters(in: .whitespacesAndNewlines), updated.deadline == deadline else { throw PersonalOSError.message("任务更新未确认") }
        await refresh(force: true)
    }
    func setCompleted(id: String, completed: Bool, requireCalendar: Bool = false) async throws {
        guard !busyIDs.contains(id) else { throw PersonalOSError.message("任务正在保存") }
        busyIDs.insert(id); defer { busyIDs.remove(id) }
        if !items.contains(where: { $0.id == id }) { await refresh(force: true) }
        guard let item = items.first(where: { $0.id == id }) else { throw PersonalOSError.message("待办已不可用，请刷新") }
        if item.completed != completed {
            let (endpoint, key) = try context()
            let data = try await post("/todos/" + id + "/update", value: ["revision": item.revision, "completed": completed], endpoint: endpoint)
            try check(key)
            let saved = try JSONDecoder().decode(TodoItem.self, from: data)
            guard saved.id == id, saved.completed == completed else { throw PersonalOSError.message("完成状态尚未保存") }
        }
        await refresh(force: true)
        await CalendarJournal.shared.refreshAndSync()
        if requireCalendar, let calendarNote { throw PersonalOSError.message(calendarNote) }
        if let error { throw PersonalOSError.message(error) }
    }
    func canvasAction(_ action: String, token: String? = nil) async throws {
        let (endpoint, key) = try context()
        _ = try await API.request("/canvas/" + action, token.map { ["token": $0] } ?? [:], endpoint: endpoint, timeout: 25)
        try check(key)
        await refresh(force: true)
    }
}

@MainActor struct TodoBoardView: View {
    @ObservedObject private var model = TodoModel.shared
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @AppStorage("server") private var server = API.defaultServer
    @State private var expanded = Set<String>()
    @State private var showCompleted = false
    @State private var editor: TodoEditorSelection?
    @State private var showCanvas = false
    @State private var showPlanning = false
    @ObservedObject private var planner=AutoScheduleModel.shared
    let refreshToken: String
    private var roots: [TodoItem] {
        model.items.filter { $0.parentID == nil && (showCompleted || !$0.completed) }.sorted {
            if $0.completed != $1.completed { return !$0.completed }
            return ($0.deadlineDate ?? .distantFuture) < ($1.deadlineDate ?? .distantFuture)
        }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Menu {
                    Button("新建任务", systemImage: "plus") { editor = .init() }
                    Button("新建项目", systemImage: "square.stack") { editor = .init(project: true) }
                } label: { Label("新增", systemImage: "plus").frame(minHeight: 44) }
                Spacer()
                Button("Canvas") { showCanvas = true }.frame(minHeight: 44)
                Button("排程") {showPlanning=true}.frame(minHeight:44)
                Menu {
                    Toggle("显示已完成", isOn: $showCompleted)
                } label: { Image(systemName: "line.3.horizontal.decrease").frame(width: 44, height: 44).accessibilityLabel("筛选待办") }
            }.font(.subheadline)
            if model.loading && model.items.isEmpty { ProgressView().frame(maxWidth: .infinity) }
            if let error = model.error {
                Text(error).font(.caption).foregroundStyle(.orange)
                Button("重试") { Task { await model.refresh(force: true) } }
            }
            if let note = model.calendarNote { Label(note, systemImage: "calendar.badge.exclamationmark").font(.caption).foregroundStyle(.secondary) }
            if let error=planner.error {Text(error).font(.caption).foregroundStyle(.orange)}
            if roots.isEmpty && !model.loading && model.error == nil {
                Text(showCompleted ? "暂无待办" : "没有未完成事项").font(.subheadline).foregroundStyle(.secondary).padding(.vertical, 24)
            }
            ForEach(roots) { item in
                if item.kind == "project" { project(item) } else { row(item) }
            }
        }
        .sheet(item: $editor) { selection in TodoEditor(selection: selection, model: model) }
        .sheet(isPresented: $showCanvas) { CanvasConnectionView(model: model) }
        .sheet(isPresented:$showPlanning){AutoScheduleSettings()}
        .task(id: "\(server)|\(scenePhase)|\(refreshToken)") {
            guard scenePhase == .active else { return }
            while !Task.isCancelled {
                await model.refresh()
                do { try await Task.sleep(nanoseconds: 8_000_000_000) } catch { return }
            }
        }
    }
    private func project(_ item: TodoItem) -> some View {
        let open = expanded.contains(item.id)
        let children = model.items.filter { $0.parentID == item.id }
        return VStack(alignment: .leading, spacing: 12) {
            Button {
                let change = { if open { expanded.remove(item.id) } else { expanded.insert(item.id) } }
                if reduceMotion { change() } else { withAnimation(.easeInOut(duration: 0.24), change) }
            } label: {
                HStack(alignment: .top, spacing: 12) {
                    Image(systemName: "square.stack").foregroundStyle(OSTheme.accent).padding(.top, 3)
                    VStack(alignment: .leading, spacing: 6) {
                        Text(item.title).font(.headline).foregroundStyle(.primary).multilineTextAlignment(.leading)
                        HStack {
                            Text("\(item.completedCount ?? 0)/\(item.totalCount ?? 0)").monospacedDigit()
                            if let due = item.deadlineLabel { Text(due).foregroundStyle(item.overdue ? .orange : .secondary) }
                        }.font(.caption).foregroundStyle(.secondary)
                    }.frame(maxWidth: .infinity, alignment: .leading)
                    Image(systemName: open ? "chevron.up" : "chevron.down").font(.caption).foregroundStyle(.secondary)
                }.frame(minHeight: 44).contentShape(Rectangle())
            }.buttonStyle(.plain)
            if (item.totalCount ?? 0) > 0 {
                ProgressView(value: Double(item.completedCount ?? 0), total: Double(item.totalCount ?? 1))
                    .tint(OSTheme.accent).accessibilityLabel("项目进度")
            }
            if open {
                ForEach(children) { child in row(child) }
                HStack {
                    Button("添加小任务", systemImage: "plus") { editor = .init(parentID: item.id) }
                    Spacer()
                    Button("编辑项目") { editor = .init(item: item, project: true) }
                }.font(.subheadline).frame(minHeight: 44)
            }
        }.padding(18).background(OSTheme.surface, in: RoundedRectangle(cornerRadius: 22))
    }
    private func row(_ item: TodoItem) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Button {
                Task {
                    do { try await model.setCompleted(id: item.id, completed: !item.completed) }
                    catch { model.error = error.localizedDescription }
                }
            } label: {
                Group {
                    if model.busyIDs.contains(item.id) { ProgressView().controlSize(.small) }
                    else { Image(systemName: item.completed ? "checkmark.circle.fill" : "circle").font(.system(size: 23)).foregroundStyle(item.completed ? OSTheme.accent : Color.secondary) }
                }.frame(width: 44, height: 44)
            }.buttonStyle(.plain).disabled(model.busyIDs.contains(item.id)).accessibilityLabel((item.completed ? "取消完成：" : "完成：") + item.title)
            Button { editor = .init(item: item, project: item.kind == "project") } label: {
                VStack(alignment: .leading, spacing: 6) {
                    Text(item.title).font(.subheadline.weight(.medium)).foregroundStyle(item.completed ? .secondary : .primary).multilineTextAlignment(.leading)
                    HStack(spacing: 8) {
                        if let due = item.deadlineLabel { Text(due).foregroundStyle(item.overdue ? .orange : .secondary) }
                        if item.source == "canvas" { Text("Canvas").foregroundStyle(OSTheme.accent) }
                    }.font(.caption)
                    if let slot=planner.slot(item.id),let label=slot.label {Text("执行 · "+label).font(.caption).foregroundStyle(.secondary)}
                    else if let slot=planner.slot(item.id),!slot.reason.isEmpty {Text(slot.reason).font(.caption).foregroundStyle(.secondary)}
                }.frame(maxWidth: .infinity, minHeight: 44, alignment: .leading).contentShape(Rectangle())
            }.buttonStyle(.plain)
                .contextMenu {
                    ForEach([30,45,60,90,120],id:\.self){minutes in Button("预计\(minutes)分钟并安排"){Task{await planner.estimate(id:item.id,minutes:minutes)}}}
                }
        }.padding(.vertical, 8)
    }
}

struct TodoEditorSelection: Identifiable {
    let id = UUID()
    var item: TodoItem? = nil
    var project = false
    var parentID: String? = nil
}

@MainActor private struct TodoEditor: View {
    let selection: TodoEditorSelection
    @ObservedObject var model: TodoModel
    @Environment(\.dismiss) private var dismiss
    @State private var title: String
    @State private var notes: String
    @State private var hasDeadline: Bool
    @State private var hasTime: Bool
    @State private var due: Date
    @State private var saving = false
    @State private var error: String?
    @State private var requestID = UUID().uuidString
    private let originalConnection: String
    init(selection: TodoEditorSelection, model: TodoModel) {
        self.selection = selection; self.model = model
        _title = State(initialValue: selection.item?.title ?? "")
        _notes = State(initialValue: selection.item?.notes ?? "")
        _hasDeadline = State(initialValue: selection.item?.deadline != nil)
        _hasTime = State(initialValue: (selection.item?.deadline?.count ?? 0) > 10)
        _due = State(initialValue: selection.item?.deadlineDate ?? Date())
        originalConnection = (try? API.endpoint()).map { $0.key + "|" + PendingSubmission.fingerprint(for: $0) } ?? ""
    }
    var body: some View {
        NavigationStack {
            Form {
                TextField(selection.project ? "项目名称" : "任务名称", text: $title, axis: .vertical)
                Section {
                    Toggle("设置截止", isOn: $hasDeadline)
                    if hasDeadline {
                        DatePicker("截止日期", selection: $due, displayedComponents: .date)
                        Toggle("具体时刻", isOn: $hasTime)
                        if hasTime { DatePicker("截止时刻", selection: $due, displayedComponents: .hourAndMinute) }
                    }
                }
                Section { TextField("备注", text: $notes, axis: .vertical).lineLimit(3...8) }
                if let link = selection.item?.sourceURL, let url = URL(string: link), url.scheme == "https", url.host == URL(string: model.canvas?.host ?? "")?.host {
                    Link("在 Canvas 查看", destination: url)
                }
                if let error { Text(error).foregroundStyle(.orange) }
            }
            .navigationTitle(selection.item == nil ? (selection.project ? "新建项目" : "新建任务") : "编辑")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() }.disabled(saving) }
                ToolbarItem(placement: .confirmationAction) {
                    Button(saving ? "保存中…" : "保存") { Task { await save() } }
                        .disabled(saving || title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }.disabled(saving).interactiveDismissDisabled(saving)
        }
    }
    private func save() async {
        guard !saving else { return }; saving = true; defer { saving = false }
        do {
            let endpoint = try API.endpoint()
            guard endpoint.key + "|" + PendingSubmission.fingerprint(for: endpoint) == originalConnection else { throw PersonalOSError.message("连接已改变，输入保留，请重新打开表单") }
            let value: String?
            if !hasDeadline { value = nil }
            else if hasTime { value = ISO8601DateFormatter().string(from: due) }
            else {
                let f = DateFormatter(); f.locale = Locale(identifier: "en_US_POSIX"); f.calendar = Calendar(identifier: .gregorian); f.timeZone = .current; f.dateFormat = "yyyy-MM-dd"; value = f.string(from: due)
            }
            if let item = selection.item { try await model.edit(item, title: title, notes: notes, deadline: value) }
            else { try await model.create(title: title, notes: notes, deadline: value, project: selection.project, parentID: selection.parentID, requestID: requestID) }
            dismiss()
        } catch { self.error = error.localizedDescription }
    }
}

@MainActor private struct CanvasConnectionView: View {
    @ObservedObject var model: TodoModel
    @Environment(\.dismiss) private var dismiss
    @State private var token = ""
    @State private var busy = false
    @State private var error: String?
    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Text("Canvas").font(.headline)
                    Text(model.canvas?.mode == "calendar_feed" ? "日历订阅 · 作业截止日期" : "只读同步待办和截止日期").font(.subheadline).foregroundStyle(.secondary)
                    if let stamp = model.canvas?.lastSync { Text("上次同步 " + Date(timeIntervalSince1970: stamp).formatted(date: .abbreviated, time: .shortened)).font(.caption) }
                }
                if model.canvas?.configured == true {
                    Button(model.canvas?.syncing == true ? "正在同步…" : "立即同步") { action("sync") }.disabled(busy || model.canvas?.syncing == true)
                    Button("断开连接", role: .destructive) { action("disconnect") }.disabled(busy)
                } else {
                    Section {
                        SecureField("日历订阅链接", text: $token).textContentType(nil).textInputAutocapitalization(.never).autocorrectionDisabled()
                        Button(busy ? "连接中…" : "连接") { action("connect") }.disabled(busy || token.isEmpty)
                    } footer: { Text("Canvas → Calendar → Calendar Feed，复制链接到这里。链接只保存在私人 Mac。") }
                    if let base = model.canvas?.host, !base.contains("example.edu"), let url = URL(string: base + "/calendar") { Link("打开 Canvas 日历", destination: url) }
                }
                if let error { Text(error).foregroundStyle(.orange) }
                if let text = model.canvas?.error, !text.isEmpty { Text(text).foregroundStyle(.orange) }
            }.navigationTitle("Canvas").navigationBarTitleDisplayMode(.inline)
                .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { token = ""; dismiss() }.disabled(busy) } }
                .interactiveDismissDisabled(busy)
        }
        .task { await model.refresh(force: true) }
    }
    private func action(_ action: String) {
        busy = true; error = nil
        Task {
            defer { busy = false }
            do { try await model.canvasAction(action, token: action == "connect" ? token : nil); token = "" }
            catch { self.error = error.localizedDescription }
        }
    }
}
