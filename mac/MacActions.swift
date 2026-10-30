@preconcurrency import AppKit
import Foundation

// Fixed structured operations, invoked only after the paired phone's approval.
// No AppleScript, synthetic clicks, terminal commands, or arbitrary executables.
@main struct MacActionsBridge {
 @MainActor static func main() {
  do {
   let data=FileHandle.standardInput.readDataToEndOfFile()
   guard data.count<=65536,let request=try JSONSerialization.jsonObject(with:data) as? [String:String],let operation=request["operation"] else{throw Failure.invalid}
   let ids=["finder":"com.apple.finder","safari":"com.apple.Safari","textedit":"com.apple.TextEdit","notes":"com.apple.Notes","calendar":"com.apple.iCal","preview":"com.apple.Preview"]
   let workspace=NSWorkspace.shared
   func finish(_ accepted:Bool,_ application:NSRunningApplication?=nil){
    let result:[String:Any]=["accepted":accepted,"operation":operation,"bundle_identifier":application?.bundleIdentifier ?? "","process_id":application?.processIdentifier ?? 0]
    FileHandle.standardOutput.write(try! JSONSerialization.data(withJSONObject:result));exit(accepted ? 0 : 1)
   }
   let config=NSWorkspace.OpenConfiguration();config.activates=true;config.promptsUserIfNeeded=true
   switch operation {
   case "launch_app":
    guard Set(request.keys)==["operation","app"],let key=request["app"],let id=ids[key],let app=workspace.urlForApplication(withBundleIdentifier:id) else{throw Failure.invalid}
    workspace.openApplication(at:app,configuration:config){app,error in Task{@MainActor in finish(error==nil && app != nil,app)}}
   case "open_url":
    guard Set(request.keys)==["operation","url"],let raw=request["url"],let url=URL(string:raw),url.scheme=="https",url.user==nil,url.password==nil,let safari=workspace.urlForApplication(withBundleIdentifier:ids["safari"]!) else{throw Failure.invalid}
    workspace.open([url],withApplicationAt:safari,configuration:config){app,error in Task{@MainActor in finish(error==nil && app != nil,app)}}
   case "open_file","reveal_file":
    guard Set(request.keys)==["operation","path"],let path=request["path"],path.hasPrefix("/"),!path.contains("/../"),["txt","md","docx","pdf","rtf","png","jpg","jpeg","heic"].contains(URL(fileURLWithPath:path).pathExtension.lowercased()) else{throw Failure.invalid}
    let url=URL(fileURLWithPath:path);let values=try url.resourceValues(forKeys:[.isRegularFileKey,.isSymbolicLinkKey]);guard values.isRegularFile==true,values.isSymbolicLink != true else{throw Failure.invalid}
    if operation=="reveal_file"{workspace.activateFileViewerSelecting([url]);finish(true)}
    else{workspace.open(url,configuration:config){app,error in Task{@MainActor in finish(error==nil && app != nil,app)}}}
   default:throw Failure.invalid
   }
   DispatchQueue.main.asyncAfter(deadline:.now()+15){finish(false)}
   RunLoop.main.run()
  }catch{FileHandle.standardError.write(Data("Mac action rejected".utf8));exit(1)}
 }
 enum Failure:Error{case invalid}
}
