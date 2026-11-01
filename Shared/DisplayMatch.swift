#if os(tvOS)
import AVFoundation
import AVKit
import CoreMedia
import Foundation
import UIKit

/// Settings › Playback › "Match frame rate" (Apple TV): asks the TV for a music video's frame rate before it starts,
/// if Apple TV Settings › Video and Audio › Match Content is on. Always SDR: VLC 3 renders 8-bit RGBA on tvOS (HDR is
/// tone-mapped), so an HDR10/HLG/DV mode would only wrap an SDR picture (washed out, extra blank screen).
@MainActor
enum DisplayMatch {
    /// Referencing the class links AVKit; without it its `UIWindow.avDisplayManager` category is missing at runtime.
    private static let avKit: AnyClass = AVDisplayManager.self
    /// Standard rates the TV can switch to (23.976 and 24 are different modes).
    static let rates: [Double] = [24000.0 / 1001, 24, 25, 30000.0 / 1001, 30, 50, 60000.0 / 1001, 60]

    /// Rate last written (nil = system mode). Identical criteria are never written again (that blanks the TV).
    private static var applied: Double?
    /// Item of the last log line, so reopens of the same item (settings rebuild, stall, retry) add none.
    private static var notedItem: Int?

    private struct Wait {
        let id: Int
        let what: String
        let started: Date
        var sawStart = false
        var ready: () -> Void
        let log: (String) -> Void
    }
    private static var wait: Wait?
    private static var waitCounter = 0
    private static var observers: [NSObjectProtocol] = []

    private static let sdrFormat: CMVideoFormatDescription? = {
        let extensions: [String: Any] = [
            kCMFormatDescriptionExtension_ColorPrimaries as String: kCMFormatDescriptionColorPrimaries_ITU_R_709_2,
            kCMFormatDescriptionExtension_TransferFunction as String: kCMFormatDescriptionTransferFunction_ITU_R_709_2,
            kCMFormatDescriptionExtension_YCbCrMatrix as String: kCMFormatDescriptionYCbCrMatrix_ITU_R_709_2,
        ]
        var format: CMVideoFormatDescription?
        let status = CMVideoFormatDescriptionCreate(allocator: kCFAllocatorDefault, codecType: kCMVideoCodecType_H264,
                                                    width: 1920, height: 1080, extensions: extensions as CFDictionary,
                                                    formatDescriptionOut: &format)
        return status == 0 ? format : nil
    }()

    private static var manager: AVDisplayManager? {
        _ = avKit
        let windows = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.flatMap(\.windows)
        return (windows.first(where: \.isKeyWindow) ?? windows.first)?.avDisplayManager
    }

    /// The standard rate for a video's fps: exact family within 0.05 %, else the nearest within 2 % (VFR clips).
    /// Above 61 fps the half rate (100/120 -> 50/60 Hz). nil = do not ask (unknown, 48 fps, far from any rate).
    static func targetRate(_ fps: Double?) -> Double? {
        guard var value = fps, value.isFinite, value > 1 else { return nil }
        if value > 61 { value /= 2 }
        if let exact = rates.first(where: { abs($0 - value) <= $0 * 0.0005 }) { return exact }
        guard let near = rates.min(by: { abs($0 - value) < abs($1 - value) }), abs(near - value) <= near * 0.02 else {
            return nil
        }
        return near
    }

    private static func label(_ rate: Double?) -> String {
        rate.map { String(format: "%.3f Hz SDR", $0) } ?? "system mode"
    }

