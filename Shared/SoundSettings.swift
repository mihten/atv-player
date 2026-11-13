import Foundation
#if os(tvOS)
import TVVLCKit
#else
import MobileVLCKit
#endif

/// Equalizer + Boost + loudness settings (stored in UserDefaults, applied by PlayerEngine). Whenever the sound is
/// processed a limiter runs after the equalizer.
enum SoundSettings {
    static let presetKey = "eqPreset"      // "off" | "custom" | "a:<app preset id>" (0.18.45: VLC's "p:<index>" are gone)
    static let lastKey = "eqLast"          // last preset in use other than Off: the output rule turns it back on
    static let customKey = "eqCustom"      // "preamp;b0,b1,…,b9" in dB
    static let loudnessKey = "loudnessMode" // "off" | "raise" (0.18.33 "level" becomes "raise")
    static let boostKey = "soundPreamp"    // Int dB 0…8, added to any equalizer choice (0.18.32 "Preamp", same key)

    private static var defaults: UserDefaults { .standard }

    /// VLC 3's equalizer (modules/audio_filter/equalizer.c) scales the dry signal by 0.25 (−12 dB)
    /// and expects the preamp to give it back: all of VLC's presets use +12 dB, and "Flat" = +12.
    /// The app shows preamp relative to that, so 0 dB here = same loudness as the equalizer off.
    static let vlcPreampOffset: Float = 12
    static let customFormatKey = "eqCustomFormat"  // "2" = preamp stored relative to the offset

    static var preset: String { defaults.string(forKey: presetKey) ?? "off" }

    // MARK: Boost, limiter, gain split

    static let boostRange = 0...8

    /// Settings → Sound → Boost (dB), on top of the preset's own gain and the loudness gain.
    static var boost: Int { min(boostRange.upperBound, max(boostRange.lowerBound, defaults.integer(forKey: boostKey))) }

    /// Equalizer Off + Boost 0 + Loudness Off = no audio filters at all. Anything else runs the fixed chain
    /// equalizer → limiter, and every later change only sets values on it (see PlayerEngine.applySound).
    static var processed: Bool { (preset != "off" && known(preset)) || boost > 0 || loudness != .off }

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

