import Foundation
import Security
import SwiftUI
import UIKit

/// Remote access from outside the home network: HTTPS address + user name + password (HTTP Basic, checked
/// by the reverse proxy), and what to stream over mobile data. Nothing here is built into the app: the
/// address and login are typed in Settings (the password is kept in the Keychain).
enum RemoteSettings {
    static let urlKey = "remoteURL"
    static let userKey = "remoteUser"
    static let modeKey = "remoteStreamMode"     // "audio" (default) or "video"
    static let keychainService = "eu.mihten.atvplayer.remote"

    private static var defaults: UserDefaults { .standard }

    static var urlText: String { defaults.string(forKey: urlKey) ?? "" }
    static var user: String { defaults.string(forKey: userKey) ?? "" }
    /// Music videos over the remote link: audio only (default, saves mobile data) or audio + video.
    static var audioOnly: Bool { (defaults.string(forKey: modeKey) ?? "audio") != "video" }

    static var password: String {
        if let demo = DemoRoute.argument("remotePasswordDemo") { return demo }  // CI test only
        return Keychain.get(keychainService) ?? ""
    }

    static func setPassword(_ value: String) {
        if value.isEmpty { Keychain.remove(keychainService) } else { Keychain.set(value, for: keychainService) }
    }

    static var configuredURL: URL? { normalize(urlText) }
    static var isConfigured: Bool { configuredURL != nil && !user.isEmpty && !password.isEmpty }

    /// "music.example.org", "https://music.example.org" or "…:8443" → https URL without a path. Plain
    /// "http://" is kept only if typed explicitly (tests); the default is https on port 443.
    static func normalize(_ raw: String) -> URL? {
        var text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        if !text.contains("://") { text = "https://" + text }
        guard var components = URLComponents(string: text), let host = components.host, !host.isEmpty else { return nil }
        components.path = ""
        components.query = nil
        components.user = nil
        components.password = nil
        return components.url
    }

    /// Asks the server (through the proxy) with the typed login. For the Settings "Test" button.
    static func test() async -> String {
        guard let url = configuredURL else { return "Enter the address first." }
        guard !user.isEmpty, !password.isEmpty else { return "Enter the user name and password." }
        var request = URLRequest(url: url.appendingPathComponent("api/state"))
        request.timeoutInterval = 8
        request.setValue(Net.basic(user: user, password: password), forHTTPHeaderField: "Authorization")
        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            switch (response as? HTTPURLResponse)?.statusCode ?? 0 {
            case 200: return "Connected. The server answered."
            case 401: return "The server says the user name or password is wrong."
            case 404, 502, 503: return "The address works, but the server behind it did not answer."
            case let code: return "Unexpected answer (HTTP \(code))."
            }
        } catch {
            return "Could not connect: \(error.localizedDescription)"
        }
    }
}

/// Requests to the media server: adds the Basic login when the remote address is in use (the proxy in
/// front of it checks it). On the home network nothing is added.
enum Net {
    static func basic(user: String, password: String) -> String {
        "Basic " + Data("\(user):\(password)".utf8).base64EncodedString()
    }

    static func request(_ url: URL) -> URLRequest {
        var request = URLRequest(url: url)
        // API answers (playlists, library, state) change with every edit: always ask the server, never the URL cache.
        // Artwork and media keep the normal cache (the server sends max-age for those).
        if url.path.contains("/api/") { request.cachePolicy = .reloadIgnoringLocalCacheData }
        if Config.isRemote, let login = Config.credentials, url.host == Config.serverURL.host {
            request.setValue(basic(user: login.user, password: login.password), forHTTPHeaderField: "Authorization")
        }
        return request
    }

    static func data(from url: URL) async throws -> (Data, URLResponse) {
        try await URLSession.shared.data(for: request(url))
    }
}

/// Minimal Keychain wrapper (one generic password per service name).
enum Keychain {
    private static func query(_ service: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
         kSecAttrAccount as String: "login"]
    }

    static func get(_ service: String) -> String? {
        var q = query(service)
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: AnyObject?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let data = out as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func set(_ value: String, for service: String) {
        remove(service)
        var q = query(service)
        q[kSecValueData as String] = Data(value.utf8)
        q[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        SecItemAdd(q as CFDictionary, nil)
    }

    static func remove(_ service: String) {
        SecItemDelete(query(service) as CFDictionary)
    }
}

/// AsyncImage replacement that sends the login (AsyncImage cannot add headers) and keeps a small cache.
struct RemoteImage<Placeholder: View>: View {
    let url: URL?
    @ViewBuilder var placeholder: () -> Placeholder
    @State private var image: UIImage?

    private static var cache: NSCache<NSURL, UIImage> { ImageCache.shared }

    var body: some View {
        ZStack {
            if let image {
                Image(uiImage: image).resizable().scaledToFill().transition(.opacity)
            } else {
                placeholder()
            }
        }
        .animation(.easeOut(duration: 0.35), value: image != nil)
        .task(id: url) { await load() }
    }

    private func load() async {
        guard let url else { image = nil; return }
        if let hit = Self.cache.object(forKey: url as NSURL) { image = hit; return }
        image = nil
        guard let (data, response) = try? await Net.data(from: url),
              (response as? HTTPURLResponse)?.statusCode == 200, let decoded = UIImage(data: data) else { return }
        Self.cache.setObject(decoded, forKey: url as NSURL)
        image = decoded
    }
}

enum ImageCache {
    static let shared: NSCache<NSURL, UIImage> = {
        let cache = NSCache<NSURL, UIImage>()
        cache.countLimit = 300
        return cache
    }()
}
