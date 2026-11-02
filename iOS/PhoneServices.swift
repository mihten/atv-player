import Combine
import Foundation
import UIKit

/// The iPhone app's long-lived objects, shared by the SwiftUI app and the Siri / Shortcuts intents. An intent can run
/// with the app launched in the background (no window, no `onAppear`), so the wiring and the reactions to a new server
/// address live here, not in the view tree.
@MainActor
final class PhoneServices {
    static let shared = PhoneServices()

    let locator = ServerLocator()
    let connection = ServerConnection()
    let engine = PlayerEngine()
    let library = LibraryStore()
    let artwork = ArtworkStore()
    private var started = false
    private var backgroundReported = false
    private var watches: Set<AnyCancellable> = []

    private init() {}

    /// Wires the objects together and connects (or starts no-server mode). Safe to call more than once.
    func start() {
        if !started {
            started = true
            engine.connection = connection
            connection.engine = engine
            OrientationLock.shared.log = { [weak engine = self.engine] in engine?.diag($0) }
            connection.onUnreachable = { [weak locator = self.locator] in locator?.serverUnreachable() }
            // @Published sends before the value is stored: handle the change on the next main-queue turn.
            locator.$url.dropFirst().removeDuplicates().receive(on: DispatchQueue.main)
                .sink { [weak self] url in self?.serverChanged(url) }
                .store(in: &watches)
            locator.$localMode.dropFirst().removeDuplicates().receive(on: DispatchQueue.main)
                .sink { [weak self] on in self?.localModeChanged(on) }
                .store(in: &watches)
            // Siri learns new or renamed playlists without opening the screens.
            connection.$connected.removeDuplicates().filter { $0 }.map { _ in () }
                .merge(with: connection.$playlistsVersion.dropFirst().map { _ in () })
                .merge(with: connection.$autoVersion.dropFirst().map { _ in () })
                .debounce(for: .seconds(1), scheduler: DispatchQueue.main)
                .sink { _ in Task { await SiriCatalog.refresh() } }
                .store(in: &watches)
        }
        if locator.url != nil { connection.start() }
    }

    private func serverChanged(_ url: URL?) {
        guard url != nil else { return }
        connection.restart()
        library.reset()
        engine.streamChanged()
    }

    /// Switching no-server mode on is handled by the new url; switching it off stops the local queue.
    private func localModeChanged(_ on: Bool) {
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

    /// For an intent: start everything and wait until the queue owner answers (server socket or no-server queue).
    /// Returns false after `timeout` seconds without a connection.
    func connectForIntent(timeout: Double = 10) async -> Bool {
        let wasStarted = started
        start()
        // Launched in the background for Siri: no scene will report it, so the player is told here (once).
        if !backgroundReported, UIApplication.shared.connectedScenes.isEmpty,
           UIApplication.shared.applicationState == .background {
            backgroundReported = true
            engine.appDidEnterBackground()
        }
        if wasStarted, UIApplication.shared.applicationState != .active { await connection.ensureAlive() }
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if locator.url != nil {
                connection.start()
                if connection.connected { return true }
            }
            try? await Task.sleep(nanoseconds: 200_000_000)
        }
        return connection.connected
    }

    /// For the intents' list lookups: wait until a server address (or no-server mode) is known.
    func waitForAddress(timeout: Double) async -> Bool {
        start()
        let deadline = Date().addingTimeInterval(timeout)
        while locator.url == nil, Date() < deadline {
            try? await Task.sleep(nanoseconds: 200_000_000)
        }
        return locator.url != nil
    }
}
