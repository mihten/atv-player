import Foundation

/// WebSocket link to the media server (role=player, kind=tv|phone). The server owns this device's queue
/// (the Apple TV and the iPhone each have their own); this side plays what it is told and reports state
/// changes. No polling: messages flow only on events, plus WebSocket keepalive pings from the server.
@MainActor
final class ServerConnection: ObservableObject {
    @Published private(set) var connected = false
    /// The start-up note (how the previous run ended) goes to the server log once per launch.
    private var launchReported = false
    @Published private(set) var state: SessionState?
    /// Bumped when the server says playlists changed (web remote edits); playlist screens reload on it.
    @Published private(set) var playlistsVersion = 0
    /// Bumped when a ♥ changed (server {"type": "auto"}, or LocalQueue); auto-playlist screens reload on it.
    @Published private(set) var autoVersion = 0
    weak var engine: PlayerEngine?
    /// Called after a few failed connection attempts in a row (the server may have a new address).
    var onUnreachable: (() -> Void)?
    /// No-server mode: the queue lives on the device and answers commands and player reports.
    private(set) var local: LocalQueue?

    private var task: URLSessionWebSocketTask?
    private var retryDelay: Double = 1
    private var failures = 0
    /// Server time for the atv-audio clock messages (ping / pong, Apple TV only).
    let serverClock = ServerClock()
    private var pingTask: Task<Void, Never>?
    /// The engine has seen state.sink from the current socket (atv-audio).
    private var sinkSynced = false
    /// The socket whose first message has arrived (pings and the atv-audio config start then).
    private var openedSocket: URLSessionWebSocketTask?

    /// Idempotent: the app and a Siri intent (app launched in the background) may both call it.
    func start() {
        if Config.localMode {
            if local == nil { startLocal() }
            return
        }
        if task == nil { connect() }
    }

    /// New server address (or a dead socket after the app was suspended).
    func restart() {
        if Config.localMode { startLocal(); return }
        stopLocal()
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
        openedSocket = nil
        pingTask?.cancel()
        #if os(tvOS)
        engine?.serverConnectionLost()  // the old socket's failure is ignored now (task !== socket)
        #endif
        connected = false
        retryDelay = 1
        connect()
    }

    /// Foreground again: iOS may have suspended the app with the socket open; check it is alive.
    func resume() {
        guard !Config.localMode else { return }
        guard let task else { connect(); return }
        task.sendPing { [weak self] error in
            guard error != nil else { return }
            Task { @MainActor in
                guard let self, self.task === task else { return }
                self.restart()
            }
        }
    }

