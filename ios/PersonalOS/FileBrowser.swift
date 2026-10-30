import SwiftUI
import QuickLook
import UniformTypeIdentifiers

// Standalone tab/sheet entry point. This browser never adds a writable root and
// only calls the paired service's fixed, authenticated /files/* endpoints.
struct FileBrowserScreen: View {
    @AppStorage("server") private var server = API.defaultServer
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @StateObject private var model = FileSourcesModel()
    @State private var path: [FileLocation] = []
    @State private var selectedDeviceID: String?
    private var selectedDevice: FileDevice? {
        model.devices.first { $0.id == selectedDeviceID } ?? model.devices.first
    }

    var body: some View {
        NavigationStack(path: $path) {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    if !model.devices.isEmpty { devicePicker }
                    if let error = model.error { FileBrowserError(message: error) { Task { await model.load() } } }
                    if model.loading && model.devices.isEmpty {
                        ProgressView("正在连接…").frame(maxWidth: .infinity).padding(.vertical, 44)
                    }
                    if let device = selectedDevice { deviceContents(device) }
                    if !model.loading && model.devices.isEmpty && model.error == nil {
                        ContentUnavailableView("还没有连接的 Mac", systemImage: "desktopcomputer", description: Text("在设置中连接后，这里就能浏览文件。"))
                    }
                }.padding(20)
            }
            .background(FileBrowserPalette.background)
            .navigationTitle("文件")
            .navigationDestination(for: FileLocation.self) { location in
                FileDirectoryScreen(location: location, navigate: navigate)
            }
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button { Task { await model.load() } } label: {
                        Image(systemName: "arrow.clockwise").frame(minWidth: 44, minHeight: 44)
                    }.accessibilityLabel("刷新文件设备").disabled(model.loading)
                }
            }
            .refreshable { await model.load() }
            .task(id: server) { path = []; selectedDeviceID = nil; await model.load() }
        }
        .tint(FileBrowserPalette.accent)
    }

    private var devicePicker: some View {
        let layout = dynamicTypeSize.isAccessibilitySize ? AnyLayout(VStackLayout(spacing: 6)) : AnyLayout(HStackLayout(spacing: 6))
        return layout {
            ForEach(model.devices) { device in
                Button { selectedDeviceID = device.id } label: {
                    HStack(spacing: 7) {
                        Image(systemName: device.id == "air" ? "laptopcomputer" : "macmini")
                        Text(device.name).fixedSize(horizontal: false, vertical: true)
                    }
                    .font(.subheadline.weight(.semibold))
                    .frame(maxWidth: .infinity, minHeight: 44)
                    .padding(.horizontal, 8)
                    .foregroundStyle(selectedDevice?.id == device.id ? FileBrowserPalette.accent : .secondary)
                    .background(selectedDevice?.id == device.id ? FileBrowserPalette.accent.opacity(0.10) : .clear, in: RoundedRectangle(cornerRadius: 12))
                    .contentShape(RoundedRectangle(cornerRadius: 12))
                }
                .buttonStyle(.plain)
                .accessibilityAddTraits(selectedDevice?.id == device.id ? .isSelected : [])
            }
        }
        .padding(5)
        .background(FileBrowserPalette.surface, in: RoundedRectangle(cornerRadius: 17))
        .accessibilityElement(children: .contain)
        .accessibilityLabel("选择 Mac")
    }

    private func deviceContents(_ device: FileDevice) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Label(device.available ? "可浏览" : "暂不可用", systemImage: device.available ? "checkmark.circle" : "exclamationmark.circle")
                Spacer()
                Label("只读", systemImage: "lock")
            }.font(.caption).foregroundStyle(.secondary)
            if device.available, let endpoint = model.endpoint {
                VStack(spacing: 0) {
                    ForEach(device.roots) { root in
                        NavigationLink(value: FileLocation(device: device.id, deviceName: device.name, path: root.path, name: root.name, endpoint: endpoint.key)) {
                            HStack(spacing: 12) {
                                Image(systemName: root.path == "/" ? "internaldrive" : "folder").foregroundStyle(FileBrowserPalette.accent).frame(width: 26)
                                Text(root.name).font(.body).fixedSize(horizontal: false, vertical: true)
                                Spacer(minLength: 8)
                                Image(systemName: "chevron.right").font(.caption.weight(.medium)).foregroundStyle(.tertiary)
                            }
                            .frame(minHeight: 48).padding(.horizontal, 16).padding(.vertical, 4)
                            .contentShape(Rectangle())
                        }.buttonStyle(.plain)
                        if root.id != device.roots.last?.id { Divider().padding(.leading, 54) }
                    }
                }.background(FileBrowserPalette.surface, in: RoundedRectangle(cornerRadius: 16))
                if device.roots.isEmpty {
                    ContentUnavailableView("还没有可浏览的目录", systemImage: "folder", description: Text("在 Mac 开启文件访问后刷新。"))
                }
            } else if !device.available {
                ContentUnavailableView("暂时无法浏览这台 Mac", systemImage: device.id == "air" ? "laptopcomputer" : "macmini", description: Text("确认设备在线后刷新，或查看下方访问说明。"))
            }
            if let note = device.permission_note, !note.isEmpty {
                DisclosureGroup {
                    Text(note).font(.caption).foregroundStyle(.secondary).padding(.top, 6)
                } label: {
                    Text("访问说明").font(.caption).foregroundStyle(.secondary).frame(minHeight: 44)
                }
            }
        }
    }

    private func navigate(_ location: FileLocation) {
        if let index = path.firstIndex(where: { $0.device == location.device && $0.path == location.path && $0.endpoint == location.endpoint }) {
            path = Array(path.prefix(index + 1))
        } else { path.append(location) }
    }
}