    /// `forSink`: the curve atv-audio plays (its own filters, see AppPreset.sinkBands), else VLC's.
    static func gainPlan(loudnessGain: Float, forSink: Bool = false) -> GainPlan {
        GainPlan(preset: curve(forSink: forSink).0, boost: Float(boost), loudness: loudnessGain)
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
    /// Settings → Sound → Loudness On / Off. On ("raise") lifts quiet tracks, it never lowers anything.
    enum Loudness: String {
        case off, raise
    }

    static var loudness: Loudness { Loudness(rawValue: defaults.string(forKey: loudnessKey) ?? "") ?? .raise }

    /// Stored value for the On / Off switch.
    static func loudnessValue(on: Bool) -> String { (on ? Loudness.raise : .off).rawValue }

    static let loudnessNote = "Raises quiet recordings. Never turns anything down."

    /// What normal music is mastered to (streaming services use −14 LUFS).
    static let loudnessTarget: Float = -14
    /// Largest loudness lift.
    static let maxBoost: Float = 8

    /// Gain in dB for this item: what it needs to reach the target, only up, at most +8 dB. It never takes back
    /// Boost or the preset's gain and ignores the true peak: the limiter catches the peaks.
    static func levelGain(for item: MediaItem?) -> Float {
        guard let item, let lufs = item.lufs else { return 0 }
        guard loudness != .off else { return 0 }
        let gain = min(maxBoost, max(0, loudnessTarget - Float(lufs)))
        let stepped = (gain * 2).rounded() / 2  // half-dB steps: fewer preamp changes between similar tracks
        return stepped == 0 ? 0 : stepped  // no "-0.0" in the log
    }

    /// (preamp in app scale, bands) of the selected curve: preset, custom, or flat for Off / an unknown tag.
    /// `forSink`: an app preset's atv-audio curve (the custom curve is the same for both).
    static func curve(forSink: Bool = false) -> (Float, [Float]) {
        switch preset {
        case "off": return (0, Array(repeating: 0, count: frequencies.count))
        case "custom": return customValues()
        default:
            if forSink, let app = appPreset(preset) { return (app.sinkPreamp, app.sinkBands) }
            return presetValues(preset) ?? (0, Array(repeating: 0, count: frequencies.count))
        }
    }

    /// App presets (tag "a:<id>"), the same list in every player (owner, 2026-11-13; server/box_sound.py):
    /// `vlcPreamp` (raw, 12 = 0 dB) + `bands` drive VLC's equalizer here; `sinkPreamp` (dB, 0 = equalizer off) +
    /// `sinkBands` are the server's values, sent when atv-audio plays the sound (its octave-wide peaking filters
    /// respond differently from VLC's band-passes, so the same numbers would not sound the same).
    struct AppPreset {
        let id: String
        let name: String
        let vlcPreamp: Float
        let bands: [Float]
        let sinkPreamp: Float
        let sinkBands: [Float]
    }

    /// Momentum 4 (2026-11-13): the parametric EqualizerAPO correction the owner uses on his laptop (AutoEQ-style,
    /// APO preamp -1 dB), the curve the web player / TV Player use since server 0.28.1. VLC fit (tools/eq_fit.py's
    /// equalizer.c model against that curve, 0.5 dB steps): max 1.4 dB / rms 0.4 dB from 25 Hz to 18 kHz. The four
    /// tone presets: VLC fits of what the server's filters play for them (max 0.7 dB). The VLC presets and Amplify
    /// were deleted (owner): a stored one becomes Off (`migrate`).
    static let appPresets: [AppPreset] = [
        AppPreset(id: "momentum4", name: "Momentum 4", vlcPreamp: 5.5,
                  bands: [-3.5, -1, 2.5, 2.5, 5, 1, 10, 3.5, 5.5, 3.5],
                  sinkPreamp: -3, sinkBands: [-7, -2, 0, 1, 2, 0, 7, 1, 3, 0]),
        AppPreset(id: "bass", name: "Bass boost", vlcPreamp: 10.5,
                  bands: [5, 6, 4.5, 2, 1, 0.5, 0.5, 0.5, 0.5, 0.5],
                  sinkPreamp: -1, sinkBands: [5, 5, 4, 2, 1, 0, 0, 0, 0, 0]),
        AppPreset(id: "vocal", name: "Vocal", vlcPreamp: 12,
                  bands: [0.5, 0, 0, 0, 1, 2, 3.5, 2, 1, 0],
                  sinkPreamp: 0, sinkBands: [0, 0, 0, 0, 1, 2, 3, 2, 1, 0]),
        AppPreset(id: "bright", name: "Bright", vlcPreamp: 12,
                  bands: [0, 0, 0, 0, 0, 1, 2, 3, 5, 4],
                  sinkPreamp: 0, sinkBands: [0, 0, 0, 0, 0, 1, 2, 3, 4, 4]),
        AppPreset(id: "vshape", name: "V-shape", vlcPreamp: 10.5,
                  bands: [4.5, 5, 3, 0, 0.5, 0.5, 0, 3, 5.5, 4.5],
                  sinkPreamp: -1, sinkBands: [4, 4, 3, 0, 0, 0, 0, 3, 4, 4]),
    ]

    static let defaultOn = "a:momentum4"

    /// A stored preset tag that no longer exists (VLC's "p:<index>", "a:amplify", typos) reads as Off.
    static func known(_ value: String) -> Bool {
        value == "off" || value == "custom" || appPreset(value) != nil
    }

    /// The preset the output rule turns on: the last one used other than Off, else Momentum 4.
    static var lastOn: String {
        let v = defaults.string(forKey: lastKey) ?? defaultOn
        return v != "off" && known(v) ? v : defaultOn
    }

    /// Remember the preset in use (not Off) as the one to turn back on.
    static func noteInUse() {
        if preset != "off", known(preset), defaults.string(forKey: lastKey) != preset { defaults.set(preset, forKey: lastKey) }
    }

    /// Owner rule (2026-11-13) for an output CHANGED on purpose: the TV's own sound (HDMI) = Equalizer Off + Boost 0,
    /// headphones (atv-audio) = Equalizer on with `lastOn`; Loudness stays. Returns what changed, for the log.
    @discardableResult
    static func applyOutputRule(headphones: Bool) -> String? {
        if headphones {
            guard preset == "off" else { return nil }
            defaults.set(lastOn, forKey: presetKey)
            return "equalizer \(presetName(lastOn))"
        }
        guard preset != "off" || boost != 0 else { return nil }
        noteInUse()
        defaults.set("off", forKey: presetKey)
        defaults.set(0, forKey: boostKey)
        return "equalizer off, boost 0"
    }

    /// Renamed app presets: old stored tag → new tag. Run once at startup, before anything reads the preset.
    static func migrate() {
        let renamed = ["a:momentus4": "a:momentum4"]
        if let old = defaults.string(forKey: presetKey), let new = renamed[old] {
            defaults.set(new, forKey: presetKey)
        }
        if let old = defaults.string(forKey: presetKey), !known(old) {
            defaults.set("off", forKey: presetKey)  // 0.18.45: VLC presets and Amplify deleted
        }
        defaults.removeObject(forKey: "stereoWiden")  // the stereo widener was removed in 0.18.20
        defaults.removeObject(forKey: "soundLimiter")  // 0.18.32's Limiter switch: the limiter is always on now
        if defaults.string(forKey: loudnessKey) == "level" {
            defaults.set(Loudness.raise.rawValue, forKey: loudnessKey)  // 0.18.33's "Level all tracks" was removed
        }
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

    /// Every equalizer choice in display order (Settings → Sound and the quick panel's list).
    static var presetChoices: [(label: String, value: String)] {
        [("Off", "off")] + appPresets.map { ($0.name, "a:\($0.id)") } + [("Custom", "custom")]
    }

    static func presetName(_ value: String) -> String {
        switch value {
        case "off": return "Off"
        case "custom": return "Custom"
        default: return appPreset(value)?.name ?? "Off"
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

    /// VLC curve of an app preset (also starts the custom editor from the current sound).
    static func presetValues(_ value: String) -> (Float, [Float])? {
        guard let app = appPreset(value) else { return nil }
        return (app.vlcPreamp - vlcPreampOffset, app.bands)
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

    /// e.g. "EQ Momentum 4, boost +2 + loudness on".
    static var summary: String {
        var chain: [String] = []
        if preset != "off" { chain.append("EQ \(presetName(preset))") }
        if boost != 0 { chain.append("boost +\(boost)") }
        var parts = chain.isEmpty ? [] : [chain.joined(separator: ", ")]
        if loudness != .off { parts.append("loudness on") }
        guard !parts.isEmpty else { return "Off (unprocessed)" }
        parts[0] = parts[0].prefix(1).uppercased() + String(parts[0].dropFirst())
        return parts.joined(separator: " + ")
    }
}