    /// Asks for the mode `item` wants (its rate for a shown video with the switch on, else the system mode) and calls
    /// `ready` when the TV has switched: at once when nothing changes, else after the switch (capped at 4 s).
    /// `allowed` false (app in the background): the system mode is kept, nothing is asked.
    static func prepare(for item: MediaItem, allowed: Bool, log: @escaping (String) -> Void,
                        ready: @escaping () -> Void) {
        let video = allowed && PlayerSettings.matchFrameRate && item.isVideo
        let wanted = video && sdrFormat != nil ? targetRate(item.fps) : nil
        let fpsNote = item.fps.map { String(format: "%.3f", $0) } ?? "unknown"
        if wanted == applied {
            if !allowed, PlayerSettings.matchFrameRate, item.isVideo {
                log("display: '\(item.title)' loaded in the background, rate not asked until the app is back")
            }
            if video, notedItem != item.id {
                notedItem = item.id
                log("display: '\(item.title)' fps \(fpsNote) -> \(label(wanted)), no change (already in that mode)")
            }
            if wait != nil {
                wait?.ready = ready  // the TV is still switching for the previous item: start after it
            } else {
                ready()
            }
            return
        }
        guard let display = manager else {
            ready()
            return
        }
        let what = "display: '\(item.title)' fps \(fpsNote) -> \(label(wanted))"
        if !display.isDisplayCriteriaMatchingEnabled {
            // tvOS ignores the request then: no wait. Forget an earlier one, so it is made again once matching is on.
            if applied != nil {
                display.preferredDisplayCriteria = nil
                applied = nil
            }
            if notedItem != item.id {
                notedItem = item.id
                log("\(what): not asked, matching off in tvOS settings")
            }
            finishWait("superseded")
            ready()
            return
        }
        notedItem = item.id
        write(wanted, to: display, what: what, log: log, ready: ready)
    }

    /// Back to the system mode at once, without waiting (stop, end of queue, unload, background, switch turned off).
    static func reset(_ reason: String, log: @escaping (String) -> Void) {
        guard applied != nil else { return }
        finishWait("superseded")
        applied = nil
        notedItem = nil
        manager?.preferredDisplayCriteria = nil
        log("display: system mode (\(reason))")
    }

    /// Observers go in BEFORE the write: the switch can start and end at once.
    private static func write(_ rate: Double?, to display: AVDisplayManager, what: String,
                              log: @escaping (String) -> Void, ready: @escaping () -> Void) {
        finishWait("superseded")
        waitCounter += 1
        let id = waitCounter
        wait = Wait(id: id, what: what, started: Date(), ready: ready, log: log)
        let center = NotificationCenter.default
        observers = [
            center.addObserver(forName: .AVDisplayManagerModeSwitchStart, object: nil, queue: .main) { _ in
                MainActor.assumeIsolated { DisplayMatch.switchStarted(id) }
            },
            center.addObserver(forName: .AVDisplayManagerModeSwitchEnd, object: nil, queue: .main) { _ in
                MainActor.assumeIsolated { DisplayMatch.switchEnded(id) }
            },
        ]
        if let rate, let format = sdrFormat {
            display.preferredDisplayCriteria = AVDisplayCriteria(refreshRate: Float(rate), formatDescription: format)
        } else {
            display.preferredDisplayCriteria = nil
        }
        applied = rate
        Task {
            try? await Task.sleep(nanoseconds: 500_000_000)
            // No start within 0.5 s: already in that mode, or the TV refused it.
            if let current = DisplayMatch.wait, current.id == id, !current.sawStart,
               DisplayMatch.manager?.isDisplayModeSwitchInProgress != true {
                DisplayMatch.finishWait("no switch", id: id)
            }
        }
        Task {
            try? await Task.sleep(nanoseconds: 4_000_000_000)
            DisplayMatch.finishWait("cap", id: id)  // some TVs never post the End notification
        }
    }

    private static func switchStarted(_ id: Int) {
        guard wait?.id == id else { return }
        wait?.sawStart = true
    }

    /// An End without this wait's own Start is left over from an earlier switch: the 0.5 s check or the cap ends it.
    private static func switchEnded(_ id: Int) {
        guard let current = wait, current.id == id, current.sawStart else { return }
        finishWait("switched", id: id)
    }

    /// Ends the wait `id` (nil = whichever is pending): one log line, then the deferred start.
    private static func finishWait(_ reason: String, id: Int? = nil) {
        guard let current = wait, id == nil || current.id == id else { return }
        wait = nil
        for token in observers { NotificationCenter.default.removeObserver(token) }
        observers = []
        let ms = Int(Date().timeIntervalSince(current.started) * 1000)
        current.log("\(current.what): \(reason), waited \(ms) ms")
        current.ready()
    }
}
#endif