private struct FileDevice: Decodable, Identifiable {
    let id: String
    let name: String
    let available: Bool
    let roots: [FileDeviceRoot]
    let permission_note: String?
}
private struct FileDeviceRoot: Decodable, Identifiable {
    let name: String
    let path: String
    var id: String { path }
}
private struct FileDevicesResponse: Decodable { let devices: [FileDevice] }
private struct FileLocation: Hashable {
    let device: String
    let deviceName: String
    let path: String
    let name: String
    let endpoint: String
    func child(_ entry: BrowsedFile) -> FileLocation {
        FileLocation(device: device, deviceName: deviceName, path: entry.path, name: entry.name, endpoint: endpoint)
    }
}
private struct BrowsedFile: Decodable, Identifiable {
    let name: String
    let path: String
    let kind: String
    let size: Int64?
    let modified: Double?
    let readable: Bool?
    var id: String { path }
    var isDirectory: Bool { kind == "directory" }
    var icon: String {
        if isDirectory { return "folder.fill" }
        let type = UTType(filenameExtension: URL(fileURLWithPath: name).pathExtension)
        if type?.conforms(to: .image) == true { return "photo" }
        if type?.conforms(to: .pdf) == true { return "doc.richtext" }
        if type?.conforms(to: .audio) == true { return "waveform" }
        if type?.conforms(to: .movie) == true { return "film" }
        if type?.conforms(to: .archive) == true { return "archivebox" }
        if type?.conforms(to: .sourceCode) == true { return "chevron.left.forwardslash.chevron.right" }
        return "doc.text"
    }
}
private struct FileListing: Decodable {
    let device: String
    let path: String
    let parent: String?
    let entries: [BrowsedFile]
    let next_cursor: String?
    let truncated: Bool?
    let note: String?
}
private struct FileReadResponse: Decodable {
    let name: String
    let path: String
    let mime_type: String
    let encoding: String
    let content: String
    let size: Int64
    let truncated: Bool?
    let limit_bytes: Int?
    let previewable: Bool?
    let download_allowed: Bool?
    var etag:String? = nil
}
private struct FileChunkResponse: Decodable {
    let name: String
    let path: String
    let mime_type: String
    let encoding: String
    let content_base64: String
    let size: Int64
    let modified: Double
    let offset: Int64
    let next_offset: Int64
    let eof: Bool
    let etag: String
    let limit_bytes: Int
}

private enum FileBrowserAPI {
    static func endpoint(_ expected: String) throws -> ServerEndpoint {
        let endpoint = try ServerEndpoint(expected)
        guard try API.endpoint() == endpoint else { throw PersonalOSError.message("连接的 Mac 已改变，请返回文件首页重新选择设备") }
        return endpoint
    }
    static func request<T: Decodable>(_ route: String, location: FileLocation, extra: [String: String] = [:], as type: T.Type) async throws -> T {
        let endpoint = try self.endpoint(location.endpoint)
        var body = ["device": location.device, "path": location.path]
        for (key, value) in extra { body[key] = value }
        let data = try await API.request(route, body, endpoint: endpoint)
        try Task.checkCancellation()
        _ = try self.endpoint(location.endpoint)
        return try JSONDecoder().decode(type, from: data)
    }
}

@MainActor private final class FileSourcesModel: ObservableObject {
    @Published var devices: [FileDevice] = []
    @Published var loading = false
    @Published var error: String?
    private(set) var endpoint: ServerEndpoint?
    private var generation = UUID()
    init() { FilePreviewStorage.removeOrphans() }
    func load() async {
        FilePreviewStorage.removeOrphans()
        let generation = UUID(); self.generation = generation
        loading = true; error = nil
        defer { if self.generation == generation { loading = false } }
        do {
            let endpoint = try API.endpoint()
            if self.endpoint != endpoint { devices = [] }
            let data = try await API.request("/files/devices", endpoint: endpoint)
            try Task.checkCancellation()
            guard self.generation == generation, (try? API.endpoint()) == endpoint else { return }
            devices = try JSONDecoder().decode(FileDevicesResponse.self, from: data).devices
            self.endpoint = endpoint
        } catch is CancellationError { return }
        catch { if self.generation == generation { self.error = error.localizedDescription } }
    }
}

