import SwiftUI

/// iPhone app: plays its OWN server queue (the Apple TV app has a separate one); library, playlists and
/// the server are shared.
@main
struct PhoneApp: App {
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
                connection.onUnreachable = { [weak locator] in locator?.serverUnreachable() }
                if locator.url != nil { connection.start() }
            }
            .onChange(of: locator.url) { _, url in
                guard url != nil else { return }
                connection.restart()
                library.reset()
            }
            .onChange(of: scenePhase) { _, phase in
                switch phase {
                case .background: engine.appDidEnterBackground()
                case .active:
                    engine.appDidBecomeActive()
                    if locator.url != nil { connection.resume() }
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

    func load(force: Bool = false) async {
        guard !loading, force || items.isEmpty else { return }
        loading = true
        defer { loading = false }
        do {
            let (data, _) = try await URLSession.shared.data(from: .api("api/library"))
            let list = try JSONDecoder.server().decode(ItemList.self, from: data).items
            items = list
            byID = Dictionary(list.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
            errorText = nil
        } catch {
            errorText = error.localizedDescription
        }
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
