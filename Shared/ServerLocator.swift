import Foundation
import Network

/// Finds the media server, so no address is built into the app:
/// 1. the address typed in Settings (if any),
/// 2. else the last address found, while Bonjour checks again in the background,
/// 3. else Bonjour: the server advertises `_atvmedia._tcp` with its URL in the TXT record.
/// A server that stops answering (new address) triggers a new Bonjour search.
/// Away from home (iPhone, when an internet address + login are set in Settings): if the home server does not
/// answer, the internet address is used; Bonjour keeps looking and the app goes back to the home server as
/// soon as it is found again. A typed address in Settings always wins and disables this.
@MainActor
final class ServerLocator: ObservableObject {
    static let manualKey = "serverAddress"   // typed in Settings; empty = automatic
    static let foundKey = "serverFound"      // last address found via Bonjour
    static let serviceType = "_atvmedia._tcp"

    @Published private(set) var url: URL?
    @Published private(set) var status = "Looking for the ATV Player server…"
    @Published private(set) var searching = false
    /// The internet address is in use (HTTPS + login) instead of the home server.
    @Published private(set) var isRemote = false
    private var browser: NWBrowser?
    private var fallbackTask: Task<Void, Never>?

    init() {
        if let manual = Self.manualURL {
            use(manual, "Address from Settings")
        } else if let found = UserDefaults.standard.string(forKey: Self.foundKey).flatMap(URL.init(string:)) {
            use(found, "Found automatically")
            browse()
            checkReachable()
        } else {
            browse()
            scheduleRemoteFallback()
        }
    }

    var remoteAvailable: Bool { !isManual && RemoteSettings.isConfigured }

    /// The home server we use does not answer within 2.5 s (phone on mobile data): use the internet address.
    func checkReachable() {
        guard !isRemote, remoteAvailable, let current = url else { return }
        Task {
            var request = URLRequest(url: current.appendingPathComponent("api/state"))
            request.timeoutInterval = 2.5
            let ok = ((try? await URLSession.shared.data(for: request))?.1 as? HTTPURLResponse)?.statusCode == 200
            if !ok, !isRemote, remoteAvailable { useRemote() }
        }
    }

    /// Nothing found at all (never been home with this app, or a fresh install away from home).
    private func scheduleRemoteFallback() {
        fallbackTask?.cancel()
        fallbackTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 4_000_000_000)
            guard let self, !Task.isCancelled, self.url == nil, self.remoteAvailable else { return }
            self.useRemote()
        }
    }

    /// Settings changed the internet address or login: apply it (reconnects when it is the one in use).
    func remoteSettingsChanged() {
        if isRemote {
            if remoteAvailable { useRemote() } else { fallbackToHome() }
        } else if url == nil {
            scheduleRemoteFallback()
        }
    }

    private func fallbackToHome() {
        isRemote = false
        Config.isRemote = false
        Config.credentials = nil
        if let found = UserDefaults.standard.string(forKey: Self.foundKey).flatMap(URL.init(string:)) {
            use(found, "Found automatically")
        }
        browse()
    }

    private func useRemote() {
        guard let remote = RemoteSettings.configuredURL else { return }
        fallbackTask?.cancel()
        use(remote, "Away from home (internet address)", remote: true)
        browse()  // keep looking for the home server
    }

    var isManual: Bool { Self.manualURL != nil }

    static var manualAddress: String { UserDefaults.standard.string(forKey: manualKey) ?? "" }

    static var manualURL: URL? { normalize(manualAddress) }

    /// "10.0.0.5", "host:8080" or a full URL → http://host:port (port 8080 if none).
    static func normalize(_ raw: String) -> URL? {
        var text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        if !text.contains("://") { text = "http://" + text }
        guard var components = URLComponents(string: text), let host = components.host, !host.isEmpty else {
            return nil
        }
        if components.port == nil { components.port = 8080 }
        components.path = ""
        components.query = nil
        return components.url
    }

    /// Settings: empty text = automatic (Bonjour). Returns false if the text is not an address.
    @discardableResult
    func setManual(_ raw: String) -> Bool {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.isEmpty {
            UserDefaults.standard.removeObject(forKey: Self.manualKey)
            browse()
            return true
        }
        guard let manual = Self.normalize(text) else { return false }
        UserDefaults.standard.set(text, forKey: Self.manualKey)
        stopBrowsing()
        use(manual, "Address from Settings")
        return true
    }

    /// The connection failed several times in a row.
    func serverUnreachable() {
        guard !isManual else { return }
        if !isRemote && remoteAvailable {
            useRemote()
            return
        }
        status = "Server not answering, searching again…"
        browse()
    }

    func browse() {
        guard browser == nil else { return }
        let parameters = NWParameters()
        parameters.includePeerToPeer = false
        let newBrowser = NWBrowser(for: .bonjourWithTXTRecord(type: Self.serviceType, domain: nil), using: parameters)
        newBrowser.browseResultsChangedHandler = { [weak self] results, _ in
            let urls: [URL] = results.compactMap { result in
                if case .bonjour(let txt) = result.metadata, let text = txt["url"] { return URL(string: text) }
                return nil
            }
            Task { @MainActor in self?.found(urls) }
        }
        newBrowser.stateUpdateHandler = { [weak self] state in
            Task { @MainActor in
                guard let self else { return }
                switch state {
                case .failed(let error):
                    self.status = "Search failed: \(error.localizedDescription)"
                    self.stopBrowsing()
                case .waiting(let error):
                    // Typically "PolicyDenied": local network access is off for this app.
                    self.status = "Search waiting: \(error.localizedDescription). Allow Local Network access, or type the address."
                default:
                    break
                }
            }
        }
        browser = newBrowser
        searching = true
        newBrowser.start(queue: .main)
    }

    private func stopBrowsing() {
        browser?.cancel()
        browser = nil
        searching = false
    }

    private func found(_ urls: [URL]) {
        guard !isManual, let first = urls.first else { return }
        stopBrowsing()
        fallbackTask?.cancel()
        UserDefaults.standard.set(first.absoluteString, forKey: Self.foundKey)
        if first != url || isRemote { use(first, "Found automatically") }  // back home
    }

    private func use(_ newURL: URL, _ how: String, remote: Bool = false) {
        isRemote = remote
        Config.isRemote = remote
        Config.credentials = remote ? (RemoteSettings.user, RemoteSettings.password) : nil
        Config.serverURL = newURL
        url = newURL
        status = how
    }
}
