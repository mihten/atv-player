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
    private var startCheck: Task<Void, Never>?
    private var startTimeout: Task<Void, Never>?
    /// iOS: playback was running when a call/Siri interrupted it.
    fileprivate var resumeAfterInterruption = false
    /// The app is not in the foreground; the video track of a playing music video is switched off.
    private var inBackground = false
    private var videoHidden = false

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
                "message": "\(self.audioReport); progress check '\(item.title)': timeEvents=\(self.timeEvents) vlcTime \(startTime)->\(self.vlc.time.intValue) ms clock=\(Int(self.clock.position)) s state=\(VLCMediaPlayerStateToString(self.vlc.state)) playing=\(self.isPlaying); \(self.mediaNote)",
            ])
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
        switch vlc.state {
        case .stopped, .ended, .error:
            // The stream was closed (long pause, network drop): reopen at the same position.
            if let item = current {
                load(item, position: position, autoplay: true, present: false)
            }
        default:
            vlc.play()
        }
    }

    func pause(source: String) {
        logRequest("pause", source)
        wantPlaying = false
        pendingToggle = (Date(), source, false)
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
        vlc.time = VLCTime(int: Int32(target * 1000))
        position = target
        clock.position = target
        report()
        updateNowPlaying()
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

    /// Leaving the app (Home button / lock): music keeps playing unless disabled in Settings; a music video
    /// keeps playing as audio only if its Settings switch is on (the picture is switched off, because iOS
    /// does not render video in the background and VLC would stall), else it pauses.
    func appDidEnterBackground() {
        inBackground = true
        UserDefaults.standard.set("background", forKey: Self.lastStateKey)
        guard wantPlaying else { return }
        if current?.isVideo == true {
            if PlayerSettings.keepVideoAudioInBackground {
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
        UserDefaults.standard.set("foreground", forKey: Self.lastStateKey)
        showVideo()
    }

    private func hideVideo() {
        guard !videoHidden else { return }
        videoHidden = true
        vlc.currentVideoTrackIndex = -1
        updateNowPlaying()
        log("background: video track off, audio keeps playing; \(envNote)")
    }

    private func showVideo() {
        guard videoHidden else { return }
        videoHidden = false
        if let track = trackIDs(vlc.videoTrackIndexes).first {
            vlc.currentVideoTrackIndex = track
        }
        updateNowPlaying()
        log("foreground: video track back on; \(envNote)")
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
        log("player rebuilt (\(why)): player \(playerTag); \(audioReport)")
    }

    // MARK: - VLC events

    fileprivate func handleState(from source: ObjectIdentifier?) {
        guard source == nil || source == ObjectIdentifier(vlc) else { return }  // event from a replaced player
        let state = vlc.state
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
        if inBackground, item.isVideo, PlayerSettings.keepVideoAudioInBackground {
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
            // While only the sound plays in the background it is audio: iOS can pause "video" playing in the background.
            MPNowPlayingInfoPropertyMediaType: (item.isVideo && !videoHidden ? MPNowPlayingInfoMediaType.video : .audio).rawValue,
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
                guard let self, reason == .oldDeviceUnavailable, self.wantPlaying else { return }
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
