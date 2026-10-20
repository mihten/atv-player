import SwiftUI

/// Screens opened by the `-demoRoute` launch argument (CI screenshots only, see Shared/DemoRoute.swift).
struct DemoDestination: View {
    var body: some View {
        switch DemoRoute.screen {
        case "videos": VideoListView()
        case "playlists": PlaylistsView()
        case "playlist": PlaylistView(playlistID: 1, title: "Playlist")
        case "folders": FolderRootsView()
        case "folder": FolderView(root: "music", folder: "Neon Harbor/Midnight Ferries", title: "Midnight Ferries")
        case "settings": SettingsView()
        case "theme": ThemeSettingsView()
        case "server": ServerSettingsView()
        case "sound": SoundSettingsView()
        case "playback": PlaybackSettingsView()
        case "lyrics": LyricsSettingsView()
        case "eq": CustomEqualizerView()
        default: EmptyView()
        }
    }

    /// Routes that push a screen on top of the main menu.
    static var pushes: Bool {
        guard let screen = DemoRoute.screen else { return false }
        return !["home", "player", "open-player"].contains(screen)
    }
}
