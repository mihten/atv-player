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
    weak var engine: PlayerEngine?
    /// Called after a few failed connection attempts in a row (the server may have a new address).
    var onUnreachable: (() -> Void)?

    private var task: URLSessionWebSocketTask?
    private var retryDelay: Double = 1
    private var failures = 0

    func start() {
        if task == nil { connect() }
    }

    /// New server address (or a dead socket after the app was suspended).
    func restart() {
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
        connected = false
        retryDelay = 1
        connect()
    }

    /// Foreground again: iOS may have suspended the app with the socket open; check it is alive.
    func resume() {
        guard let task else { connect(); return }
        task.sendPing { [weak self] error in
            guard error != nil else { return }
            Task { @MainActor in
                guard let self, self.task === task else { return }
                self.restart()
            }
        }
    }

    private func connect() {
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

    private func handle(_ data: Data) {
        guard let message = try? JSONDecoder.server().decode(ServerMessage.self, from: data) else { return }
        switch message.type {
        case "state":
            if let newState = message.state { state = newState }
        case "load":
            if let item = message.item {
                engine?.load(item, position: message.position ?? 0, autoplay: message.autoplay ?? true,
                             present: message.present ?? false)
            }
        case "control":
            if let action = message.action { engine?.control(action, position: message.position) }
        case "playlists":
            playlistsVersion += 1
        default:
            break
        }
    }

    func send(_ object: [String: Any]) {
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
