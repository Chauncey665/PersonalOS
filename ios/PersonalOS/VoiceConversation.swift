import Foundation
import AppIntents
import SwiftUI
import MapKit
import CryptoKit
import UniformTypeIdentifiers
import AVFoundation
@preconcurrency import CoreLocation

struct PersonalOSPhoneAction: Codable {
    let id: String; let operation: String
    var query: String?; var nearby: Bool?; var navigation: Bool?; var mode: String?
    var place_id: String?; var text: String?
}
struct PersonalOSSession: Codable {
    let id: String; let task_id: String; let revision: Int; let status: String
    let calendar_task_id: String?
    let speech: String; let phone_action: PersonalOSPhoneAction?; let can_continue: Bool
}

@MainActor enum PersonalOSConversation {
    private struct Pending: Codable { let endpoint: String; let pair: String; let digest: String; let session: String; let body: [String:String] }
    private struct Place: Codable {let id: String; let name: String; let latitude: Double; let longitude: Double; let expires: Date}
    static func directory() throws -> URL {
        let url=FileManager.default.urls(for:.applicationSupportDirectory,in:.userDomainMask)[0].appendingPathComponent("VoiceConversation",isDirectory:true)
        try FileManager.default.createDirectory(at:url,withIntermediateDirectories:true,attributes:[.protectionKey:FileProtectionType.complete])
        return url
    }
    static func protectedWrite(_ data:Data,to url:URL)throws {
        try data.write(to:url,options:[.atomic,.completeFileProtection])
    }
    static func namespace(_ endpoint:ServerEndpoint)->String { JournalMetadata.hash(endpoint.key+"|"+PendingSubmission.fingerprint(for:endpoint)) }
    static func remember(_ session:String,endpoint:ServerEndpoint,action:PersonalOSPhoneAction?=nil,audio:String?=nil,task:String?=nil)throws {
        let context=VoiceInvocationContext(endpoint:endpoint.key,pair:PendingSubmission.fingerprint(for:endpoint),session:session,action:action?.id,operation:action?.operation,audio:audio,expires:Date().addingTimeInterval(600),task:task)
        try protectedWrite(JSONEncoder().encode(context),to:directory().appendingPathComponent("active-"+namespace(endpoint)+".json"))
    }
    static func context(_ endpoint:ServerEndpoint)throws->VoiceInvocationContext {
        let url=try directory().appendingPathComponent("active-"+namespace(endpoint)+".json")
        guard let data=try? Data(contentsOf:url),let value=try? JSONDecoder().decode(VoiceInvocationContext.self,from:data) else{throw VoiceInvocationContext.ContextError.unavailable}
        _ = try value.sessionID(nil,endpoint:endpoint.key,pair:PendingSubmission.fingerprint(for:endpoint))
        return value
    }
    static func current(_ endpoint:ServerEndpoint)async throws->PersonalOSSession {
        try JSONDecoder().decode(PersonalOSSession.self,from:await API.request("/assistant/sessions/current",endpoint:endpoint))
    }
    static func state(_ id:String,_ endpoint:ServerEndpoint)async throws->PersonalOSSession {
        guard UUID(uuidString:id) != nil else{throw PersonalOSError.message("会话编号无效")}
        return try JSONDecoder().decode(PersonalOSSession.self,from:await API.request("/assistant/sessions/"+id,endpoint:endpoint,timeout:4))
    }
    static func check(_ endpoint:ServerEndpoint,_ pair:String)throws {
        guard try API.endpoint().key==endpoint.key,PendingSubmission.fingerprint(for:endpoint)==pair else{throw PersonalOSError.message("私人连接已改变，已停止这次操作")}
    }
    static func submit(_ audio:IntentFile)async throws->String {
        let endpoint=try API.endpoint(),pair=PendingSubmission.fingerprint(for:endpoint)
        let recording=try await AudioIntentPayload.load(audio)
        let digest=AudioIntentPayload.requestID(for:recording.data)
        let pendingURL=try directory().appendingPathComponent("pending-"+namespace(endpoint)+".json")
        let pending:Pending
        if FileManager.default.fileExists(atPath:pendingURL.path) {
            let old=try JSONDecoder().decode(Pending.self,from:Data(contentsOf:pendingURL))
            guard old.endpoint==endpoint.key,old.pair==pair else{throw PersonalOSError.message("旧录音属于另一配对，请先检查历史")}
            // A new recording does not overwrite an unacknowledged upload.
            pending=old
        }else{
            let session=try await current(endpoint)
            if session.status=="processing"{throw PersonalOSError.message("上一轮仍在处理，这段新录音尚未提交；请稍后继续") }
            var body=["audio_base64":recording.data.base64EncodedString(),"mime_type":recording.mimeType,"request_id":UUID().uuidString,"time_zone":TimeZone.current.identifier,"session_id":session.id,"reply_to":session.task_id]
            if let context=CompanionFeatures.shared.todayCalendarContext(){body["calendar_context"]=context}
            let shared=try directory().appendingPathComponent("shared-"+namespace(endpoint)+".json")
            if let data=try? Data(contentsOf:shared),let refs=String(data:data,encoding:.utf8){body["input_refs"]=refs}
            pending=Pending(endpoint:endpoint.key,pair:pair,digest:digest,session:session.id,body:body)
            try protectedWrite(JSONEncoder().encode(pending),to:pendingURL)
        }
        let submitted=try await API.request("/voice",pending.body,endpoint:endpoint,timeout:25)
        let submittedID=(try? JSONDecoder().decode([String:String].self,from:submitted))?["id"]
        try check(endpoint,pair)
        try remember(pending.session,endpoint:endpoint,task:submittedID)
        try FileManager.default.removeItem(at:pendingURL)
        let shared=try directory().appendingPathComponent("shared-"+namespace(endpoint)+".json")
        if let data=try? Data(contentsOf:shared),String(data:data,encoding:.utf8)==pending.body["input_refs"] {try? FileManager.default.removeItem(at:shared)}
        let budget=try directory().appendingPathComponent("wait-"+namespace(endpoint)+"-"+pending.session+".json")
        try protectedWrite(JSONEncoder().encode(Date().addingTimeInterval(90)),to:budget)
        if pending.digest != digest {throw PersonalOSError.message("上一段断网录音已补交成功；这段新录音未提交，请稍后再说")}
        return pending.session
    }
    static func receipt(session:String,action:String,result:[String:Any],endpoint:ServerEndpoint)async throws {
        let payload=try JSONSerialization.data(withJSONObject:["action_id":action,"result":result])
        _ = try await API.request("/assistant/sessions/"+session+"/receipts",["payload":String(decoding:payload,as:UTF8.self)],endpoint:endpoint)
    }
    static func search(_ action:PersonalOSPhoneAction,session:String,endpoint:ServerEndpoint,location:CLLocation?=nil)async throws {
        let places=PhonePlaces();places.find(action.query ?? "餐厅",nearby:action.nearby ?? false,suppliedLocation:location)
        let until=Date().addingTimeInterval(location == nil ? 18:10)
        while places.busy && Date()<until {try await Task.sleep(nanoseconds:150_000_000);try Task.checkCancellation()}
        defer{places.cancel()}
        if places.busy || places.error != nil {
            try await receipt(session:session,action:action.id,result:["error":places.error ?? "定位或地图查询超时，请说明具体地区"],endpoint:endpoint);return
        }
        var choices=[[String:String]]()
        for item in places.results.prefix(3) {
            let id=UUID().uuidString,location=item.location
            let saved=Place(id:id,name:item.item.name ?? "地点",latitude:location.coordinate.latitude,longitude:location.coordinate.longitude,expires:Date().addingTimeInterval(600))
            let path=try directory().appendingPathComponent(namespace(endpoint)+"-place-"+id+".json")
            try protectedWrite(JSONEncoder().encode(saved),to:path)
            let distance=item.distance.map{$0<1000 ? "约\(Int($0))米" : String(format:"约%.1f公里",$0/1000)}
            choices.append(["id":id,"name":saved.name,"detail":distance ?? String(item.address.prefix(120))])
        }
        try await receipt(session:session,action:action.id,result:["choices":choices],endpoint:endpoint)
    }
    static func route(_ action:PersonalOSPhoneAction,endpoint:ServerEndpoint)throws->String {
        guard let id=action.place_id,UUID(uuidString:id) != nil else{throw PersonalOSError.message("导航地点无效")}
        let p=try directory().appendingPathComponent(namespace(endpoint)+"-place-"+id+".json")
        let place=try JSONDecoder().decode(Place.self,from:Data(contentsOf:p))
        guard place.expires>Date() else{throw PersonalOSError.message("地图选择已过期，请重新查找")}
        if #available(iOS 18.4,*) {return try PersonalOSNavigationURL.make(latitude:place.latitude,longitude:place.longitude,mode:action.mode ?? "")}
        let flag=action.mode=="transit" ? "r" : action.mode=="driving" ? "d" : "w"
        var url=URLComponents(string:"https://maps.apple.com/")!
        url.queryItems=[URLQueryItem(name:"daddr",value:"\(place.latitude),\(place.longitude)"),URLQueryItem(name:"dirflg",value:flag)]
        return url.url!.absoluteString
    }
    static func poll(_ supplied:String?=nil,externalSearch:Bool=false)async throws->String {
        let endpoint=try API.endpoint()
        let id=try context(endpoint).sessionID(supplied,endpoint:endpoint.key,pair:PendingSubmission.fingerprint(for:endpoint))
        let budget=try directory().appendingPathComponent("wait-"+namespace(endpoint)+"-"+id+".json")
        let end=(try? JSONDecoder().decode(Date.self,from:Data(contentsOf:budget))) ?? Date().addingTimeInterval(100)
        if !FileManager.default.fileExists(atPath:budget.path){try protectedWrite(JSONEncoder().encode(end),to:budget)}
        let remaining=end.timeIntervalSinceNow
        guard remaining>0 else{return "{\"status\":\"processing\",\"continue\":\"no\"}"}
        return try await withThrowingTaskGroup(of:String.self){group in
            group.addTask{ @MainActor in try await pollWithinBudget(id,externalSearch:externalSearch) }
            group.addTask{
                try await Task.sleep(nanoseconds:UInt64(min(remaining,110)*1_000_000_000))
                throw PersonalOSError.message("本轮等待已结束。任务保留在 Mac，下次按操作按钮可继续查询。")
            }
            defer{group.cancelAll()}
            return try await group.next()!
        }
    }
    static func pollWithinBudget(_ id:String,externalSearch:Bool=false)async throws->String {
        let endpoint=try API.endpoint(),pair=PendingSubmission.fingerprint(for:endpoint)
        var session=try await state(id,endpoint)
        let budget=try directory().appendingPathComponent("wait-"+namespace(endpoint)+"-"+id+".json")
        let end=(try? JSONDecoder().decode(Date.self,from:Data(contentsOf:budget))) ?? Date().addingTimeInterval(100)
        if Date()>=end && session.status=="processing" {return String(decoding:try JSONSerialization.data(withJSONObject:["status":"processing","continue":"no","speech":"仍在处理中，可以稍后继续查询。"]),as:UTF8.self)}
        let until=min(Date().addingTimeInterval(11),end)
        while session.status=="processing" && Date()<until {try await Task.sleep(nanoseconds:800_000_000);try check(endpoint,pair);session=try await state(id,endpoint)}
        if !externalSearch,let action=session.phone_action,action.operation=="search_places" {
            try await search(action,session:id,endpoint:endpoint);session=try await state(id,endpoint)
        }
        try check(endpoint,pair)
        var output:[String:Any]=["session":session.id,"task":session.task_id,"status":session.status,"speech":session.speech,"continue":session.can_continue ? "yes":"no","action":"","action_id":"","text":"","url":"","audio_id":"system"]
        if let a=session.phone_action {
            output["action"]=a.operation;output["action_id"]=a.id;output["text"]=a.text ?? "";output["nearby"]=(a.nearby == true) ? "yes":"no"
            if a.operation=="navigate" {output["url"]=try route(a,endpoint:endpoint)}
        }
        if session.status != "processing" && !(externalSearch && session.phone_action?.operation == "search_places") {
            // EventKit success is only spoken after its own read-back/acknowledgment.
            let target=(session.calendar_task_id?.isEmpty==false) ? session.calendar_task_id! : session.task_id
            let task=try? JSONDecoder().decode(TaskRecord.self,from:await API.request("/tasks/"+target,endpoint:endpoint,timeout:4))
            let plan=task.flatMap{try? JSONSerialization.jsonObject(with:Data($0.plan.utf8)) as? [String:Any]}
            let todo=(plan?["kind"] as? String)=="todo" || ((plan?["brain"] as? [String:Any])?["mode"] as? String)=="structured_todos"
            if CalendarJournal.shared.enabled && (task?.calendarEntry != nil || todo) {
                await CalendarJournal.shared.refreshAndSync(endpoint:endpoint)
                await TodoModel.shared.refresh(force:true)
                if let task=try? JSONDecoder().decode(TaskRecord.self,from:await API.request("/tasks/"+target,endpoint:endpoint)),let entry=task.calendarEntry {
                    output["speech"]=entry.device_sync=="written" ? "已写入 Apple 日历。"+entry.title : "日程已保存到 Mac，手机日历尚未确认写入。"
                }
            }
            let text=output["speech"] as? String ?? ""
            if !text.isEmpty {let audio=await prepareSpeech(text,endpoint:endpoint,maxWait:10);output["audio_id"]=audio.isEmpty ? "system":audio}
        }
        try check(endpoint,pair)
        try remember(id,endpoint:endpoint,action:session.phone_action,audio:(output["audio_id"] as? String).flatMap{$0=="system" ? nil:$0},task:session.task_id)
        return String(decoding:try JSONSerialization.data(withJSONObject:output),as:UTF8.self)
    }
    static func prepareSpeech(_ text:String,endpoint:ServerEndpoint,voice:String?=nil,maxWait:TimeInterval=45)async->String {
        do {
            var body=["text":String(text.prefix(600))];if let voice{body["voice"]=voice}
            struct Speech:Decodable{let id:String;let state:String}
            var reply=try JSONDecoder().decode(Speech.self,from:await API.request("/assistant/speech",body,endpoint:endpoint))
            if reply.id.isEmpty{return ""}
            let until=Date().addingTimeInterval(maxWait)
            while reply.state=="queued" && Date()<until {try await Task.sleep(nanoseconds:500_000_000);reply=try JSONDecoder().decode(Speech.self,from:await API.request("/assistant/speech/"+reply.id,endpoint:endpoint,timeout:3))}
            return reply.state=="ready" ? reply.id : ""
        }catch{return ""}
    }
    static func upload(_ file:IntentFile,request:String)async throws->String {
        let endpoint=try API.endpoint(),pair=PendingSubmission.fingerprint(for:endpoint)
        let name=file.filename
        let bytes:Data
        if let url=file.fileURL {
            let access=url.startAccessingSecurityScopedResource();defer{if access{url.stopAccessingSecurityScopedResource()}}
            var read:Data?,failure:Error?,coordination:NSError?
            NSFileCoordinator().coordinate(readingItemAt:url,options:.withoutChanges,error:&coordination){location in
                do{let info=try location.resourceValues(forKeys:[.isRegularFileKey,.fileSizeKey]);guard info.isRegularFile==true,(info.fileSize ?? Int.max)<=20*1024*1024 else{throw PersonalOSError.message("请选择20MB以内的普通文件")};read=try Data(contentsOf:location)}catch{failure=error}
            }
            if let e=failure ?? coordination{throw e};guard let read else{throw PersonalOSError.message("文件未读取完成")};bytes=read
        }else{bytes=file.data}
        guard !bytes.isEmpty,bytes.count<=20*1024*1024 else{throw PersonalOSError.message("文件须为1字节到20MB")}
        let stable=request=="shared-file" ? "shared-"+JournalMetadata.hash(name+"|"+bytes.base64EncodedString()) : request
        let data=try await API.request("/transfer",["name":name,"data":bytes.base64EncodedString(),"request_id":stable],endpoint:endpoint,timeout:90)
        struct Reply:Decodable{let id:String};let reply=try JSONDecoder().decode(Reply.self,from:data)
        try check(endpoint,pair);return reply.id
    }
    static func transfer(_ file:IntentFile,session:String,action:String)async throws {
        let endpoint=try API.endpoint()
        let id=try await upload(file,request:"voice-transfer-"+action)
        try await receipt(session:session,action:action,result:["task_id":id],endpoint:endpoint)
    }
}

