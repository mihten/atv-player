import Foundation
#if os(tvOS)
import TVVLCKit
#else
import MobileVLCKit
#endif

/// Equalizer + preamp + limiter + loudness settings (stored in UserDefaults, applied by PlayerEngine).
enum SoundSettings {
    static let presetKey = "eqPreset"      // "off" | "custom" | "p:<VLC preset index>"
    static let customKey = "eqCustom"      // "preamp;b0,b1,…,b9" in dB
    static let loudnessKey = "loudnessMode" // "off" | "raise" | "level"
    static let preampKey = "soundPreamp"   // Int dB, added to any equalizer choice (also Off)
    static let limiterKey = "soundLimiter" // Bool, VLC's compressor as a limiter after the equalizer

    private static var defaults: UserDefaults { .standard }

    /// VLC 3's equalizer (modules/audio_filter/equalizer.c) scales the dry signal by 0.25 (−12 dB)
    /// and expects the preamp to give it back: all of VLC's presets use +12 dB, and "Flat" = +12.
    /// The app shows preamp relative to that, so 0 dB here = same loudness as the equalizer off.
    static let vlcPreampOffset: Float = 12
    static let customFormatKey = "eqCustomFormat"  // "2" = preamp stored relative to the offset

    static var preset: String { defaults.string(forKey: presetKey) ?? "off" }

    static var presets: [VLCAudioEqualizer.Preset] { VLCAudioEqualizer.presets }

    // MARK: Preamp and limiter

    static let preampRange = -6...8

    /// Settings → Sound → Preamp (dB). Independent of the preset: the presets keep their own preamp.
    static var preamp: Int { min(preampRange.upperBound, max(preampRange.lowerBound, defaults.integer(forKey: preampKey))) }
    static var limiter: Bool { defaults.bool(forKey: limiterKey) }

    /// "+3 dB", "0 dB", "−2 dB" (half dB shown as "+4.5 dB").
    static func dbLabel(_ value: Float) -> String {
        let rounded = (value * 2).rounded() / 2
        let magnitude = abs(rounded)
        let number = magnitude == magnitude.rounded() ? String(format: "%.0f", magnitude) : String(format: "%.1f", magnitude)
        return rounded > 0 ? "+\(number) dB" : rounded < 0 ? "\u{2212}\(number) dB" : "0 dB"
    }

    /// Limiter = VLC 3's compressor (modules/audio_filter/compressor.c) set up as a peak limiter. Values to be tuned
    /// by ear; VLC's ranges: rms-peak 0…1, attack 1.5…400 ms, release 2…800 ms, threshold −30…0 dB, ratio 1…20,
    /// knee 1…10 dB, makeup gain 0…24 dB (its default is +7, so it must be set to 0 here).
    private static let limiterParameters: [(String, String)] = [
        ("rms-peak", "1.0"),    // 1 = peak detection (0 = RMS)
        ("attack", "1.5"),      // ms, VLC's fastest
        ("release", "100"),     // ms
        ("threshold", "-1"),    // dBFS
        ("ratio", "20"),
        ("knee", "1"),          // dB, VLC's hardest
        ("makeup-gain", "0"),   // dB
    ]

    /// Player options for the limiter (read by the compressor when VLC creates it, inherited from the instance).
    static func limiterOptions() -> [String] {
        guard limiter else { return [] }
        return limiterParameters.map { "--compressor-\($0.0)=\($0.1)" }
    }

