import SwiftUI

private func byteText(_ bytes: Int64) -> String {
    ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
}

// MARK: - Media server mode: the "Download for offline" switch

/// Playlist, Loved and Most played screens (Media server mode): the switch, the size before it starts, then the state.
struct OfflineDownloadSection: View {
    let collection: String
    let playlist: Int?
    @ObservedObject private var store = OfflineStore.shared
    @State private var plan: OfflinePlanInfo?
    @State private var asking = false
    @State private var checking = false
    @State private var errorText: String?

    private var key: String { OfflineList.key(collection: collection, playlist: playlist) }

    var body: some View {
        Section {
            Toggle(isOn: Binding(get: { store.isOn(key) || checking }, set: { on in
                if on { check() } else { store.disable(key) }
            })) {
                Label("Download for offline", systemImage: "arrow.down.circle")
            }
            .disabled(checking)
            if checking {
                HStack(spacing: 8) {
                    ProgressView()
                    Text("Checking the size…").font(.footnote).foregroundStyle(.secondary)
                }
            } else if let list = store.list(key) {
                let status = store.status(list)
                HStack {
                    Text(status.text)
                    Spacer()
                    Text(byteText(status.bytes))
                }
                .font(.footnote)
                .foregroundStyle(.secondary)
                if status.failed > 0 && !status.paused {
                    Button("Retry") { store.retry(key) }
                }
            }
            if let errorText {
                Text(errorText).font(.footnote).foregroundStyle(.red)
            }
        } footer: {
            Text("Audio only, best version. Plays in Settings → Server → Library → Offline.")
        }
        .themedRow()
        .alert(plan?.fits == false ? "Not enough space" : "Download for offline?", isPresented: $asking,
               presenting: plan) { plan in
            if plan.fits {
                Button("Download") { store.enable(plan) }
                Button("Cancel", role: .cancel) {}
            } else {
                Button("OK", role: .cancel) {}
            }
        } message: { plan in
            Text(message(plan))
        }
    }

    private func check() {
        checking = true
        errorText = nil
        Task {
            defer { checking = false }
            do {
                plan = try await store.plan(collection: collection, playlist: playlist)
                asking = true
            } catch {
                errorText = error.localizedDescription
            }
        }
    }

    private func message(_ plan: OfflinePlanInfo) -> String {
        let size = (plan.estimated ? "about " : "") + byteText(plan.bytes)
        let items = plan.files == 1 ? "1 item" : "\(plan.files) items"
        if !plan.fits {
            return "\(items), \(size). That needs \(byteText(plan.bytes + OfflinePlan.reserve)) free (1 GB to spare); "
                + "this iPhone has \(byteText(plan.free))."
        }
        return "\(items), \(size) to download. Free on this iPhone: \(byteText(plan.free))."
            + (store.onlyWiFi ? " Downloads run on Wi-Fi only." : "")
    }
}

// MARK: - Offline mode: Library and Playlists tabs

/// The downloaded lists (Loved, Most played, playlists A–Z) and every download.
struct OfflineListsRows: View {
    @ObservedObject private var store = OfflineStore.shared

    var body: some View {
        if store.lists.isEmpty {
            Section {
                Text("Nothing downloaded yet. In Media server mode, turn on Download for offline in a playlist, Loved or Most played.")
                    .foregroundStyle(.secondary)
            }
            .themedRow()
        } else {
            Section("Downloaded lists") {
                ForEach(store.lists) { list in
                    NavigationLink {
                        OfflineListView(key: list.key, title: list.displayName)
                    } label: {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(list.displayName)
                            Text(PlayLabels.count(store.items(of: list.key).count)).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
            }
            .themedRow()
        }
        Section {
            NavigationLink {
                OfflineListView(key: nil, title: "Downloaded")
            } label: {
                LabeledContent {
                    Text(PlayLabels.count(store.items.count))
                } label: {
                    Label("Downloaded", systemImage: "arrow.down.circle")
                }
            }
        }
        .themedRow()
    }
}

/// Library tab in Offline mode: the lists, or search results over the downloads.
struct OfflineLibraryList: View {
    let query: String

    var body: some View {
        List {
            if query.trimmingCharacters(in: .whitespaces).isEmpty {
                OfflineListsRows()
            } else {
                SearchResults(query: query).themedRow()
            }
        }
        .phoneThemed()
    }
}

/// Playlists tab in Offline mode.
struct OfflineListsScreen: View {
    var body: some View {
        List { OfflineListsRows() }
            .phoneThemed()
            .navigationTitle("Playlists")
            .withMiniPlayer()
    }
}

/// One downloaded list (key nil = every download): Play (shuffled, repeat all), then its items.
struct OfflineListView: View {
    @EnvironmentObject var connection: ServerConnection
    @ObservedObject private var store = OfflineStore.shared
    let key: String?
    let title: String

    private var items: [MediaItem] { key.map { store.items(of: $0) } ?? store.items }

    var body: some View {
        let items = self.items
        List {
            Section {
                if items.isEmpty {
                    Text("Nothing downloaded here yet.").foregroundStyle(.secondary)
                } else {
                    Button { play() } label: { PlayRow(title: PlayLabels.play, count: items.count) }
                }
            }
            .themedRow()
            if !items.isEmpty {
                Section {
                    // By position: a playlist may hold the same item twice.
                    ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                        Button { play(from: item.id) } label: { ItemRow(item: item) }
                            .itemMenu(item)
                    }
                }
                .themedRow()
            }
        }
        .phoneThemed()
        .navigationTitle(title)
        .withMiniPlayer()
    }

    /// startPlayback through the local queue: shuffled, repeat all; a picked item plays first.
    private func play(from id: Int? = nil) {
        var args: [String: Any] = key.map { ["collection": "offline", "list": $0] } ?? ["collection": "offline_all"]
        if let id { args["start_id"] = id }
        connection.startPlayback(args)
    }
}

// MARK: - Settings → Downloads

struct PhoneDownloadsSettingsView: View {
    @ObservedObject private var store = OfflineStore.shared
    @State private var used: Int64 = 0
    @State private var free: Int64 = 0
    @State private var confirmAll = false

