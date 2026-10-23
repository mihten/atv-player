import AVFoundation
import Foundation
import ObjectiveC
import os

/// Bluetooth pause/play keep-alive (iPhone only).
///
/// VLC 3's iOS audio output stops its AudioUnit AND deactivates the app's AVAudioSession on every pause
/// (modules/audio_output/audiounit_ios.m `Pause()` -> `avas_SetActive(false)`, also in `Stop()` between items). With the
/// session inactive iOS may close the Bluetooth A2DP stream to the headphones, and play has to reopen it: the 1-2 s
/// of lost sound on Bluetooth only (the speaker has no link to reopen). Apple Music / YouTube keep the session.
///
/// Two parts, both active only while `AudioSessionGuard.holding`:
/// - `AudioSessionGuard`: VLC's `setActive:NO` calls are ignored (VLC ignores the result of that call).
/// - `AudioKeepAlive`: a tiny AVAudioEngine plays digital silence, so the A2DP stream keeps carrying audio while VLC's
///   own AudioUnit is stopped.
/// The engine holds them while playback is wanted and releases them 30 s after a pause, then deactivates the session
/// itself so other apps can resume.
final class AudioSessionGuard: @unchecked Sendable {
    static let shared = AudioSessionGuard()

    private struct State {
        var holding = false
        var blocked = 0
    }

    private let state = OSAllocatedUnfairLock(initialState: State())

    var holding: Bool { state.withLock { $0.holding } }

    func setHolding(_ on: Bool) {
        state.withLock { $0.holding = on }
    }

    /// Called from the swizzled setActive on whatever thread VLC uses. true = swallow this deactivation.
    func shouldBlockDeactivation() -> Bool {
        state.withLock { s in
            guard s.holding else { return false }
            s.blocked += 1
            return true
        }
    }

    /// Blocked deactivations since the last call (and resets the count).
    func takeBlockedCount() -> Int {
        state.withLock { s in
            let n = s.blocked
            s.blocked = 0
            return n
        }
    }

    /// Installs the setActive hook once (idempotent).
    static func install() {
        _ = AVAudioSession.atvGuardInstalled
    }
}

extension AVAudioSession {
    fileprivate static let atvGuardInstalled: Bool = {
        let cls: AnyClass = AVAudioSession.self
        let original = NSSelectorFromString("setActive:withOptions:error:")
        let replacement = #selector(AVAudioSession.atv_setActive(_:withOptions:))
        guard let a = class_getInstanceMethod(cls, original), let b = class_getInstanceMethod(cls, replacement) else {
            return false
        }
        method_exchangeImplementations(a, b)
        return true
    }()

    /// After the exchange this name points at Apple's original implementation, so the inner call is not recursion.
    @objc dynamic func atv_setActive(_ active: Bool, withOptions options: AVAudioSession.SetActiveOptions) throws {
        if !active, AudioSessionGuard.shared.shouldBlockDeactivation() {
            VLCLogTap.shared.note("app: kept the audio session active (VLC asked to deactivate it)")
            return
        }
        try atv_setActive(active, withOptions: options)
    }
}

@MainActor
final class AudioKeepAlive {
    static let shared = AudioKeepAlive()

    private let engine = AVAudioEngine()
    private var configured = false
    var log: ((String) -> Void)?

    var isRunning: Bool { engine.isRunning }

    private init() {
        NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine, queue: .main) { _ in
            MainActor.assumeIsolated {
                let keeper = AudioKeepAlive.shared
                guard AudioSessionGuard.shared.holding, !keeper.engine.isRunning else { return }
                keeper.start("audio route/configuration changed")
            }
        }
    }

    private func configure() {
        guard !configured else { return }
        configured = true
        // Digital silence; the render block runs on the audio thread: no allocation, no locks.
        let silence = AVAudioSourceNode { _, _, _, bufferList -> OSStatus in
            for buffer in UnsafeMutableAudioBufferListPointer(bufferList) {
                if let data = buffer.mData { memset(data, 0, Int(buffer.mDataByteSize)) }
            }
            return noErr
        }
        engine.attach(silence)
        let rate = engine.outputNode.outputFormat(forBus: 0).sampleRate
        let format = AVAudioFormat(standardFormatWithSampleRate: rate > 0 ? rate : 48_000, channels: 2)
        engine.connect(silence, to: engine.mainMixerNode, format: format)
    }

    /// Hold the session and keep the output running (playback wanted).
    func hold(_ why: String) {
        AudioSessionGuard.shared.setHolding(true)
        start(why)
    }

    private func start(_ why: String) {
        guard !engine.isRunning else { return }
        configure()
        do {
            try engine.start()
            log?("audio keep-alive on (\(why)), output \(Self.route)")
            VLCLogTap.shared.note("app: keep-alive on (\(why))")
        } catch {
            log?("audio keep-alive could not start (\(why)): \(error.localizedDescription)")
        }
    }

    /// Stop the silence, stop holding the session; deactivate it ourselves if VLC asked while we held it.
    func release(_ why: String) {
        if engine.isRunning { engine.stop() }
        AudioSessionGuard.shared.setHolding(false)
        let blocked = AudioSessionGuard.shared.takeBlockedCount()
        var deactivated = "session left active"
        if blocked > 0 {
            do {
                try AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
                deactivated = "session deactivated (VLC had asked \(blocked) x)"
            } catch {
                deactivated = "deactivation failed: \(error.localizedDescription)"
            }
        }
        log?("audio keep-alive off (\(why)); \(deactivated)")
    }

    private static var route: String {
        AVAudioSession.sharedInstance().currentRoute.outputs.map { $0.portType.rawValue }.joined(separator: "+")
    }
}
