import Combine
import SwiftUI

/// iPhone app: plays its OWN server queue (the Apple TV app has a separate one); library, playlists and
/// the server are shared.
@main
struct PhoneApp: App {
    @UIApplicationDelegateAdaptor(PhoneAppDelegate.self) private var appDelegate
    @StateObject private var locator = ServerLocator()
    @StateObject private var connection = ServerConnection()
    @StateObject private var engine = PlayerEngine()
    @StateObject private var library = LibraryStore()
    @StateObject private var artwork = ArtworkStore()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            Group {
                if locator.url != nil {
                    PhoneRootView()
                } else {
                    ServerSearchView()
                }
            }
            .modifier(PhoneAppearance())
            .themeRoot()
            .environmentObject(locator)
            .environmentObject(connection)
            .environmentObject(engine)
            .environmentObject(library)
            .environmentObject(artwork)
            .onAppear {
                engine.connection = connection
                connection.engine = engine
                OrientationLock.shared.log = { [weak engine] in engine?.diag($0) }
                connection.onUnreachable = { [weak locator] in locator?.serverUnreachable() }
                if locator.url != nil { connection.start() }
            }
            .onChange(of: locator.url) { _, url in
                guard url != nil else { return }
                connection.restart()
                library.reset()
                engine.streamChanged()
            }
            // Switching no-server mode on is handled above (new url); switching it off stops the local queue.
            .onChange(of: locator.localMode) { _, on in
                if !on {
                    connection.stopLocal()
                    engine.unload()
                }
                // Server and no-server item ids overlap: drop covers, lyrics and keyframes cached by id.
                artwork.reset()
                LyricsStore.shared.reset()
                KeyframeStore.shared.reset()
                let store = artwork, player = engine
                Task { store.follow(player.current) }  // after this update, when the new mode's item is loaded
            }
            .onChange(of: scenePhase) { _, phase in
                switch phase {
                case .background: engine.appDidEnterBackground()
                case .active:
                    engine.appDidBecomeActive()
                    if locator.url != nil { connection.resume() }
                    locator.checkReachable()
                default: break
                }
            }
        }
    }
}

/// The whole library (/api/library, ~2,000 rows) for search and for naming queue entries.
@MainActor
final class LibraryStore: ObservableObject {
    @Published private(set) var items: [MediaItem] = []
    @Published private(set) var byID: [Int: MediaItem] = [:]
    @Published private(set) var errorText: String?
    private var loading = false
    /// A forced reload was asked for while loading: load once more afterwards.
    private var reloadPending = false
    private var localWatch: AnyCancellable?

    init() {
        // No-server mode: the on-device library changed (scan, tags read, reset).
        localWatch = LocalLibrary.shared.$version.dropFirst().sink { [weak self] _ in
            guard Config.localMode else { return }
            Task { @MainActor in await self?.load(force: true) }
        }
    }

    func load(force: Bool = false) async {
        if loading {
            if force { reloadPending = true }
            return
        }
        guard force || items.isEmpty else { return }
        loading = true
        defer { loading = false }
        repeat {
            reloadPending = false
            do {
                let (data, _) = try await Net.data(from: .api("api/library"))
                let list = try JSONDecoder.server().decode(ItemList.self, from: data).items
                items = list
                byID = Dictionary(list.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
                errorText = nil
            } catch {
                errorText = error.localizedDescription
            }
        } while reloadPending
    }

    func reset() {
        items = []
        byID = [:]
    }

    func search(_ text: String) -> [MediaItem] {
        let words = text.lowercased().split(separator: " ").map(String.init)
        guard !words.isEmpty else { return [] }
        return items.filter { item in
            let haystack = [item.title, item.artist ?? "", item.album ?? "", item.folder ?? ""]
                .joined(separator: " ").lowercased()
            return words.allSatisfy { haystack.contains($0) }
        }
    }
}

/// Themed looks are dark by design; Classic follows the system setting as before.
private struct PhoneAppearance: ViewModifier {
    @Environment(\.appTheme) private var theme

    func body(content: Content) -> some View {
        content.preferredColorScheme(theme.isClassic ? nil : .dark)
    }
}
