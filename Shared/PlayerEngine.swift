import AVFoundation
import Foundation
import MediaPlayer
import os
#if os(tvOS)
import TVVLCKit
#else
import MobileVLCKit
#endif
import UIKit

/// Playback position for the UI, kept in its own object so only the progress views redraw.
@MainActor
final class PlaybackClock: ObservableObject {
    @Published var position: Double = 0
    @Published var duration: Double = 0
}

/// Wraps VLCMediaPlayer: plays the original files (MKV/AV1/VP9/FLAC/Opus/SRT) streamed from the server.
@MainActor
final class PlayerEngine: NSObject, ObservableObject {
    private(set) var vlc: VLCMediaPlayer
    private var vlcOptions: [String]
    let videoView: UIView = {
        let view = UIView()
        view.backgroundColor = .black
        return view
    }()

    @Published private(set) var current: MediaItem?
    @Published private(set) var isPlaying = false
    /// Not @Published: changes several times a second; the UI reads `clock` instead.
    private(set) var position: Double = 0
    private(set) var duration: Double = 0
    let clock = PlaybackClock()
    private var ticker: Timer?
    private var timeEvents = 0
    private var diagTask: Task<Void, Never>?
    /// Bumped when the user starts something (server load with present=true, or the Now Playing
    /// card / Play-Pause in a menu), so the UI shows the player. Auto-advance does not bump it.
    @Published private(set) var startCounter = 0
    /// iPhone: the picture area shows a cover instead of the video until the next keyframe has played, after the picture
    /// track came back mid-stream (lyrics -> video, return to the app after the background grace). See coverUntilKeyframe.
    @Published private(set) var pictureSettling = false

    weak var connection: ServerConnection?
    private var tracksApplied = false
    private var endedSent = false
    private var artwork: MPMediaItemArtwork?
    private var artworkFor: Int?
    /// Last play/pause request, to measure how long VLC takes to actually change state.
    private var pendingToggle: (at: Date, source: String, wantPlaying: Bool)?
    /// What the user asked for. Toggle decisions use this, never vlc.isPlaying: VLC reports
    /// buffering states during network playback and isPlaying is unreliable while paused.
    private var wantPlaying = false
    private var appliedSound: String
    private var audioReport = ""
    /// Loudness leveling gain (dB) currently applied through the equalizer preamp.
    private var levelGain: Float = 0
    /// Start watchdog: VLC can sit in "stopped" without any error event (file gone on the server →
    /// HTTP 404, or a dead player instance). A load counts as started at its first time event.
    private var startedPlaying = false
    private var retriedStart = false
    private var loadGeneration = 0
    /// The picture track was left on when the app went to the background (see appDidEnterBackground).
    private var pictureKeptOn = false
    /// iPhone: the picture track stays on for `lyricsPictureGrace` s after the lyrics view opens (see setLyricsCover).
    private var lyricsKeptOn = false
    private var lyricsWatch: Task<Void, Never>?
    private var settleTask: Task<Void, Never>?
    private var backgroundWatch: Task<Void, Never>?
    private var seekWatch: Task<Void, Never>?
    /// Seek timing trace (log only, nothing acts on it): events since the seek call, see traceSeek.
    private var traceT0: Date?
    private var traceEvents: [String] = []
    private var traceTimeEvents = 0
    private var traceTask: Task<Void, Never>?
    private var startCheck: Task<Void, Never>?
    private var startTimeout: Task<Void, Never>?
    /// iOS: playback was running when a call/Siri interrupted it.
    fileprivate var resumeAfterInterruption = false
    /// The app is not in the foreground; the video track of a playing music video is switched off.
    private var inBackground = false
    private var videoHidden = false
    /// iPhone lyrics view: the lyrics replace the picture, so a video's picture track is switched off
    /// (the sound keeps playing and nothing is decoded for a picture nobody sees).
    private var lyricsCover = false

    /// Short id of the current VLC instance, for the logs (changes when the player is rebuilt).
    private var playerTag: String {
        String(UInt(bitPattern: ObjectIdentifier(vlc).hashValue) & 0xFFFFFF, radix: 16)
    }

    private func log(_ message: String) {
        connection?.send(["type": "log", "message": message])
    }

    /// Equalizer + widener on a fresh player, before its first playback creates the audio output.
    private func configureAudio() {
        levelGain = SoundSettings.levelGain(for: current)
        let eq = SoundSettings.makeEqualizer(extraGain: levelGain)
        if let eq {
            vlc.equalizer = eq
        }
        let gainNote = levelGain != 0 ? String(format: ", loudness %+.1f dB", levelGain) : ""
        if let chain = SoundSettings.filterChain(withEqualizer: eq != nil) {
            audioReport = "audio filters '\(chain)': \(VLCInternals.setAudioFilter(vlc, chain))\(gainNote)"
        } else {
            audioReport = "audio: \(SoundSettings.summary)\(gainNote)"
        }
    }

    /// Per-track loudness gain for the item that is about to load. Without the widener the equalizer's
    /// preamp is changed on the running player (that is what VLC's own equalizer sliders do); with the
    /// widener the filter chain depends on whether an equalizer exists, so the player is rebuilt (the same
    /// path as a settings change). Called before the new media is set.
    private func applyLevel(for item: MediaItem) {
        let gain = SoundSettings.levelGain(for: item)
        guard gain != levelGain else { return }
        let before = levelGain
        if SoundSettings.widen > 0 {
            rebuildPlayer("loudness")  // configureAudio picks up the gain of `current`
        } else {
            levelGain = gain
            vlc.equalizer = SoundSettings.makeEqualizer(extraGain: gain)
            audioReport = "audio: \(SoundSettings.summary), loudness \(String(format: "%+.1f", gain)) dB"
        }
        log(String(format: "loudness gain %+.1f -> %+.1f dB for '%@' (%@ LUFS, peak %@ dBFS)", before, gain, item.title,
                   item.lufs.map { String(format: "%.1f", $0) } ?? "?", item.tpeak.map { String(format: "%.1f", $0) } ?? "?"))
    }

    /// UI diagnostics into the server log (e.g. full-screen events), with the app state.
    func diag(_ message: String) {
        log("\(message); \(envNote)")
    }

