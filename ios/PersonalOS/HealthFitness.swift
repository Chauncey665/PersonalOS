import SwiftUI
import HealthKit

struct FitnessProfile: Codable {
    var goal = "", sport = "", experience = "", restrictions = "", equipment = "", training_schedule = ""
    var adult = false
}
struct HealthMetric: Codable { var value: Double; var unit: String; var measured_at: Double; var source: String }
struct FitnessSession: Codable, Identifiable { var key: String; var day: String; var title: String; var minutes: Int; var exercises: [String]; var id: String {key} }
struct FitnessPlan: Codable { var id: String; var committed: Bool; var sessions: [FitnessSession]; var basis: String; var guidance: String }
struct FitnessPlanEnvelope: Codable { var plan: FitnessPlan? }
struct FitnessStatus: Codable {var connected: Bool; var fresh: Bool; var captured_at: Double?; var metrics: [String: HealthMetric]; var profile: FitnessProfile; var coverage: String}

@MainActor final class HealthFitness: ObservableObject {
    static let shared = HealthFitness()
    @Published var status: FitnessStatus?
    @Published var plan: FitnessPlan?
    @Published var message = "尚未共享健康数据"
    @Published var busy = false
    @Published private(set) var enabled = UserDefaults.standard.bool(forKey: "PersonalOS.health.enabled")
    private let store = HKHealthStore()
    private var lastSync = Date.distantPast
    private var enabledEndpoint: String { UserDefaults.standard.string(forKey: "PersonalOS.health.endpoint") ?? "" }
    private let specs: [(String, HKQuantityTypeIdentifier, HKUnit, String, Bool)] = [
        ("weight_kg", .bodyMass, .gramUnit(with: .kilo), "kg", false),
        ("height_cm", .height, .meterUnit(with: .centi), "cm", false),
        ("steps", .stepCount, .count(), "steps", true),
        ("active_kcal", .activeEnergyBurned, .kilocalorie(), "kcal", true),
        ("resting_hr", .restingHeartRate, HKUnit.count().unitDivided(by: .minute()), "bpm", false),
        ("dietary_kcal", .dietaryEnergyConsumed, .kilocalorie(), "kcal", true),
        ("protein_g", .dietaryProtein, .gram(), "g", true)
    ]
    var healthKitConfigured: Bool { Bundle.main.object(forInfoDictionaryKey: "PersonalOSHealthKitEnabled") as? Bool ?? false }
    func enable() async {
        guard healthKitConfigured else { message = "此基础构建未启用 HealthKit；请按安装文档配置自己的签名能力"; return }
        guard HKHealthStore.isHealthDataAvailable() else { message = "此设备不支持健康数据"; return }
        guard API.isPaired(), let endpoint = try? API.endpoint() else { message = "请先连接私人 Mac"; return }
        var types = Set<HKObjectType>(specs.compactMap { HKObjectType.quantityType(forIdentifier: $0.1) })
        types.insert(HKObjectType.workoutType()); types.insert(HKObjectType.categoryType(forIdentifier: .sleepAnalysis)!)
        do {
            try await store.requestAuthorization(toShare: [], read: types)
            guard (try? API.endpoint().key) == endpoint.key else { throw PersonalOSError.message("连接已变化，请重新开启") }
            enabled = true; UserDefaults.standard.set(true, forKey:"PersonalOS.health.enabled")
            UserDefaults.standard.set(endpoint.key, forKey:"PersonalOS.health.endpoint")
            // Authorization completion does not prove that read access was granted.
            await sync(force: true)
        } catch { message = error.localizedDescription }
    }
    func disable() async {
        enabled = false; UserDefaults.standard.set(false, forKey:"PersonalOS.health.enabled"); lastSync = .distantPast
        message = "已关闭后续共享"
        do {
            let endpoint = try API.endpoint()
            guard endpoint.key == enabledEndpoint else { message = "已停止共享；请连接原 Mac 清除旧摘要"; return }
            _ = try await API.request("/fitness/clear", [:], endpoint:endpoint)
            message = "已停止共享并清除 Mac 当前健康摘要"; await refresh()
        } catch { message = "已停止共享，Mac 摘要尚未清除：\(error.localizedDescription)" }
    }
    func refresh() async {
        do {status = try JSONDecoder().decode(FitnessStatus.self, from: await API.request("/fitness/status"))}
        catch {message = error.localizedDescription}
    }
    func loadPlan() async {
        do {plan = try JSONDecoder().decode(FitnessPlanEnvelope.self,from:await API.request("/fitness/plan")).plan} catch {message=error.localizedDescription}
    }
    func previewPlan() async {
        do {plan = try JSONDecoder().decode(FitnessPlanEnvelope.self,from:await API.request("/fitness/preview",[:])).plan;message="训练草案已生成，尚未安排日历"} catch {message=error.localizedDescription}
    }
    func commitPlan() async {
        guard let plan else {return}
        do {let data=try await API.request("/fitness/commit",["id":plan.id]);let reply=try JSONSerialization.jsonObject(with:data) as? [String:Any];message=reply?["message"] as? String ?? "已返回处理结果";await loadPlan()}
        catch {message=error.localizedDescription}
    }
    func save(_ profile: FitnessProfile) async {
        do {
            let payload = String(data:try JSONEncoder().encode(profile),encoding:.utf8)!
            _ = try await API.request("/fitness/profile",["payload":payload]); await refresh(); message = "训练资料已保存到私人 Mac"
        } catch { message = error.localizedDescription }
    }
    private func samples(_ type: HKSampleType, since: Date, limit: Int = 5000) async throws -> [HKSample] {
        try await withCheckedThrowingContinuation { continuation in
            let query = HKSampleQuery(sampleType:type, predicate:HKQuery.predicateForSamples(withStart:since,end:Date(),options:.strictEndDate),limit:limit,sortDescriptors:[NSSortDescriptor(key:HKSampleSortIdentifierEndDate,ascending:false)]) { _,values,error in
                if let error {continuation.resume(throwing:error)} else {continuation.resume(returning:values ?? [])}
            }
            store.execute(query)
        }
    }
    // Prefer one Huawei source, otherwise the most recent source. Never add
    // overlapping phone/watch/app sources together; provenance remains visible.
    private func sourceGroup(_ samples:[HKSample]) -> [HKSample] {
        let groups = Dictionary(grouping:samples,by:{$0.sourceRevision.source.bundleIdentifier})
        let ordered = groups.values.sorted {
            let a = $0[0].sourceRevision.source; let b = $1[0].sourceRevision.source
            let ah = (a.name + a.bundleIdentifier).localizedCaseInsensitiveContains("huawei") || a.name.contains("华为")
            let bh = (b.name + b.bundleIdentifier).localizedCaseInsensitiveContains("huawei") || b.name.contains("华为")
            if ah != bh {return ah}
            return ($0.map(\.endDate).max() ?? .distantPast) > ($1.map(\.endDate).max() ?? .distantPast)
        }
        return ordered.first ?? []
    }
    func sync(force: Bool = false) async {
        guard healthKitConfigured else { return }
        guard enabled,!busy,API.isPaired(),force || Date().timeIntervalSince(lastSync)>900 else { return }
        guard let endpoint = try? API.endpoint(),endpoint.key == enabledEndpoint else {message = "连接已变化，请重新开启健康共享"; return}
        let token = Vault.read(for:endpoint)
        busy = true; defer {busy = false}
        do {
            let now = Date(), calendar = Calendar.current, dayStart = calendar.startOfDay(for:now)
            var metrics = [String:HealthMetric]()
            for (key,identifier,unit,label,sum) in specs {
                let type = HKObjectType.quantityType(forIdentifier:identifier)!
                let since = sum ? dayStart : now.addingTimeInterval(key == "height_cm" ? -366*86400 : -30*86400)
                let all = try await samples(type,since:since)
                if all.count >= 5000 {continue} // Never publish a truncated aggregate.
                let selected = sourceGroup(all).compactMap{$0 as? HKQuantitySample}
                guard let latest = selected.first else {continue}
                let value:Double
                if sum {
                    // Partial intervals crossing midnight cannot be allocated reliably.
                    let today = selected.filter{$0.startDate >= dayStart}
                    var accepted = [HKQuantitySample]()
                    for sample in today.sorted(by:{$0.startDate < $1.startDate}) {
                        if accepted.contains(where:{$0.startDate == sample.startDate && $0.endDate == sample.endDate && $0.quantity.doubleValue(for:unit) == sample.quantity.doubleValue(for:unit)}) {continue}
                        accepted.append(sample)
                    }
                    guard !accepted.isEmpty else {continue}
                    if key == "steps" || key == "active_kcal" {
                        let ordered = accepted.sorted{$0.startDate < $1.startDate}
                        if zip(ordered,ordered.dropFirst()).contains(where:{$0.0.endDate > $0.1.startDate}) {continue}
                    }
                    value = accepted.reduce(0){$0 + $1.quantity.doubleValue(for:unit)}
                } else {value = latest.quantity.doubleValue(for:unit)}
                metrics[key] = HealthMetric(value:value,unit:label,measured_at:latest.endDate.timeIntervalSince1970,source:latest.sourceRevision.source.name)
            }
            let sleepType = HKObjectType.categoryType(forIdentifier:.sleepAnalysis)!
            let sleepAll = try await samples(sleepType,since:now.addingTimeInterval(-36*3600))
            let asleep = sourceGroup(sleepAll).compactMap{$0 as? HKCategorySample}.filter{[HKCategoryValueSleepAnalysis.asleepUnspecified.rawValue,HKCategoryValueSleepAnalysis.asleepCore.rawValue,HKCategoryValueSleepAnalysis.asleepDeep.rawValue,HKCategoryValueSleepAnalysis.asleepREM.rawValue].contains($0.value)}
            if sleepAll.count < 5000, let latest = asleep.max(by:{$0.endDate < $1.endDate}) {
                // Last sleep episode only; merge overlapping stages, exclude inBed/awake.
                let sorted = asleep.sorted{$0.startDate < $1.startDate}
                var intervals = [(Date,Date)]()
                for item in sorted {
                    if let last = intervals.last,item.startDate <= last.1 { intervals[intervals.count-1].1 = max(last.1,item.endDate) }
                    else {intervals.append((item.startDate,item.endDate))}
                }
                var total:TimeInterval = 0, previous = latest.endDate
                for pair in intervals.reversed() {
                    if previous.timeIntervalSince(pair.1)>3*3600 {break}
                    total += pair.1.timeIntervalSince(pair.0); previous = pair.0
                }
                metrics["sleep_hours"] = HealthMetric(value:total/3600,unit:"h",measured_at:latest.endDate.timeIntervalSince1970,source:latest.sourceRevision.source.name)
            }
            let workouts = try await samples(HKObjectType.workoutType(),since:now.addingTimeInterval(-7*86400),limit:51)
            let selected = workouts.count > 50 ? [] : sourceGroup(workouts).compactMap{$0 as? HKWorkout}
            var seen = Set<String>()
            let summaries:[[String:Any]] = selected.filter{seen.insert("\($0.startDate.timeIntervalSince1970)-\($0.endDate.timeIntervalSince1970)-\($0.workoutActivityType.rawValue)").inserted}.map { w in
                let kind:String
                switch w.workoutActivityType {case .running:kind="跑步";case .swimming:kind="游泳";case .walking:kind="步行";case .traditionalStrengthTraining,.functionalStrengthTraining:kind="力量训练";case .baseball:kind="棒球";case .basketball:kind="篮球";case .cycling:kind="骑行";default:kind="运动（HealthKit \(w.workoutActivityType.rawValue)）"}
                return ["kind":kind,"start":w.startDate.timeIntervalSince1970,"end":w.endDate.timeIntervalSince1970,"source":w.sourceRevision.source.name]
            }
            let day = DateFormatter();day.dateFormat="yyyy-MM-dd";day.locale=Locale(identifier:"en_US_POSIX");day.timeZone=calendar.timeZone
            let metricsObject = try JSONSerialization.jsonObject(with:JSONEncoder().encode(metrics))
            guard calendar.startOfDay(for:Date()) == dayStart else {throw PersonalOSError.message("日期已变化，请重新同步今天的数据")}
            let payload:[String:Any] = ["captured_at":Date().timeIntervalSince1970,"day":day.string(from:Date()),"time_zone":calendar.timeZone.identifier,"metrics":metricsObject,"workouts":summaries]
            guard enabled,(try? API.endpoint().key)==endpoint.key,Vault.read(for:endpoint)==token else {throw PersonalOSError.message("共享已关闭或连接已变化，没有发送摘要")}
            _ = try await API.request("/fitness/snapshot",["payload":String(data:try JSONSerialization.data(withJSONObject:payload),encoding:.utf8)!],endpoint:endpoint)
            lastSync = Date(); await refresh()
            message = metrics.isEmpty && summaries.isEmpty ? "没有读到数据。可能未授权、未同步或没有记录；不代表数值为零。" : "已同步 \(metrics.count) 项数据、\(summaries.count) 条运动记录"
        } catch {message = "健康同步未完成：\(error.localizedDescription)"}
    }
}

