import SwiftUI

/// Settings → Mixes: the two Home shuffle tiles. A mix holds a name and folders and / or playlists of the library; the
/// server keeps them (Shared/Mixes.swift), so the iPhone and the web page show the same two. Pages open in the right half
/// of Settings (SettingsNavigator), one level per folder.
struct MixesSettingsView: View {
    @StateObject private var editor = MixEditor()
    @Environment(\.settingsNav) private var nav

    var body: some View {
        SettingsPage {
            Section {
                if let error = editor.error {
                    Text(error).foregroundStyle(.red)
                }
                ForEach(editor.mixes) { mix in
                    Button {
                        nav.push(SettingsScreen(title: mix.name, content: AnyView(MixSettingsPage(editor: editor, number: mix.number))))
                    } label: {
                        HStack(spacing: 20) {
                            Text(mix.name)
                            Spacer()
                            Text(mix.summary).foregroundStyle(.secondary).lineLimit(1)
                            Image(systemName: "chevron.right").font(.system(size: 24, weight: .semibold)).foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
        .task { await editor.reload() }
    }
}

/// One mix: its name, what is in it (a click removes), and where to add more.
struct MixSettingsPage: View {
    @ObservedObject var editor: MixEditor
    let number: Int
    @Environment(\.settingsNav) private var nav
    @State private var name = ""

    var body: some View {
        let mix = editor.mix(number)
        SettingsPage {
            Section {
                TextField("Name", text: $name)
                    .autocorrectionDisabled()
                    .onSubmit { Task { await editor.rename(number, to: name) } }
            }
            Section {
                ForEach(mix?.sources ?? []) { source in
                    Button {
                        Task { await editor.remove(number, source: source) }
                    } label: {
                        HStack(spacing: 20) {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(source.title).lineLimit(1)
                                Text(source.detail).font(.system(size: 22)).foregroundStyle(.secondary).lineLimit(1)
                            }
                            Spacer()
                            Image(systemName: "trash").foregroundStyle(.secondary)
                        }
                    }
                }
                if mix?.sources.isEmpty ?? true {
                    Text("Nothing added yet.").foregroundStyle(.secondary)
                }
            } header: {
                Text("A click removes")
            }
            Section {
                Button {
                    nav.push(SettingsScreen(title: "Add a folder", content: AnyView(
                        MixFolderPage(editor: editor, number: number, root: nil, folder: "", pops: 1))))
                } label: {
                    Label("Add folder", systemImage: "folder.badge.plus")
                }
                Button {
                    nav.push(SettingsScreen(title: "Add a playlist", content: AnyView(
                        MixPlaylistPage(editor: editor, number: number))))
                } label: {
                    Label("Add playlist", systemImage: "music.note.list")
                }
            }
        }
        .onAppear { name = mix?.name ?? "" }
        .onDisappear { Task { await editor.rename(number, to: name) } }
    }
}

/// The library folders, then their subfolders; "Add this folder" puts it in the mix and goes back to the mix page.
struct MixFolderPage: View {
    @ObservedObject var editor: MixEditor
    let number: Int
    let root: String?
    let folder: String
    /// Pages pushed since "Add folder" (each one is popped after adding).
    let pops: Int
    @Environment(\.settingsNav) private var nav
    @AppStorage("mixIncludeSubfolders") private var recursive = true
    @State private var roots: [RootInfo] = []
    @State private var subfolders: [SubfolderInfo] = []
    @State private var total = 0

    var body: some View {
        SettingsPage {
            Section {
                Toggle("Include subfolders", isOn: $recursive)
                if let root {
                    Button {
                        Task {
                            await editor.add(number, source: .folder(root: root, folder: folder, recursive: recursive))
                            for _ in 0..<pops { nav.pop() }
                        }
                    } label: {
                        Label("Add this folder (\(total))", systemImage: "plus.circle.fill")
                    }
                }
            }
            Section {
                if let root {
                    ForEach(subfolders) { sub in
                        Button {
                            nav.push(SettingsScreen(title: sub.name, content: AnyView(
                                MixFolderPage(editor: editor, number: number, root: root, folder: sub.folder, pops: pops + 1))))
                        } label: {
                            row(sub.name, count: sub.count)
                        }
                    }
                } else {
                    ForEach(roots) { entry in
                        Button {
                            nav.push(SettingsScreen(title: entry.name, content: AnyView(
                                MixFolderPage(editor: editor, number: number, root: entry.root, folder: "", pops: pops + 1))))
                        } label: {
                            row(entry.name, count: entry.count)
                        }
                    }
                }
            }
        }
        .task {
            if let root {
                if let result = await MixesAPI.browse(root: root, folder: folder) {
                    subfolders = result.subfolders
                    total = result.total
                }
            } else {
                roots = await MixesAPI.roots() ?? []
            }
        }
    }

    private func row(_ title: String, count: Int) -> some View {
        HStack(spacing: 20) {
            Text(title).lineLimit(1)
            Spacer()
            Text("\(count)").foregroundStyle(.secondary)
            Image(systemName: "chevron.right").font(.system(size: 24, weight: .semibold)).foregroundStyle(.secondary)
        }
    }
}

/// The playlists that are not in the mix yet; a click adds one and goes back.
struct MixPlaylistPage: View {
    @ObservedObject var editor: MixEditor
    let number: Int
    @Environment(\.settingsNav) private var nav
    @State private var playlists: [PlaylistInfo] = []
    @State private var loaded = false

    var body: some View {
        let have = Set(editor.mix(number)?.sources.compactMap(\.playlistID) ?? [])
        let free = playlists.filter { !have.contains($0.id) }
        SettingsPage {
            ForEach(free) { playlist in
                Button {
                    Task {
                        await editor.add(number, source: .playlist(playlist.id))
                        nav.pop()
                    }
                } label: {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(playlist.name).lineLimit(1)
                        Text(playlist.detail).font(.system(size: 22)).foregroundStyle(.secondary).lineLimit(1)
                    }
                }
            }
            if loaded && free.isEmpty {
                Text("No other playlists.").foregroundStyle(.secondary)
            }
        }
        .task {
            playlists = await MixesAPI.playlists() ?? []
            loaded = true
        }
    }
}
