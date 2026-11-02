import Combine
import SwiftUI

/// iPhone app: plays its OWN server queue (the Apple TV app has a separate one); library, playlists and
/// the server are shared.
@main
struct PhoneApp: App {
    @UIApplicationDelegateAdaptor(PhoneAppDelegate.self) private var appDelegate
    // Owned by PhoneServices: a Siri intent may use them while the app runs in the background without a window.
    @StateObject private var locator = PhoneServices.shared.locator
    @StateObject private var connection = PhoneServices.shared.connection
    @StateObject private var engine = PhoneServices.shared.engine
    @StateObject private var library = PhoneServices.shared.library
    @StateObject private var artwork = PhoneServices.shared.artwork
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
            // Wiring, a new server address and no-server mode on/off: PhoneServices.
            .onAppear { PhoneServices.shared.start() }
            .onChange(of: connection.autoVersion) {
                Task { await library.refreshLoved() }
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
    /// Items with a ♥: /api/collection/loved after every load and every change (PhoneApp, autoVersion).
    /// nil = not known yet (the rows' own flag counts).
    @Published private(set) var lovedIDs: Set<Int>?
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
                let list: [MediaItem]
                if Config.localMode {
                    list = LocalLibrary.shared.items  // already linked (VersionLinks) and marked (♥)
                } else {
                    let (data, _) = try await Net.data(from: .api("api/library"))
                    // Song <-> video links for the menus (the server's own rules; summaries carry only the count)
                    list = VersionLinks.attach(try JSONDecoder.server().decode(ItemList.self, from: data).items)
                }
                items = list
                byID = Dictionary(list.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
                lovedIDs = Set(list.filter { $0.loved == true }.map(\.id))
                errorText = nil
                await refreshLoved()  // the library rows leave out hidden duplicates; the Loved list keeps them
            } catch {
                errorText = error.localizedDescription
            }
        } while reloadPending
    }

    func reset() {
        items = []
        byID = [:]
        lovedIDs = nil
    }

    /// The heart is per version group: the item or any of its versions is in the Loved list.
    func isLoved(_ item: MediaItem) -> Bool {
        guard let lovedIDs else { return item.loved == true }
        return group(item.id, item.versions).contains { lovedIDs.contains($0) }
    }

    /// Shown at once; the queue owner's answer (refreshLoved) confirms it. Off clears the whole group (like the server).
    func markLoved(_ id: Int, _ on: Bool) {
        var ids = lovedIDs ?? []
        if on { ids.insert(id) } else { ids.subtract(group(id, nil)) }
        lovedIDs = ids
    }

    /// MediaItem.versionGroup: the versions and the item itself.
    private func group(_ id: Int, _ versions: [MediaVersion]?) -> [Int] {
        let ids = (versions ?? byID[id]?.versions)?.map(\.id) ?? []
        return ids.contains(id) ? ids : ids + [id]
    }

    /// A ♥ changed somewhere (this phone, the web remote, the Apple TV): the server's list of loved items.
    func refreshLoved() async {
        guard let (data, response) = try? await Net.data(from: .api("api/collection/loved")),
              ((response as? HTTPURLResponse)?.statusCode ?? 200) == 200,
              let list = try? JSONDecoder.server().decode(ItemList.self, from: data) else { return }
        lovedIDs = Set(list.items.map(\.id))
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
