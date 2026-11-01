import Foundation
#if os(tvOS)
import TVVLCKit
#else
import MobileVLCKit
#endif

/// Equalizer + Boost + loudness settings (stored in UserDefaults, applied by PlayerEngine). Whenever the sound is
/// processed a limiter runs after the equalizer.
enum SoundSettings {
    static let presetKey = "eqPreset"      // "off" | "custom" | "p:<VLC preset index>"
    static let customKey = "eqCustom"      // "preamp;b0,b1,…,b9" in dB
    static let loudnessKey = "loudnessMode" // "off" | "raise" | "level"
    static let boostKey = "soundPreamp"    // Int dB 0…8, added to any equalizer choice (0.18.32 "Preamp", same key)

    private static var defaults: UserDefaults { .standard }

    /// VLC 3's equalizer (modules/audio_filter/equalizer.c) scales the dry signal by 0.25 (−12 dB)
    /// and expects the preamp to give it back: all of VLC's presets use +12 dB, and "Flat" = +12.
    /// The app shows preamp relative to that, so 0 dB here = same loudness as the equalizer off.
    static let vlcPreampOffset: Float = 12
    static let customFormatKey = "eqCustomFormat"  // "2" = preamp stored relative to the offset

    static var preset: String { defaults.string(forKey: presetKey) ?? "off" }

    static var presets: [VLCAudioEqualizer.Preset] { VLCAudioEqualizer.presets }

    // MARK: Boost, limiter, gain split

    static let boostRange = 0...8

    /// Settings → Sound → Boost (dB), on top of the preset's own gain and the loudness gain.
    static var boost: Int { min(boostRange.upperBound, max(boostRange.lowerBound, defaults.integer(forKey: boostKey))) }

    /// Equalizer Off + Boost 0 + Loudness Off = no audio filters at all. Anything else runs the fixed chain
    /// equalizer → limiter, and every later change only sets values on it (see PlayerEngine.applySound).
    static var processed: Bool { preset != "off" || boost > 0 || loudness != .off }

    static let filterChain = "equalizer:compressor"

    /// "+3 dB", "0 dB", "−2 dB" (half dB shown as "+4.5 dB").
    static func dbLabel(_ value: Float) -> String {
        let rounded = (value * 2).rounded() / 2
        let magnitude = abs(rounded)
        let number = magnitude == magnitude.rounded() ? String(format: "%.0f", magnitude) : String(format: "%.1f", magnitude)
        return rounded > 0 ? "+\(number) dB" : rounded < 0 ? "\u{2212}\(number) dB" : "0 dB"
    }

    /// Limiter = VLC 3's compressor (modules/audio_filter/compressor.c, 10 ms look-ahead) set up as a peak limiter.
    /// VLC's ranges: rms-peak 0…1, attack 1.5…400 ms, release 2…800 ms, threshold −30…0 dB, ratio 1…20,
    /// knee 1…10 dB, makeup gain 0…24 dB (its default is +7, so it must be set here). Threshold and makeup are the
    /// base values; GainPlan moves them with the gain above the equalizer's +8 dB.
    private static let limiterParameters: [(String, String)] = [
        ("rms-peak", "1.0"),    // 1 = peak detection (0 = RMS)
        ("attack", "1.5"),      // ms, VLC's fastest
        ("release", "100"),     // ms
        ("threshold", "-1"),    // dBFS = limiterCeiling
        ("ratio", "20"),
        ("knee", "1"),          // dB, VLC's hardest
        ("makeup-gain", "0"),   // dB
    ]
    /// Output ceiling of the limiter (dBFS).
    static let limiterCeiling: Float = -1

    /// Player options for the limiter (read by the compressor when VLC creates it, inherited from the instance).
    /// Always the same, so they never cause a player rebuild.
    static func limiterOptions() -> [String] {
        limiterParameters.map { "--compressor-\($0.0)=\($0.1)" }
    }

    /// The equalizer's preamp reaches +8 dB at most (raw 20, libvlc's clamp); the limiter's makeup gain carries up
    /// to `maxOverflow` more, with its threshold lowered by the same amount so the output ceiling stays put.
    static let maxEqualizerGain: Float = 8
    static let maxOverflow: Float = 6

    /// How the total gain is split between the equalizer's preamp and the limiter (app scale: 0 = equalizer off).
    struct GainPlan: Equatable {
        var preset: Float
        var boost: Float
        var loudness: Float

        var requested: Float { preset + boost + loudness }
        var total: Float { min(SoundSettings.maxEqualizerGain + SoundSettings.maxOverflow, requested) }
        var equalizer: Float { min(SoundSettings.maxEqualizerGain, total) }
        var makeup: Float { max(0, total - SoundSettings.maxEqualizerGain) }
        var threshold: Float { SoundSettings.limiterCeiling - makeup }
        /// Raw VLC preamp (libvlc clamps to ±20).
        var vlcPreamp: Float { max(-20, SoundSettings.vlcPreampOffset + equalizer) }

        /// "gain: preset +4, boost +2, loudness +3.0 = +9.0 dB (EQ +8.0, limiter makeup +1.0, threshold -2.0)"
        var logText: String {
            func db(_ v: Float) -> String { String(format: v == v.rounded() ? "%+.0f" : "%+.1f", v) }
            var text = String(format: "gain: preset %@, boost %@, loudness %+.1f = %+.1f dB (EQ %+.1f, limiter makeup %+.1f, threshold %.1f)",
                              db(preset), db(boost), loudness, total, equalizer, makeup, threshold)
            if requested > total + 0.01 { text += String(format: ", %+.1f asked, max %+.0f", requested, total) }
            return text
        }
    }