struct CheckPersonalOSPendingIntent:AppIntent {
    static var title:LocalizedStringResource="检查 PersonalOS 待处理对话"
    static var openAppWhenRun=false
    static var authenticationPolicy:IntentAuthenticationPolicy{.requiresLocalDeviceAuthentication}
    @available(iOS 26.0,*) static var supportedModes:IntentModes{.background}
    @MainActor func perform()async throws->some IntentResult & ReturnsValue<String> {
        let endpoint=try API.endpoint();let state=try await PersonalOSConversation.current(endpoint)
        try PersonalOSConversation.remember(state.id,endpoint:endpoint,task:state.task_id)
        let pending=state.status=="processing" || state.status=="phone_action"
        if pending {
            let path=try PersonalOSConversation.directory().appendingPathComponent("wait-"+PersonalOSConversation.namespace(endpoint)+"-"+state.id+".json")
            try PersonalOSConversation.protectedWrite(JSONEncoder().encode(Date().addingTimeInterval(100)),to:path)
        }
        return .result(value:String(decoding:try JSONSerialization.data(withJSONObject:["session":state.id,"pending":pending ? "yes":"no"]),as:UTF8.self))
    }
}
struct ConverseWithPersonalOSIntent:AppIntent {
    static var title:LocalizedStringResource="与 PersonalOS 对话"
    static var openAppWhenRun=false
    static var authenticationPolicy:IntentAuthenticationPolicy{.requiresLocalDeviceAuthentication}
    @available(iOS 26.0,*) static var supportedModes:IntentModes{.background}
    @Parameter(title:"录音")var audio:IntentFile
    @MainActor func perform()async throws->some IntentResult & ReturnsValue<String>{.result(value:try await PersonalOSConversation.submit(audio))}
}
struct PollPersonalOSConversationIntent:AppIntent {
    static var title:LocalizedStringResource="读取 PersonalOS 对话进展"
    static var openAppWhenRun=false
    static var authenticationPolicy:IntentAuthenticationPolicy{.requiresLocalDeviceAuthentication}
    @available(iOS 26.0,*) static var supportedModes:IntentModes{.background}
    @Parameter(title:"会话")var session:String?
    @MainActor func perform()async throws->some IntentResult & ReturnsValue<String>{.result(value:try await PersonalOSConversation.poll(session))}
}
// v0.12.3 separates system location, MapKit and polling into short background actions.
struct ReadPersonalOSVoiceTurnIntent:AppIntent {
    static var title:LocalizedStringResource="读取 PersonalOS 连续对话"
    static var openAppWhenRun=false
    static var authenticationPolicy:IntentAuthenticationPolicy{.requiresLocalDeviceAuthentication}
    @available(iOS 26.0,*) static var supportedModes:IntentModes{.background}
    @MainActor func perform()async throws->some IntentResult & ReturnsValue<String>{.result(value:try await PersonalOSConversation.poll(externalSearch:true))}
}
struct SearchPersonalOSPlacesIntent:AppIntent {
    static var title:LocalizedStringResource="搜索 PersonalOS 地点候选"
    static var openAppWhenRun=false
    static var authenticationPolicy:IntentAuthenticationPolicy{.requiresLocalDeviceAuthentication}
    @available(iOS 26.0,*) static var supportedModes:IntentModes{.background}
    @Parameter(title:"当前位置")var location:CLPlacemark?
    @MainActor func perform()async throws->some IntentResult {
        let endpoint=try API.endpoint(),context=try PersonalOSConversation.context(endpoint)
        let id=try context.actionID(nil,allowed:["search_places"])
        let state=try await PersonalOSConversation.state(context.session,endpoint)
        guard let action=state.phone_action,action.id==id else{throw PersonalOSError.message("地点查询已更新，请重新开始")}
        if action.nearby == true {
            guard let point=location?.location,CLLocationCoordinate2DIsValid(point.coordinate),point.coordinate.latitude.isFinite,point.coordinate.longitude.isFinite,abs(point.timestamp.timeIntervalSinceNow)<300 else {
                try await PersonalOSConversation.receipt(session:state.id,action:id,result:["error":"location_unavailable"],endpoint:endpoint);return .result()
            }
            try await PersonalOSConversation.search(action,session:state.id,endpoint:endpoint,location:point)
        }else{try await PersonalOSConversation.search(action,session:state.id,endpoint:endpoint)}
        return .result()
    }
}
struct MarkPersonalOSVoiceStageIntent:AppIntent {
    static var title:LocalizedStringResource="记录 PersonalOS 语音进展"
    static var openAppWhenRun=false
    static var authenticationPolicy:IntentAuthenticationPolicy{.requiresLocalDeviceAuthentication}
    @available(iOS 26.0,*) static var supportedModes:IntentModes{.background}
    @Parameter(title:"阶段")var stage:String
    @MainActor func perform()async throws->some IntentResult {
        // Best effort only. Diagnostics must never prevent speaking or continuation.
        do {
            let endpoint=try API.endpoint(),context=try PersonalOSConversation.context(endpoint)
            guard let task=context.task else{return .result()}
            let payload=try JSONSerialization.data(withJSONObject:["task":task,"stage":stage,"version":"0.12.3"])
            _ = try await API.request("/assistant/sessions/"+context.session+"/client-events",["payload":String(decoding:payload,as:UTF8.self)],endpoint:endpoint,timeout:1)
        }catch{}
        return .result()
    }
}
struct FinishPersonalOSPhoneActionIntent:AppIntent {
    static var title:LocalizedStringResource="记录 PersonalOS 手机操作回执"
    static var openAppWhenRun=false
    static var authenticationPolicy:IntentAuthenticationPolicy{.requiresLocalDeviceAuthentication}
    @available(iOS 26.0,*) static var supportedModes:IntentModes{.background}
    @Parameter(title:"会话")var session:String?
    @Parameter(title:"操作")var action:String?
    @MainActor func perform()async throws->some IntentResult {
        let endpoint=try API.endpoint(),context=try PersonalOSConversation.context(endpoint)
        let sid=try context.sessionID(session,endpoint:endpoint.key,pair:PendingSubmission.fingerprint(for:endpoint))
        let aid=try context.actionID(action,allowed:["navigate","copy","share"])
        try await PersonalOSConversation.receipt(session:sid,action:aid,result:["accepted":true],endpoint:endpoint);return .result()
    }
}
struct TransferPersonalOSFileIntent:AppIntent {
    static var title:LocalizedStringResource="传文件给 PersonalOS Mini"
    static var openAppWhenRun=false
    static var authenticationPolicy:IntentAuthenticationPolicy{.requiresLocalDeviceAuthentication}
    @available(iOS 26.0,*) static var supportedModes:IntentModes{.background}
    @Parameter(title:"文件")var file:IntentFile
    @Parameter(title:"会话")var session:String?
    @Parameter(title:"操作")var action:String?
    @MainActor func perform()async throws->some IntentResult {
        let endpoint=try API.endpoint(),context=try PersonalOSConversation.context(endpoint)
        let sid=try context.sessionID(session,endpoint:endpoint.key,pair:PendingSubmission.fingerprint(for:endpoint))
        let aid=try context.actionID(action,allowed:["select_file"])
        try await PersonalOSConversation.transfer(file,session:sid,action:aid);return .result()
    }
}
struct PersonalOSSpeechFileIntent:AppIntent {
    static var title:LocalizedStringResource="获取 PersonalOS 播报音频"
    static var openAppWhenRun=false
    static var authenticationPolicy:IntentAuthenticationPolicy{.requiresLocalDeviceAuthentication}
    @available(iOS 26.0,*) static var supportedModes:IntentModes{.background}
    @Parameter(title:"音频编号")var identifier:String?
    @MainActor func perform()async throws->some IntentResult & ReturnsValue<IntentFile> {
        let endpoint=try API.endpoint(),context=try PersonalOSConversation.context(endpoint)
        let audio=try context.audioID(identifier)
        let bytes=try await API.request("/assistant/speech/"+audio+"/audio",endpoint:endpoint)
        return .result(value:IntentFile(data:bytes,filename:"PersonalOS.wav",type:.wav))
    }
}
struct ShareFileWithPersonalOSIntent:AppIntent {
    static var title:LocalizedStringResource="把文件交给 PersonalOS 对话"
    static var openAppWhenRun=false
    static var authenticationPolicy:IntentAuthenticationPolicy{.requiresLocalDeviceAuthentication}
    @available(iOS 26.0,*) static var supportedModes:IntentModes{.background}
    @Parameter(title:"文件")var file:IntentFile
    @MainActor func perform()async throws->some IntentResult & ProvidesDialog {
        let endpoint=try API.endpoint()
        // A content hash makes a repeated share of the same source idempotent.
        let id=try await PersonalOSConversation.upload(file,request:"shared-file")
        let path=try PersonalOSConversation.directory().appendingPathComponent("shared-"+PersonalOSConversation.namespace(endpoint)+".json")
        try PersonalOSConversation.protectedWrite(JSONSerialization.data(withJSONObject:["transfer_task":id]),to:path)
        return .result(dialog:"文件已传到 Mini。接着说要做什么。")
    }
}
struct ShareTextWithPersonalOSIntent:AppIntent {
    static var title:LocalizedStringResource="把文字交给 PersonalOS 对话"
    static var openAppWhenRun=false
    static var authenticationPolicy:IntentAuthenticationPolicy{.requiresLocalDeviceAuthentication}
    @available(iOS 26.0,*) static var supportedModes:IntentModes{.background}
    @Parameter(title:"文字")var text:String
    @MainActor func perform()async throws->some IntentResult & ProvidesDialog {
        guard !text.isEmpty,text.count<=3000 else{throw PersonalOSError.message("请选择3000字以内的消息原文")}
        let endpoint=try API.endpoint();let path=try PersonalOSConversation.directory().appendingPathComponent("shared-"+PersonalOSConversation.namespace(endpoint)+".json")
        try PersonalOSConversation.protectedWrite(JSONSerialization.data(withJSONObject:["shared_text":text]),to:path)
        return .result(dialog:"已收到文字，接着说要怎样回复。")
    }
}