@MainActor private final class FileDirectoryModel: ObservableObject {
    let location: FileLocation
    @Published var entries: [BrowsedFile] = []
    @Published var loading = false
    @Published var error: String?
    @Published var note: String?
    @Published var truncated = false
    @Published var activeQuery = ""
    @Published var nextCursor: String?
    private var generation = UUID()
    init(location: FileLocation) { self.location = location }
    func load(query: String = "", more: Bool = false) async {
        if more && loading { return }
        let query = more ? activeQuery : query.trimmingCharacters(in: .whitespacesAndNewlines)
        let generation = UUID(); self.generation = generation
        loading = true; error = nil
        if !more { entries = []; nextCursor = nil; note = nil; truncated = false; activeQuery = query }
        defer { if self.generation == generation { loading = false } }
        do {
            var extra: [String: String] = [:]
            if !query.isEmpty { extra["query"] = query }
            if more, let cursor = nextCursor { extra["cursor"] = cursor }
            let result = try await FileBrowserAPI.request(query.isEmpty ? "/files/list" : "/files/search", location: location, extra: extra, as: FileListing.self)
            guard self.generation == generation else { return }
            guard result.device == location.device, result.path == location.path else { throw PersonalOSError.message("Mac 返回的目录与请求不一致，请重新打开目录") }
            if more {
                let existing = Set(entries.map(\.path)); entries += result.entries.filter { !existing.contains($0.path) }
            } else { entries = result.entries }
            nextCursor = result.next_cursor.flatMap { $0.isEmpty ? nil : $0 }
            note = result.note; truncated = result.truncated == true
        } catch is CancellationError { return }
        catch { if self.generation == generation { self.error = error.localizedDescription } }
    }
}

private struct FileDirectoryScreen: View {
    let location: FileLocation
    let navigate: (FileLocation) -> Void
    @StateObject private var model: FileDirectoryModel
    @State private var query = ""
    @State private var selectedFile: BrowsedFile?
    init(location: FileLocation, navigate: @escaping (FileLocation) -> Void) {
        self.location = location
        self.navigate = navigate
        _model = StateObject(wrappedValue: FileDirectoryModel(location: location))
    }
    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 16) {
                VStack(alignment: .leading, spacing: 2) {
                    HStack {
                        Text(location.deviceName)
                        Spacer()
                        Label("只读", systemImage: "lock")
                    }.font(.caption).foregroundStyle(.secondary)
                    FileBreadcrumbs(location: location, navigate: navigate)
                    FilePathDisclosure(path: location.path)
                    if !model.activeQuery.isEmpty {
                        HStack(alignment: .firstTextBaseline) {
                            Text("“\(model.activeQuery)”的结果").font(.subheadline).fixedSize(horizontal: false, vertical: true)
                            Spacer()
                            Button("清除") { query = ""; Task { await model.load() } }.font(.subheadline).frame(minWidth: 44, minHeight: 44)
                        }
                    }
                }
                if let error = model.error { FileBrowserError(message: error) { Task { await model.load(query: model.activeQuery) } } }
                if model.entries.isEmpty && !model.loading && model.error == nil {
                    ContentUnavailableView(model.activeQuery.isEmpty ? "没有可显示的文件" : "没有找到文件", systemImage: model.activeQuery.isEmpty ? "folder" : "doc.text.magnifyingglass", description: Text(model.activeQuery.isEmpty ? "此目录为空，或没有读取权限。" : "换个文件名，或到上一级目录查找。"))
                }
                if !model.entries.isEmpty {
                    LazyVStack(spacing: 0) {
                        ForEach(model.entries) { entry in
                            Group {
                                if entry.isDirectory {
                                    NavigationLink(value: location.child(entry)) { FileBrowserRow(entry: entry, searchRoot: model.activeQuery.isEmpty ? nil : location.path) }
                                } else {
                                    Button { selectedFile = entry } label: { FileBrowserRow(entry: entry, searchRoot: model.activeQuery.isEmpty ? nil : location.path) }
                                }
                            }.buttonStyle(.plain).disabled(entry.readable == false)
                            if entry.id != model.entries.last?.id { Divider().padding(.leading, 58) }
                        }
                    }.background(FileBrowserPalette.surface, in: RoundedRectangle(cornerRadius: 16))
                }
                if model.loading { ProgressView(model.entries.isEmpty ? "正在读取…" : "正在加载更多…").frame(maxWidth: .infinity).padding(.vertical, 20) }
                if let note = model.note, !note.isEmpty {
                    DisclosureGroup {
                        Text(note).font(.caption).foregroundStyle(.secondary).padding(.top, 6)
                    } label: { Text("查看范围").font(.caption).foregroundStyle(.secondary).frame(minHeight: 44) }
                }
                if model.nextCursor != nil {
                    Button("加载更多") { Task { await model.load(more: true) } }.frame(maxWidth: .infinity, minHeight: 44).buttonStyle(.bordered).disabled(model.loading)
                } else if model.truncated {
                    Text("结果较多，可进入具体文件夹继续查找。").font(.caption).foregroundStyle(.secondary)
                }
            }.padding(20)
        }
        .background(FileBrowserPalette.background)
        .navigationTitle(location.name).navigationBarTitleDisplayMode(.inline)
        .searchable(text: $query, prompt: "搜索此文件夹的文件名")
        .onSubmit(of: .search) { Task { await model.load(query: query) } }
        .onChange(of: query) { old, value in if !old.isEmpty && value.isEmpty && !model.activeQuery.isEmpty { Task { await model.load() } } }
        .refreshable { await model.load(query: model.activeQuery) }
        .task { await model.load() }
        .sheet(item: $selectedFile) { entry in FilePreviewScreen(location: location.child(entry)) }
    }
}

