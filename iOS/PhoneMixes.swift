import SwiftUI

/// Settings → Mixes: the two Home shuffle tiles. A mix holds a name and folders and / or playlists of the library; the
/// server keeps them (Shared/Mixes.swift), so the Apple TV and the web page show the same two.
struct PhoneMixesView: View {
    @StateObject private var editor = MixEditor()
    @State private var folderTarget: MixTarget?
    @State private var playlistTarget: MixTarget?

    var body: some View {
        Form {
            if let error = editor.error {
                Section { Text(error).foregroundStyle(.red) }.themedRow()
            }
            ForEach(editor.mixes) { mix in
                Section {
                    MixNameField(mix: mix, editor: editor)
                    ForEach(mix.sources) { source in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(source.title)
                            Text(source.detail).font(.footnote).foregroundStyle(.secondary)
                        }
                    }
                    .onDelete { offsets in
                        let doomed = offsets.map { mix.sources[$0] }
                        Task { for source in doomed { await editor.remove(mix.number, source: source) } }
                    }
                    Button { folderTarget = MixTarget(number: mix.number) } label: {
                        Label("Add folder", systemImage: "folder.badge.plus")
                    }
                    Button { playlistTarget = MixTarget(number: mix.number) } label: {
                        Label("Add playlist", systemImage: "music.note.list")
                    }
                } header: {
                    Text("Mix \(mix.number)")
                } footer: {
                    Text(mix.count == 1 ? "1 file" : "\(mix.count) files")
                }
                .themedRow()
            }
        }
        .phoneThemed()
        .navigationTitle("Mixes")
        .task { await editor.reload() }
        .sheet(item: $folderTarget) { target in
            NavigationStack {
                PhoneMixFolderPicker(editor: editor, number: target.number, root: nil, folder: "", title: "Add a folder") {
                    folderTarget = nil
                }
            }
        }
        .sheet(item: $playlistTarget) { target in
            NavigationStack {
                PhoneMixPlaylistPicker(editor: editor, number: target.number) { playlistTarget = nil }
            }
        }
    }
}

private struct MixTarget: Identifiable {
    let number: Int
    var id: Int { number }
}

/// The name of one mix: saved when the keyboard's Done is tapped or the screen is left.
private struct MixNameField: View {
    let mix: MixDetail
    @ObservedObject var editor: MixEditor
    @State private var text = ""

    var body: some View {
        TextField("Name", text: $text)
            .submitLabel(.done)
            .onAppear { text = mix.name }
            .onChange(of: mix.name) { text = mix.name }
            .onSubmit { Task { await editor.rename(mix.number, to: text) } }
            .onDisappear { Task { await editor.rename(mix.number, to: text) } }
    }
}

/// The library folders, then their subfolders: open a folder, "Add this folder" puts it in the mix.
struct PhoneMixFolderPicker: View {
    @ObservedObject var editor: MixEditor
    let number: Int
    let root: String?
    let folder: String
    let title: String
    let done: () -> Void
    @AppStorage("mixIncludeSubfolders") private var recursive = true
    @State private var roots: [RootInfo] = []
    @State private var subfolders: [SubfolderInfo] = []
    @State private var total = 0

    var body: some View {
        List {
            Section {
                Toggle("Include subfolders", isOn: $recursive)
                if let root {
                    Button {
                        Task {
                            await editor.add(number, source: .folder(root: root, folder: folder, recursive: recursive))
                            done()
                        }
                    } label: {
                        Label("Add this folder (\(total))", systemImage: "plus.circle.fill")
                    }
                }
            }
            .themedRow()
            Section {
                if let root {
                    ForEach(subfolders) { sub in
                        NavigationLink {
                            PhoneMixFolderPicker(editor: editor, number: number, root: root, folder: sub.folder,
                                                 title: sub.name, done: done)
                        } label: {
                            Text("\(sub.name) (\(sub.count))")
                        }
                    }
                } else {
                    ForEach(roots) { entry in
                        NavigationLink {
                            PhoneMixFolderPicker(editor: editor, number: number, root: entry.root, folder: "",
                                                 title: entry.name, done: done)
                        } label: {
                            Label("\(entry.name) (\(entry.count))", systemImage: "externaldrive")
                        }
                    }
                }
            }
            .themedRow()
        }
        .phoneThemed()
        .navigationTitle(title)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .cancellationAction) { Button("Cancel", action: done) }
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
}

/// The playlists that are not in the mix yet.
struct PhoneMixPlaylistPicker: View {
    @ObservedObject var editor: MixEditor
    let number: Int
    let done: () -> Void
    @State private var playlists: [PlaylistInfo] = []
    @State private var loaded = false

    var body: some View {
        let have = Set(editor.mix(number)?.sources.compactMap(\.playlistID) ?? [])
        let free = playlists.filter { !have.contains($0.id) }
        List {
            ForEach(free) { playlist in
                Button {
                    Task {
                        await editor.add(number, source: .playlist(playlist.id))
                        done()
                    }
                } label: {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(playlist.name)
                        Text(playlist.detail).font(.footnote).foregroundStyle(.secondary)
                    }
                }
            }
            if loaded && free.isEmpty {
                Text("No other playlists.").foregroundStyle(.secondary)
            }
        }
        .phoneThemed()
        .navigationTitle("Add a playlist")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .cancellationAction) { Button("Cancel", action: done) }
        }
        .task {
            playlists = await MixesAPI.playlists() ?? []
            loaded = true
        }
    }
}

/// The editor in a sheet, for a Home tile that has nothing in it yet.
struct PhoneMixesSheet: View {
    let done: () -> Void

    var body: some View {
        NavigationStack {
            PhoneMixesView()
                .toolbar {
                    ToolbarItem(placement: .confirmationAction) { Button("Done", action: done) }
                }
        }
    }
}
