import SwiftUI

@main
struct AtvPlayerApp: App {
    @StateObject private var locator = ServerLocator()
    @StateObject private var connection = ServerConnection()
    @StateObject private var engine = PlayerEngine()
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
            .environmentObject(locator)
            .environmentObject(connection)
            .environmentObject(engine)
            .onAppear {
                engine.connection = connection
                connection.engine = engine
                connection.onUnreachable = { [weak locator] in locator?.serverUnreachable() }
                if locator.url != nil { connection.start() }
            }
            .onChange(of: locator.url) { _, url in
                if url != nil { connection.restart() }
            }
            .onChange(of: scenePhase) { _, phase in
                if phase == .background {
                    engine.appDidEnterBackground()
                }
            }
        }
    }
}
