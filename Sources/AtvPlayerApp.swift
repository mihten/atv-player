import SwiftUI

@main
struct AtvPlayerApp: App {
    @StateObject private var locator = ServerLocator()
    @StateObject private var connection = ServerConnection()
    @StateObject private var engine = PlayerEngine()
    @StateObject private var artwork = ArtworkStore()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            Group {
                if locator.url != nil {
                    ContentView()
                } else {
                    ServerSearchView()
                }
            }
            .themeRoot()
            .environmentObject(locator)
            .environmentObject(connection)
            .environmentObject(engine)
            .environmentObject(artwork)
            .onAppear {
                engine.connection = connection
                connection.engine = engine
                connection.onUnreachable = { [weak locator] in locator?.serverUnreachable() }
                if locator.url != nil { connection.start() }
            }
            .onChange(of: locator.url) { _, url in
                if url != nil { connection.restart() }
            }
            // Switching no-server mode on is handled by the url change above (restart starts the local queue).
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
                if phase == .background {
                    engine.appDidEnterBackground()
                } else if phase == .active {
                    engine.appDidBecomeActive()
                }
            }
        }
    }
}