private struct FileBreadcrumbs: View {
    let location: FileLocation
    let navigate: (FileLocation) -> Void
    private var locations: [FileLocation] {
        let parts = location.path.split(separator: "/").map(String.init)
        let isHome = parts.count >= 2 && parts[0] == "Users" && parts[1] != "Shared"
        var path = isHome ? "/Users/" + parts[1] : "/"
        var result = [FileLocation(device: location.device, deviceName: location.deviceName, path: path, name: isHome ? "主目录" : "所有文件", endpoint: location.endpoint)]
        for part in parts.dropFirst(isHome ? 2 : 0) {
            path = path == "/" ? "/" + part : path + "/" + part
            result.append(FileLocation(device: location.device, deviceName: location.deviceName, path: path, name: part, endpoint: location.endpoint))
        }
        return result
    }
    var body: some View {
        ScrollViewReader { reader in
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 4) {
                    ForEach(locations, id: \.path) { item in
                        if item.path != locations.first?.path { Image(systemName: "chevron.right").font(.caption2).foregroundStyle(.tertiary) }
                        Button { navigate(item) } label: {
                            Text(item.name).font(.subheadline.weight(item.path == location.path ? .semibold : .regular))
                                .fixedSize().frame(minWidth: 44, minHeight: 44)
                                .foregroundStyle(item.path == location.path ? OSTheme.ink : FileBrowserPalette.accent)
                        }
                        .buttonStyle(.plain).disabled(item.path == location.path)
                        .accessibilityLabel(item.path == location.path ? "当前文件夹，" + item.name : "前往" + item.name)
                        .id(item.path)
                    }
                }
            }
            .onAppear { reader.scrollTo(location.path, anchor: .trailing) }
        }
    }
}

private struct FilePathDisclosure: View {
    let path: String
    var body: some View {
        DisclosureGroup {
            Text(path).font(.caption).foregroundStyle(.secondary).textSelection(.enabled).fixedSize(horizontal: false, vertical: true).padding(.top, 4)
        } label: { Text("完整路径").font(.caption).foregroundStyle(.secondary).frame(minHeight: 44) }
    }
}

private struct FileBrowserRow: View {
    let entry: BrowsedFile
    let searchRoot: String?
    private var relativeFolder: String? {
        guard let root = searchRoot else { return nil }
        let parent = URL(fileURLWithPath: entry.path).deletingLastPathComponent().path
        if parent == root { return nil }
        let prefix = root == "/" ? "/" : root + "/"
        return parent.hasPrefix(prefix) ? String(parent.dropFirst(prefix.count)) : parent
    }
    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: entry.icon).font(.title3).foregroundStyle(FileBrowserPalette.accent).frame(width: 28, height: 28)
            VStack(alignment: .leading, spacing: 5) {
                Text(entry.name).font(.subheadline.weight(.medium)).fixedSize(horizontal: false, vertical: true).multilineTextAlignment(.leading)
                if let folder = relativeFolder { Text(folder).font(.caption2).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true).multilineTextAlignment(.leading) }
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 8) { metadata }
                    VStack(alignment: .leading, spacing: 3) { metadata }
                }.font(.caption2).foregroundStyle(.secondary)
            }
            Spacer(minLength: 2)
            Image(systemName: entry.isDirectory ? "chevron.right" : "eye").font(.caption).foregroundStyle(.tertiary).padding(.top, 7)
        }
        .padding(.horizontal, 16).padding(.vertical, 14)
        .frame(maxWidth: .infinity, minHeight: 60, alignment: .leading)
        .contentShape(Rectangle()).opacity(entry.readable == false ? 0.65 : 1)
    }
    @ViewBuilder private var metadata: some View {
        if entry.readable == false { Label("无读取权限", systemImage: "lock").fixedSize() }
        else if entry.isDirectory { Text("文件夹").fixedSize() }
        else if let size = entry.size { Text(ByteCountFormatter.string(fromByteCount: size, countStyle: .file)).fixedSize() }
        if let modified = entry.modified { Text(Date(timeIntervalSince1970: modified), format: .dateTime.month().day().hour().minute()).fixedSize() }
    }
}

