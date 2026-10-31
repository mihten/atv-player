import Foundation

/// Video keyframe times per item (`GET /api/keyframes/<id>`), fetched when a video loads. The iPhone uses them to cover
/// the picture after its track comes back mid-stream: until the next keyframe the decoder has no reference picture and
/// shows a blocky image, so the cover lifts once playback has passed that keyframe.
@MainActor
final class KeyframeStore {
    static let shared = KeyframeStore()

    private struct Reply: Decodable {
        let id: Int
        let times: [Double]
    }

    private var cache: [Int: [Double]] = [:]
    private var order: [Int] = []
    private var inflight: Set<Int> = []
    private var generation = 0

    func prefetch(_ item: MediaItem?) {
        guard let item, item.isVideo, cache[item.id] == nil, !inflight.contains(item.id) else { return }
        inflight.insert(item.id)
        Task { await fetch(item.id) }
    }

    /// The first keyframe strictly after `seconds`. nil = times not known (yet); `.infinity` = none left in the file.
    func next(after seconds: Double, for id: Int) -> Double? {
        guard let times = cache[id] else { return nil }
        return times.first(where: { $0 > seconds }) ?? .infinity
    }

    /// Forgets everything (server and no-server item ids overlap): called when the library mode changes.
    func reset() {
        generation += 1
        cache = [:]
        order = []
        inflight = []
    }

    private func fetch(_ id: Int) async {
        let gen = generation
        defer { if gen == generation { inflight.remove(id) } }
        let url = Config.serverURL.appendingPathComponent("api/keyframes/\(id)")
        let result = try? await Net.data(from: url)
        guard gen == generation else { return }
        guard let (data, response) = result,
              (response as? HTTPURLResponse)?.statusCode == 200,
              let reply = try? JSONDecoder().decode(Reply.self, from: data) else { return }
        cache[id] = reply.times
        order.append(id)
        if order.count > 40 { cache[order.removeFirst()] = nil }
    }
}