    /// First connection of this launch: tell the server log how the previous run ended.
    func reportLaunch() {
        log("\(launchNote); \(envNote)")
    }

    func presentPlayer() {
        startCounter += 1
    }

    /// "foreground" / "background": where the app was when the previous run ended. iOS gives no exit
    /// notice, so the next launch reports it: ended in the foreground = crash (or swiped away there);
    /// in the background = killed by iOS (memory) or swiped away from the app switcher.
    private static let lastStateKey = "lastRunState"
    private var launchNote = ""

    override init() {
        launchNote = "app started; previous run ended in the \(UserDefaults.standard.string(forKey: Self.lastStateKey) ?? "unknown state")"
        UserDefaults.standard.set("foreground", forKey: Self.lastStateKey)
        SoundSettings.migrate()
        let options = PlayerSettings.vlcOptions() + SoundSettings.widenerOptions()
        vlcOptions = options
        appliedSound = SoundSettings.configKey
        vlc = Self.makePlayer(options)
        super.init()
        vlc.delegate = self
        vlc.drawable = videoView
        configureAudio()
        try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .moviePlayback)
        try? AVAudioSession.sharedInstance().setActive(true)
        #if os(iOS)
        vlc.libraryInstance.loggers = [VLCLogTap.shared]
        observeAudioSession()
        #endif
        setupRemoteCommands()
        startTicker()
    }

    /// Twice a second: copy VLC's time into `clock` (local UI refresh only, nothing goes over the network).
    private func startTicker() {
        let timer = Timer(timeInterval: 0.5, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.tick() }
        }
        RunLoop.main.add(timer, forMode: .common)
        ticker = timer
    }

    private func tick() {
        guard current != nil else { return }
        let vlcTime = Double(vlc.time.intValue) / 1000
        if vlcTime > 0 { position = vlcTime }
        if let length = vlc.media?.length, length.intValue > 0 {
            duration = Double(length.intValue) / 1000
        }
        if abs(clock.position - position) >= 0.25 { clock.position = position }
        if abs(clock.duration - duration) >= 0.5 { clock.duration = duration }
        watchProgress(vlcTime)
    }

    // MARK: - Stall watchdog

    /// VLC sometimes goes quiet while it should be playing: no time progress and no state event (seen on the iPhone:
    /// frozen at 4 s of a video, and stuck 0.1 s before the end of a video without the Ended event, so the queue
    /// never moved on). The ticker watches VLC's time while the user wants playback:
    /// - stuck within 3 s of the end for 4 s = the item is over -> report "ended" (the server plays the next one);
    /// - stuck elsewhere for 8 s = a stall -> reopen the item at the same spot once, a second stall -> report an error
    ///   (the server skips).
    /// Every 10 s while playing the app also sends its position, so the server and the web remote stay true.
    private var lastVLCTime: Double = -1
    private var lastProgressAt = Date()
    private var stallRetriedItem: Int?
    private var lastHeartbeat = Date()

    private func watchProgress(_ vlcTime: Double) {
        let now = Date()
        if vlcTime != lastVLCTime {
            lastVLCTime = vlcTime
            lastProgressAt = now
        }
        guard let item = current, wantPlaying, startedPlaying, !endedSent else {
            lastProgressAt = now  // the clock only runs while playback is wanted
            return
        }
        if now.timeIntervalSince(lastHeartbeat) >= 10 {
            lastHeartbeat = now
            report()
        }
        let stuck = now.timeIntervalSince(lastProgressAt)
        let nearEnd = duration > 0 && vlcTime >= duration - 3
        let state = VLCMediaPlayerStateToString(vlc.state)
        if nearEnd && stuck >= 4 {
            log("end not signalled by VLC: '\(item.title)' stuck at \(String(format: "%.1f", vlcTime)) of \(Int(duration)) s for \(Int(stuck)) s, state \(state) -> next; \(envNote)")
            finish()
        } else if !nearEnd && stuck >= 8 {
            lastProgressAt = now
            if stallRetriedItem != item.id {
                stallRetriedItem = item.id
                log("stall: '\(item.title)' stuck at \(String(format: "%.1f", vlcTime)) s for \(Int(stuck)) s, state \(state), video hidden \(videoHidden) -> reopening at that spot; \(mediaNote)")
                load(item, position: max(0, vlcTime - 1), autoplay: true, present: false, force: true)
            } else {
                log("stall again: '\(item.title)' stuck at \(String(format: "%.1f", vlcTime)) s, state \(state) -> skipping; \(mediaNote)")
                wantPlaying = false  // the server answers the error with the next item (autoplay)
                setPlaying(false)
                connection?.send(["type": "error", "item": item.id, "message": "playback stalled twice"])
            }
        }
    }

    /// One log line per item, 6 s after it starts: shows whether VLC time events arrive and time advances.
    private func scheduleDiagnostics(for item: MediaItem) {
        diagTask?.cancel()
        timeEvents = 0
        let startTime = vlc.time.intValue
        diagTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 6_000_000_000)
            guard let self, !Task.isCancelled, self.current?.id == item.id else { return }
            self.connection?.send([
                "type": "log",
                "message": "\(self.audioReport); progress check '\(item.title)': timeEvents=\(self.timeEvents) vlcTime \(startTime)->\(self.vlc.time.intValue) ms clock=\(Int(self.clock.position)) s state=\(VLCMediaPlayerStateToString(self.vlc.state)) playing=\(self.isPlaying); audio blocks decoded \(self.vlc.media?.numberOfDecodedAudioBlocks ?? -1), buffers lost \(self.vlc.media?.numberOfLostAudioBuffers ?? -1); \(self.mediaNote)",
            ])
            if item.isVideo && !self.videoHidden {
                self.logPictureStats(after: 10, label: "at 6-16 s of '\(item.title)'")
            }
        }
    }

    private static func makePlayer(_ options: [String]) -> VLCMediaPlayer {
        options.isEmpty ? VLCMediaPlayer() : VLCMediaPlayer(options: options)
    }

    // MARK: - Commands from the server

    /// Settings: audio only / audio + video over the remote link changed, or the link switched between home
    /// and remote: reopen what is loaded, same position.
    func streamChanged() {
        guard let item = current else { return }
        load(item, position: position, autoplay: wantPlaying, present: false, force: true)
    }

    func load(_ item: MediaItem, position startAt: Double, autoplay: Bool, present: Bool = false,
              retry: Bool = false, force: Bool = false) {
        if !autoplay, !force, current?.id == item.id, vlc.media != nil {
            return  // resume sync after a reconnect: keep whatever is playing now
        }
        current = item
        applyLevel(for: item)
        videoHidden = false  // new media starts with its picture track on
        traceTask?.cancel()
        traceT0 = nil
        pictureKeptOn = false
        backgroundWatch?.cancel()
        seekWatch?.cancel()
        lyricsKeptOn = false
        lyricsWatch?.cancel()
        settleTask?.cancel()
        pictureSettling = false
        if item.isVideo { KeyframeStore.shared.prefetch(item) }
        tracksApplied = false
        endedSent = false
        isPlaying = false  // the next VLC event reports the real state for the new item
        pendingToggle = nil
        wantPlaying = autoplay
        startedPlaying = false
        if !retry { retriedStart = false }
        loadGeneration += 1
        startCheck?.cancel()
        startTimeout?.cancel()
        duration = item.duration ?? 0
        position = startAt
        let media = VLCMedia(url: item.mediaURL)
        media.addOption(":network-caching=1500")
        if PlayerSettings.fastSeek {
            // Seek to the nearest keyframe instead of decoding up to it (AV1 keyframes are 2-7 s apart).
            media.addOption(":input-fast-seek")
        }
        if startAt > 1 {
            media.addOption(":start-time=\(Int(startAt))")
        }
        vlc.media = media
        log("load '\(item.title)' id \(item.id) at \(Int(startAt)) s autoplay=\(autoplay) present=\(present)\(retry ? " RETRY" : "") player \(playerTag)")
        if autoplay {
            vlc.play()
            if present { startCounter += 1 }
            armStartTimeout(for: item)
        }
        clock.position = startAt
        clock.duration = duration
        scheduleDiagnostics(for: item)
        fetchArtwork(for: item)
        updateNowPlaying()
    }

    /// 15 s without a single time event while the user wants playback = the start failed.
    private func armStartTimeout(for item: MediaItem) {
        let generation = loadGeneration
        startTimeout = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 15_000_000_000)
            guard let self, !Task.isCancelled, generation == self.loadGeneration,
                  self.wantPlaying, !self.startedPlaying else { return }
            self.startFailed(item, "no playback after 15 s, state \(VLCMediaPlayerStateToString(self.vlc.state))")
        }
    }

    /// VLC reported stopped/error before the item ever played. Re-check 1.5 s later (VLC also emits
    /// a stop for the previous media when a new one is set), then treat it as a failed start.
    private func checkStartSoon() {
        guard wantPlaying, !startedPlaying, let item = current else { return }
        let generation = loadGeneration
        startCheck?.cancel()
        startCheck = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            guard let self, !Task.isCancelled, generation == self.loadGeneration,
                  self.wantPlaying, !self.startedPlaying else { return }
            let state = self.vlc.state
            guard state == .stopped || state == .error || state == .ended else { return }
            self.startFailed(item, "VLC \(VLCMediaPlayerStateToString(state)) before playback started")
        }
    }

    /// First failure: new VLC instance and one retry. Second: report it, the server skips to the next item.
    private func startFailed(_ item: MediaItem, _ reason: String) {
        guard current?.id == item.id else { return }
        startCheck?.cancel()
        startTimeout?.cancel()
        if !retriedStart {
            retriedStart = true
            log("start failed '\(item.title)': \(reason); player \(playerTag) -> rebuilding VLC and retrying once")
            rebuildPlayer("start failed")
            load(item, position: position, autoplay: true, present: false, retry: true)
        } else {
            log("start failed again '\(item.title)': \(reason); player \(playerTag) -> skipping")
            wantPlaying = false
            setPlaying(false)
            updateNowPlaying()
            connection?.send(["type": "error", "item": item.id, "message": "VLC could not start the file: \(reason)"])
        }
    }

    func control(_ action: String, position target: Double?) {
        switch action {
        case "play": play(source: "remote")
        case "pause": pause(source: "remote")
        case "toggle": togglePlay(source: "remote")
        case "seek": seek(to: target ?? 0)
        case "stop":
            wantPlaying = false
            vlc.stop()
            setPlaying(false)
            updateNowPlaying()
        default: break
        }
    }

    // MARK: - Local controls (Siri Remote / Control Center)

    /// Menus have nested Play/Pause handlers (ContentView's NavigationStack and SettingsView); if
    /// tvOS delivers one press to both, the second toggle would undo the first.
    private var lastMenuToggle = Date.distantPast

    func togglePlay(source: String) {
        if source == "menu" {
            guard Date().timeIntervalSince(lastMenuToggle) > 0.3 else { return }
            lastMenuToggle = Date()
        }
        if wantPlaying {
            pause(source: source)
        } else {
            play(source: source)
        }
    }

    func play(source: String) {
        logRequest("play", source)
        wantPlaying = true
        pendingToggle = (Date(), source, true)
        #if os(iOS)
        vlcLogWindow("play via \(source)")
        #endif
        switch vlc.state {
        case .stopped, .ended, .error:
            // The stream was closed (long pause, network drop): reopen at the same position.
            if let item = current {
                load(item, position: position, autoplay: true, present: false)
            }
        default:
            traceSeek(to: Double(vlc.time.intValue) / 1000, label: "resume")
            vlc.play()
        }
    }

    func pause(source: String) {
        logRequest("pause", source)
        wantPlaying = false
        pendingToggle = (Date(), source, false)
        #if os(iOS)
        vlcLogWindow("pause via \(source)")
        #endif
        if vlc.canPause {
            vlc.pause()
        } else {
            vlc.stop()
        }
    }

    private func logRequest(_ action: String, _ source: String) {
        connection?.send([
            "type": "log",
            "message": "\(action) requested via \(source): vlc state \(VLCMediaPlayerStateToString(vlc.state)), wantPlaying \(wantPlaying); \(envNote)",
        ])
    }

    /// Where the app is and how much memory it may still use: tells a system pause (lock screen, headset,
    /// background video) from a user press, and a memory kill from a crash, in the server log.
    private var envNote: String {
        let state: String
        switch UIApplication.shared.applicationState {
        case .active: state = "active"
        case .inactive: state = "inactive"
        default: state = "background"
        }
        let route = AVAudioSession.sharedInstance().currentRoute.outputs.map { $0.portType.rawValue }.joined(separator: "+")
        return "app \(state), output \(route.isEmpty ? "none" : route), free memory \(os_proc_available_memory() / 1_048_576) MB"
    }

    /// What VLC currently has: picture, tracks. Logged with the 6 s progress check of every item.
    private var mediaNote: String {
        let video = current?.isVideo == true
            ? "video out \(vlc.hasVideoOut) size \(Int(vlc.videoSize.width))x\(Int(vlc.videoSize.height)) track \(vlc.currentVideoTrackIndex)"
            : "audio only"
        return "\(video), audio track \(vlc.currentAudioTrackIndex) of \(vlc.numberOfAudioTracks), \(envNote)"
    }

    /// Position/length straight from VLC, for the info bar's own refresh.
    var livePosition: Double {
        let t = Double(vlc.time.intValue) / 1000
        return t > 0 ? t : position
    }

    var liveDuration: Double {
        if let length = vlc.media?.length, length.intValue > 0 {
            return Double(length.intValue) / 1000
        }
        return duration
    }

    func seek(to seconds: Double) {
        let target = max(0, seconds)
        log("seek to \(Int(target)) s requested: seekable \(vlc.isSeekable), state \(VLCMediaPlayerStateToString(vlc.state))")
        watchSeek(to: target)
        traceSeek(to: target)
        endPictureCover(reason: "seek")
        #if os(iOS)
        vlcLogWindow(String(format: "seek to %.1f s", target))
        #endif
        vlc.time = VLCTime(int: Int32(target * 1000))
        position = target
        clock.position = target
        report()
        updateNowPlaying()
    }

    /// Diagnostic only (nothing acts on it): where a seek lands and what it costs. Logged: the position before the jump, the
    /// target, where VLC's clock is 0.3 s after the jump (with fast seek on VLC lands on the keyframe BEFORE the target:
    /// target - landed = how far early), and 3 s later how much playback time passed against the wall clock (the difference =
    /// how long it stood still) plus audio blocks decoded / buffers lost meanwhile. A new seek or load cancels the watch (a
    /// burst of seeks reports the last one).
    private func watchSeek(to target: Double) {
        seekWatch?.cancel()
        guard wantPlaying, current != nil else { return }
        let generation = loadGeneration
        let id = current?.id
        let from = Double(vlc.time.intValue) / 1000
        let fast = PlayerSettings.fastSeek
        seekWatch = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 300_000_000)
            guard let self, !Task.isCancelled, generation == self.loadGeneration else { return }
            let w0 = Date(), t0 = Double(self.vlc.time.intValue) / 1000
            let decoded0 = self.vlc.media?.numberOfDecodedAudioBlocks ?? 0
            let lost0 = self.vlc.media?.numberOfLostAudioBuffers ?? 0
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            guard !Task.isCancelled, generation == self.loadGeneration, self.current?.id == id, self.wantPlaying else { return }
            let wall = Date().timeIntervalSince(w0), t1 = Double(self.vlc.time.intValue) / 1000
            let stalled = max(0, wall - (t1 - t0))
            let decoded = (self.vlc.media?.numberOfDecodedAudioBlocks ?? 0) - decoded0
            let lost = (self.vlc.media?.numberOfLostAudioBuffers ?? 0) - lost0
            func f(_ x: Double) -> String { String(format: "%.1f", x) }
            self.log("seek diag: from \(f(from)) s to target \(f(target)) s, fast seek \(fast ? "on" : "off"); clock 0.3 s later \(f(t0)) s (\(f(target - t0)) s before target), \(f(wall)) s later \(f(t1)) s: playback advanced \(f(t1 - t0)) s (stood still ~\(f(stalled)) s), audio blocks +\(decoded), buffers lost +\(lost), video \(self.videoHidden ? "off" : "on"); \(self.envNote)")
        }
    }

    private func traceNote(_ text: String) {
        guard let t0 = traceT0, traceEvents.count < 30 else { return }
        traceEvents.append("\(text)@\(Int(Date().timeIntervalSince(t0) * 1000))")
    }

    /// Diagnostic only: WHERE the time goes after a seek. The seek diag above starts 0.3 s late and reads VLC's clock,
    /// which already shows the target right after the call, so it cannot see an audio break. This one starts at the seek
    /// call and samples every 50 ms for 4 s: VLC's clock, the audio buffers actually handed to the audio output
    /// (`playedAudioBuffers`), and bytes read from the input; plus the VLC state changes and the first time events with
    /// their ms offsets. One log line per seek (a new seek or load cancels the previous trace).
    private func traceSeek(to target: Double, label: String = "seek") {
        traceTask?.cancel()
        guard wantPlaying, current != nil, vlc.media != nil else { return }
        let t0 = Date()
        traceT0 = t0
        traceEvents = []
        traceTimeEvents = 0
        let generation = loadGeneration
        let id = current?.id
        let from = Double(vlc.time.intValue) / 1000
        let fast = PlayerSettings.fastSeek
        let video = videoHidden ? "off" : "on"
        let latency = AVAudioSession.sharedInstance().outputLatency
        traceTask = Task { [weak self] in
            var samples: [(ms: Int, clock: Int, played: Int, read: Int)] = []
            while !Task.isCancelled {
                guard let self, generation == self.loadGeneration, self.current?.id == id else { return }
                let ms = Int(Date().timeIntervalSince(t0) * 1000)
                if ms > 4000 { break }
                let st = self.vlc.media?.statistics
                samples.append((ms, Int(self.vlc.time.intValue), Int(st?.playedAudioBuffers ?? 0), Int(st?.readBytes ?? 0)))
                try? await Task.sleep(nanoseconds: 50_000_000)
            }
            guard let self, !Task.isCancelled, generation == self.loadGeneration, samples.count > 4 else { return }
            // longest stretch in which no audio buffer reached the audio output
            var lastChange = samples[0].ms
            var maxGap = 0, gapStart = 0
            var firstIncrease = -1
            for i in 1..<samples.count {
                if samples[i].played != samples[i - 1].played {
                    if firstIncrease < 0 { firstIncrease = samples[i].ms }
                    let gap = samples[i].ms - lastChange
                    if gap > maxGap { maxGap = gap; gapStart = lastChange }
                    lastChange = samples[i].ms
                }
            }
            let tail = samples[samples.count - 1].ms - lastChange
            if tail > maxGap { maxGap = tail; gapStart = lastChange }
            func at(_ ms: Int) -> (clock: Int, played: Int, read: Int) {
                let s = samples.last(where: { $0.ms <= ms }) ?? samples[0]
                return (s.clock, s.played, s.read)
            }
            let base = at(0)
            let marks = [0, 500, 1000, 1500, 2000, 2500, 3000, 3500]
            let clockTxt = marks.map { String(format: "%.1f", Double(at($0).clock) / 1000 - target) }.joined(separator: " ")
            let playedTxt = zip(marks, marks.dropFirst()).map { String(at($1).played - at($0).played) }.joined(separator: " ")
            let readAll = Double(samples[samples.count - 1].read - base.read) / 1e6
            let read1 = Double(at(1000).read - base.read) / 1e6
            func f(_ x: Double) -> String { String(format: "%.1f", x) }
            self.log("\(label) trace: from \(f(from)) s to \(f(target)) s, fast seek \(fast ? "on" : "off"), video \(video), output latency \(Int(latency * 1000)) ms | events [\(self.traceEvents.joined(separator: ", "))], \(self.traceTimeEvents) time events | audio buffers played: first increase at \(firstIncrease) ms, longest silent stretch \(maxGap) ms starting at \(gapStart) ms | clock minus target every 0.5 s [\(clockTxt)] | buffers played per 0.5 s [\(playedTxt)] | input read +\(f(readAll)) MB (first second +\(f(read1)) MB); \(self.envNote)")
            self.traceT0 = nil
        }
    }

    func seek(by delta: Double) {
        let upper = duration > 1 ? duration - 1 : position + max(delta, 0)
        seek(to: min(max(0, position + delta), upper))
    }

    func next() {
        connection?.command("next")
    }

    func previous() {
        connection?.command("prev", ["position": position])
    }

    /// How long the iPhone keeps a music video's picture track on after the app is left (fixed, no setting).
    private static let backgroundPictureGrace: TimeInterval = 60

    /// Leaving the app (Home button / lock): music keeps playing unless disabled in Settings; a music video
    /// keeps playing as audio only if its Settings switch is on, else it pauses. With the picture track switched off
    /// (tvOS never gets here) it needs a decoder restart on return, which waits for the next keyframe (1.6-7 s in this
    /// library) = the picture glitches for a few seconds. So on the iPhone the track stays ON for the first
    /// `backgroundPictureGrace` seconds: VLC's iOS video output skips drawing while the app is in the background
    /// (makeCurrent fails, the picture is released) and the decoder keeps running unseen, so a quick app switch finds
    /// the picture current. After that the track is switched off, so a long stretch (locked screen) costs no decoding;
    /// returning then restarts the picture as before. A video that LOADS while in the background still starts with
    /// the track off (the vout cannot be created then, see applyTracks).
    func appDidEnterBackground() {
        inBackground = true
        UserDefaults.standard.set("background", forKey: Self.lastStateKey)
        #if os(iOS)
        if lyricsKeptOn {  // lyrics view + app left: nobody will see the picture soon, stop decoding it
            lyricsKeptOn = false
            lyricsWatch?.cancel()
            hideVideo()
        }
        #endif
        guard wantPlaying else { return }
        if current?.isVideo == true {
            if PlayerSettings.keepVideoAudioInBackground {
                #if os(iOS)
                if !lyricsCover, !videoHidden {
                    pictureKeptOn = true
                    log("background: picture track kept on for \(Int(Self.backgroundPictureGrace)) s (VLC drops the frames itself), watching the clock; \(envNote)")
                    watchBackgroundPicture()
                    return
                }
                #endif
                hideVideo()
            } else {
                pause(source: "background")
            }
        } else if !PlayerSettings.keepMusicInBackground {
            pause(source: "background")
        }
    }

    /// Back in the app: bring the picture back.
    func appDidBecomeActive() {
        inBackground = false
        backgroundWatch?.cancel()
        UserDefaults.standard.set("foreground", forKey: Self.lastStateKey)
        if pictureKeptOn {
            pictureKeptOn = false
            if !videoHidden {
                log("foreground: picture track was kept on in the background, nothing to restart; \(envNote)")
                logPictureStats(after: 3, label: "3 s after returning with the picture kept on")
            }
        }
        if !lyricsCover { showVideo() }
    }

    #if os(iOS)
    /// Ends the "picture kept on" state in the background: after `backgroundPictureGrace` seconds, and at once (safety net)
    /// if the VLC clock stands still for 3 s while playback is wanted, so the sound never depends on this.
    private func watchBackgroundPicture() {
        backgroundWatch?.cancel()
        let generation = loadGeneration
        let started = Date()
        backgroundWatch = Task { [weak self] in
            var last: Int32 = -1
            var since = Date()
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 500_000_000)
                guard let self, generation == self.loadGeneration, self.inBackground, self.pictureKeptOn,
                      !self.videoHidden, self.wantPlaying else { return }
                if Date().timeIntervalSince(started) >= Self.backgroundPictureGrace {
                    self.log("background: \(Int(Self.backgroundPictureGrace)) s are up -> picture track off (decoding stops); \(self.envNote)")
                    self.pictureKeptOn = false
                    self.hideVideo()
                    return
                }
                let now = self.vlc.time.intValue
                if now != last {
                    last = now
                    since = Date()
                } else if Date().timeIntervalSince(since) >= 3 {
                    self.log("background: VLC clock frozen at \(now / 1000) s for 3 s with the picture kept on, state \(VLCMediaPlayerStateToString(self.vlc.state)) -> picture track off; \(self.envNote)")
                    self.pictureKeptOn = false
                    self.hideVideo()
                    return
                }
            }
        }
    }
    #endif

    /// How long the iPhone keeps decoding a music video's picture unseen after the lyrics view opens (fixed, no setting).
    private static let lyricsPictureGrace: TimeInterval = 60

    /// Apple TV: the lyrics screen is drawn over the video and the picture track is never switched off for it (mains
    /// power, so unseen decoding costs nothing that matters), so switching back never waits for a keyframe.
    #if os(tvOS)
    private static let pictureStaysOnUnderLyrics = true
    #else
    private static let pictureStaysOnUnderLyrics = false
    #endif

    /// The lyrics view is on screen instead of the picture (or not any more). iPhone: the picture track stays on for the
    /// first `lyricsPictureGrace` seconds, so switching back to the video within that time shows a clean picture at once
    /// (a track switched on mid-stream is blocky until the next keyframe); after that it is switched off (no unseen
    /// decoding while the lyrics stay up) and the return is covered until the next keyframe (coverUntilKeyframe).
    func setLyricsCover(_ on: Bool) {
        guard on != lyricsCover else { return }
        lyricsCover = on
        guard current?.isVideo == true else { return }
        if Self.pictureStaysOnUnderLyrics {
            if on {
                log("lyrics view: picture track stays on under the lyrics (Apple TV), switching back is instant; \(envNote)")
            } else if videoHidden {
                if !(inBackground && PlayerSettings.keepVideoAudioInBackground) { showVideo() }
            } else {
                log("lyrics view closed: the picture track stayed on, nothing to restart; \(envNote)")
            }
            return
        }
        if on {
            #if os(iOS)
            if !videoHidden {
                endPictureCover(reason: "lyrics view")
                lyricsKeptOn = true
                log("lyrics view: picture track kept on for \(Int(Self.lyricsPictureGrace)) s, switching back within that time is instant; \(envNote)")
                watchLyricsPicture()
                return
            }
            #endif
            hideVideo()
        } else {
            #if os(iOS)
            lyricsWatch?.cancel()
            if lyricsKeptOn {
                lyricsKeptOn = false
                if !videoHidden {
                    log("lyrics view closed within \(Int(Self.lyricsPictureGrace)) s: the picture track was kept on, nothing to restart; \(envNote)")
                    return
                }
            }
            #endif
            if !(inBackground && PlayerSettings.keepVideoAudioInBackground) {
                showVideo()
            }
        }
    }

    #if os(iOS)
    private func watchLyricsPicture() {
        lyricsWatch?.cancel()
        let generation = loadGeneration
        lyricsWatch = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(Self.lyricsPictureGrace * 1_000_000_000))
            guard let self, !Task.isCancelled, generation == self.loadGeneration, self.lyricsKeptOn,
                  self.lyricsCover, !self.videoHidden else { return }
            self.lyricsKeptOn = false
            self.log("lyrics view: \(Int(Self.lyricsPictureGrace)) s are up -> picture track off (decoding stops); \(self.envNote)")
            self.hideVideo()
        }
    }
    #endif

    /// The picture track just came back mid-stream: the decoder has no reference picture until the next keyframe and the
    /// picture is blocky meanwhile. Cover it (the views show the artwork) until playback has passed that keyframe, using
    /// the keyframe times from the server; unknown times = 2.5 s; never longer than 8 s of playback.
    private func coverUntilKeyframe() {
        guard let item = current, item.isVideo else { return }
        settleTask?.cancel()
        let now = Double(vlc.time.intValue) / 1000
        let next = KeyframeStore.shared.next(after: now + 0.05, for: item.id)
        let until: Double
        let why: String
        if let next {
            until = min(next + 0.25, now + 8)
            why = next.isFinite ? String(format: "next keyframe at %.1f s", next) : "no keyframe left in the file"
        } else {
            until = now + 2.5
            why = "keyframe times unknown"
        }
        pictureSettling = true
        let started = Date()
        log(String(format: "picture cover on at %.1f s until %.1f s (%@); ", now, until, why) + envNote)
        let generation = loadGeneration
        settleTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 100_000_000)
                guard let self, generation == self.loadGeneration, self.pictureSettling, !self.videoHidden else { return }
                if Double(self.vlc.time.intValue) / 1000 >= until { break }
            }
            guard let self, !Task.isCancelled, generation == self.loadGeneration, self.pictureSettling else { return }
            self.pictureSettling = false
            self.log(String(format: "picture cover off at %.1f s after %.1f s", Double(self.vlc.time.intValue) / 1000,
                            Date().timeIntervalSince(started)))
        }
    }

    private func endPictureCover(reason: String) {
        settleTask?.cancel()
        guard pictureSettling else { return }
        pictureSettling = false
        log("picture cover off (\(reason))")
    }

    private func hideVideo() {
        guard !videoHidden else { return }
        videoHidden = true
        endPictureCover(reason: "picture track off")
        vlc.currentVideoTrackIndex = -1
        #if os(iOS)
        updateNowPlaying()
        #endif
        log("video track off (\(lyricsCover ? "lyrics view" : "background")), audio keeps playing; \(envNote)")
    }

    private func showVideo() {
        guard videoHidden else { return }
        videoHidden = false
        if let track = trackIDs(vlc.videoTrackIndexes).first {
            vlc.currentVideoTrackIndex = track
        }
        #if os(iOS)
        updateNowPlaying()
        #endif
        log("foreground: video track back on; \(envNote)")
        checkPictureAfterSwitch()
        coverUntilKeyframe()
    }

    /// Turning the picture track on mid-stream can start the decoder off a keyframe. Fixes tried so far made it worse:
    /// v0.18.2 seeked to the current spot (a ~1 s gap plus a jump back with fast seek) and v0.18.5 reopened the item
    /// when the VLC picture counters said "choppy", but those counters read 0 right after the switch (and garbage after
    /// a reload), so it reopened on EVERY switch. Now the switch touches nothing; the counters are only logged.
    private func checkPictureAfterSwitch() {
        logPictureStats(after: 3, label: "3 s after the picture came back (log only, nothing is restarted)")
    }

    /// Displayed vs lost pictures over the next `seconds` (VLC media statistics): a choppy video shows lost pictures.
    func logPictureStats(after seconds: Double, label: String) {
        guard let media = vlc.media else { return }
        let shown0 = media.numberOfDisplayedPictures, lost0 = media.numberOfLostPictures
        let id = current?.id
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { [weak self] in
            guard let self, self.current?.id == id, let m = self.vlc.media else { return }
            let shown = m.numberOfDisplayedPictures - shown0, lost = m.numberOfLostPictures - lost0
            self.log("pictures \(label): \(shown) shown, \(lost) lost in \(Int(seconds)) s; \(self.envNote)")
        }
    }

    /// Re-read Settings. Subtitle style options need a new VLC instance; playback resumes at the same position.
    func applySettings() {
        let options = PlayerSettings.vlcOptions() + SoundSettings.widenerOptions()
        let sound = SoundSettings.configKey
        guard options != vlcOptions || sound != appliedSound else {
            tracksApplied = false
            applyTracks()
            return
        }
        vlcOptions = options
        appliedSound = sound
        let item = current
        let resumeAt = position
        let wasPlaying = wantPlaying
        rebuildPlayer("settings")
        if let item {
            load(item, position: resumeAt, autoplay: wasPlaying, present: false)
        }
    }

    /// Fresh VLC instance with the current options (subtitle style, EQ, widener).
    private func rebuildPlayer(_ why: String) {
        vlc.delegate = nil
        vlc.stop()
        vlc = Self.makePlayer(vlcOptions)
        videoHidden = false
        vlc.delegate = self
        vlc.drawable = videoView
        configureAudio()
        #if os(iOS)
        vlc.libraryInstance.loggers = [VLCLogTap.shared]
        #endif
        log("player rebuilt (\(why)): player \(playerTag); \(audioReport)")
    }

    // MARK: - VLC events

    fileprivate func handleState(from source: ObjectIdentifier?) {
        guard source == nil || source == ObjectIdentifier(vlc) else { return }  // event from a replaced player
        let state = vlc.state
        traceNote("state " + VLCMediaPlayerStateToString(state).replacingOccurrences(of: "VLCMediaPlayerState", with: ""))
        switch state {
        case .ended:
            if !startedPlaying {
                log("state Ended before playback started, wantPlaying \(wantPlaying), player \(playerTag)")
                checkStartSoon()
                setPlaying(false)
                break
            }
            finish()
        case .stopped:
            setPlaying(false)
            if !startedPlaying {
                log("state Stopped before playback started, wantPlaying \(wantPlaying), player \(playerTag)")
                checkStartSoon()
            } else if duration > 0 && position >= duration - 2 {
                finish()
            }
        case .paused:
            if let pending = pendingToggle, pending.wantPlaying, Date().timeIntervalSince(pending.at) < 2 {
                break  // stale pause event that arrived right after a play request
            }
            wantPlaying = false
            setPlaying(false)
        case .error:
            setPlaying(false)
            log("state Error, started \(startedPlaying), player \(playerTag)")
            if !startedPlaying {
                checkStartSoon()  // retry once with a new player, then report
            } else if let item = current {
                connection?.send(["type": "error", "item": item.id, "message": "VLC could not play the file"])
            }
        default:
            // opening / buffering / playing / esAdded. VLC flips to "buffering" during network
            // playback; while the user wants playback, those count as playing.
            if state == .playing || state == .esAdded {
                applyTracks()
            }
            setPlaying(state == .playing || wantPlaying)
        }
        updateNowPlaying()
    }

    fileprivate func handleTime(from source: ObjectIdentifier?) {
        guard source == nil || source == ObjectIdentifier(vlc) else { return }
        timeEvents += 1
        traceTimeEvents += 1
        if traceTimeEvents <= 3 { traceNote("time \(vlc.time.intValue)") }
        if !startedPlaying {
            startedPlaying = true
            startCheck?.cancel()
            startTimeout?.cancel()
        }
        position = Double(vlc.time.intValue) / 1000
        if let length = vlc.media?.length, length.intValue > 0 {
            duration = Double(length.intValue) / 1000
        }
        if !isPlaying && wantPlaying {  // time only advances while playing
            setPlaying(true)
            updateNowPlaying()
        }
    }

    private func setPlaying(_ value: Bool) {
        guard value != isPlaying else { return }
        isPlaying = value
        if let pending = pendingToggle, pending.wantPlaying == value {
            let ms = Int(Date().timeIntervalSince(pending.at) * 1000)
            connection?.send([
                "type": "log",
                "message": "\(value ? "play" : "pause") via \(pending.source): VLC state changed after \(ms) ms",
            ])
            pendingToggle = nil
        }
        report()
    }

    private func finish() {
        setPlaying(false)
        guard !endedSent, let item = current else { return }
        endedSent = true
        connection?.send(["type": "ended", "item": item.id])
    }

    private func report() {
        guard let item = current else { return }
        connection?.send([
            "type": "status", "item": item.id, "playing": isPlaying,
            "position": position, "duration": duration,
        ])
    }

    /// Select the audio track the server marked as default (FLAC in the MKVs) and the subtitle track
    /// for the chosen language (Settings → Lyrics / subtitles → Language).
    private func applyTracks() {
        guard !tracksApplied, let item = current else { return }
        let audio = trackIDs(vlc.audioTrackIndexes)
        let subtitles = trackIDs(vlc.videoSubTitlesIndexes)
        if item.isVideo && audio.isEmpty { return }  // streams not known yet, retry on the next event
        tracksApplied = true
        if item.isVideo, (lyricsCover && !Self.pictureStaysOnUnderLyrics)
            || (inBackground && PlayerSettings.keepVideoAudioInBackground) {
            videoHidden = false  // new media: its video track is on again, switch it off
            hideVideo()
        }
        if let ordinal = item.audioOrd, ordinal < audio.count {
            vlc.currentAudioTrackIndex = audio[ordinal]
        }
        if item.isVideo {
            if PlayerSettings.subtitlesOn, let ordinal = PlayerSettings.subtitleOrdinal(for: item),
               ordinal < subtitles.count {
                vlc.currentVideoSubTitleIndex = subtitles[ordinal]
            } else {
                vlc.currentVideoSubTitleIndex = -1
            }
        }
    }

    private func trackIDs(_ raw: [Any]?) -> [Int32] {
        (raw ?? []).compactMap { ($0 as? NSNumber)?.int32Value }.filter { $0 >= 0 }
    }

    // MARK: - Now Playing / remote commands

    private func setupRemoteCommands() {
        let center = MPRemoteCommandCenter.shared()
        center.playCommand.addTarget { [weak self] _ in
            MainActor.assumeIsolated { self?.play(source: "system") }
            return .success
        }
        center.pauseCommand.addTarget { [weak self] _ in
            MainActor.assumeIsolated { self?.pause(source: "system") }
            return .success
        }
        center.togglePlayPauseCommand.addTarget { [weak self] _ in
            MainActor.assumeIsolated { self?.togglePlay(source: "system") }
            return .success
        }
        center.nextTrackCommand.addTarget { [weak self] _ in
            MainActor.assumeIsolated { self?.next() }
            return .success
        }
        center.previousTrackCommand.addTarget { [weak self] _ in
            MainActor.assumeIsolated { self?.previous() }
            return .success
        }
        center.changePlaybackPositionCommand.addTarget { [weak self] event in
            guard let event = event as? MPChangePlaybackPositionCommandEvent else { return .commandFailed }
            let target = event.positionTime
            MainActor.assumeIsolated { self?.seek(to: target) }
            return .success
        }
    }

    /// iPhone: while only the sound of a video plays (background, lyrics view) it is "audio", because iOS can pause
    /// "video" playing in the background. Apple TV: always the item's real type. tvOS shows a system "Now Playing"
    /// banner ("To open player, hold ...") whenever the media type changes, so switching the lyrics view on and off for
    /// a video flashed it on every press (v0.18.0).
    private func nowPlayingMediaType(_ item: MediaItem) -> MPNowPlayingInfoMediaType {
        #if os(tvOS)
        return item.kind == "video" ? .video : .audio
        #else
        return item.isVideo && !videoHidden ? .video : .audio
        #endif
    }

    private func updateNowPlaying() {
        guard let item = current else {
            MPNowPlayingInfoCenter.default().nowPlayingInfo = nil
            return
        }
        var info: [String: Any] = [
            MPMediaItemPropertyTitle: item.title,
            MPMediaItemPropertyPlaybackDuration: duration,
            MPNowPlayingInfoPropertyElapsedPlaybackTime: position,
            MPNowPlayingInfoPropertyPlaybackRate: isPlaying ? 1.0 : 0.0,
            MPNowPlayingInfoPropertyMediaType: nowPlayingMediaType(item).rawValue,
        ]
        if let artist = item.artist { info[MPMediaItemPropertyArtist] = artist }
        if let album = item.album { info[MPMediaItemPropertyAlbumTitle] = album }
        if let artwork, artworkFor == item.id { info[MPMediaItemPropertyArtwork] = artwork }
        MPNowPlayingInfoCenter.default().nowPlayingInfo = info
    }

    private func fetchArtwork(for item: MediaItem) {
        artwork = nil
        artworkFor = nil
        guard item.art == true else { return }
        let id = item.id
        let url = item.artURL
        Task {
            guard let (data, response) = try? await Net.data(from: url),
                  (response as? HTTPURLResponse)?.statusCode == 200,
                  let image = UIImage(data: data),
                  self.current?.id == id else { return }
            self.artwork = MPMediaItemArtwork(boundsSize: image.size) { _ in image }
            self.artworkFor = id
            self.updateNowPlaying()
        }
    }
}

