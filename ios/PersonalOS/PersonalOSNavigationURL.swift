import Foundation

enum PersonalOSNavigationURL {
    static func make(latitude:Double,longitude:Double,mode:String)throws->String {
        guard latitude.isFinite,longitude.isFinite,(-90...90).contains(latitude),(-180...180).contains(longitude),["walking","driving","transit"].contains(mode) else{throw URLError(.badURL)}
        var url=URLComponents(string:"https://maps.apple.com/directions")!
        // Apple's documented start parameter requests navigation after one second.
        // Leave source unset so Maps uses the phone's current location.
        url.queryItems=[URLQueryItem(name:"destination",value:"\(latitude),\(longitude)"),URLQueryItem(name:"mode",value:mode),URLQueryItem(name:"start",value:"1")]
        return url.url!.absoluteString
    }
}
