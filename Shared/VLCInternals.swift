import Foundation
import ObjectiveC
#if os(tvOS)
import TVVLCKit
#else
import MobileVLCKit
#endif

/// Access to libVLC internals that VLCKit does not expose (restored from 0.18.19, where the stereo widener used it).
///
/// Why: in VLC 3 the media player creates its own empty "audio-filter" variable (lib/media_player.c,
/// no inherit flag), so a `--audio-filter=…` startup option never reaches the audio output, and
/// `libvlc_media_player_set_equalizer` overwrites it with exactly "equalizer". To run the limiter
/// after the equalizer the app sets the player's variable itself, once, on a fresh player.
///
/// Gain changes on a running player go straight into the filters' variables (`setVariables`): VLC 3's
/// equalizer.c and compressor.c create "equalizer-preamp" / "equalizer-bands" / "compressor-*" on their
/// parent, the audio output, and read every later change through var_AddCallback (no restart). Only a
/// different "audio-filter" string restarts the filters (output.c FilterCallback), which is what
/// `vlc.equalizer = …` does with the limiter in the chain (2-3 s of silence in 0.18.32).
/// `var_SetChecked`, `var_Create`, `var_Type` and `vlc_list_children` are libvlccore exports (VLC_API); if one
/// is missing at runtime the result text says so and playback continues.
enum VLCInternals {
    /// var_SetChecked(obj, name, type, vlc_value_t): the 8-byte union goes in one integer register on arm64 and
    /// x86_64 (mixed union, not a float aggregate), so a char* or a float's bits in the low half pass as UInt64.
    private typealias SetChecked = @convention(c) (UnsafeMutableRawPointer?, UnsafePointer<CChar>, Int32, UInt64) -> Int32
    private typealias VarCreate = @convention(c) (UnsafeMutableRawPointer?, UnsafePointer<CChar>, Int32) -> Int32
    private typealias VarType = @convention(c) (UnsafeMutableRawPointer?, UnsafePointer<CChar>) -> Int32
    private typealias ListChildren = @convention(c) (UnsafeMutableRawPointer?) -> UnsafeMutableRawPointer?
    private typealias ListRelease = @convention(c) (UnsafeMutableRawPointer?) -> Void

    private static let varTypeString: Int32 = 0x0040  // VLC_VAR_STRING
    private static let varTypeFloat: Int32 = 0x0050   // VLC_VAR_FLOAT
    private static let varDoInherit: Int32 = 0x8000   // VLC_VAR_DOINHERIT

    private static func symbol<T>(_ name: String, as _: T.Type) -> T? {
        guard let pointer = dlsym(UnsafeMutableRawPointer(bitPattern: -2), name) else { return nil }  // RTLD_DEFAULT
        return unsafeBitCast(pointer, to: T.self)
    }

    private static let setChecked = symbol("var_SetChecked", as: SetChecked.self)
    private static let varCreate = symbol("var_Create", as: VarCreate.self)
    private static let varType = symbol("var_Type", as: VarType.self)
    private static let listChildren = symbol("vlc_list_children", as: ListChildren.self)
    private static let listRelease = symbol("vlc_list_release", as: ListRelease.self)

    enum Value: Equatable {
        case float(Float)
        case string(String)
    }

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

    /// 0 = VLC_SUCCESS. `create`: make the variable first if the object does not have it (inheriting, as the
    /// filters create theirs), so a value set before the filter opens is the one it starts with.
    private static func set(_ object: UnsafeMutableRawPointer, _ name: String, _ value: Value, create: Bool,
                            _ setChecked: SetChecked) -> Int32 {
        name.withCString { (namePtr) -> Int32 in
            let type: Int32
            switch value {
            case .float: type = varTypeFloat
            case .string: type = varTypeString
            }
            if create, let varType, let varCreate, varType(object, namePtr) == 0 {
                _ = varCreate(object, namePtr, type | varDoInherit)
            }
            switch value {
            case .float(let number):
                return setChecked(object, namePtr, type, UInt64(number.bitPattern))
            case .string(let text):
                return text.withCString { setChecked(object, namePtr, type, UInt64(UInt(bitPattern: $0))) }
            }
        }
    }

    /// The player's audio output objects: libVLC creates the output together with the player
    /// (lib/media_player.c:748) as its child with type "audio output" (vlc_list_children; vlc_list_t =
    /// {int type; int count; vlc_value_t *values}, object_type is the first field of every vlc_object_t).
    /// It has its own "audio-filter" variable (src/audio_output/output.c:312), read at every stream start,
    /// and it is the parent of the filters, so their variables live there. nil = the list functions are missing.
    private static func withAudioOutputs<T>(_ object: UnsafeMutableRawPointer,
                                            _ body: ([UnsafeMutableRawPointer], Int) -> T) -> T? {
        guard let listChildren, let listRelease, let list = listChildren(object) else { return nil }
        defer { listRelease(list) }
        let count = Int(list.load(fromByteOffset: 4, as: Int32.self))
        var outputs: [UnsafeMutableRawPointer] = []
        if count > 0, let values = list.load(fromByteOffset: 8, as: UnsafeMutableRawPointer?.self) {
            for index in 0..<count {
                guard let child = values.load(fromByteOffset: index * 8, as: UnsafeMutableRawPointer?.self),
                      let type = child.load(as: UnsafePointer<CChar>?.self),
                      String(cString: type) == "audio output" else { continue }
                outputs.append(child)
            }
        }
        return body(outputs, count)
    }

    /// The filter chain goes on the player AND its audio output (libvlc_media_player_set_equalizer writes both;
    /// setting only the player's copy, v0.7–0.9.4, left the output at "equalizer").
    static func setAudioFilter(_ player: VLCMediaPlayer, _ chain: String) -> String {
        guard let setChecked else { return "var_SetChecked not found" }
        guard let object = playerObject(player) else { return "player object not found" }
        let onPlayer = set(object, "audio-filter", .string(chain), create: false, setChecked) == 0 ? "ok" : "error"
        let text = withAudioOutputs(object) { (outputs, count) -> String in
            let failed = outputs.filter { set($0, "audio-filter", .string(chain), create: false, setChecked) != 0 }.count
            return "audio output \(outputs.count - failed) ok\(failed > 0 ? ", \(failed) failed" : "") of \(count) children"
        }
        return "player \(onPlayer), \(text ?? "vlc_list_children not found")"
    }

    /// Filter parameters on the running player, without touching "audio-filter" (no restart): set on the player
    /// (the output inherits from it when a filter first creates the variable) and on its audio output (what the
    /// running filters' callbacks watch). Returns e.g. "player 4/4, audio output 4/4" for the log.
    static func setVariables(_ player: VLCMediaPlayer, _ values: [(String, Value)]) -> String {
        guard let setChecked else { return "var_SetChecked not found" }
        guard let object = playerObject(player) else { return "player object not found" }
        func apply(_ target: UnsafeMutableRawPointer) -> Int {
            values.filter { set(target, $0.0, $0.1, create: true, setChecked) == 0 }.count
        }
        var text = "player \(apply(object))/\(values.count)"
        if varCreate == nil || varType == nil { text += " (var_Create not found)" }
        let outputs = withAudioOutputs(object) { (outputs, _) -> String in
            outputs.isEmpty ? "no audio output" : outputs.map { "audio output \(apply($0))/\(values.count)" }.joined(separator: ", ")
        }
        return text + ", " + (outputs ?? "vlc_list_children not found")
    }
}
