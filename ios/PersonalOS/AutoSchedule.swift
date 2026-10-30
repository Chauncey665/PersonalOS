import Foundation
import SwiftUI
import EventKit

struct PlanningSettings:Codable {
    let enabled:Bool
    let start_minute:Int
    let end_minute:Int
    let meal_minutes:Int
    let duration_minutes:Int
}
struct PlanningSlot:Codable,Identifiable {
    let todo_id:String
    let entry_id:String
    let state:String
    let reason:String
    let start:String?
    let end:String?
    let device_sync:String?
    var id:String {todo_id}
    var label:String? {
        guard let start,let date=JournalMetadata.date(start) else{return nil}
        return date.formatted(.dateTime.month().day().hour().minute()) + (device_sync=="written" ? "" : " · 待同步")
    }
}
struct PlanningStatus:Codable {
    let settings:PlanningSettings
    let context_fresh:Bool
    let slots:[PlanningSlot]
    let estimates:[String:Int]
}

@MainActor final class AutoScheduleModel:ObservableObject {
    static let shared=AutoScheduleModel()
    @Published private(set) var status:PlanningStatus?
    @Published private(set) var busy=false
    @Published var error:String?
    private var key=""
    private var refreshedAt=Date.distantPast
    private var signature=""
    private let store=EKEventStore()
    func slot(_ id:String)->PlanningSlot? {status?.slots.first{$0.todo_id==id}}
    private func context(_ endpoint:ServerEndpoint,_ value:PlanningStatus)throws->[String:Any] {
        guard CalendarJournal.shared.enabled,EKEventStore.authorizationStatus(for:.event) == .fullAccess else {
            throw PersonalOSError.message("请先开启 PersonalOS 自动记入日历并允许日历完整访问")
        }
        store.reset()
        var calendar=Calendar(identifier:.gregorian);calendar.timeZone = .current
        let start=calendar.startOfDay(for:Date()),end=calendar.date(byAdding:.day,value:8,to:start)!
        let known=Set(value.slots.map(\.entry_id)),completed=Set(value.slots.filter{$0.state=="completed"}.map(\.entry_id))
        let events=store.events(matching:store.predicateForEvents(withStart:start,end:end,calendars:nil)).filter {
            CalendarContextEncoding.representsPlanningOccupiedTime($0,completedAutoIDs:completed,origin:endpoint.key)
        }
        guard events.count<=500 else{throw PersonalOSError.message("日历超过500项，未用截断内容安排任务")}
        let format=ISO8601DateFormatter()
        let rows:[[String:Any]]=events.compactMap { e in
            guard let a=e.startDate,let b=e.endDate,b>a else{return nil}
            var row:[String:Any]=["start":format.string(from:max(a,start)),"end":format.string(from:min(b,end)),"all_day":e.isAllDay]
            if let marker=DailyAgendaMetadata.markerParts(e.url,scheme:"personalos-journal"),marker.origin==JournalMetadata.hash(endpoint.key),known.contains(marker.taskID) {row["auto_id"]=marker.taskID}
            return row
        }
        return ["captured_at":Date().timeIntervalSince1970,"time_zone":TimeZone.current.identifier,"range_start":format.string(from:start),"range_end":format.string(from:end),"truncated":false,"events":rows]
    }
    private func post(_ path:String,_ value:[String:Any],endpoint:ServerEndpoint)async throws->PlanningStatus {
        let data=try JSONSerialization.data(withJSONObject:value,options:[.sortedKeys])
        let reply=try await API.request(path,["payload":String(decoding:data,as:UTF8.self)],endpoint:endpoint,timeout:20)
        return try JSONDecoder().decode(PlanningStatus.self,from:reply)
    }
    func refresh(force:Bool=false)async {
        guard !busy,API.isPaired() else{return}
        do {
            let endpoint=try API.endpoint(),pair=PendingSubmission.fingerprint(for:endpoint),connection=endpoint.key+"|"+pair
            if connection != key {key=connection;status=nil;refreshedAt = .distantPast;signature=""}
            let next=TodoModel.shared.items.map{$0.id+":"+String($0.revision)+":"+String($0.completed)}.joined(separator:"|")
            guard force || next != signature || Date().timeIntervalSince(refreshedAt)>45 else{return}
            busy=true;defer{busy=false}
            let data=try await API.request("/planning/status",endpoint:endpoint)
            guard try API.endpoint().key==endpoint.key,PendingSubmission.fingerprint(for:endpoint)==pair else{return}
            var value=try JSONDecoder().decode(PlanningStatus.self,from:data)
            status=value
            if value.settings.enabled {
                value=try await post("/planning/context",context(endpoint,value),endpoint:endpoint)
                guard try API.endpoint().key==endpoint.key,connection==endpoint.key+"|"+PendingSubmission.fingerprint(for:endpoint) else{return}
                status=value
                await CalendarJournal.shared.refreshAndSync(endpoint:endpoint)
            }
            signature=next;refreshedAt=Date();error=nil
        }catch{self.error=error.localizedDescription;refreshedAt=Date()}
    }
    func enabled(_ value:Bool)async {
        do {let endpoint=try API.endpoint();status=try await post("/planning/settings",["enabled":value],endpoint:endpoint);await refresh(force:true)}catch{self.error=error.localizedDescription}
    }
    func estimate(id:String,minutes:Int)async {
        do{let endpoint=try API.endpoint();status=try await post("/planning/estimate",["id":id,"minutes":minutes,"reschedule":true],endpoint:endpoint);await refresh(force:true)}catch{self.error=error.localizedDescription}
    }
}

@MainActor struct AutoScheduleSettings:View {
    @ObservedObject var model=AutoScheduleModel.shared
    @Environment(\.dismiss)private var dismiss
    var body:some View {
        NavigationStack{Form{
            Toggle("自动安排待办",isOn:Binding(get:{model.status?.settings.enabled ?? false},set:{value in Task{await model.enabled(value)}})).disabled(model.busy)
            LabeledContent("工作时段",value:"11:00 – 次日 01:00")
            LabeledContent("午餐 / 晚餐",value:"各预留45分钟")
            LabeledContent("默认用时",value:"45分钟")
            Button(model.busy ? "正在安排…" : "立即安排"){Task{await model.refresh(force:true)}}.disabled(model.busy)
            if let error=model.error {Text(error).foregroundStyle(.orange)}
            if let value=model.status,!value.context_fresh {Text("打开 PersonalOS 后更新日历，再按空档安排").font(.caption).foregroundStyle(.secondary)}
        }.navigationTitle("排程").navigationBarTitleDisplayMode(.inline).toolbar{ToolbarItem(placement:.confirmationAction){Button("完成"){dismiss()}}}}
    }
}