#if os(iOS)
extension PlayerEngine {
    /// Diagnostic: VLC's own audio messages for the next 5 s go to the server log as one entry (VLCLogTap.swift).
    fileprivate func vlcLogWindow(_ label: String) {
        let (id, previous) = VLCLogTap.shared.begin(label)
        if let previous { sendVLCLog(previous) }
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: 5_000_000_000)
            guard let self, let text = VLCLogTap.shared.end(id) else { return }
            self.sendVLCLog(text)
        }
    }

    /// The server keeps 1500 characters per log line: split into at most 3 parts.
    private func sendVLCLog(_ text: String) {
        var rest = Substring(text)
        var part = 1
        while !rest.isEmpty && part <= 3 {
            let chunk = rest.prefix(1400)
            log(part == 1 ? String(chunk) : "vlc log (part \(part)): " + chunk)
            rest = rest.dropFirst(chunk.count)
            part += 1
        }
    }
}

extension PlayerEngine {
    /// Phone calls / Siri pause and resume playback; unplugging headphones (or AirPods out) pauses,
    /// like every iOS music app.
    fileprivate func observeAudioSession() {
        let center = NotificationCenter.default
        center.addObserver(forName: AVAudioSession.interruptionNotification, object: nil, queue: .main) { [weak self] note in
            let info = note.userInfo ?? [:]
            let type = (info[AVAudioSessionInterruptionTypeKey] as? UInt).flatMap(AVAudioSession.InterruptionType.init)
            let options = AVAudioSession.InterruptionOptions(rawValue: info[AVAudioSessionInterruptionOptionKey] as? UInt ?? 0)
            MainActor.assumeIsolated {
                guard let self else { return }
                switch type {
                case .began:
                    self.resumeAfterInterruption = self.wantPlaying
                    if self.wantPlaying { self.pause(source: "interruption") }
                case .ended:
                    try? AVAudioSession.sharedInstance().setActive(true)
                    if self.resumeAfterInterruption, options.contains(.shouldResume) { self.play(source: "interruption") }
                    self.resumeAfterInterruption = false
                default:
                    break
                }
            }
        }
        center.addObserver(forName: AVAudioSession.routeChangeNotification, object: nil, queue: .main) { [weak self] note in
            let reason = (note.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt)
                .flatMap(AVAudioSession.RouteChangeReason.init)
            MainActor.assumeIsolated {
                guard let self else { return }
                self.vlcLogWindow("route change, reason \(reason.map { String($0.rawValue) } ?? "?") (1 new device, 2 device gone, 3 category, 4 override, 6 wake, 7 no route, 8 config)")
                guard reason == .oldDeviceUnavailable, self.wantPlaying else { return }
                self.pause(source: "headphones removed")
            }
        }
    }
}
#endif

extension PlayerEngine: VLCMediaPlayerDelegate {
    nonisolated func mediaPlayerStateChanged(_ aNotification: Notification) {
        let source = (aNotification.object as AnyObject?).map(ObjectIdentifier.init)
        Task { @MainActor in self.handleState(from: source) }
    }

    nonisolated func mediaPlayerTimeChanged(_ aNotification: Notification) {
        let source = (aNotification.object as AnyObject?).map(ObjectIdentifier.init)
        Task { @MainActor in self.handleTime(from: source) }
    }
}
