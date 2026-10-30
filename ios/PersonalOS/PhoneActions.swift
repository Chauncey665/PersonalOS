import SwiftUI
import MapKit
@preconcurrency import CoreLocation
import UniformTypeIdentifiers

struct PhonePlace:Identifiable {
 let id=UUID();let item:MKMapItem;let distance:Double?
 var location:CLLocation {if #available(iOS 26.0,*){return item.location};return item.placemark.location ?? CLLocation(latitude:0,longitude:0)}
 var address:String {if #available(iOS 26.0,*){return item.address?.fullAddress ?? ""};return item.placemark.title ?? ""}
}
@MainActor final class PhonePlaces:NSObject,ObservableObject,@preconcurrency CLLocationManagerDelegate {
 @Published var results:[PhonePlace]=[];@Published var busy=false;@Published var error:String?
 private lazy var manager:CLLocationManager={let m=CLLocationManager();m.delegate=self;m.desiredAccuracy=kCLLocationAccuracyHundredMeters;return m}()
 private var location:CLLocation?;private var pending:(String,Bool)?;private var search:MKLocalSearch?
 func find(_ query:String,nearby:Bool,suppliedLocation:CLLocation?=nil){
  let query=query.trimmingCharacters(in:.whitespacesAndNewlines);guard !query.isEmpty,!busy else{return}
  busy=true;error=nil;results=[];pending=(query,nearby)
  if !nearby{perform(query,false);return}
  if let suppliedLocation{location=suppliedLocation;perform(query,true);return}
  if let location,abs(location.timestamp.timeIntervalSinceNow)<120{perform(query,true);return}
  switch manager.authorizationStatus {
  case .notDetermined:manager.requestWhenInUseAuthorization()
  case .authorizedAlways,.authorizedWhenInUse:manager.requestLocation()
  default:busy=false;pending=nil;error="请在系统设置允许 PersonalOS 使用期间定位，或输入具体地区后搜索"
  }
 }
 func locationManagerDidChangeAuthorization(_ manager:CLLocationManager){
  guard pending != nil else{return}
  if [.authorizedAlways,.authorizedWhenInUse].contains(manager.authorizationStatus){manager.requestLocation()}
  else if manager.authorizationStatus != .notDetermined{busy=false;pending=nil;error="定位未获允许，请输入具体地区搜索"}
 }
 func locationManager(_ manager:CLLocationManager,didUpdateLocations values:[CLLocation]){
  guard let point=values.last,point.horizontalAccuracy>=0,abs(point.timestamp.timeIntervalSinceNow)<120,let (query,nearby)=pending else{failed();return}
  location=point;perform(query,nearby)
 }
 func locationManager(_ manager:CLLocationManager,didFailWithError error:Error){failed()}
 private func failed(){busy=false;pending=nil;error="暂时无法定位，请输入具体地区搜索"}
 private func perform(_ query:String,_ nearby:Bool){
  pending=nil;let request=MKLocalSearch.Request();request.naturalLanguageQuery=query
  if nearby,let location{request.region=MKCoordinateRegion(center:location.coordinate,latitudinalMeters:10000,longitudinalMeters:10000);request.resultTypes = .pointOfInterest}
  let task=MKLocalSearch(request:request);search=task
  Task{do{let reply=try await task.start();guard !Task.isCancelled else{return};results=reply.mapItems.prefix(15).map{item in
    let place=PhonePlace(item:item,distance:nil);return PhonePlace(item:item,distance:location.map{$0.distance(from:place.location)})
   };if nearby{results.removeAll{($0.distance ?? .infinity)>10000};results.sort{($0.distance ?? .infinity)<($1.distance ?? .infinity)}};error=results.isEmpty ? "没有找到匹配地点，请换关键词或地区" : nil
  }catch{self.error="Apple地图暂时无法搜索，请稍后重试"};busy=false;search=nil}
 }
 func navigate(_ place:PhonePlace,mode:String){
  place.item.openInMaps(launchOptions:[MKLaunchOptionsDirectionsModeKey:mode],from:nil){success in Task{@MainActor in if !success{self.error="系统未能打开地图，请确认 Apple 地图已安装"}}}
 }
 func cancel(){pending=nil;search?.cancel();busy=false;location=nil}
}

@MainActor struct PhoneActionsScreen:View {
 @Environment(\.dismiss)private var dismiss
 @State private var area="手机"
 @State private var section:String
 private let initialQuery:String
 init(section:String="找餐厅",query:String=""){_section=State(initialValue:section);initialQuery=query}
 var body:some View {
  NavigationStack{
   VStack(spacing:0){Picker("设备",selection:$area){Text("手机").tag("手机");Text("Mac").tag("Mac")}.pickerStyle(.segmented).padding()
    if area=="Mac"{MacOperationsForm()}
    else{VStack(spacing:0){Picker("操作",selection:$section){ForEach(["找餐厅","导航","传文档","回消息"],id:\.self){Text($0).tag($0)}}.pickerStyle(.segmented).padding(.horizontal).padding(.bottom,8)
     switch section{case "传文档":PhoneTransferForm();case "回消息":PhoneReplyForm();default:PhonePlacesForm(restaurant:section=="找餐厅",initialQuery:initialQuery).id(section)}
    }}
   }.navigationTitle("操作").navigationBarTitleDisplayMode(.inline).toolbar(.visible,for:.navigationBar).toolbar{ToolbarItem(placement:.confirmationAction){Button("完成"){dismiss()}}}
  }
 }
}
@MainActor private struct PhonePlacesForm:View {
 let restaurant:Bool
 @StateObject private var places=PhonePlaces()
 private let initialSearch:Bool
 @State private var query:String;@State private var nearby:Bool
 init(restaurant:Bool,initialQuery:String=""){self.restaurant=restaurant;initialSearch = !initialQuery.isEmpty;_query=State(initialValue:initialQuery);_nearby=State(initialValue:restaurant && (initialQuery.isEmpty || initialQuery.contains("附近")))}
 var body:some View {
  Form{
   Section{TextField(restaurant ? "餐厅、菜系或地区" : "目的地",text:$query).submitLabel(.search).onSubmit{find()}
    if restaurant{Toggle("在我附近",isOn:$nearby)}
    Button(places.busy ? "搜索中…" : "搜索",action:find).disabled(places.busy || (!restaurant && query.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty))
   }
   if let error=places.error{Section{Text(error).foregroundStyle(.orange)}}
   ForEach(places.results){place in
    Section{VStack(alignment:.leading,spacing:8){Text(place.item.name ?? "地点").font(.headline)
     if !place.address.isEmpty{Text(place.address).font(.caption).foregroundStyle(.secondary)}
     if let distance=place.distance{Text(distance<1000 ? "约\(Int(distance))米" : String(format:"约%.1f公里",distance/1000)).font(.caption).foregroundStyle(.secondary)}
     HStack{Button("步行"){places.navigate(place,mode:MKLaunchOptionsDirectionsModeWalking)};Spacer();Button("公交"){places.navigate(place,mode:MKLaunchOptionsDirectionsModeTransit)};Spacer();Button("驾车"){places.navigate(place,mode:MKLaunchOptionsDirectionsModeDriving)}}.buttonStyle(.bordered)
    }}
   }
   Section{Text("Apple地图提供地点和路线；定位只在手机用于本次地图查询。").font(.caption).foregroundStyle(.secondary)}
  }.task{if initialSearch{find()}}.onDisappear{places.cancel()}
 }
 private func find(){places.find(query.isEmpty && restaurant ? "餐厅" : query,nearby:restaurant && nearby)}
}

@MainActor private struct PhoneTransferForm:View {
 @State private var importing=false;@State private var busy=false;@State private var pendingURL:URL?;@State private var requestID=UUID().uuidString;@State private var message:String?;@State private var receiptPath:String?;@State private var macRequest:MacRequestSelection?
 var body:some View {
  Form{Section{
   Button(busy ? "传送中…" : "选择文档"){importing=true}.disabled(busy)
   if let pendingURL{Text(pendingURL.lastPathComponent).font(.subheadline);Button("重试传送"){send(pendingURL)}.disabled(busy)}
   if let message{Text(message).foregroundStyle(receiptPath==nil ? .secondary : .primary)}
   if let receiptPath{Button("在 Mac Finder 显示"){Task{do{let reply=try await MacTaskClient.create(["operation":"reveal_file","path":receiptPath]);macRequest=MacRequestSelection(id:reply)}catch{message=error.localizedDescription}}}}
  }footer:{Text("通过私人连接保存到 Mac 的 Documents/PersonalOS Inbox。支持普通文档和图片，最多20MB；保留原文件，不覆盖同名文件。")}}
  .sheet(item:$macRequest){MacActionReviewSheet(taskID:$0.id)}
  .fileImporter(isPresented:$importing,allowedContentTypes:[.pdf,.plainText,.rtf,.image,UTType(filenameExtension:"docx") ?? .data,UTType(filenameExtension:"md") ?? .plainText,UTType(filenameExtension:"xlsx") ?? .data,UTType(filenameExtension:"pptx") ?? .data]){result in
   switch result{case .success(let url):pendingURL=url;requestID=UUID().uuidString;receiptPath=nil;send(url);case .failure(let error):message=error.localizedDescription}
  }
 }
 private func send(_ url:URL){Task{busy=true;defer{busy=false};do{
  let endpoint=try API.endpoint(),pair=PendingSubmission.fingerprint(for:endpoint),request=requestID
  let imported=try await Task.detached{()throws->Data in
   let access=url.startAccessingSecurityScopedResource();defer{if access{url.stopAccessingSecurityScopedResource()}}
   let values=try url.resourceValues(forKeys:[.isRegularFileKey,.fileSizeKey]);guard values.isRegularFile==true,(values.fileSize ?? Int.max)<=20*1024*1024 else{throw PersonalOSError.message("请选择20MB以内的普通文件")}
   var data:Data?,failure:Error?,coordination:NSError?;NSFileCoordinator().coordinate(readingItemAt:url,options:.withoutChanges,error:&coordination){source in do{data=try Data(contentsOf:source)}catch{failure=error}}
   if let error=failure ?? coordination{throw error};guard let data,data.count<=20*1024*1024 else{throw PersonalOSError.message("文件未读取完整")};return data
  }.value
  let data=try await API.request("/transfer",["name":url.lastPathComponent,"data":imported.base64EncodedString(),"request_id":request],endpoint:endpoint,timeout:90)
  guard try API.endpoint().key==endpoint.key,PendingSubmission.fingerprint(for:endpoint)==pair else{throw PersonalOSError.message("连接已变化，请到原 Mac 检查接收记录")}
  struct Reply:Decodable{let id:String}
  let result=try JSONDecoder().decode(Reply.self,from:data);let task=try JSONDecoder().decode(TaskRecord.self,from:try await API.request("/tasks/"+result.id,endpoint:endpoint))
  let plan=try JSONSerialization.jsonObject(with:Data(task.plan.utf8)) as? [String:Any];receiptPath=plan?["path"] as? String;message="已保存到 Mac · PersonalOS Inbox";pendingURL=nil
 }catch{message=error.localizedDescription}}}
}

@MainActor private struct PhoneReplyForm:View {
 @State private var original="";@State private var instruction="";@State private var tone="简洁";@State private var busy=false;@State private var error:String?;@State private var reply="";@State private var pendingID:String?;@State private var requestID=UUID().uuidString
 var body:some View {
  Form{
   Section("消息原文"){TextEditor(text:$original).frame(minHeight:100).disabled(busy);Picker("语气",selection:$tone){ForEach(["简洁","友好","正式"],id:\.self){Text($0).tag($0)}};TextField("补充要求（可选）",text:$instruction)
    Button(busy ? "准备草稿…" : pendingID==nil ? "生成回复" : "查看生成结果"){generate()}.disabled(busy || original.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty || original.count>3000 || instruction.count>500)
   }
   if let error{Section{Text(error).foregroundStyle(.orange)}}
   if !reply.isEmpty{Section("回复草稿"){Text(reply).textSelection(.enabled);HStack{Button("复制"){PersonalOSClipboard.copy(reply)};Spacer();ShareLink(item:reply){Label("分享到…",systemImage:"square.and.arrow.up")}}}}
   Section{Text("原文由你粘贴；本地模型生成草稿，发送由你在微信或信息中确认。").font(.caption).foregroundStyle(.secondary)}
  }.onChange(of:original){_,_ in resetDraftRequest()}.onChange(of:tone){_,_ in resetDraftRequest()}.onChange(of:instruction){_,_ in resetDraftRequest()}
 }
 private func resetDraftRequest(){if !busy{pendingID=nil;requestID=UUID().uuidString}}
 private func generate(){Task{busy=true;error=nil;defer{busy=false};do{
  let endpoint=try API.endpoint(),pair=PendingSubmission.fingerprint(for:endpoint)
  if pendingID==nil{let input=try JSONSerialization.data(withJSONObject:["message":original,"tone":tone,"instruction":instruction]);let data=try await API.request("/tasks",["text":"准备消息回复草稿","message_draft":String(decoding:input,as:UTF8.self),"request_id":requestID],endpoint:endpoint);pendingID=try JSONDecoder().decode([String:String].self,from:data)["id"]}
  guard let id=pendingID else{throw PersonalOSError.message("Mac没有返回任务编号")}
  for _ in 0..<90{
   try Task.checkCancellation();let row=try JSONDecoder().decode(TaskRecord.self,from:try await API.request("/tasks/"+id,endpoint:endpoint))
   guard try API.endpoint().key==endpoint.key,PendingSubmission.fingerprint(for:endpoint)==pair else{throw PersonalOSError.message("连接已变化，请重新打开")}
   if row.state=="completed"{reply=row.result;pendingID=nil;requestID=UUID().uuidString;return};if ["failed","needs_review","needs_details"].contains(row.state){throw PersonalOSError.message(row.result)}
   try await Task.sleep(nanoseconds:1_000_000_000)
  };error="Mac仍在生成；稍后点击查看生成结果，无需重复提交"
 }catch{self.error=error.localizedDescription}}}
}

@MainActor enum MacTaskClient {
 static func create(_ action:[String:Any])async throws->String{let bytes=try JSONSerialization.data(withJSONObject:action);let data=try await API.request("/mac/actions",["payload":String(decoding:bytes,as:UTF8.self)]);guard let id=try JSONDecoder().decode([String:String].self,from:data)["id"] else{throw PersonalOSError.message("Mac未返回任务编号")};return id}
}
@MainActor private struct MacOperationsForm:View {
 @State private var macRequest:MacRequestSelection?
 @State private var app="finder";@State private var url="";@State private var busy=false;@State private var error:String?
 private let apps=[("finder","Finder"),("safari","Safari"),("textedit","文本编辑"),("notes","备忘录"),("calendar","日历"),("preview","预览")]
 var body:some View{Form{
  Section("打开应用"){Picker("应用",selection:$app){ForEach(apps,id:\.0){item in Text(item.1).tag(item.0)}};Button("准备打开"){prepare(["operation":"launch_app","app":app])}.disabled(busy)}
  Section("打开网页"){TextField("https://",text:$url).textInputAutocapitalization(.never).autocorrectionDisabled();Button("在 Mac Safari 打开"){prepare(["operation":"open_url","url":url])}.disabled(busy || url.isEmpty)}
  if let error{Section{Text(error).foregroundStyle(.orange)}}
  Section{Text("先检查清单，再确认执行。文件的打开、编辑及写回在「文件」页选择。").font(.caption).foregroundStyle(.secondary)}
 }.sheet(item:$macRequest){MacActionReviewSheet(taskID:$0.id)}}
 private func prepare(_ action:[String:Any]){Task{busy=true;defer{busy=false};do{macRequest=MacRequestSelection(id:try await MacTaskClient.create(action))}catch{self.error=error.localizedDescription}}}
}

struct PhoneExecutionPlan:Decodable{let kind:String;let section:String;let query:String}
extension TaskRecord{var phonePlan:PhoneExecutionPlan?{try? JSONDecoder().decode(PhoneExecutionPlan.self,from:Data(plan.utf8))}}
@MainActor struct PhoneActionResult:View {
 let plan:PhoneExecutionPlan
 @State private var presenting=false
 var body:some View{Button{presenting=true}label:{Label(plan.section=="导航" ? "在手机选择导航地点" : "在手机搜索餐厅",systemImage:"map")}.buttonStyle(.borderedProminent).sheet(isPresented:$presenting){PhoneActionsScreen(section:plan.section,query:plan.query)}}
}