    var body: some View {
        Form {
            Section {
                LabeledContent("Space used", value: byteText(used))
                LabeledContent("Free on this iPhone", value: byteText(free))
                Toggle("Only on Wi-Fi", isOn: $store.onlyWiFi)
            } footer: {
                Text("Audio only, the best version of each song (FLAC stays FLAC). Downloaded lists stay current while "
                     + "the app is connected to the media server.")
            }
            .themedRow()

            Section("Lists") {
                if store.lists.isEmpty {
                    Text("Nothing downloaded. Turn on Download for offline in a playlist, Loved or Most played (Media server mode).")
                        .foregroundStyle(.secondary)
                }
                ForEach(store.lists) { list in
                    let status = store.status(list)
                    NavigationLink {
                        OfflineFilesView(key: list.key)
                    } label: {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(list.displayName)
                            Text("\(PlayLabels.count(status.total)) · \(byteText(status.bytes)) · \(status.text)")
                                .font(.caption)
                                .foregroundStyle(status.failed > 0 ? .red : .secondary)
                        }
                    }
                    .swipeActions {
                        Button(role: .destructive) { store.disable(list.key) } label: { Label("Remove", systemImage: "trash") }
                    }
                }
            }
            .themedRow()

            if !store.lists.isEmpty {
                Section {
                    Button("Remove all downloads", role: .destructive) { confirmAll = true }
                }
                .themedRow()
            }
        }
        .phoneThemed()
        .navigationTitle("Downloads")
        .withMiniPlayer()
        .confirmationDialog("Remove all downloads?", isPresented: $confirmAll, titleVisibility: .visible) {
            Button("Remove all downloads", role: .destructive) { store.removeAll() }
            Button("Cancel", role: .cancel) {}
        }
        .task(id: store.version) {
            used = store.usedBytes()
            free = OfflineStore.freeBytes()
        }
    }
}

/// One downloaded list's files: format (e.g. FLAC 24/48) and size, or the download state; Retry, Remove.
struct OfflineFilesView: View {
    @ObservedObject private var store = OfflineStore.shared
    @Environment(\.dismiss) private var dismiss
    let key: String

    var body: some View {
        Form {
            if let list = store.list(key) {
                let status = store.status(list)
                Section {
                    LabeledContent("State", value: status.text)
                    LabeledContent("Size", value: byteText(status.bytes))
                    if status.failed > 0 && !status.paused {
                        Button("Retry") { store.retry(key) }
                    }
                    Button("Remove", role: .destructive) {
                        store.disable(key)
                        dismiss()
                    }
                }
                .themedRow()
                Section("Songs") {
                    ForEach(Array(list.entries.enumerated()), id: \.offset) { _, entry in
                        if let file = store.file(entry.source) {
                            LabeledContent {
                                Text(detail(file)).font(.caption).foregroundStyle(file.failures >= 3 ? .red : .secondary)
                            } label: {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(file.item.title).lineLimit(1)
                                    if let artist = file.item.artist {
                                        Text(artist).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                                    }
                                }
                            }
                        }
                    }
                }
                .themedRow()
            }
        }
        .phoneThemed()
        .navigationTitle(store.list(key)?.displayName ?? "Downloads")
    }

    private func detail(_ file: OfflineFile) -> String {
        if file.isCurrent {
            return [OfflinePlan.formatLabel(file.format), byteText(file.size)].compactMap { $0 }.joined(separator: " · ")
        }
        if file.failures >= 3 { return "Failed" }
        return store.downloading.contains(file.id) ? "Downloading" : "Waiting"
    }
}
