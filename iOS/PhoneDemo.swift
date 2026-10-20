import SwiftUI

/// Screens opened by the `-demoRoute` launch argument (CI screenshots only, see Shared/DemoRoute.swift).
enum PhoneDemo {
    static var initialTab: Int {
        switch DemoRoute.screen {
        case "playlists", "playlist": 1
        case "queue": 2
        case "settings", "eq", "theme", "server", "sound", "playback", "lyrics": 3
        default: 0
        }
    }

    @ViewBuilder
    static func destination(_ route: String) -> some View {
        switch route {
        case "videos": PhoneVideoListView()
        case "folder": PhoneFolderView(root: "music", folder: "Neon Harbor/Midnight Ferries", title: "Midnight Ferries")
        case "playlist": PhonePlaylistView(playlistID: 1, title: "Playlist")
        case "eq": PhoneEqualizerView()
        case "theme": PhoneThemeSettingsView()
        case "server": PhoneServerSettingsView()
        case "sound": PhoneSoundSettingsView()
        case "playback": PhonePlaybackSettingsView()
        case "lyrics": PhoneLyricsSettingsView()
        default: EmptyView()
        }
    }
}

private struct DemoPush: ViewModifier {
    let route: String
    @State private var pushed = false

    func body(content: Content) -> some View {
        content
            .navigationDestination(isPresented: $pushed) { PhoneDemo.destination(route) }
            .onAppear { if DemoRoute.is(route) { pushed = true } }
    }
}

extension View {
    /// Pushes the screen for `route` when the app was launched with `-demoRoute <route>`.
    func demoPush(_ route: String) -> some View { modifier(DemoPush(route: route)) }
}
