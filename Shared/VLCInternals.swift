import Foundation
import ObjectiveC
#if os(tvOS)
import TVVLCKit
#else
import MobileVLCKit
#endif

/// Access to two libVLC internals that VLCKit does not expose.
///
/// Why: in VLC 3 the media player creates its own empty "audio-filter" variable (lib/media_player.c,
/// no inherit flag), so a `--audio-filter=…` startup option never reaches the audio output, and
/// `libvlc_media_player_set_equalizer` overwrites it with exactly "equalizer". To run the stereo
/// widener (alone or after the equalizer) the app sets the player's variable itself, right after
/// creating the player and before the first playback creates the audio output.
/// `var_SetChecked` is exported by TVVLCKit (checked in the 3.7.4 binary); if it is missing at
/// runtime this returns an error and playback continues without the widener.
enum VLCInternals {
    private typealias SetChecked = @convention(c) (UnsafeMutableRawPointer?, UnsafePointer<CChar>, Int32, UnsafeRawPointer?) -> Int32
    private static let varTypeString: Int32 = 0x0040  // VLC_VAR_STRING

    private static let setChecked: SetChecked? = {
        let rtldDefault = UnsafeMutableRawPointer(bitPattern: -2)  // RTLD_DEFAULT on Darwin
        guard let symbol = dlsym(rtldDefault, "var_SetChecked") else { return nil }
        return unsafeBitCast(symbol, to: SetChecked.self)
    }()

    /// The libvlc_media_player_t behind a VLCMediaPlayer (also a vlc_object_t).
    static func playerObject(_ player: VLCMediaPlayer) -> UnsafeMutableRawPointer? {
        let selector = NSSelectorFromString("libVLCMediaPlayer")
        if player.responds(to: selector), let method = class_getInstanceMethod(type(of: player), selector) {
            typealias Getter = @convention(c) (AnyObject, Selector) -> UnsafeMutableRawPointer?
            return unsafeBitCast(method_getImplementation(method), to: Getter.self)(player, selector)
        }
        if let ivar = class_getInstanceVariable(VLCMediaPlayer.self, "_playerInstance") {
            let base = Unmanaged.passUnretained(player).toOpaque()
            return base.load(fromByteOffset: ivar_getOffset(ivar), as: UnsafeMutableRawPointer?.self)
        }
        return nil
    }

    /// Set a string variable on the player object. Returns a short result for the log.
    static func setString(_ player: VLCMediaPlayer, _ name: String, _ value: String) -> String {
        guard let setChecked else { return "var_SetChecked not found" }
        guard let object = playerObject(player) else { return "player object not found" }
        return set(object, name, value, setChecked) == 0 ? "ok" : "error"
    }

    private static func set(_ object: UnsafeMutableRawPointer, _ name: String, _ value: String,
                            _ setChecked: SetChecked) -> Int32 {
        name.withCString { namePtr in
            value.withCString { valuePtr in
                setChecked(object, namePtr, varTypeString, UnsafeRawPointer(valuePtr))
            }
        }
    }

    private typealias ListChildren = @convention(c) (UnsafeMutableRawPointer?) -> UnsafeMutableRawPointer?
    private typealias ListRelease = @convention(c) (UnsafeMutableRawPointer?) -> Void

    private static func symbol<T>(_ name: String, as _: T.Type) -> T? {
        guard let pointer = dlsym(UnsafeMutableRawPointer(bitPattern: -2), name) else { return nil }
        return unsafeBitCast(pointer, to: T.self)
    }

    private static let listChildren = symbol("vlc_list_children", as: ListChildren.self)
    private static let listRelease = symbol("vlc_list_release", as: ListRelease.self)

    /// The audio filter chain must ALSO go on the player's audio output object: libVLC creates that
    /// output together with the player (lib/media_player.c:748), and it gets its own "audio-filter"
    /// variable (src/audio_output/output.c:312), which aout_FiltersNew reads at every stream start.
    /// libvlc_media_player_set_equalizer writes both; setting only the player's copy (v0.7–0.9.4) left
    /// the output at "equalizer", so the widener never ran. The output is the player's child object
    /// with type "audio output" (vlc_list_children; vlc_list_t = {int type; int count; vlc_value_t *values},
    /// object_type is the first field of every vlc_object_t). Both functions are exported by TVVLCKit.
    static func setAudioFilter(_ player: VLCMediaPlayer, _ chain: String) -> String {
        guard let setChecked else { return "var_SetChecked not found" }
        guard let object = playerObject(player) else { return "player object not found" }
        let onPlayer = set(object, "audio-filter", chain, setChecked) == 0 ? "ok" : "error"
        guard let listChildren, let listRelease else { return "player \(onPlayer), vlc_list_children not found" }
        guard let list = listChildren(object) else { return "player \(onPlayer), no child list" }
        defer { listRelease(list) }
        let count = Int(list.load(fromByteOffset: 4, as: Int32.self))
        var outputs = 0, failed = 0
        if count > 0, let values = list.load(fromByteOffset: 8, as: UnsafeMutableRawPointer?.self) {
            for index in 0..<count {
                guard let child = values.load(fromByteOffset: index * 8, as: UnsafeMutableRawPointer?.self),
                      let type = child.load(as: UnsafePointer<CChar>?.self),
                      String(cString: type) == "audio output" else { continue }
                if set(child, "audio-filter", chain, setChecked) == 0 { outputs += 1 } else { failed += 1 }
            }
        }
        return "player \(onPlayer), audio output \(outputs) ok\(failed > 0 ? ", \(failed) failed" : "") of \(count) children"
    }
}