@MainActor struct PersonalOSVoiceSettings:View {
    @State private var voice="system";@State private var status="";@State private var busy=false
    @State private var player:AVAudioPlayer?
    @State private var loaded=false
    var body:some View {
        Picker("播报声音",selection:$voice){Text("系统声音").tag("system");Text("沉稳男声").tag("Uncle_Fu")}
            .task{do{struct Prefs:Decodable{let voice:String};voice=try JSONDecoder().decode(Prefs.self,from:await API.request("/assistant/speech/preferences")).voice;loaded=true}catch{status=error.localizedDescription}}
            .onChange(of:voice){_,value in guard loaded else{return};Task{do{_ = try await API.request("/assistant/speech/preferences",["voice":value])}catch{status=error.localizedDescription}}}
        Button("试听沉稳男声"){sample("Uncle_Fu")}.disabled(busy)
        if !status.isEmpty{Text(status).font(.caption).foregroundStyle(.secondary)}
    }
    private func sample(_ selected:String){Task{busy=true;defer{busy=false};do{
        let endpoint=try API.endpoint();let id=await PersonalOSConversation.prepareSpeech("我是 PersonalOS。今天二十三点五十九分之前，需要完成复习。",endpoint:endpoint,voice:selected,maxWait:90)
        guard !id.isEmpty else{status="样音仍在准备，稍后再点试听";return}
        let data=try await API.request("/assistant/speech/"+id+"/audio",endpoint:endpoint)
        try AVAudioSession.sharedInstance().setCategory(.playback,mode:.spokenAudio);try AVAudioSession.sharedInstance().setActive(true)
        player=try AVAudioPlayer(data:data);player?.play();status=""
    }catch{status=error.localizedDescription}}}
}
