import Foundation

/// Optional "atv-audio" (shown as ATV-Server): a separate program (server/sink) plays the sound of the Apple TV or the
/// iPhone (0.19.1), the device shows the video with VLC muted. Settings → Sound → Sound output is the only switch: while
/// it is ATV-Server VLC stays muted, also when the sink or its output is missing (PlayerEngine pauses with a notice then).
/// The server gives the headphones to whichever device pressed Play last (Apple TV, iPhone, TV Player).
enum AtvAudio {
    static let outputKey = "soundOutput"   // "tv" | "atv-audio"
    static let lipSyncKey = "lipSyncMs"    // Int ms, how much earlier the sink plays
    static let tvOutput = "tv"
    static let sinkOutput = "atv-audio"

    static let lipSyncRange = -200...400
    static let lipSyncStep = 20
    /// The TV's picture comes late (its processing), the iPhone's screen does not.
    #if os(tvOS)
    static let defaultLipSync = -100
    #else
    static let defaultLipSync = 0
    #endif

    private static var defaults: UserDefaults { .standard }

    /// Settings → Sound output is atv-audio (stored even while no sink is connected).
    static var selected: Bool { defaults.string(forKey: outputKey) == sinkOutput }

    /// The sink really plays this device's sound: chosen, with the media server (no sink without it) and on the home
    /// network (away from home the iPhone plays its own sound: the headphones are at home). Away = the app uses the
    /// internet address, or the server says the connection comes from outside the home network (0.30.2).
    static var inUse: Bool { selected && !Config.localMode && !isAway }

    static var isAway: Bool { Config.isRemote || Config.serverSaysAway }

    static var lipSyncMs: Int {
        guard defaults.object(forKey: lipSyncKey) != nil else { return defaultLipSync }
        return clampLipSync(defaults.integer(forKey: lipSyncKey))
    }

    /// Into −200…+400, on the 20 ms grid.
    static func clampLipSync(_ ms: Int) -> Int {
        let clamped = min(lipSyncRange.upperBound, max(lipSyncRange.lowerBound, ms))
        return Int((Double(clamped) / Double(lipSyncStep)).rounded()) * lipSyncStep
    }

    static var lipSyncValues: [Int] {
        Array(stride(from: lipSyncRange.lowerBound, through: lipSyncRange.upperBound, by: lipSyncStep))
    }

    /// "+200 ms", "0 ms", "−100 ms".
    static func lipSyncLabel(_ ms: Int) -> String {
        ms > 0 ? "+\(ms) ms" : ms < 0 ? "\u{2212}\(-ms) ms" : "0 ms"
    }

    /// "ATV-Server: not connected" / "ATV-Server: connected · Headphones X · aptX HD" / "… · USB audio · codec unknown".
    static func statusText(_ sink: SinkState?) -> String {
        guard let sink, sink.isConnected else { return "ATV-Server: not connected" }
        guard let output = sink.output else { return "ATV-Server: connected · no output found" }
        let device = outputName(output)
        guard output.ready == true else { return "ATV-Server: connected · \(device) · not available" }
        return "ATV-Server: connected · \(device) · \(codecLabel(output.codec))"
    }

    /// The device name; a USB sound card without a real name is "USB audio".
    static func outputName(_ output: SinkOutput) -> String {
        let name = (output.name ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let lower = name.lowercased()
        switch output.kind {
        case "usb":
            let generic = lower.isEmpty || lower == "usb" || lower.hasPrefix("usb audio") || lower.hasPrefix("usb sound")
            return generic ? "USB audio" : name
        case "bluetooth":
            return name.isEmpty ? "Bluetooth" : name
        default:
            return name.isEmpty ? "Audio output" : name
        }
    }

    /// PipeWire / BlueZ codec ids ("aptx_hd", "ldac", "sbc_xq", "opus_05", …) -> display names.
    static func codecLabel(_ codec: String?) -> String {
        let id = (codec ?? "").trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            .replacingOccurrences(of: "-", with: "_").replacingOccurrences(of: " ", with: "_")
        if id.isEmpty { return "codec unknown" }
        if id.hasPrefix("opus_g") { return "Opus (LE)" }
        if id.hasPrefix("opus") { return "Opus" }
        if id.hasPrefix("faststream") { return "FastStream" }
        if id.hasPrefix("aptx_ll") { return "aptX LL" }
        if id.hasPrefix("aptx_ad") { return "aptX Adaptive" }
        if id.hasPrefix("lc3plus") { return "LC3plus" }
        if id.hasPrefix("lc3") { return "LC3" }
        switch id {
        case "aptx_hd", "aptxhd": return "aptX HD"
        case "aptx": return "aptX"
        case "ldac": return "LDAC"
        case "sbc": return "SBC"
        case "sbc_xq": return "SBC XQ"
        case "aac": return "AAC"
        case "aac_eld": return "AAC-ELD"
        case "mpeg", "mp3": return "MP3"
        case "msbc": return "mSBC (headset)"
        case "cvsd": return "CVSD (headset)"
        default: return id.uppercased()
        }
    }
}

/// Maps this device's monotonic clock to the server's (`time.monotonic()`): ping / pong, offset = t − (sent + received) / 2
/// from the sample with the lowest round trip of the last 8, as NTP does.
@MainActor
final class ServerClock {
    private var sent: [Int: Double] = [:]
    private var samples: [(rtt: Double, offset: Double)] = []
    private var counter = 0

    /// Seconds since boot (does not jump with the wall clock).
    nonisolated static func now() -> Double { ProcessInfo.processInfo.systemUptime }

    /// New socket: the server may have restarted (its monotonic clock too).
    func reset() {
        sent = [:]
        samples = []
    }

    /// Number for the next ping; remembers when it was sent.
    func nextPing() -> Int {
        counter += 1
        sent[counter] = Self.now()
        sent = sent.filter { $0.key > counter - 16 }  // unanswered pings
        return counter
    }

    func pong(n: Int, t: Double) {
        guard let start = sent.removeValue(forKey: n) else { return }
        let received = Self.now()
        samples.append((received - start, t - (start + received) / 2))
        if samples.count > 8 { samples.removeFirst(samples.count - 8) }
    }

    /// Server time minus local time (nil until the first pong).
    var offset: Double? { samples.min { $0.rtt < $1.rtt }?.offset }

    /// Server time of a local `now()` value (nil until the first pong).
    func serverTime(_ local: Double = ServerClock.now()) -> Double? { offset.map { local + $0 } }
}

/// VLC 3 updates `time` only about every 250 ms. Position = local time + intercept (slope fixed at 1), the intercept the
/// median of the samples of the last ~5 s. A sample far off the fit (seek, stall, pause) starts it again.
struct PositionFit {
    private var samples: [(wall: Double, time: Double)] = []
    private static let window: Double = 5
    private static let jump: Double = 0.25

    var count: Int { samples.count }

    mutating func reset() { samples = [] }

    mutating func add(wall: Double, time: Double) {
        if let predicted = position(at: wall), abs(predicted - time) > Self.jump { samples = [] }
        samples.append((wall, time))
        samples.removeAll { $0.wall < wall - Self.window }
    }

    /// Fitted position at local time `wall` (nil without samples).
    func position(at wall: Double) -> Double? {
        guard !samples.isEmpty else { return nil }
        let intercepts = samples.map { $0.time - $0.wall }.sorted()
        let mid = intercepts.count / 2
        let intercept = intercepts.count % 2 == 1 ? intercepts[mid] : (intercepts[mid - 1] + intercepts[mid]) / 2
        return wall + intercept
    }
}