@MainActor private enum FilePreviewStorage {
    static let prefix = "PersonalOS-FilePreview-"
    static let largeFileBytes: Int64 = 10 * 1024 * 1024
    private static var activeDirectories = Set<URL>()
    static func register(_ directory: URL) { activeDirectories.insert(directory.standardizedFileURL) }
    static func release(_ directory: URL) { activeDirectories.remove(directory.standardizedFileURL) }
    static func requiresConfirmation(size: Int64, approvedSize: Int64?) -> Bool {
        size > largeFileBytes && (approvedSize == nil || size > approvedSize!)
    }
    static func removeOrphans(in root: URL = FileManager.default.temporaryDirectory) {
        // A new process has no active previews, so this also cleans files left by a crash
        // or force-quit. A refresh must never remove a preview still being shared.
        let manager = FileManager.default
        guard let candidates = try? manager.contentsOfDirectory(at: root, includingPropertiesForKeys: [.isDirectoryKey, .isSymbolicLinkKey]) else { return }
        for directory in candidates {
            let name = directory.lastPathComponent
            guard name.hasPrefix(prefix), UUID(uuidString: String(name.dropFirst(prefix.count))) != nil,
                  !activeDirectories.contains(directory.standardizedFileURL),
                  let values = try? directory.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey]),
                  values.isDirectory == true, values.isSymbolicLink != true else { continue }
            // Locked-device protection may defer deletion until the next foreground load.
            try? manager.removeItem(at: directory)
        }
    }
}

