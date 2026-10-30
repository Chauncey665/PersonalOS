import Foundation
import SwiftUI
import CryptoKit
import UIKit

struct DocumentDescriptor:Codable,Identifiable {
    let id:String
    let name:String
    let version:Int
    let sha256:String
    let paragraph_count:Int
    let bytes:Int
    var source_device:String?=nil
    var source_path:String?=nil
}
extension TaskRecord {
    var documentVersions:[DocumentDescriptor] {
        guard let data=plan.data(using:.utf8),let value=try? JSONSerialization.jsonObject(with:data) as? [String:Any],let rows=value["documents"],let bytes=try? JSONSerialization.data(withJSONObject:rows) else{return []}
        return (try? JSONDecoder().decode([DocumentDescriptor].self,from:bytes)) ?? []
    }
}
@MainActor enum PersonalOSClipboard {
    static func copy(_ text:String) {
        UIPasteboard.general.setItems([["public.utf8-plain-text":text]],options:[.localOnly:true,.expirationDate:Date().addingTimeInterval(3600)])
    }
    static func deliverIfRequested(_ tasks:[TaskRecord],endpoint:ServerEndpoint)->Bool {
        guard UIApplication.shared.applicationState == .active else{return false}
        for task in tasks where task.state=="completed" && Date().timeIntervalSince1970-(task.created ?? 0)<600 {
            guard task.title.range(of:#"剪贴板|copy.{0,30}clipboard"#,options:[.regularExpression,.caseInsensitive]) != nil else{continue}
            let receipt="PersonalOS.clipboard."+JournalMetadata.hash(endpoint.key+"|"+PendingSubmission.fingerprint(for:endpoint)+"|"+task.id+"|"+task.result)
            guard !UserDefaults.standard.bool(forKey:receipt) else{continue}
            copy(task.result);UserDefaults.standard.set(true,forKey:receipt);return true
        }
        return false
    }
}

@MainActor final class DocumentClient {
    static let shared=DocumentClient()
    func upload(_ url:URL)async throws->DocumentDescriptor {
        let endpoint=try API.endpoint(),pair=PendingSubmission.fingerprint(for:endpoint)
        let imported=try await Task.detached(priority:.userInitiated){()throws->(String,Data) in
            let scoped=url.startAccessingSecurityScopedResource();defer{if scoped{url.stopAccessingSecurityScopedResource()}}
            var result:Data?,readError:Error?,coordinationError:NSError?
            NSFileCoordinator().coordinate(readingItemAt:url,options:.withoutChanges,error:&coordinationError){value in
                do{let size=try value.resourceValues(forKeys:[.fileSizeKey]).fileSize ?? 0;guard size<=5*1024*1024 else{throw PersonalOSError.message("文档请控制在5MB以内")};result=try Data(contentsOf:value);if result!.count>5*1024*1024{throw PersonalOSError.message("文档请控制在5MB以内")}}catch{readError=error}
            }
            if let error=readError ?? coordinationError{throw error}
            guard let result else{throw PersonalOSError.message("文档暂不可读取，请先下载到手机")}
            return(url.lastPathComponent,result)
        }.value
        let body=["name":imported.0,"data":imported.1.base64EncodedString(),"request_id":UUID().uuidString]
        let data=try await API.request("/documents",body,endpoint:endpoint,timeout:60)
        guard try API.endpoint().key==endpoint.key,PendingSubmission.fingerprint(for:endpoint)==pair else{throw PersonalOSError.message("连接已改变，请重新选择文档")}
        return try JSONDecoder().decode(DocumentDescriptor.self,from:data)
    }
    func info(_ id:String)async throws->DocumentDescriptor {try JSONDecoder().decode(DocumentDescriptor.self,from:try await API.request("/documents/"+id))}
    func undo(_ document:DocumentDescriptor)async throws->DocumentDescriptor {
        let data=try await API.request("/documents/"+document.id+"/undo",["version":String(document.version)])
        return try JSONDecoder().decode(DocumentDescriptor.self,from:data)
    }
    func download(_ document:DocumentDescriptor)async throws->URL {
        struct Chunk:Decodable {let id:String;let version:Int;let name:String;let sha256:String;let total:Int;let offset:Int;let data:String;let next_offset:Int?}
        let endpoint=try API.endpoint(),pair=PendingSubmission.fingerprint(for:endpoint)
        var output=Data(),offset=0,name=document.name
        for _ in 0..<24 {
            try Task.checkCancellation()
            let response=try await API.request("/documents/"+document.id+"/download?version="+String(document.version)+"&offset="+String(offset),endpoint:endpoint)
            guard try API.endpoint().key==endpoint.key,PendingSubmission.fingerprint(for:endpoint)==pair else{throw PersonalOSError.message("连接已改变，未保存下载")}
            let chunk=try JSONDecoder().decode(Chunk.self,from:response)
            guard chunk.id==document.id,chunk.version==document.version,chunk.sha256==document.sha256,chunk.offset==offset,chunk.total<=5*1024*1024,let bytes=Data(base64Encoded:chunk.data) else{throw PersonalOSError.message("文档下载校验未通过")}
            output.append(bytes);name=chunk.name
            if let next=chunk.next_offset {guard next==output.count,next>offset else{throw PersonalOSError.message("文档下载范围无效")};offset=next;continue}
            guard output.count==chunk.total,SHA256.hash(data:output).map({String(format:"%02x",$0)}).joined()==document.sha256 else{throw PersonalOSError.message("文档下载不完整，未保存")}
            let folder=FileManager.default.temporaryDirectory.appendingPathComponent("PersonalOS-Documents/"+UUID().uuidString,isDirectory:true)
            try FileManager.default.createDirectory(at:folder,withIntermediateDirectories:true)
            let ext=(name as NSString).pathExtension,stem=String((name as NSString).deletingPathExtension.prefix(60))
            let target=folder.appendingPathComponent(stem+"."+ext)
            try output.write(to:target,options:[.atomic,.completeFileProtection]);return target
        }
        throw PersonalOSError.message("文档下载超过本次范围，请重试")
    }
}

@MainActor struct DocumentResultRow:View {
    let document:DocumentDescriptor
    @State private var macRequest:MacRequestSelection?
    @State private var restored:DocumentDescriptor?
    @State private var url:URL?
    @State private var busy=false
    @State private var message:String?
    private var current:DocumentDescriptor{restored ?? document}
    var body:some View {
        VStack(alignment:.leading,spacing:10){
            Label(current.name,systemImage:"doc.text").font(.subheadline).lineLimit(2)
            HStack{
                if let url {ShareLink(item:url){Label("保存或分享",systemImage:"square.and.arrow.up")}}
                else {Button(busy ? "正在下载…" : "下载文档"){Task{busy=true;defer{busy=false};do{url=try await DocumentClient.shared.download(current)}catch{message=error.localizedDescription}}}}
                Spacer()
                if restored==nil && document.version>1 {Button("撤销修改"){Task{busy=true;defer{busy=false};do{restored=try await DocumentClient.shared.undo(document);url=nil;message="已恢复原版，修改版仍保留"}catch{message=error.localizedDescription}}}}
            }.font(.subheadline).disabled(busy)
            if current.source_device=="mini",current.version>1 {
                Button("查看写回Mac原件清单"){Task{busy=true;defer{busy=false};do{let data=try await API.request("/documents/"+current.id+"/writeback",["version":String(current.version)]);if let id=try JSONDecoder().decode([String:String].self,from:data)["id"]{macRequest=MacRequestSelection(id:id)}}catch{message=error.localizedDescription}}}.disabled(busy)
            }
            if let message{Text(message).font(.caption).foregroundStyle(.secondary)}
        }.padding(.vertical,8).sheet(item:$macRequest){MacActionReviewSheet(taskID:$0.id)}
    }
}
