import SwiftUI

/// Player menu: every version of the playing item (the song and its music videos, tick on the current one) and the
/// queue's "Prefer videos" switch. A pick plays that version in the same queue slot (same position for the same cut).
struct VersionsMenu<Content: View>: View {
    @ObservedObject var connection: ServerConnection
    let engine: PlayerEngine
    let item: MediaItem
    @ViewBuilder var label: () -> Content

    var body: some View {
        Menu {
            Section("Versions") {
                ForEach(item.versions ?? []) { version in
                    Button {
                        guard version.id != item.id else { return }
                        engine.diag("versions menu: \(item.id) -> \(version.id) (\(version.label))")
                        connection.switchVersion(to: version.id, position: engine.livePosition)
                    } label: {
                        if version.id == item.id {
                            Label(version.label, systemImage: "checkmark")
                        } else {
                            Label(version.label, systemImage: version.isVideo ? "film" : "music.note")
                        }
                    }
                }
            }
            Section {
                Toggle(isOn: preferVideosBinding(connection)) {
                    Label("Prefer videos", systemImage: "play.rectangle")
                }
            }
        } label: {
            label()
        }
    }
}

/// The queue's "Prefer videos" flag (server or LocalQueue): songs play as one of their music videos.
@MainActor
func preferVideosBinding(_ connection: ServerConnection) -> Binding<Bool> {
    Binding(get: { connection.state?.preferVideos == true },
            set: { connection.command("prefer_videos", ["on": $0]) })
}