@MainActor private final class FilePreviewModel: ObservableObject {
    let location: FileLocation
    @Published var preview: FileReadResponse?
    @Published var loading = false
    @Published var downloading = false
    @Published var error: String?
    @Published var notice: String?
    @Published private(set) var localURL: URL?
    @Published private(set) var downloadedBytes: Int64 = 0
    @Published private(set) var expectedBytes: Int64 = 0
    @Published var downloadConfirmationBytes: Int64?
    private(set) var isClosed = false
    private var temporaryDirectory: URL?
    private var downloadTask: Task<URL?, Never>?
    init(location: FileLocation) { self.location = location }
    func load() async {
        guard !isClosed else { return }
        loading = true; error = nil; defer { loading = false }
        do {
            let result = try await FileBrowserAPI.request("/files/read", location: location, as: FileReadResponse.self)
            guard !isClosed else { return }
            guard result.path == location.path, result.encoding == "utf-8", result.content.utf8.count <= 262_144 else { throw PersonalOSError.message("文件预览格式或大小不符合约定，请重新打开文件") }
            preview = result
        } catch is CancellationError { return }
        catch { if !isClosed { self.error = error.localizedDescription } }
    }
    func download(forSystemPreview: Bool = false, approvedLargeFileSize: Int64? = nil) async -> URL? {
        guard !isClosed, downloadTask == nil else { return nil }
        if let localURL { return localURL }
        let task = Task { await downloadChunks(forSystemPreview: forSystemPreview, approvedLargeFileSize: approvedLargeFileSize) }
        downloadTask = task
        let result = await task.value
        downloadTask = nil
        return result
    }
    func cancelDownload() { downloadTask?.cancel() }
    func close() {
        isClosed = true
        downloadConfirmationBytes = nil
        cancelDownload()
        // The running task closes its handle and removes its partial file in defer.
        // A completed download has no open writer and can be removed immediately.
        if !downloading { removeLocalPreview() }
    }
    private func downloadChunks(forSystemPreview: Bool, approvedLargeFileSize: Int64?) async -> URL? {
        guard !isClosed else { return nil }
        downloading = true; error = nil; notice = nil; downloadedBytes = 0; expectedBytes = preview?.size ?? 0
        downloadConfirmationBytes = nil
        var completed = false
        var handle: FileHandle?
        defer {
            do { try handle?.close() } catch { self.error = "关闭手机临时文件失败：\(error.localizedDescription)" }
            if !completed || isClosed { removeLocalPreview() }
            downloading = false
        }
        do {
            _ = try FileBrowserAPI.endpoint(location.endpoint)
            if temporaryDirectory != nil {
                removeLocalPreview()
                guard temporaryDirectory == nil else { throw PersonalOSError.message("此前临时文件尚未清理，请关闭预览后重试") }
            }
            let manager = FileManager.default
            var offset: Int64 = 0
            var expectedETag: String?
            var expectedModified: Double?
            var fileURL: URL?
            while true {
                try Task.checkCancellation()
                var extra = ["offset": String(offset), "max_bytes": "262144"]
                if let expectedETag { extra["etag"] = expectedETag }
                let chunk = try await FileBrowserAPI.request("/files/download", location: location, extra: extra, as: FileChunkResponse.self)
                try Task.checkCancellation()
                guard chunk.path == location.path, chunk.encoding == "base64", !chunk.etag.isEmpty,
                      chunk.size >= 0, chunk.offset == offset, chunk.next_offset >= offset, chunk.next_offset <= chunk.size,
                      chunk.content_base64.utf8.count <= 349_528, let data = Data(base64Encoded: chunk.content_base64),
                      data.count <= 262_144, chunk.next_offset - offset == Int64(data.count),
                      chunk.eof == (chunk.next_offset == chunk.size), chunk.eof || !data.isEmpty else {
                    throw PersonalOSError.message("文件分块不完整或与请求不一致，已停止下载，请重新尝试")
                }
                if let expectedETag {
                    guard chunk.etag == expectedETag, chunk.size == expectedBytes, chunk.modified == expectedModified else {
                        throw PersonalOSError.message("Mac 上的文件在下载中已改变，已停止并清理未完成内容，请重新下载")
                    }
                } else {
                    expectedETag = chunk.etag; expectedModified = chunk.modified; expectedBytes = chunk.size
                    if FilePreviewStorage.requiresConfirmation(size: chunk.size, approvedSize: approvedLargeFileSize) {
                        // The file may have grown since /files/read or since the last prompt.
                        // Inspect only the bounded first chunk; create no local copy and fetch
                        // no further chunks until the owner confirms its current size.
                        downloadConfirmationBytes = chunk.size
                        notice = "文件当前大小需要确认后才能继续下载"
                        return nil
                    }
                    if forSystemPreview && chunk.size > 10 * 1024 * 1024 {
                        throw PersonalOSError.message("文件超过系统预览的 10 MB 范围，请使用“下载原文件”后分享或保存")
                    }
                    let capacity = try manager.temporaryDirectory.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey]).volumeAvailableCapacityForImportantUsage
                    if let capacity, chunk.size > capacity { throw PersonalOSError.message("iPhone 可用空间不足以保存这份文件，请先释放空间") }
                    var directory = manager.temporaryDirectory.appendingPathComponent(FilePreviewStorage.prefix + UUID().uuidString, isDirectory: true)
                    try manager.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.protectionKey: FileProtectionType.complete])
                    temporaryDirectory = directory
                    FilePreviewStorage.register(directory)
                    var values = URLResourceValues(); values.isExcludedFromBackup = true
                    try directory.setResourceValues(values)
                    var name = URL(fileURLWithPath: chunk.name).lastPathComponent
                    name = String(name.unicodeScalars.filter { !CharacterSet.controlCharacters.contains($0) })
                    if name.isEmpty || name == "." || name == ".." { name = "预览文件" }
                    var url = directory.appendingPathComponent(name, isDirectory: false)
                    guard manager.createFile(atPath: url.path, contents: nil, attributes: [.protectionKey: FileProtectionType.complete]) else {
                        throw PersonalOSError.message("无法在 iPhone 创建临时文件，请检查可用空间")
                    }
                    try url.setResourceValues(values)
                    fileURL = url; handle = try FileHandle(forWritingTo: url)
                }
                guard let handle, let fileURL else { throw PersonalOSError.message("手机临时文件尚未准备好") }
                try handle.write(contentsOf: data)
                offset = chunk.next_offset; downloadedBytes = offset
                if chunk.eof {
                    try handle.synchronize()
                    try Task.checkCancellation()
                    localURL = fileURL; completed = true
                    notice = "原文件已准备好，可通过系统分享保存到“文件”或其他位置。离开本页会清理这份临时副本。"
                    return fileURL
                }
            }
        } catch is CancellationError {
            notice = "下载已取消，未完成的手机临时文件会被清理"
            return nil
        } catch {
            if Task.isCancelled { notice = "下载已取消，未完成的手机临时文件会被清理" }
            else { self.error = error.localizedDescription }
            return nil
        }
    }
    func removeLocalPreview() {
        guard let temporaryDirectory else { return }
        // This directory is unique to the current preview and never contains user files.
        do {
            try FileManager.default.removeItem(at: temporaryDirectory)
            FilePreviewStorage.release(temporaryDirectory)
            self.temporaryDirectory = nil; localURL = nil
        }
        catch { self.error = "手机临时预览未能清理：\(error.localizedDescription)" }
    }
}

