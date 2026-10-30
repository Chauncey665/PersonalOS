import SwiftUI

@MainActor struct TodayScreen: View {
    @ObservedObject var agenda: DailyAgendaModel
    let tasks: [TaskRecord]
    var openSettings: (() -> Void)? = nil
    var refresh: (() async -> Void)? = nil
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    @Environment(\.colorScheme) private var colorScheme
    @ScaledMetric(relativeTo: .largeTitle) private var countFontSize = 62.0
    @State private var displayedProgress = 0.0
    @State private var expandedItems = Set<String>()
    @State private var section = 0

    private var total: Int { agenda.items.count }
    private var completed: Int { agenda.items.filter(\.isCompleted).count }
    private var progress: Double { total > 0 ? Double(completed) / Double(total) : 0 }
    private var percent: Int { Int((progress * 100).rounded()) }
    private var unavailable: Bool { total == 0 && agenda.error != nil }
    private var ringProgress: Double { max(0, min(1, displayedProgress)) }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    header
                    Picker("事项类型", selection: $section) {
                        Text("今天").tag(0)
                        Text("待办").tag(1)
                    }.pickerStyle(.segmented)
                    if section == 1 {
                        TodoBoardView(refreshToken: tasks.map { $0.id + $0.state }.joined(separator: "|"))
                    } else {
                    progressRing
                        .frame(maxWidth: 280)
                        .aspectRatio(1, contentMode: .fit)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 16)
                    if total == 0 && !agenda.loading && !unavailable {
                        Text("今天没有事项").font(.subheadline).foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity).padding(.bottom, 12)
                    }
                    if let error = agenda.error {
                        VStack(alignment: .leading, spacing: 4) {
                            Text(error).font(.caption).foregroundStyle(.orange)
                            Button("重试") { Task { await agenda.refresh(tasks: tasks) } }
                                .font(.subheadline).frame(minHeight: 44).disabled(agenda.loading)
                        }
                    }
                    LazyVStack(spacing: 0) {
                        ForEach(agenda.items) { item in
                            agendaRow(item)
                            if item.id != agenda.items.last?.id { Divider().padding(.leading, 48) }
                        }
                    }
                    }
                }.padding(.horizontal, 24).padding(.top, 14).padding(.bottom, 28)
            }
            .background(pageBackground)
            .toolbar(.hidden, for: .navigationBar)
            .refreshable {
                if let refresh { await refresh() }
                else { await agenda.refresh(tasks: tasks) }
                if section == 1 { await TodoModel.shared.refresh(force: true) }
                await AutoScheduleModel.shared.refresh(force: true)
                await agenda.refresh(tasks: tasks)
            }
        }
        .onAppear { updateProgress() }
        .onChange(of: completed) { _, _ in updateProgress() }
        .onChange(of: total) { _, _ in updateProgress() }
        .onChange(of: reduceMotion) { _, _ in updateProgress() }
        .onChange(of: agenda.items.map(\.id)) { _, ids in expandedItems.formIntersection(ids) }
    }

    private var header: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 5) {
                HStack(spacing: 10) {
                    Text(section == 0 ? "今天" : "待办").font(.system(.largeTitle, design: .rounded).weight(.semibold)).tracking(-0.6)
                    if agenda.loading { ProgressView().controlSize(.small).accessibilityLabel("正在刷新今天的事项") }
                }
                if section == 0 {
                    Text(Date(), format: .dateTime.month().day().weekday(.wide))
                        .font(.subheadline).foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 0)
            if let openSettings {
                Button(action: openSettings) {
                    Image(systemName: "gearshape").font(.system(size: 20)).foregroundStyle(.secondary)
                        .frame(width: 44, height: 44)
                }.buttonStyle(.plain).accessibilityLabel("设置")
            }
        }
    }

    private var pageBackground: some View {
        OSTheme.background
            .overlay {
                if !reduceTransparency {
                    RadialGradient(colors: [OSTheme.accent.opacity(colorScheme == .dark ? 0.13 : 0.10), .clear],
                        center: UnitPoint(x: 0.78, y: 0.18), startRadius: 10, endRadius: 370)
                }
            }
            .ignoresSafeArea()
    }

    private var progressRing: some View {
        ZStack {
            glassTrack
            Circle()
                .trim(from: 0, to: ringProgress)
                .stroke(progressTint, style: StrokeStyle(lineWidth: 24, lineCap: .round))
                .rotationEffect(.degrees(-90))
                .padding(14)
                .shadow(color: reduceTransparency ? .clear : OSTheme.accent.opacity(0.14), radius: 6, y: 3)
            if !reduceTransparency {
                // The inner and outer highlights follow the same real progress
                // arc, so the glass is in the ring itself rather than a flat disc.
                Circle().trim(from: 0, to: ringProgress)
                    .stroke(LinearGradient(colors: [.white.opacity(0.72), .white.opacity(0.06)],
                        startPoint: .topLeading, endPoint: .bottomTrailing), lineWidth: 1.1)
                    .rotationEffect(.degrees(-90)).padding(2.5)
                Circle().trim(from: 0, to: ringProgress)
                    .stroke(LinearGradient(colors: [.white.opacity(0.10), .white.opacity(0.42)],
                        startPoint: .topLeading, endPoint: .bottomTrailing), lineWidth: 1)
                    .rotationEffect(.degrees(-90)).padding(25.5)
            }
            VStack(spacing: 8) {
                Text(unavailable ? "—" : "\(completed)/\(total)")
                    .font(.system(size: min(countFontSize, 94), weight: .medium, design: .rounded))
                    .tracking(-1.5).monospacedDigit().lineLimit(1).minimumScaleFactor(0.5)
                    .foregroundStyle(OSTheme.ink)
                if !unavailable {
                    Text("\(percent)%").font(.title3.weight(.regular)).monospacedDigit().foregroundStyle(.secondary)
                }
            }.padding(44)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("今日进度")
        .accessibilityValue(unavailable ? "暂时无法读取" : "已完成 \(completed) 项，共 \(total) 项，百分之 \(percent)")
    }

    private var progressTint: LinearGradient {
        let opacity = reduceTransparency ? 1.0 : colorScheme == .dark ? 0.68 : 0.60
        return LinearGradient(colors: [OSTheme.accent.opacity(opacity), OSTheme.accent.opacity(reduceTransparency ? 1 : opacity * 0.68)],
            startPoint: .topLeading, endPoint: .bottomTrailing)
    }

    @ViewBuilder private var glassTrack: some View {
        if reduceTransparency {
            Circle().stroke(Color.secondary.opacity(0.18), lineWidth: 24).padding(14)
        } else {
            Circle().stroke(.ultraThinMaterial, lineWidth: 24).padding(14)
                .shadow(color: colorScheme == .dark ? .black.opacity(0.28) : OSTheme.accent.opacity(0.10), radius: 14, y: 8)
            Circle().stroke(OSTheme.accent.opacity(colorScheme == .dark ? 0.08 : 0.035), lineWidth: 24).padding(14)
            Circle().stroke(LinearGradient(colors: [.white.opacity(colorScheme == .dark ? 0.35 : 0.80), .white.opacity(0.04)],
                startPoint: .topLeading, endPoint: .bottomTrailing), lineWidth: 1).padding(2)
            Circle().stroke(LinearGradient(colors: [.white.opacity(0.05), .white.opacity(colorScheme == .dark ? 0.20 : 0.65)],
                startPoint: .topLeading, endPoint: .bottomTrailing), lineWidth: 1).padding(26)
        }
    }

    private func agendaRow(_ item: DailyAgendaItem) -> some View {
        let expanded = expandedItems.contains(item.id)
        let busy = agenda.busyIDs.contains(item.id)
        return VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .top, spacing: 4) {
                Button {
                    Task { await agenda.toggle(item, tasks: tasks) }
                } label: {
                    ZStack {
                        if busy { ProgressView().controlSize(.small) }
                        else {
                            Circle().stroke(item.isCompleted ? OSTheme.accent : Color.secondary.opacity(0.55), lineWidth: 1.5)
                                .background(Circle().fill(item.isCompleted ? OSTheme.accent.opacity(0.10) : .clear))
                                .frame(width: 23, height: 23)
                            if item.isCompleted {
                                Image(systemName: "checkmark").font(.system(size: 11, weight: .semibold)).foregroundStyle(OSTheme.accent)
                            }
                        }
                    }.frame(width: 44, height: 44).contentShape(Rectangle())
                }
                .buttonStyle(.plain).disabled(busy || !item.canToggle)
                .accessibilityLabel((item.isCompleted ? "标为未完成：" : "完成：") + item.title)
                .accessibilityValue(busy ? "正在保存" : item.isCompleted ? "已完成" : "未完成")
                .accessibilityHint(item.restriction ?? "保存后更新今日进度")

                Button {
                    changeLayout {
                        if expanded { expandedItems.remove(item.id) }
                        else { expandedItems.insert(item.id) }
                    }
                } label: {
                    HStack(alignment: .top, spacing: 8) {
                        VStack(alignment: .leading, spacing: 5) {
                            Text(item.title).font(.subheadline.weight(.medium))
                                .foregroundStyle(item.isCompleted ? Color.secondary : Color.primary)
                                .multilineTextAlignment(.leading).lineLimit(expanded ? nil : 2)
                            Text(shortTime(item)).font(.caption).foregroundStyle(.secondary)
                        }.frame(maxWidth: .infinity, alignment: .leading)
                        Image(systemName: expanded ? "chevron.up" : "chevron.down")
                            .font(.caption2.weight(.medium)).foregroundStyle(.tertiary).padding(.top, 4)
                    }.frame(minHeight: 44, alignment: .center).contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(item.title + "，" + shortTime(item))
                .accessibilityHint(expanded ? "收起详情" : "展开时间和来源")
            }
            if expanded {
                VStack(alignment: .leading, spacing: 9) {
                    Label(fullTime(item), systemImage: "clock")
                    Label(item.calendarName, systemImage: item.isReminder ? "bell" : "calendar")
                    if let restriction = item.restriction { Text(restriction).foregroundStyle(.orange) }
                    if let note = item.completionNote { Text(note) }
                }
                .font(.caption).foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.leading, 48).padding(.top, 8).padding(.bottom, 4)
                .transition(.opacity)
            }
        }.padding(.vertical, 10)
    }

    private func shortTime(_ item: DailyAgendaItem) -> String {
        if item.isAllDay { return "全天" }
        if item.isReminder { return item.start.formatted(date: .omitted, time: .shortened) }
        return item.start.formatted(date: .omitted, time: .shortened) + "–" + item.end.formatted(date: .omitted, time: .shortened)
    }
    private func fullTime(_ item: DailyAgendaItem) -> String {
        let day = item.start.formatted(.dateTime.month().day())
        if item.isAllDay { return day + " · 全天" }
        if item.isReminder || Calendar.current.isDate(item.start, inSameDayAs: item.end) {
            return day + " · " + shortTime(item)
        }
        return item.start.formatted(date: .abbreviated, time: .shortened) + " — " + item.end.formatted(date: .abbreviated, time: .shortened)
    }
    private func updateProgress() {
        if reduceMotion { displayedProgress = progress }
        else { withAnimation(.easeInOut(duration: 0.5)) { displayedProgress = progress } }
    }
    private func changeLayout(_ change: () -> Void) {
        if reduceMotion { change() }
        else { withAnimation(.easeInOut(duration: 0.24), change) }
    }
}