    /// Siri woke the app in the background (no scene, so `resume()` did not run): a socket kept from before a
    /// suspension may be dead. Ping it; no answer within `timeout` s = reconnect.
    func ensureAlive(timeout: Double = 3) async {
        guard !Config.localMode, let socket = task, connected else { return }
        final class Answer: @unchecked Sendable { var ok: Bool? }
        let answer = Answer()
        socket.sendPing { error in DispatchQueue.main.async { answer.ok = error == nil } }
        let deadline = Date().addingTimeInterval(timeout)
        while answer.ok == nil, Date() < deadline {
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        guard task === socket, answer.ok != true else { return }
        restart()
    }

    /// No-server mode: no socket; LocalQueue plays the saved queue and the library is scanned on the device.
    func startLocal() {
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
        pingTask?.cancel()
        openedSocket = nil
        retryDelay = 1
        failures = 0
        connected = true
        state = nil
        let queue = LocalQueue(engine: engine, publish: { [weak self] newState in
            self?.state = newState
            if let item = newState.current { self?.engine?.refreshLinks(item) }
            #if os(iOS)
            self?.engine?.syncSystemControls()
            #endif
        }, autoChanged: { [weak self] in
            self?.autoVersion += 1
        })
        local = queue
        LocalLibrary.shared.start()
        queue.start()
        if !launchReported {
            launchReported = true
            engine?.reportLaunch()
        }
    }

    /// Leaving no-server mode (the app also unloads the player).
    func stopLocal() {
        guard local != nil else { return }
        local = nil
        connected = false
        state = nil
    }

    private func connect() {
        guard !Config.localMode else { return }
        var components = URLComponents(url: Config.serverURL, resolvingAgainstBaseURL: false)!
        components.scheme = Config.serverURL.scheme == "https" ? "wss" : "ws"
        components.path = "/ws"
        components.queryItems = [
            URLQueryItem(name: "role", value: "player"),
            URLQueryItem(name: "kind", value: Config.deviceKind),
        ]
        let newTask = URLSession.shared.webSocketTask(with: Net.request(components.url!))
        task = newTask
        newTask.resume()
        listen(newTask)
    }

    private func listen(_ socket: URLSessionWebSocketTask) {
        socket.receive { [weak self] result in
            Task { @MainActor in
                guard let self, self.task === socket else { return }
                switch result {
                case .success(let message):
                    self.connected = true
                    self.retryDelay = 1
                    self.failures = 0
                    if !self.launchReported {
                        self.launchReported = true
                        self.engine?.reportLaunch()
                    }
                    if self.openedSocket !== socket {
                        self.openedSocket = socket
                        self.socketOpened(socket)
                    }
                    switch message {
                    case .string(let text):
                        self.handle(Data(text.utf8))
                    case .data(let data):
                        self.handle(data)
                    @unknown default:
                        break
                    }
                    self.listen(socket)
                case .failure:
                    self.connected = false
                    self.task = nil
                    self.openedSocket = nil
                    self.pingTask?.cancel()
                    #if os(tvOS)
                    self.engine?.serverConnectionLost()
                    #endif
                    socket.cancel()
                    self.failures += 1
                    if self.failures == 3 { self.onUnreachable?() }
                    let delay = self.retryDelay
                    self.retryDelay = min(self.retryDelay * 2, 30)
                    Task { @MainActor in
                        try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
                        if self.task == nil { self.connect() }
                    }
                }
            }
        }
    }

    /// First message on a new socket: Apple TV pings for the server clock (2 s for the first 8, then every 15 s) and
    /// sends its atv-audio config (route, sound, lip sync) for the sink.
    private func socketOpened(_ socket: URLSessionWebSocketTask) {
        #if os(tvOS)
        pingTask?.cancel()
        serverClock.reset()
        sinkSynced = false
        pingTask = Task { [weak self] in
            var count = 0
            while !Task.isCancelled {
                guard let self, self.task === socket, !Config.localMode else { return }
                self.send(["type": "ping", "n": self.serverClock.nextPing()])
                count += 1
                try? await Task.sleep(nanoseconds: count < 8 ? 2_000_000_000 : 15_000_000_000)
            }
        }
        engine?.sendSinkConfig()
        #endif
    }

    private func handle(_ data: Data) {
        guard let message = try? JSONDecoder.server().decode(ServerMessage.self, from: data) else { return }
        switch message.type {
        case "state":
            if let newState = message.state {
                let oldSink = state?.sink
                state = newState
                if let item = newState.current { engine?.refreshLinks(item) }
                #if os(iOS)
                engine?.syncSystemControls()
                #endif
                #if os(tvOS)
                // Also the first state on a new socket (the old one may be stale): the engine re-checks atv-audio.
                if oldSink != newState.sink || !sinkSynced {
                    sinkSynced = true
                    engine?.sinkChanged(from: oldSink, to: newState.sink)
                }
                #endif
            }
        case "load":
            if let item = message.item {
                engine?.load(item, position: message.position ?? 0, autoplay: message.autoplay ?? true,
                             present: message.present ?? false, fromServer: true)
            }
        case "control":
            if let action = message.action { engine?.control(action, position: message.position) }
        case "playlists":
            playlistsVersion += 1
        case "auto":
            autoVersion += 1
        case "pong":
            if let n = message.n, let t = message.t { serverClock.pong(n: n, t: t) }
        case "sink_ready":
            #if os(tvOS)
            if let id = message.id { engine?.sinkReady(id) }
            #endif
            break
        default:
            break
        }
    }

    func send(_ object: [String: Any]) {
        if let local {
            if (object["type"] as? String) == "cmd" {
                local.handleCommand(object)
            } else {
                local.playerMessage(object)
            }
            return
        }
        guard let task,
              let data = try? JSONSerialization.data(withJSONObject: object),
              let text = String(data: data, encoding: .utf8) else { return }
        task.send(.string(text)) { _ in }
    }

    func command(_ name: String, _ arguments: [String: Any] = [:]) {
        var message = arguments
        message["type"] = "cmd"
        message["cmd"] = name
        send(message)
    }
}