private struct FilePresentation: Identifiable {
    let id = UUID()
    let url: URL
    let share: Bool
}
private struct FilePreviewScreen: View {
    let location: FileLocation
    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var model: FilePreviewModel
    @State private var presentation: FilePresentation?
    @State private var confirmingDownload = false
    @State private var editingDocument=false
    @State private var macRequest:MacRequestSelection?
    init(location: FileLocation) {
        self.location = location
        _model = StateObject(wrappedValue: FilePreviewModel(location: location))
    }
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    VStack(alignment: .leading, spacing: 10) {
                        Text(location.name).font(.title3.weight(.semibold)).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                        HStack(spacing: 8) {
                            Text(location.deviceName)
                            if let preview = model.preview { Text(ByteCountFormatter.string(fromByteCount: preview.size, countStyle: .file)) }
                            Spacer(minLength: 0)
                            Label("只读", systemImage: "lock")
                        }.font(.caption).foregroundStyle(.secondary)
                        FilePathDisclosure(path: location.path)
                        if let preview = model.preview {
                            if preview.download_allowed == true {
                                ViewThatFits(in: .horizontal) {
                                    HStack(spacing: 12) { fileActions }
                                    VStack(alignment: .leading, spacing: 8) { fileActions }
                                }
                            } else { Text("此文件暂不支持下载。").font(.caption).foregroundStyle(.secondary) }
                        }
                    }
                    macControls
                    if let error = model.error { FileBrowserError(message: error) { Task { await model.load() } } }
                    if let notice = displayNotice { Text(notice).font(.caption).foregroundStyle(.secondary) }
                    if model.loading { ProgressView("正在读取…").frame(maxWidth: .infinity).padding(.vertical, 20) }
                    if model.downloading {
                        FileBrowserCard {
                            VStack(alignment: .leading, spacing: 10) {
                                HStack {
                                    Text("正在下载").font(.subheadline.weight(.medium))
                                    Spacer()
                                    Button("取消", role: .cancel) { model.cancelDownload() }.font(.subheadline).frame(minWidth: 44, minHeight: 44)
                                }
                                ProgressView(value: Double(model.downloadedBytes), total: Double(max(model.expectedBytes, 1)))
                                Text("\(ByteCountFormatter.string(fromByteCount: model.downloadedBytes, countStyle: .file)) / \(ByteCountFormatter.string(fromByteCount: model.expectedBytes, countStyle: .file))")
                                    .font(.caption).foregroundStyle(.secondary).monospacedDigit()
                            }
                        }
                    }
                    if let preview = model.preview {
                        FileBrowserCard {
                            VStack(alignment: .leading, spacing: 12) {
                                if preview.truncated == true {
                                    Text("预览前 \(ByteCountFormatter.string(fromByteCount: Int64(preview.limit_bytes ?? 65_536), countStyle: .file))")
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                                if preview.content.isEmpty { Text("空文件").font(.subheadline).foregroundStyle(.secondary) }
                                else {
                                    Text(preview.content).font(preview.previewable == true ? .system(.callout, design: .monospaced) : .callout)
                                        .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                                }
                            }
                        }
                    }
                }.padding(20)
            }
            .background(FileBrowserPalette.background)
            .navigationTitle("预览").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button { closePreview() } label: {
                        Label(model.downloading ? "取消并关闭" : "关闭", systemImage: "xmark").frame(minHeight: 44)
                    }
                    .accessibilityLabel("关闭文件预览")
                    .accessibilityHint("返回文件列表；正在进行的下载会取消并清理临时副本")
                    .accessibilityIdentifier("filePreview.close")
                }
            }
            .task { await model.load() }
            .sheet(item: $presentation) { item in
                if item.share { FileShareSheet(url: item.url) }
                else { FileSystemPreviewScreen(url: item.url) { presentation = nil } }
            }
            .sheet(isPresented:$editingDocument){MacDocumentEditSheet(device:location.device,path:location.path,etag:model.preview?.etag ?? "")}
            .sheet(item:$macRequest){MacActionReviewSheet(taskID:$0.id)}
            .confirmationDialog("下载这份文件？", isPresented: $confirmingDownload, titleVisibility: .visible) {
                Button("下载") { prepareFile(preview: false, approvedLargeFileSize: model.downloadConfirmationBytes) }
                Button("取消", role: .cancel) { model.downloadConfirmationBytes = nil }
            } message: {
                Text("约 \(ByteCountFormatter.string(fromByteCount: model.downloadConfirmationBytes ?? model.preview?.size ?? 0, countStyle: .file))。下载后可保存或分享，离开预览会清理临时副本。请保持 App 前台。")
            }
            .onChange(of: scenePhase) { _, phase in if phase == .background && model.downloading { model.cancelDownload() } }
            .onDisappear {
                if presentation == nil { model.close() }
            }
        }
    }
    @ViewBuilder private var macControls:some View {
                    if let preview=model.preview,preview.size<=5*1024*1024,["txt","md","docx"].contains((location.path as NSString).pathExtension.lowercased()) {
                        Button("让PersonalOS修改文档"){editingDocument=true}.buttonStyle(.bordered)
                    }
                    if location.device=="mini" {
                        HStack{Button("Mac打开"){prepareMac("open_file")};Spacer();Button("Finder显示"){prepareMac("reveal_file")}}.buttonStyle(.bordered)
                    }
    }
    private func prepareMac(_ operation:String){Task{do{macRequest=MacRequestSelection(id:try await MacTaskClient.create(["operation":operation,"path":location.path]))}catch{model.error=error.localizedDescription}}}
    private var displayNotice: String? {
        guard let notice = model.notice else { return nil }
        if notice.hasPrefix("原文件已准备好") { return "文件已备好，可保存或分享。" }
        if notice.hasPrefix("下载已取消") { return "下载已取消。" }
        if notice == "文件当前大小需要确认后才能继续下载" { return "文件大小已变化，请确认后继续。" }
        return notice
    }
    @ViewBuilder private var fileActions: some View {
        if let url = model.localURL {
            if model.expectedBytes <= 10 * 1024 * 1024 {
                Button { presentation = FilePresentation(url: url, share: false) } label: { Label("预览", systemImage: "eye").frame(minHeight: 44) }.buttonStyle(.borderedProminent)
            }
            Button { presentation = FilePresentation(url: url, share: true) } label: { Label("分享", systemImage: "square.and.arrow.up").frame(minHeight: 44) }.buttonStyle(.bordered)
        } else {
            if let preview = model.preview, preview.size <= 10 * 1024 * 1024 {
                Button { prepareFile(preview: true) } label: { Label("预览", systemImage: "eye").frame(minHeight: 44) }.buttonStyle(.bordered).disabled(model.downloading)
            }
            Button {
                if let preview = model.preview, preview.size > 10 * 1024 * 1024 {
                    model.downloadConfirmationBytes = preview.size; confirmingDownload = true
                }
                else { prepareFile(preview: false) }
            } label: { Label("下载", systemImage: "arrow.down.circle").frame(minHeight: 44) }.buttonStyle(.borderedProminent).disabled(model.downloading)
        }
    }
    private func prepareFile(preview: Bool, approvedLargeFileSize: Int64? = nil) {
        Task {
            let url = await model.download(forSystemPreview: preview, approvedLargeFileSize: approvedLargeFileSize)
            guard !model.isClosed else { return }
            if let url, preview {
                presentation = FilePresentation(url: url, share: false)
            } else if model.downloadConfirmationBytes != nil { confirmingDownload = true }
        }
    }
    private func closePreview() {
        confirmingDownload = false
        model.close()
        dismiss()
    }
}

