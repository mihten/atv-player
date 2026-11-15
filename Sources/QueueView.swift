import SwiftUI

/// Titles for the queue's ids. The queue state carries only ids (plus the current item and the next five), so the
/// list reads the library once: `/api/library?all=1` (hidden duplicates too: a queue may hold one), kept until a queue
/// id is missing (a rescan added it). No-server mode: the device's own library.
@MainActor
final class QueueTitles: ObservableObject {
    static let shared = QueueTitles()

    @Published private(set) var byID: [Int: MediaItem] = [:]
    @Published private(set) var errorText: String?
    private var loading = false
    private var loadedLocal = false

    /// Loads when an id of `queue` is unknown (or the library source changed between server and no-server mode).
    func ensure(_ queue: [Int]) async {
        if Config.localMode {
            byID = LocalLibrary.shared.byID
            loadedLocal = true
            return
        }
        if loadedLocal { byID = [:]; loadedLocal = false }
        guard !loading, queue.contains(where: { byID[$0] == nil }) else { return }
        loading = true
        defer { loading = false }
        do {
            let (data, _) = try await Net.data(from: Config.serverURL.appendingPathComponent("api/library").appending(queryItems: [URLQueryItem(name: "all", value: "1")]))
            let items = try JSONDecoder.server().decode(ItemList.self, from: data).items
            byID = Dictionary(items.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
            errorText = nil
        } catch {
            errorText = error.localizedDescription
        }
    }
}

/// The play queue (owner, 0.19.3): every entry numbered, the playing one marked; a click plays that entry (server
/// `jump`, it starts at once). From the main menu the player opens by itself (the jump is a user start); from the
/// player's quick menu `onJump` closes this screen again, back to the player. Jump only: no remove / clear here.
struct QueueView: View {
    @EnvironmentObject var connection: ServerConnection
    @ObservedObject private var titles = QueueTitles.shared
    @FocusState private var focused: Int?
    /// The playing entry got the focus once (later track changes must not move the focus while browsing).
    @State private var didFocus = false
    /// Set by the quick menu: called after a jump (closes the full-screen queue over the player).
    var onJump: (() -> Void)? = nil

    private var queue: [Int] { connection.state?.queue ?? [] }
    private var current: Int { connection.state?.index ?? -1 }

    var body: some View {
        ScrollViewReader { proxy in
            List {
                if let errorText = titles.errorText {
                    Text(errorText).foregroundStyle(.secondary)
                }
                if queue.isEmpty {
                    Text("The queue is empty. Start something from Playlists, Folders or Music videos.")
                        .foregroundStyle(.secondary)
                }
                ForEach(Array(queue.enumerated()), id: \.offset) { index, id in
                    Button {
                        connection.command("jump", ["index": index])
                        connection.send(["type": "log", "message": "queue: jump to \(index + 1) of \(queue.count) (item \(id))"])
                        onJump?()
                    } label: {
                        row(index, id)
                    }
                    .focused($focused, equals: index)
                    .id(index)
                    .accessibilityIdentifier(index == current ? "queue.current" : "queue.entry")  // UI tests
                }
            }
            .denseList()
            .accessibilityIdentifier("queue.list")
            .onAppear { focusCurrent(proxy) }
            // the state may arrive after the screen (fresh start): focus the playing entry as soon as it is known
            .onChange(of: queue.isEmpty) { focusCurrent(proxy) }
        }
        .withNowPlaying(shown: onJump == nil)  // over the player: no sidebar (the player is right behind)
        .themedScreen()
        .navigationTitle(queue.isEmpty ? "Queue" : "Queue · \(queue.count)")
        .task(id: queue) { await titles.ensure(queue) }
    }

    private func row(_ index: Int, _ id: Int) -> some View {
        let item = titles.byID[id] ?? (index == current ? connection.state?.current : nil)
        let playing = index == current
        return HStack(spacing: 18) {
            Text("\(index + 1)")
                .monospacedDigit()
                .frame(width: 70, alignment: .trailing)
                .opacity(0.55)
            RowLabel(title: item?.title ?? "Item #\(id)",
                     detail: [item?.artist, playing ? "Now playing" : nil].compactMap { $0 }.joined(separator: " · "),
                     systemImage: playing ? "speaker.wave.2.fill" : (item?.isVideo == true ? "film" : "music.note"),
                     item: item)
            Spacer(minLength: 0)
        }
        .fontWeight(playing ? .semibold : .regular)
    }

    /// Start on the playing entry, like the tvOS picker: scroll it into view (List is lazy: an entry far down does not
    /// exist until scrolled to), then focus it; again after 0.35 s whatever tvOS picked meanwhile (as ChoiceList).
    private func focusCurrent(_ proxy: ScrollViewProxy) {
        let target = current
        guard !didFocus, queue.indices.contains(target) else { return }
        didFocus = true
        DispatchQueue.main.async { proxy.scrollTo(target, anchor: .center) }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) {
            proxy.scrollTo(target, anchor: .center)
            focused = target
        }
        // a pushed screen can get tvOS's own first-row focus after that (push animation): once more, only if the focus
        // is still on nothing or on the first row
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) {
            guard focused == nil || (focused == 0 && target != 0) else { return }
            proxy.scrollTo(target, anchor: .center)
            focused = target
        }
    }
}
