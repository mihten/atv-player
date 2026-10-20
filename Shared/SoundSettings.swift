import Foundation
#if os(tvOS)
import TVVLCKit
#else
import MobileVLCKit
#endif

/// Equalizer + stereo widener settings (stored in UserDefaults, applied by PlayerEngine).
enum SoundSettings {
    static let presetKey = "eqPreset"      // "off" | "custom" | "p:<VLC preset index>"
    static let customKey = "eqCustom"      // "preamp;b0,b1,…,b9" in dB
    static let widenKey = "stereoWiden"    // 0 off, 1 light, 2 normal, 3 strong
    static let loudnessKey = "loudnessMode" // "off" | "raise" | "level"

    private static var defaults: UserDefaults { .standard }

    /// VLC 3's equalizer (modules/audio_filter/equalizer.c) scales the dry signal by 0.25 (−12 dB)
    /// and expects the preamp to give it back: all of VLC's presets use +12 dB, and "Flat" = +12.
    /// The app shows preamp relative to that, so 0 dB here = same loudness as the equalizer off.
    static let vlcPreampOffset: Float = 12
    static let customFormatKey = "eqCustomFormat"  // "2" = preamp stored relative to the offset

    static var preset: String { defaults.string(forKey: presetKey) ?? "off" }
    static var widen: Int { defaults.integer(forKey: widenKey) }

    static var presets: [VLCAudioEqualizer.Preset] { VLCAudioEqualizer.presets }

    // MARK: Loudness leveling

    /// Per-track gain from the server's loudness measurement. Applied through the equalizer's preamp:
    /// VLC 3's equalizer outputs preamp × (0.25 × input + Σ band terms), and a band at 0 dB adds nothing,
    /// so a flat equalizer is a pure gain of (preamp − 12) dB; no filtering happens (equalizer.c).
    enum Loudness: String, CaseIterable {
        case off, raise, level

        var name: String {
            switch self {
            case .off: "Off"
            case .raise: "Raise quiet tracks"
            case .level: "Level all tracks"
            }
        }
    }

    static var loudness: Loudness { Loudness(rawValue: defaults.string(forKey: loudnessKey) ?? "") ?? .raise }

    /// What normal music is mastered to (streaming services use −14 LUFS).
    static let loudnessTarget: Float = -14
    /// The raw VLC preamp is capped at +20 dB (libvlc clamps it), i.e. +8 dB above "equalizer off".
    static let maxBoost: Float = 8
    static let maxCut: Float = 12

    /// Gain in dB for this item: lifts it towards the target, never past its true peak (minus 0.5 dB and
    /// the preamp the user already added), so nothing clips because of it. `level` also lowers loud tracks.
    static func levelGain(for item: MediaItem?) -> Float {
        guard let item, let lufs = item.lufs else { return 0 }
        let mode = loudness
        guard mode != .off else { return 0 }
        var gain = loudnessTarget - Float(lufs)
        gain = min(maxBoost, max(mode == .level ? -maxCut : 0, gain))
        if gain > 0 {
            let headroom = item.tpeak.map { -Float($0) - 0.5 } ?? 0
            gain = min(gain, max(0, headroom - max(0, userPreamp)))
        }
        return (gain * 2).rounded() / 2  // half-dB steps: fewer preamp changes between similar tracks
    }

    /// The preamp the user chose, in app scale (0 = same loudness as the equalizer off).
    private static var userPreamp: Float {
        switch preset {
        case "off": return 0
        case "custom": return customValues().0
        default: return presetValues(preset)?.0 ?? 0
        }
    }

    /// App presets (tag "a:<id>"): raw VLC preamp + 10 band gains (60 Hz … 16 kHz).
    struct AppPreset {
        let id: String
        let name: String
        let vlcPreamp: Float
        let bands: [Float]
    }

    /// "Momentum 4" (Sennheiser headphones; was misnamed "Momentus 4", id "momentus4", until v0.9.4 —
    /// `migrate()` rewrites the stored selection): an EqualizerAPO headphone curve (Preamp −1 dB,
    /// 31-band GraphicEQ +3…+7.68 dB peaking 1.6–2.5 kHz), fitted by tools/eq_fit.py to the ISO bands
    /// the app's equalizer really uses (31 Hz … 16 kHz), rounded to 1 dB: max 0.8 dB / rms 0.26 dB
    /// from APO. v0.9.0–0.9.2 had a fit for VLC's 60 Hz … 16 kHz table (too hot at 0.5–1 kHz).
    /// Preamp: the fit wants +3 (app scale) but that clipped on loud tracks; +1 is clean by ear
    /// (2026-10-18), i.e. 2 dB quieter than APO overall, same curve shape.
    static let appPresets: [AppPreset] = [
        AppPreset(id: "momentum4", name: "Momentum 4", vlcPreamp: 13,
                  bands: [-1, -1, 0, 1, 1, 2, 4, 2, 0, -1]),
    ]

    /// Renamed app presets: old stored tag → new tag. Run once at startup, before anything reads the preset.
    static func migrate() {
        let renamed = ["a:momentus4": "a:momentum4"]
        if let old = defaults.string(forKey: presetKey), let new = renamed[old] {
            defaults.set(new, forKey: presetKey)
        }
    }

    static func appPreset(_ value: String) -> AppPreset? {
        guard value.hasPrefix("a:") else { return nil }
        let id = String(value.dropFirst(2))
        return appPresets.first { $0.id == id }
    }