    static func gainPlan(loudnessGain: Float) -> GainPlan {
        GainPlan(preset: curve().0, boost: Float(boost), loudness: loudnessGain)
    }

    /// Footnote under Boost. `loudnessGain`: PlayerEngine.levelGain (the playing track's).
    static func gainNote(loudnessGain: Float) -> String {
        guard processed else { return "Equalizer, Boost and Loudness are off: the sound is played unprocessed." }
        let plan = gainPlan(loudnessGain: loudnessGain)
        let cap = plan.requested > plan.total + 0.25 ? " (the maximum)" : ""
        return "Total now: \(dbLabel(plan.total))\(cap). A limiter keeps the sound from distorting."
    }

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
    /// Largest loudness lift.
    static let maxBoost: Float = 8
    static let maxCut: Float = 12

    /// Gain in dB for this item: what it needs to reach the target (`raise` only lifts, `level` also lowers),
    /// at most +8 dB. It never takes back Boost or the preset's gain and ignores the true peak: the limiter
    /// catches the peaks.
    static func levelGain(for item: MediaItem?) -> Float {
        guard let item, let lufs = item.lufs else { return 0 }
        let mode = loudness
        guard mode != .off else { return 0 }
        let gain = min(maxBoost, max(mode == .level ? -maxCut : 0, loudnessTarget - Float(lufs)))
        let stepped = (gain * 2).rounded() / 2  // half-dB steps: fewer preamp changes between similar tracks
        return stepped == 0 ? 0 : stepped  // no "-0.0" in the log
    }

    /// (preamp in app scale, bands) of the selected curve: preset, custom, or flat for Off / an unknown tag.
    static func curve() -> (Float, [Float]) {
        switch preset {
        case "off": return (0, Array(repeating: 0, count: frequencies.count))
        case "custom": return customValues()
        default: return presetValues(preset) ?? (0, Array(repeating: 0, count: frequencies.count))
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
    /// `migrate()` rewrites the stored selection): an EqualizerAPO headphone curve (31-band GraphicEQ
    /// +3…+7.68 dB peaking 1.6–2.5 kHz), fitted by tools/eq_fit.py to the ISO bands the app's equalizer
    /// really uses (31 Hz … 16 kHz) for APO preamp 0 dB (how the owner listens), 0.5 dB steps: max 0.61 dB /
    /// rms 0.34 dB from APO. Raw preamp 16 (+4): as loud as APO; the limiter keeps loud tracks from clipping.
    static let appPresets: [AppPreset] = [
        AppPreset(id: "momentum4", name: "Momentum 4", vlcPreamp: 16,
                  bands: [-1, -1, -0.5, 0.5, 1, 2, 3.5, 2, -0.5, -1.5]),
        // "Amplify": flat (a pure gain, no filtering) as loud as Momentum 4: its preamp +1 dB, the relation
        // measured in 0.18.23 (Momentum 4's curve K-weighted over music spectra plays ~1 dB above its preamp).
        AppPreset(id: "amplify", name: "Amplify", vlcPreamp: 17,
                  bands: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]),
    ]

    /// Renamed app presets: old stored tag → new tag. Run once at startup, before anything reads the preset.
    static func migrate() {
        let renamed = ["a:momentus4": "a:momentum4"]
        if let old = defaults.string(forKey: presetKey), let new = renamed[old] {
            defaults.set(new, forKey: presetKey)
        }
        defaults.removeObject(forKey: "stereoWiden")  // the stereo widener was removed in 0.18.20
        defaults.removeObject(forKey: "soundLimiter")  // 0.18.32's Limiter switch: the limiter is always on now
        if defaults.object(forKey: boostKey) != nil, defaults.integer(forKey: boostKey) != boost {
            defaults.set(boost, forKey: boostKey)  // 0.18.32's Preamp went down to −6
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

    /// The equalizer for a fresh player (nil = unprocessed). On a running player PlayerEngine sets the same values
    /// as variables instead (`vlc.equalizer = …` there would restart the filters).
    static func makeEqualizer(loudnessGain: Float) -> VLCAudioEqualizer? {
        guard processed else { return nil }
        let eq = VLCAudioEqualizer()
        eq.preAmplification = gainPlan(loudnessGain: loudnessGain).vlcPreamp
        for (band, value) in zip(eq.bands, curve().1) { band.amplification = value }
        return eq
    }

    /// "equalizer-bands" as VLC's equalizer parses it (space-separated dB values).
    static func bandsText(_ bands: [Float]) -> String {
        bands.map { String(format: "%.2f", $0) }.joined(separator: " ")
    }

    /// A change here rebuilds the player (the filter chain changes). Everything else changes on the running player.
    static var configKey: String { processed ? "processed" : "unprocessed" }

    /// e.g. "EQ Momentum 4, boost +2 + loudness".
    static var summary: String {
        var chain: [String] = []
        if preset != "off" { chain.append("EQ \(presetName(preset))") }
        if boost != 0 { chain.append("boost +\(boost)") }
        var parts = chain.isEmpty ? [] : [chain.joined(separator: ", ")]
        if loudness != .off { parts.append(loudness == .raise ? "loudness" : "leveling") }
        guard !parts.isEmpty else { return "Off (unprocessed)" }
        parts[0] = parts[0].prefix(1).uppercased() + String(parts[0].dropFirst())
        return parts.joined(separator: " + ")
    }
}
