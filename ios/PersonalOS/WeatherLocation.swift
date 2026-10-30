import Foundation
import SwiftUI
import UIKit
@preconcurrency import CoreLocation

@MainActor final class WeatherLocation:NSObject,ObservableObject,@preconcurrency CLLocationManagerDelegate {
    static let shared=WeatherLocation()
    @Published private(set) var enabled=UserDefaults.standard.bool(forKey:"PersonalOS.weather.location.enabled.v1")
    @Published private(set) var status="天气位置未更新"
    @Published private(set) var busy=false
    private var updatedAt=Date.distantPast
    private lazy var manager:CLLocationManager={let value=CLLocationManager();value.delegate=self;value.desiredAccuracy=kCLLocationAccuracyKilometer;return value}()
    func enable() {
        enabled=true;UserDefaults.standard.set(true,forKey:"PersonalOS.weather.location.enabled.v1")
        if manager.authorizationStatus == .notDetermined {manager.requestWhenInUseAuthorization()}
        else {request()}
    }
    func disable() {
        enabled=false;UserDefaults.standard.set(false,forKey:"PersonalOS.weather.location.enabled.v1");updatedAt = .distantPast;status="天气定位已关闭"
        Task{_ = try? await API.request("/location/clear",[:])}
    }
    private func request() {
        guard enabled,!busy else{return}
        guard [.authorizedWhenInUse,.authorizedAlways].contains(manager.authorizationStatus) else {status="请在系统设置允许使用期间定位";return}
        busy=true;status="正在更新大致位置";manager.requestLocation()
    }
    func locationManagerDidChangeAuthorization(_ manager:CLLocationManager) {
        if enabled {request()}
    }
    func locationManager(_ manager:CLLocationManager,didUpdateLocations locations:[CLLocation]) {
        busy=false
        guard enabled,let location=locations.last,location.horizontalAccuracy>=0,abs(location.timestamp.timeIntervalSinceNow)<120 else {status="位置暂不可用，请重试";return}
        // Only an approximately 10km grid leaves this callback. No precise
        // location is written to preferences, logs, the model or the server.
        let lat=(location.coordinate.latitude*10).rounded()/10,lon=(location.coordinate.longitude*10).rounded()/10
        let captured=location.timestamp.timeIntervalSince1970
        Task{
            do {
                let endpoint=try API.endpoint(),pair=PendingSubmission.fingerprint(for:endpoint)
                let value:[String:Any]=["latitude":lat,"longitude":lon,"captured_at":captured,"time_zone":TimeZone.current.identifier]
                let data=try JSONSerialization.data(withJSONObject:value)
                _ = try await API.request("/location",["payload":String(decoding:data,as:UTF8.self)],endpoint:endpoint)
                guard enabled,try API.endpoint().key==endpoint.key,PendingSubmission.fingerprint(for:endpoint)==pair else{return}
                updatedAt=Date(timeIntervalSince1970:captured);status="大致位置已更新"
            }catch{status=error.localizedDescription}
        }
    }
    func locationManager(_ manager:CLLocationManager,didFailWithError error:Error){busy=false;status="定位暂不可用，请重试"}
    func prepareIfNeeded(_ text:String)async throws {
        guard text.range(of:#"天气|下雨|降雨|带伞|\bweather\b|\brain\b"#,options:[.regularExpression,.caseInsensitive]) != nil else{return}
        if text.range(of:#"API|文档|资料|模型|research"#,options:[.regularExpression,.caseInsensitive]) != nil{return}
        guard enabled else {throw PersonalOSError.message("请先在设置开启天气定位，然后查询当地天气")}
        if Date().timeIntervalSince(updatedAt)<600{return}
        if UIApplication.shared.applicationState != .active {return} // Existing private server cache may still be fresh; never request a background permission.
        request()
        for _ in 0..<24 {
            if Date().timeIntervalSince(updatedAt)<600{return}
            try await Task.sleep(nanoseconds:250_000_000)
        }
        throw PersonalOSError.message(status)
    }
}
