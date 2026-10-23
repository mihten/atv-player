import Foundation
import MobileVLCKit
import os

/// Diagnostic only: VLC's own log messages about audio, captured for a few seconds after a seek / pause / play / route
/// change and sent to the server log as one or two lines ("vlc log [label] ..."). Nothing acts on them.
///
/// Installed once per VLC instance as a `VLCLibrary` logger (VLCKit 3.7 `loggers` API). VLC calls `handleMessage` on
/// its own threads for every message, so the filter is cheap and the state sits behind a lock. Outside a window the
/// message is dropped at once.
final class VLCLogTap: NSObject, VLCLogging, @unchecked Sendable {
    static let shared = VLCLogTap()

    /// VLCLogging requirement: deliver everything up to debug (VLCLogLevel raw 3); the window filter below decides.
    var level: VLCLogLevel = VLCLogLevel(rawValue: 3) ?? VLCLogLevel(rawValue: 1)!

    private struct Window {
        var id = 0
        var open = false
        var label = ""
        var start = Date()
        var entries: [String] = []
        var dropped = 0
    }

    private let window = OSAllocatedUnfairLock(initialState: Window())
    private static let maxEntries = 45
    private static let keywords = ["flush", "silence", "late", "early", "toggling", "avaudiosession", "route", "latency",
                                   "deadlock", "discontinuity", "resampl", "underrun", "pause", "resum", "audiounit",
                                   "deferred", "drift", "buffering", "starting", "stopping"]

    // MARK: - VLCLogging

    func handleMessage(_ message: String, logLevel level: VLCLogLevel, context: VLCLogContext?) {
        let module = context?.module ?? ""
        let type = context?.objectType ?? ""
        let lower = message.lowercased()
        let audioSource = type.contains("audio") || module.contains("audio") || module.contains("aout")
        let isProblem = level.rawValue <= 1  // errors and warnings
        let videoChatter = type.contains("video output") && !isProblem
        guard !videoChatter, audioSource || isProblem || Self.keywords.contains(where: { lower.contains($0) }) else { return }
        let text = message.count > 150 ? String(message.prefix(150)) + "…" : message
        add("\(module.isEmpty ? type : module): \(text)")
    }

    // MARK: - Windows (main thread)

    /// Anything else worth seeing in the same timeline (the app's own audio session steps). Any thread.
    func note(_ text: String) {
        add(text)
    }

    private func add(_ text: String) {
        window.withLock { w in
            guard w.open else { return }
            guard w.entries.count < Self.maxEntries else {
                w.dropped += 1
                return
            }
            let ms = Int(Date().timeIntervalSince(w.start) * 1000)
            w.entries.append("+\(ms) \(text)")
        }
    }

    /// Opens a capture window; returns the id to close it with. A still-open window is returned via `previous`.
    func begin(_ label: String) -> (id: Int, previous: String?) {
        window.withLock { w in
            let previous = w.open ? Self.render(w) : nil
            w.id += 1
            w.open = true
            w.label = label
            w.start = Date()
            w.entries = []
            w.dropped = 0
            return (w.id, previous)
        }
    }

    /// Closes the window if it is still the one with `id`; returns its text (nil if superseded or empty).
    func end(_ id: Int) -> String? {
        window.withLock { w in
            guard w.open, w.id == id else { return nil }
            w.open = false
            return Self.render(w)
        }
    }

    private static func render(_ w: Window) -> String? {
        guard !w.entries.isEmpty else { return "vlc log [\(w.label)]: no audio messages" }
        let dropped = w.dropped > 0 ? " (+\(w.dropped) more not kept)" : ""
        return "vlc log [\(w.label)]\(dropped): " + w.entries.joined(separator: " | ")
    }
}