struct HealthFitnessView: View {
    @ObservedObject private var model = HealthFitness.shared
    @State private var profile = FitnessProfile()
    private let labels = ["weight_kg":"体重","height_cm":"身高","steps":"今日步数","active_kcal":"今日活动消耗","resting_hr":"静息心率","sleep_hours":"最近睡眠","dietary_kcal":"今日已记录摄入","protein_g":"今日已记录蛋白质"]
    var body: some View {
        Form {
            Section("健康数据") {
                Toggle("共享到私人 Mac",isOn:Binding(get:{model.enabled},set:{value in Task{if value {await model.enable()} else {await model.disable()}}})).disabled(model.busy || (!model.healthKitConfigured && !model.enabled))
                if !model.healthKitConfigured { Text("基础版未启用健康签名能力；需要时按安装文档配置自己的 HealthKit。").font(.caption).foregroundStyle(.secondary) }
                Text("只读 Apple 健康中你允许的项目。华为数据需先同步到 Apple 健康；不读取账号密码，不上传云端模型。").font(.caption).foregroundStyle(.secondary)
                Button(model.busy ? "正在同步…" : "现在同步"){Task{await model.sync(force:true)}}.disabled(!model.enabled || model.busy || !model.healthKitConfigured)
                Text(model.message).font(.caption)
                if let status = model.status {
                    if let stamp = status.captured_at {Text("摘要：\(Date(timeIntervalSince1970:stamp).formatted(date:.abbreviated,time:.shortened))\(status.fresh ? "" : " · 已过期")").font(.caption).foregroundStyle(.secondary)}
                    ForEach(status.metrics.keys.sorted(),id:\.self){key in
                        if let value=status.metrics[key] {VStack(alignment:.leading,spacing:4){LabeledContent(labels[key] ?? key,value:"\(value.value.formatted(.number.precision(.fractionLength(0...1)))) \(value.unit)");Text("\(value.source) · \(Date(timeIntervalSince1970:value.measured_at).formatted(date:.abbreviated,time:.shortened))").font(.caption2).foregroundStyle(.secondary)}}
                    }
                    Text(status.coverage).font(.caption).foregroundStyle(.secondary)
                }
            }
            Section("训练资料") {
                TextField("目标，如增肌、体能",text:$profile.goal)
                TextField("运动背景／专项",text:$profile.sport,axis:.vertical)
                TextField("当前活动量、力量训练经历",text:$profile.experience,axis:.vertical)
                TextField("伤病或禁忌；没有请填无",text:$profile.restrictions,axis:.vertical)
                TextField("可用器械；可以之后调整",text:$profile.equipment,axis:.vertical)
                TextField("专项训练与比赛；没有固定安排请说明",text:$profile.training_schedule,axis:.vertical)
                Toggle("我已成年",isOn:$profile.adult)
                Button("保存训练资料"){Task{await model.save(profile)}}
            }
            Section("恢复训练") {
                if let plan = model.plan {
                    Text(plan.basis).font(.caption).foregroundStyle(.secondary)
                    ForEach(plan.sessions){session in
                        DisclosureGroup("\(session.day) · \(session.title)") {
                            Text("约\(session.minutes)分钟").font(.caption)
                            ForEach(session.exercises,id:\.self){Text($0).font(.subheadline)}
                        }
                    }
                    Text(plan.guidance).font(.caption).foregroundStyle(.secondary)
                    if plan.committed {Text("已记录待办；实际时段与同步状态请到「今天」查看。").font(.caption)}
                    else {Button("确认安排这份计划"){Task{await model.commitPlan()}}}
                }
                if model.plan?.committed != true {Button("生成下周恢复训练草案"){Task{await model.previewPlan()}}}
            }
            Section("动作演示") {
                NavigationLink("哑铃侧平举"){ExerciseDemoView(kind:"lateral_raise")}
                NavigationLink("墙壁俯卧撑"){ExerciseDemoView(kind:"wall_pushup")}
                NavigationLink("椅子坐站"){ExerciseDemoView(kind:"sit_to_stand")}
            }
        }.navigationTitle("健康与训练").navigationBarTitleDisplayMode(.inline)
        .task{await model.refresh();await model.loadPlan();if let saved=model.status?.profile {profile=saved}}
    }
}