    /// Band centre frequencies (Hz) of VLC's 10-band equalizer.
    static var frequencies: [Float] { VLCAudioEqualizer().bands.map(\.frequency) }

    static func presetName(_ value: String) -> String {
        switch value {
        case "off": return "Off"
        case "custom": return "Custom"
        case _ where value.hasPrefix("a:"): return appPreset(value)?.name ?? "Off"
        default:
            let index = UInt32(value.dropFirst(2)) ?? 0
            return presets.first { $0.index == index }?.name ?? "Off"
        }
    }

    /// (preamp, bands) of the stored custom curve; flat if nothing stored.
    static func customValues() -> (Float, [Float]) {
        let count = frequencies.count
        guard let raw = defaults.string(forKey: customKey) else { return (0, Array(repeating: 0, count: count)) }
        let parts = raw.split(separator: ";", maxSplits: 1)
        var pre = Float(parts.first ?? "0") ?? 0
        if defaults.string(forKey: customFormatKey) != "2" {
            pre -= vlcPreampOffset  // curve saved by v0.8.0 (raw VLC preamp): convert once
            pre = max(-12, min(12, pre))
        }
        var bands = (parts.count > 1 ? parts[1].split(separator: ",").compactMap { Float($0) } : [])
        bands += Array(repeating: 0, count: max(0, count - bands.count))
        return (pre, Array(bands.prefix(count)))
    }

    static func saveCustom(preamp: Float, bands: [Float]) {
        let text = "\(preamp);" + bands.map { String(format: "%.0f", $0) }.joined(separator: ",")
        defaults.set(text, forKey: customKey)
        defaults.set("2", forKey: customFormatKey)
    }

    /// Curve of a VLC preset, used to start the custom editor from the current sound.
    static func presetValues(_ value: String) -> (Float, [Float])? {
        if let app = appPreset(value) {
            return (app.vlcPreamp - vlcPreampOffset, app.bands)
        }
        guard value.hasPrefix("p:"), let index = UInt32(value.dropFirst(2)),
              let preset = presets.first(where: { $0.index == index }) else { return nil }
        let eq = VLCAudioEqualizer(preset: preset)
        return (eq.preAmplification - vlcPreampOffset, eq.bands.map(\.amplification))
    }

    /// `extraGain` (dB) is the loudness leveling gain. With the equalizer off and no extra gain there is
    /// no equalizer at all (unprocessed); with extra gain it is a flat one (a pure gain, see above).
    static func makeEqualizer(extraGain: Float = 0) -> VLCAudioEqualizer? {
        let eq: VLCAudioEqualizer
        switch preset {
        case "off":
            guard extraGain != 0 else { return nil }
            eq = VLCAudioEqualizer()
            eq.preAmplification = vlcPreampOffset
        case _ where preset.hasPrefix("a:"):
            guard let app = appPreset(preset) else { return nil }
            eq = VLCAudioEqualizer()
            eq.preAmplification = app.vlcPreamp
            for (band, value) in zip(eq.bands, app.bands) { band.amplification = value }
        case "custom":
            let (pre, values) = customValues()
            eq = VLCAudioEqualizer()
            eq.preAmplification = min(20, pre + vlcPreampOffset)
            for (band, value) in zip(eq.bands, values) { band.amplification = value }
        default:
            let index = UInt32(preset.dropFirst(2)) ?? 0
            guard let p = presets.first(where: { $0.index == index }) else { return nil }
            eq = VLCAudioEqualizer(preset: p)
        }
        eq.preAmplification = min(20, max(-20, eq.preAmplification + extraGain))
        return eq
    }

    /// Audio filter chain to force on the player when the widener is on (nil = leave VLC's own).
    /// `withEqualizer`: the player has an equalizer (preset or loudness gain), which must stay in the chain.
    static func filterChain(withEqualizer: Bool) -> String? {
        guard widen > 0 else { return nil }
        return (withEqualizer ? "equalizer:" : "") + "stereo_widen"
    }

    /// stereo_widen parameters per strength (VLC defaults = Normal).
    static func widenerOptions() -> [String] {
        let params: (Int, Double, Double, Double)
        switch widen {
        case 1: params = (12, 0.2, 0.2, 0.9)
        case 3: params = (25, 0.5, 0.4, 0.7)
        default: params = (20, 0.3, 0.3, 0.8)
        }
        guard widen > 0 else { return [] }
        return [
            "--stereowiden-delay=\(params.0)",
            "--stereowiden-feedback=\(params.1)",
            "--stereowiden-crossfeed=\(params.2)",
            "--stereowiden-dry-mix=\(params.3)",
        ]
    }

    /// Changes in this key mean the player must be rebuilt.
    static var configKey: String {
        "\(preset)|\(preset == "custom" ? defaults.string(forKey: customKey) ?? "" : "")|\(widen)|\(loudness.rawValue)"
    }

    static var summary: String {
        var parts: [String] = []
        if preset != "off" { parts.append("EQ \(presetName(preset))") }
        if widen > 0 { parts.append("widener " + ["", "light", "normal", "strong"][min(max(widen, 0), 3)]) }
        if loudness != .off { parts.append(loudness == .raise ? "loudness" : "leveling") }
        guard !parts.isEmpty else { return "Off (unprocessed)" }
        parts[0] = parts[0].prefix(1).uppercased() + String(parts[0].dropFirst())
        return parts.joined(separator: " + ")
    }
}
