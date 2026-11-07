import Foundation
#if os(iOS)
import AVFoundation
#endif

/// Sound profiles: Equalizer (preset + custom curve), Boost and Loudness kept per output. Volume is not part of them.
///   iPhone:   one profile per Bluetooth device (key = its port UID, name = the device's name) and one for CarPlay
///             (wins over the car's Bluetooth name); speaker, wired headphones and AirPlay share the iPhone profile.
///   Apple TV: "Apple TV" (Sound output = Apple TV) and "atv-audio" (Sound output = atv-audio).
/// The SoundSettings keys always hold the ACTIVE profile's values: Settings, the quick panel and PlayerEngine read and
/// write only those. A switch stores them under the old profile and loads the new one's; a profile used for the first
/// time starts as a copy of the default profile.
enum SoundProfiles {
    static let storeKey = "soundProfiles"         // [profile key: [setting key: value, "name": String]], inactive ones
    static let activeKey = "soundProfileActive"   // key of the profile whose values are in the SoundSettings keys
    static let activeNameKey = "soundProfileName" // its display name

    static let defaultKey = "default"
    static let carPlayKey = "carplay"
    static let sinkKey = "atv-audio"
    static let bluetoothPrefix = "bt:"

    /// What a profile holds (the custom curve and its format marker belong to it, so "Custom" is per profile too).
    static let valueKeys = [SoundSettings.presetKey, SoundSettings.customKey, SoundSettings.customFormatKey,
                            SoundSettings.loudnessKey, SoundSettings.boostKey]

    struct Target: Equatable {
        let key: String
        let name: String
    }

    private static var defaults: UserDefaults { .standard }

    #if os(tvOS)
    static let defaultName = "Apple TV"
    #else
    static let defaultName = "iPhone"
    #endif

    static var activeName: String { defaults.string(forKey: activeNameKey) ?? defaultName }

    /// Stored (inactive) profiles.
    static var store: [String: [String: Any]] {
        (defaults.dictionary(forKey: storeKey) ?? [:]).compactMapValues { $0 as? [String: Any] }
    }

    private static func snapshot() -> [String: Any] {
        var values: [String: Any] = [:]
        for key in valueKeys {
            if let value = defaults.object(forKey: key) { values[key] = value }
        }
        return values
    }

    private static func load(_ values: [String: Any]) {
        for key in valueKeys {
            if let value = values[key] {
                defaults.set(value, forKey: key)
            } else {
                defaults.removeObject(forKey: key)  // unset = the setting's default, as before profiles existed
            }
        }
    }

    /// Makes `target` the active profile. Returns true when the active profile changed (the sound values may still be
    /// equal). The first call after the update adopts the present values for the profile in use: nothing changes.
    @discardableResult
    static func activate(_ target: Target) -> Bool {
        guard let current = defaults.string(forKey: activeKey) else {
            defaults.set(target.key, forKey: activeKey)
            defaults.set(target.name, forKey: activeNameKey)
            return false
        }
        guard current != target.key else {
            if defaults.string(forKey: activeNameKey) != target.name { defaults.set(target.name, forKey: activeNameKey) }
            return false
        }
        var all = store
        let present = snapshot()
        var saved = present
        saved["name"] = activeName
        all[current] = saved
        let next = all.removeValue(forKey: target.key) ?? all[defaultKey] ?? present
        defaults.set(all, forKey: storeKey)
        load(next)
        defaults.set(target.key, forKey: activeKey)
        defaults.set(target.name, forKey: activeNameKey)
        return true
    }

    struct Saved: Identifiable, Equatable {
        let key: String
        let name: String
        var id: String { key }
    }

    /// Saved profiles for Settings (not the default one, not the active one), sorted by name.
    static var saved: [Saved] {
        let active = defaults.string(forKey: activeKey)
        return store.compactMap { key, values in
            key == defaultKey || key == active ? nil : Saved(key: key, name: values["name"] as? String ?? key)
        }
        .sorted { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
    }

    /// Deletes a saved (inactive) profile: that device starts again from the default profile next time.
    static func forget(_ key: String) {
        guard key != defaultKey, key != defaults.string(forKey: activeKey) else { return }
        var all = store
        all.removeValue(forKey: key)
        defaults.set(all, forKey: storeKey)
    }

    /// Profile for the output in use now; nil = keep the current one (iPhone: a route without outputs, mid-switch).
    static func currentTarget() -> Target? {
        #if os(tvOS)
        return AtvAudio.selected ? Target(key: sinkKey, name: "atv-audio") : Target(key: defaultKey, name: defaultName)
        #else
        return target(for: AVAudioSession.sharedInstance().currentRoute.outputs.map { ($0.portType, $0.uid, $0.portName) })
        #endif
    }

    #if os(iOS)
    /// CarPlay first, then the first Bluetooth output (A2DP / LE audio), else the iPhone profile. Hands-free (HFP, calls)
    /// counts as iPhone: it only shows up during calls, when the app's sound is interrupted anyway.
    static func target(for outputs: [(type: AVAudioSession.Port, uid: String, name: String)]) -> Target? {
        guard !outputs.isEmpty else { return nil }
        if outputs.contains(where: { $0.type == .carAudio }) { return Target(key: carPlayKey, name: "CarPlay") }
        if let bt = outputs.first(where: { $0.type == .bluetoothA2DP || $0.type == .bluetoothLE }) {
            return Target(key: bluetoothPrefix + bt.uid, name: bt.name.isEmpty ? "Bluetooth" : bt.name)
        }
        return Target(key: defaultKey, name: defaultName)
    }
    #endif
}