struct ExerciseDemoView: View {
    let kind: String
    @State private var playing = true
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    private var title: String {kind == "lateral_raise" ? "哑铃侧平举" : kind == "wall_pushup" ? "墙壁俯卧撑" : "椅子坐站"}
    var body: some View {
        ScrollView { VStack(spacing:22){
            TimelineView(.animation(minimumInterval:1/30,paused:!playing || reduceMotion)){timeline in
                let phase = playing && !reduceMotion ? (1-cos(timeline.date.timeIntervalSinceReferenceDate * .pi/2))/2 : 0.45
                Canvas{context,size in
                    let scale = min(size.width/320,size.height/340)
                    func point(_ x:Double,_ y:Double)->CGPoint{CGPoint(x:x*scale+(size.width-320*scale)/2,y:y*scale)}
                    func line(_ points:[CGPoint],color:Color = .primary,width:Double = 8){var p=Path();p.addLines(points);context.stroke(p,with:.color(color),style:StrokeStyle(lineWidth:width*scale,lineCap:.round,lineJoin:.round))}
                    func head(_ x:Double,_ y:Double){let c=point(x,y);context.fill(Path(ellipseIn:CGRect(x:c.x-15*scale,y:c.y-15*scale,width:30*scale,height:30*scale)),with:.color(.primary))}
                    if kind == "lateral_raise" {
                        head(160,48);line([point(160,72),point(160,190)]);line([point(142,88),point(178,88)]);line([point(160,190),point(130,290)]);line([point(160,190),point(190,290)])
                        let a = phase * .pi/2, x=sin(a)*94,y=cos(a)*94
                        line([point(142,88),point(142-x*0.55,88+y*0.55),point(142-x,88+y)])
                        line([point(178,88),point(178+x*0.55,88+y*0.55),point(178+x,88+y)])
                        line([point(134-x,88+y),point(150-x,88+y)],color:.teal,width:13);line([point(170+x,88+y),point(186+x,88+y)],color:.teal,width:13)
                    } else if kind == "wall_pushup" {
                        line([point(270,30),point(270,300)],color:.secondary,width:3)
                        let shoulder=190+phase*34
                        head(shoulder+5,63);line([point(shoulder,89),point(125,285)])
                        line([point(shoulder,89),point(238-phase*8,130),point(270,96)])
                    } else {
                        line([point(80,190),point(165,190),point(165,290)],color:.secondary,width:4);line([point(85,190),point(85,290)],color:.secondary,width:4)
                        let hipX=120+phase*42,hipY=190-phase*36, shoulderX=155+phase*7,shoulderY=100-phase*42
                        head(shoulderX,shoulderY-22);line([point(shoulderX,shoulderY),point(hipX,hipY),point(200-phase*30,205),point(170,290)])
                        line([point(shoulderX,shoulderY+10),point(220,shoulderY+15)])
                    }
                    line([point(45,300),point(290,300)],color:.secondary.opacity(0.35),width:2)
                }.frame(height:320).background(.thinMaterial,in:RoundedRectangle(cornerRadius:24))
            }
            Button(playing ? "暂停示意" : "播放示意"){playing.toggle()}.buttonStyle(.bordered)
            Text(kind == "lateral_raise" ? "轻重量，肘微屈；手臂略在身体前方，最高约肩高。不耸肩、不甩动，缓慢放下。" : kind == "wall_pushup" ? "双手约肩宽，身体保持一条线。屈肘缓慢靠近墙，再推回；不要塌腰。" : "用稳固、无轮子的椅子，双脚髋宽。略前倾站起，控制速度坐回；需要时可扶手。").font(.body)
            Text("二维动作轨迹示意，不用于评估姿势；疼痛时停止。\(reduceMotion ? "系统已开启减少动态效果，显示静态示意。" : "")").font(.caption).foregroundStyle(.secondary)
            Link("查看动作来源",destination:URL(string:kind == "lateral_raise" ? "https://www.acefitness.org/resources/everyone/exercise-library/26/lateral-raise/" : "https://www.nhs.uk/live-well/exercise/strength-exercises/")!)
            Spacer(minLength:0)
        }.padding(24) }.navigationTitle(title).navigationBarTitleDisplayMode(.inline)
    }
}
