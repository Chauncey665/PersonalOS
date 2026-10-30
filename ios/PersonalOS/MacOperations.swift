import SwiftUI

struct MacRequestSelection:Identifiable {let id:String}
struct MacExecutionPlan:Decodable {
 let kind:String;let title:String;let action:MacExecutionAction;let undo_available:Bool
}
struct MacExecutionAction:Decodable {
 let operation:String;let app:String?;let url:String?;let path:String?;let preview:String?
}
extension TaskRecord {
 var macPlan:MacExecutionPlan?{try? JSONDecoder().decode(MacExecutionPlan.self,from:Data(plan.utf8))}
}
@MainActor struct MacActionCard:View {
 let task:TaskRecord
 let act:(String)->Void
 var body:some View {
  if let plan=task.macPlan{VStack(alignment:.leading,spacing:14){
   Label(plan.title,systemImage:"desktopcomputer").font(.headline)
   Text("Mac mini").font(.caption).foregroundStyle(.secondary)
   if let path=plan.action.path{Text(path).font(.caption).textSelection(.enabled)}
   if let url=plan.action.url{Text(url).font(.subheadline).textSelection(.enabled)}
   if let preview=plan.action.preview,!preview.isEmpty{DisclosureGroup("查看修改"){Text(preview).font(.system(.caption,design:.monospaced)).textSelection(.enabled)}}
   if task.state=="awaiting_confirmation"{Button("确认执行"){act("approve")}.buttonStyle(.borderedProminent).frame(minHeight:44)}
   if task.state=="completed" && plan.undo_available{Button("撤销写回"){act("undo")}.buttonStyle(.bordered).frame(minHeight:44)}
   if !plan.undo_available{Text("仅打开指定内容；应用内操作尚未执行，不会自动关闭应用。").font(.caption).foregroundStyle(.secondary)}
  }.padding(18).frame(maxWidth:.infinity,alignment:.leading).background(Color(uiColor:.secondarySystemGroupedBackground),in:RoundedRectangle(cornerRadius:20))}
 }
}
@MainActor struct MacActionReviewSheet:View {
 let taskID:String
 @Environment(\.dismiss)private var dismiss
 @State private var task:TaskRecord?;@State private var error:String?;@State private var busy=false;@State private var action:String?
 var body:some View{
  NavigationStack{ScrollView{VStack(alignment:.leading,spacing:16){
   if let task{MacActionCard(task:task){action=$0}.disabled(busy);Text(task.result).font(.subheadline).textSelection(.enabled)
    if task.canCancel{Button("取消任务",role:.destructive){action="cancel"}.disabled(busy)}
   }else{ProgressView("读取清单…")}
   if let error{Text(error).foregroundStyle(.orange)}
  }.padding()}.navigationTitle("Mac操作").navigationBarTitleDisplayMode(.inline).toolbar(.visible,for:.navigationBar).toolbar{ToolbarItem(placement:.confirmationAction){Button("完成"){dismiss()}}}
  .task{await refresh()}
  .confirmationDialog(action=="undo" ? "恢复写回前的原件？" : action=="cancel" ? "取消这次操作？" : "在Mac执行这项操作？",isPresented:Binding(get:{action != nil},set:{if !$0{action=nil}}),titleVisibility:.visible){
   if let action{Button(action=="approve" ? "确认执行" : action=="undo" ? "撤销写回" : "取消任务",role:action=="cancel" ? .destructive : nil){self.action=nil;Task{busy=true;defer{busy=false};do{_ = try await API.request("/tasks/"+taskID+"/"+action,[:],timeout:30);await refresh()}catch{self.error=error.localizedDescription;await refresh()}}}}
  }message:{Text(action=="undo" ? "仅在原文件没有新的修改时恢复，修改版仍保留。" : "以这份清单为准；遇到文件变化会停止。")}
 }}
 private func refresh()async{do{task=try JSONDecoder().decode(TaskRecord.self,from:try await API.request("/tasks/"+taskID));}catch{self.error=error.localizedDescription}}
}
@MainActor struct MacDocumentEditSheet:View {
 let device:String;let path:String;let etag:String
 @Environment(\.dismiss)private var dismiss
 @State private var instruction="请润色这份文档，保持事实、日期和所有数字不变。"
 @State private var document:DocumentDescriptor?;@State private var result="";@State private var error:String?;@State private var busy=false;@State private var taskID:String?;@State private var requestID=UUID().uuidString;@State private var importID=UUID().uuidString
 var body:some View{
  NavigationStack{Form{
   Section{Text((path as NSString).lastPathComponent).font(.headline);TextEditor(text:$instruction).frame(minHeight:100).disabled(busy)
    Button(busy ? "处理中…" : taskID==nil ? "生成修改版" : "查看修改结果"){submit()}.disabled(busy || instruction.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty)
   }
   if !result.isEmpty{Section{Text(result)}}
   if let document,document.version>1{Section{DocumentResultRow(document:document)}}
   if let error{Section{Text(error).foregroundStyle(.orange)}}
   Section{Text(device=="mini" ? "先生成副本；查看修改后可确认写回Mac原件，也可下载。" : "Air原件保持只读；修改版可下载或保存到Mini。").font(.caption).foregroundStyle(.secondary)}
  }.navigationTitle("修改文档").navigationBarTitleDisplayMode(.inline).toolbar(.visible,for:.navigationBar).toolbar{ToolbarItem(placement:.confirmationAction){Button("完成"){dismiss()}}}}
 }
 private func submit(){Task{busy=true;error=nil;defer{busy=false};do{
  let endpoint=try API.endpoint(),pair=PendingSubmission.fingerprint(for:endpoint)
  if document==nil{document=try JSONDecoder().decode(DocumentDescriptor.self,from:try await API.request("/documents/import",["device":device,"path":path,"etag":etag,"request_id":importID],endpoint:endpoint,timeout:60))}
  guard let document else{throw PersonalOSError.message("Mac没有返回文档")}
  if taskID==nil{let ids=try JSONEncoder().encode([document.id]);let data=try await API.request("/tasks",["text":instruction,"document_ids":String(decoding:ids,as:UTF8.self),"request_id":requestID],endpoint:endpoint);taskID=try JSONDecoder().decode([String:String].self,from:data)["id"]}
  guard let id=taskID else{throw PersonalOSError.message("Mac没有返回任务编号")}
  for _ in 0..<110{
   try Task.checkCancellation();let task=try JSONDecoder().decode(TaskRecord.self,from:try await API.request("/tasks/"+id,endpoint:endpoint))
   guard try API.endpoint().key==endpoint.key,PendingSubmission.fingerprint(for:endpoint)==pair else{throw PersonalOSError.message("连接已变化，请到原Mac查看记录")}
   if task.state=="completed"{self.document=task.documentVersions.first ?? document;result=task.result;return}
   if ["failed","needs_review","needs_details"].contains(task.state){throw PersonalOSError.message(task.result)}
   try await Task.sleep(nanoseconds:1_000_000_000)
  };result="Mac仍在处理，稍后点击查看结果，不会重复提交。"
 }catch{self.error=error.localizedDescription}}}
}