private struct FileSystemPreviewScreen: View {
    let url: URL
    let close: () -> Void
    var body: some View {
        // Quick Look is a separate sheet: the outer preview toolbar is obscured.
        // Keep this close bar outside QLPreviewController so its content, loading
        // state, or an unsupported-file error can never hide the exit control.
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                Button(action: close) { Label("关闭", systemImage: "xmark").frame(minHeight: 44) }
                    .accessibilityLabel("关闭系统预览")
                    .accessibilityHint("返回文件预览页面")
                    .accessibilityIdentifier("systemPreview.close")
                Spacer(minLength: 12)
                Text("系统预览").font(.headline)
                Spacer(minLength: 12)
            }
            .padding(.horizontal, 20).padding(.vertical, 4)
            .background(.bar)
            Divider()
            FileQuickLook(url: url).frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .background(FileBrowserPalette.surface)
        .tint(FileBrowserPalette.accent)
        .presentationDragIndicator(.visible)
    }
}

private struct FileQuickLook: UIViewControllerRepresentable {
    let url: URL
    func makeCoordinator() -> Coordinator { Coordinator(url: url) }
    func makeUIViewController(context: Context) -> QLPreviewController {
        let controller = QLPreviewController(); controller.dataSource = context.coordinator
        return controller
    }
    func updateUIViewController(_ uiViewController: QLPreviewController, context: Context) {}
    final class Coordinator: NSObject, QLPreviewControllerDataSource {
        let url: URL
        init(url: URL) { self.url = url }
        func numberOfPreviewItems(in controller: QLPreviewController) -> Int { 1 }
        func previewController(_ controller: QLPreviewController, previewItemAt index: Int) -> QLPreviewItem { url as NSURL }
    }
}
private struct FileShareSheet: UIViewControllerRepresentable {
    let url: URL
    func makeUIViewController(context: Context) -> UIActivityViewController { UIActivityViewController(activityItems: [url], applicationActivities: nil) }
    func updateUIViewController(_ uiViewController: UIActivityViewController, context: Context) {}
}

private enum FileBrowserPalette {
    static var background: Color { OSTheme.background }
    static var surface: Color { OSTheme.surface }
    static var accent: Color { OSTheme.accent }
}

private struct FileBrowserCard<Content: View>: View {
    let content: Content
    init(@ViewBuilder content: () -> Content) { self.content = content() }
    var body: some View { content.padding(16).frame(maxWidth: .infinity, alignment: .leading).background(FileBrowserPalette.surface, in: RoundedRectangle(cornerRadius: 16)) }
}
private struct FileBrowserError: View {
    let message: String
    let retry: () -> Void
    var body: some View {
        FileBrowserCard {
            VStack(alignment: .leading, spacing: 10) {
                Label("无法读取", systemImage: "exclamationmark.circle").font(.subheadline.weight(.semibold))
                Text(message).font(.caption).foregroundStyle(.secondary)
                Button("重试", action: retry).font(.subheadline).frame(minHeight: 44)
            }
        }
    }
}