    /// Audio filter chain to force on the player when the limiter is on (nil = leave VLC's own). The compressor runs
    /// after the equalizer; `withEqualizer`: the player has one (preset, preamp or loudness gain).
    static func filterChain(withEqualizer: Bool) -> String? {
        guard limiter else { return nil }
        return (withEqualizer ? "equalizer:" : "") + "compressor"
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
    /// The raw VLC preamp is capped at +20 dB (libvlc clamps it), i.e. +8 dB above "equalizer off".
    static let maxBoost: Float = 8
    static let maxCut: Float = 12

    /// Gain in dB for this item: lifts it towards the target, never past its true peak (minus 0.5 dB and
    /// the gain the user already added), so nothing clips because of it. `level` also lowers loud tracks.
    /// The boost only uses what the preset's preamp + Preamp leave below +8. With the limiter on the true peak
    /// is ignored (the limiter catches it).
    static func levelGain(for item: MediaItem?) -> Float {
        guard let item, let lufs = item.lufs else { return 0 }
        let mode = loudness
        guard mode != .off else { return 0 }
        let userGain = max(0, presetPreamp + Float(preamp))
        var gain = loudnessTarget - Float(lufs)
        gain = min(max(0, maxBoost - userGain), max(mode == .level ? -maxCut : 0, gain))
        if gain > 0, !limiter {
            let headroom = item.tpeak.map { -Float($0) - 0.5 } ?? 0
            gain = min(gain, max(0, headroom - userGain))
        }
        return (gain * 2).rounded() / 2  // half-dB steps: fewer preamp changes between similar tracks
    }

    /// Total gain the equalizer's preamp applies now, app scale: preset/custom preamp + Preamp + `extraGain`
    /// (loudness), after VLC's clamp. `requested` is the same before the clamp.
    static func totalGain(extraGain: Float) -> (applied: Float, requested: Float) {
        let requested = presetPreamp + Float(preamp) + extraGain
        let applied = makeEqualizer(extraGain: extraGain).map { $0.preAmplification - vlcPreampOffset } ?? 0
        return (applied, requested)
    }

    /// Footnote under Preamp, e.g. "Total gain now: +4 dB (max +8)". `loudnessGain`: PlayerEngine.levelGain.
    static func gainNote(loudnessGain: Float) -> String {
        let (applied, requested) = totalGain(extraGain: loudnessGain)
        var text = "Total gain now: \(dbLabel(applied))"
        if loudnessGain != 0 { text += " incl. loudness \(dbLabel(loudnessGain))" }
        text += requested > applied + 0.25 ? " (max +8, \(dbLabel(requested)) asked)" : " (max +8)"
        return text
    }

    /// The preamp of the chosen preset / custom curve, in app scale (0 = same loudness as the equalizer off).
    private static var presetPreamp: Float {
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
        // "Amplify": flat (a pure gain, no filtering) as loud as Momentum 4. Momentum 4's real combined
        // response (eq_fit.py model, +1 preamp, peak +5.5 dB near 2 kHz), power-averaged over music spectra
        // with K-weighting (BS.1770 loudness): +1.6…+2.4 dB; power mean of the band gains +2.0; pink noise
        // +2.4…+2.8 (too bright for music). +2.0 dB (VLC 14): mid-range, and whole dB so the custom
        // editor (1 dB steps) keeps it when loaded from this preset.
        AppPreset(id: "amplify", name: "Amplify", vlcPreamp: 14,
                  bands: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]),
    ]

    /// Renamed app presets: old stored tag → new tag. Run once at startup, before anything reads the preset.
    static func migrate() {
        let renamed = ["a:momentus4": "a:momentum4"]
        if let old = defaults.string(forKey: presetKey), let new = renamed[old] {
            defaults.set(new, forKey: presetKey)
        }
        defaults.removeObject(forKey: "stereoWiden")  // the stereo widener was removed in 0.18.20
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

    /// `extraGain` (dB) is the loudness leveling gain; the Preamp setting adds to it. With the equalizer off and
    /// no gain there is no equalizer at all (unprocessed); with gain it is a flat one (a pure gain, see above).
    static func makeEqualizer(extraGain: Float = 0) -> VLCAudioEqualizer? {
        let gain = extraGain + Float(preamp)
        let eq: VLCAudioEqualizer
        switch preset {
        case "off":
            guard gain != 0 else { return nil }
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
            eq.preAmplification = pre + vlcPreampOffset  // clamped once below, after Preamp + loudness
            for (band, value) in zip(eq.bands, values) { band.amplification = value }
        default:
            let index = UInt32(preset.dropFirst(2)) ?? 0
            guard let p = presets.first(where: { $0.index == index }) else { return nil }
            eq = VLCAudioEqualizer(preset: p)
        }
        // libvlc clamps the raw preamp to ±20 dB (+8 above "equalizer off")
        eq.preAmplification = min(20, max(-20, eq.preAmplification + gain))
        return eq
    }

    /// Changes in this key mean the player must be rebuilt. The Preamp is not in it: it changes on the running player.
    static var configKey: String {
        "\(preset)|\(preset == "custom" ? defaults.string(forKey: customKey) ?? "" : "")|\(loudness.rawValue)|\(limiter)"
    }

    /// e.g. "EQ Momentum 4, preamp +3, limiter + loudness".
    static var summary: String {
        var chain: [String] = []
        if preset != "off" { chain.append("EQ \(presetName(preset))") }
        if preamp != 0 { chain.append("preamp \(preamp > 0 ? "+" : "\u{2212}")\(abs(preamp))") }
        if limiter { chain.append("limiter") }
        var parts = chain.isEmpty ? [] : [chain.joined(separator: ", ")]
        if loudness != .off { parts.append(loudness == .raise ? "loudness" : "leveling") }
        guard !parts.isEmpty else { return "Off (unprocessed)" }
        parts[0] = parts[0].prefix(1).uppercased() + String(parts[0].dropFirst())
        return parts.joined(separator: " + ")
    }
}
