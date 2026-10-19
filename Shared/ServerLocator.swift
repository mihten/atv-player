import Foundation
import Network

/// Finds the media server, so no address is built into the app:
/// 1. the address typed in Settings (if any),
/// 2. else the last address found, while Bonjour checks again in the background,
/// 3. else Bonjour: the server advertises `_atvmedia._tcp` with its URL in the TXT record.
/// A server that stops answering (new address) triggers a new Bonjour search.
@MainActor
final class ServerLocator: ObservableObject {
    static let manualKey = "serverAddress"   // typed in Settings; empty = automatic
    static let foundKey = "serverFound"      // last address found via Bonjour
    static let serviceType = "_atvmedia._tcp"

    @Published private(set) var url: URL?
    @Published private(set) var status = "Looking for the ATV Player server…"
    @Published private(set) var searching = false
    private var browser: NWBrowser?

    init() {
        if let manual = Self.manualURL {
            use(manual, "Address from Settings")
        } else if let found = UserDefaults.standard.string(forKey: Self.foundKey).flatMap(URL.init(string:)) {
            use(found, "Found automatically")
            browse()
        } else {
            browse()
        }
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
        UserDefaults.standard.set(first.absoluteString, forKey: Self.foundKey)
        if first != url { use(first, "Found automatically") }
    }

    private func use(_ newURL: URL, _ how: String) {
        Config.serverURL = newURL
        url = newURL
        status = how
    }
}
