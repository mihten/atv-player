#if os(iOS)
import Foundation
import Network

/// Away from home VLC does not fetch the stream itself: it plays `http://127.0.0.1:<port>/media/<id>` and this relay
/// fetches the bytes with URLSession (HTTP/2: one TLS connection to the internet address carries every request, the
/// login goes in a header). VLC's own HTTPS opened 5-8 new TLS connections per item and more per seek; on mobile data
/// (carrier NAT, few ports) those setups often hung for 15 s and longer. Every new item cancels the requests of the
/// previous one, and requests for any other item are refused, so a replaced player cannot keep downloading.
final class StreamRelay: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    static let shared = StreamRelay()

    /// Bytes handed to the local socket but not yet taken by VLC: above `highWater` the download pauses,
    /// below `lowWater` it goes on (VLC reads ahead ~16 MiB, then only as fast as it plays).
    private static let highWater = 4 << 20
    private static let lowWater = 1 << 20

    private let queue = DispatchQueue(label: "atv.relay")
    private var session: URLSession!
    private var listener: NWListener?
    private var port: UInt16 = 0
    private var log: ((String) -> Void)?
    /// The item whose requests are served (nil = none: home network or nothing playing).
    private var focusID: Int?
    private var origin: URL?
    private var authorization: String?
    /// Open requests by URLSession task id. Only touched on `queue`.
    private var exchanges: [Int: Exchange] = [:]

    private final class Exchange {
        let connection: NWConnection
        let itemID: Int
        let label: String
        let started = Date()
        var task: URLSessionDataTask?
        var firstByteMs: Int?
        var status = 0
        var bytes = 0
        var pending = 0
        var maxPending = 0
        var suspended = false
        var closed = false

        init(connection: NWConnection, itemID: Int, label: String) {
            self.connection = connection
            self.itemID = itemID
            self.label = label
        }
    }

    private override init() {
        super.init()
        let config = URLSessionConfiguration.ephemeral
        config.urlCache = nil
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.timeoutIntervalForRequest = 30
        let delegateQueue = OperationQueue()
        delegateQueue.underlyingQueue = queue
        delegateQueue.maxConcurrentOperationCount = 1
        session = URLSession(configuration: config, delegate: self, delegateQueue: delegateQueue)
    }

    /// Starts listening on the loopback interface (idempotent). `log` lines go to the server log.
    func start(log: @escaping (String) -> Void) {
        queue.async {
            self.log = log
            self.startListener()
        }
    }

    /// The local URL VLC opens instead of `direct` (the internet address), or nil while the relay is not ready
    /// (VLC then fetches directly as before). Cancels every open request: they belong to the media being replaced.
    func url(for direct: URL, itemID: Int, login: (user: String, password: String)) -> URL? {
        queue.sync {
            guard port != 0, var components = URLComponents(url: direct, resolvingAgainstBaseURL: false) else {
                startListener()
                return nil
            }
            var base = components
            base.path = ""
            base.query = nil
            base.user = nil
            base.password = nil
            origin = base.url
            authorization = Net.basic(user: login.user, password: login.password)
            focusID = itemID
            closeAll(reason: "new media")
            components.scheme = "http"
            components.host = "127.0.0.1"
            components.port = Int(port)
            components.user = nil
            components.password = nil
            return components.url
        }
    }

    /// Not streaming through the relay any more (home network, local files): drop everything still open.
    func release() {
        queue.async {
            self.focusID = nil
            self.closeAll(reason: "not in use")
        }
    }

    // MARK: - Listener

    private func startListener() {
        guard listener == nil else { return }
        let parameters = NWParameters.tcp
        parameters.requiredInterfaceType = .loopback
        parameters.allowLocalEndpointReuse = true
        guard let newListener = try? NWListener(using: parameters) else {
            log?("relay: could not create the listener")
            return
        }
        newListener.stateUpdateHandler = { [weak self, weak newListener] state in
            guard let self, let newListener else { return }
            switch state {
            case .ready:
                self.port = newListener.port?.rawValue ?? 0
                self.log?("relay: ready on port \(self.port)")
            case .failed(let error):
                self.log?("relay: listener failed (\(error)), restarting")
                newListener.cancel()
                if self.listener === newListener {
                    self.listener = nil
                    self.port = 0
                }
                self.queue.asyncAfter(deadline: .now() + 1) { self.startListener() }
            default:
                break
            }
        }
        newListener.newConnectionHandler = { [weak self] connection in self?.accept(connection) }
        listener = newListener
        newListener.start(queue: queue)
    }

    private static func isLoopback(_ endpoint: NWEndpoint) -> Bool {
        guard case .hostPort(let host, _) = endpoint else { return false }
        switch host {
        case .ipv4(let address): return address.isLoopback
        case .ipv6(let address): return address.isLoopback
        default: return false
        }
    }

    private func accept(_ connection: NWConnection) {
        guard Self.isLoopback(connection.endpoint) else {
            connection.cancel()
            return
        }
        connection.stateUpdateHandler = { [weak connection] state in
            if case .failed = state { connection?.cancel() }  // a pending receive then ends the exchange
        }
        connection.start(queue: queue)
        readRequest(connection, buffer: Data())
    }

    private func readRequest(_ connection: NWConnection, buffer: Data) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 16_384) { [weak self] data, _, isComplete, error in
            guard let self else { return }
            var buffer = buffer
            if let data { buffer.append(data) }
            if let end = buffer.range(of: Data("\r\n\r\n".utf8)) {
                self.handle(String(decoding: buffer[..<end.lowerBound], as: UTF8.self), connection)
            } else if error != nil || isComplete || buffer.count > 32_768 {
                connection.cancel()
            } else {
                self.readRequest(connection, buffer: buffer)
            }
        }
    }

    // MARK: - Requests

    private func handle(_ head: String, _ connection: NWConnection) {
        let lines = head.components(separatedBy: "\r\n")
        let parts = lines.first?.split(separator: " ").map(String.init) ?? []
        guard parts.count >= 2, parts[0] == "GET" || parts[0] == "HEAD",
              let origin, let authorization, let focusID,
              let id = Self.mediaID(parts[1]), id == focusID,
              let target = URL(string: origin.absoluteString + parts[1]) else {
            reject(connection)
            return
        }
        let range = lines.dropFirst().first { $0.lowercased().hasPrefix("range:") }
            .map { String($0.dropFirst("range:".count)).trimmingCharacters(in: .whitespaces) }
        var request = URLRequest(url: target)
        request.httpMethod = parts[0]
        request.setValue(authorization, forHTTPHeaderField: "Authorization")
        if let range { request.setValue(range, forHTTPHeaderField: "Range") }
        let exchange = Exchange(connection: connection, itemID: id, label: range ?? "whole file")
        let task = session.dataTask(with: request)
        exchange.task = task
        exchanges[task.taskIdentifier] = exchange
        watchClient(exchange)
        task.resume()
    }

    /// `/media/<id>` or `/media/<id>?...` -> id.
    private static func mediaID(_ path: String) -> Int? {
        let plain = path.split(separator: "?", maxSplits: 1).first.map(String.init) ?? path
        guard plain.hasPrefix("/media/") else { return nil }
        return Int(plain.dropFirst("/media/".count))
    }

    private func reject(_ connection: NWConnection) {
        let reply = Data("HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".utf8)
        connection.send(content: reply, contentContext: .finalMessage, isComplete: true, completion: .contentProcessed { _ in
            connection.cancel()
        })
    }

    /// VLC closes the socket on a seek or a new item: then the download stops too.
    private func watchClient(_ exchange: Exchange) {
        exchange.connection.receive(minimumIncompleteLength: 1, maximumLength: 4096) { [weak self] _, _, isComplete, error in
            guard let self, !exchange.closed else { return }
            if isComplete || error != nil {
                self.close(exchange, reason: "player closed it")
            } else {
                self.watchClient(exchange)
            }
        }
    }

    private func send(_ data: Data, _ exchange: Exchange) {
        exchange.pending += data.count
        exchange.maxPending = max(exchange.maxPending, exchange.pending)
        exchange.connection.send(content: data, completion: .contentProcessed { [weak self] error in
            guard let self else { return }
            exchange.pending -= data.count
            if error != nil {
                self.close(exchange, reason: "player closed it")
            } else if exchange.suspended, !exchange.closed, exchange.pending < Self.lowWater {
                exchange.suspended = false
                exchange.task?.resume()
            }
        })
    }

    /// Everything queued is sent, then the socket closes (one request per connection).
    private func finish(_ exchange: Exchange) {
        exchange.closed = true
        let connection = exchange.connection
        connection.send(content: nil, contentContext: .finalMessage, isComplete: true, completion: .contentProcessed { _ in
            connection.cancel()
        })
    }

    private func close(_ exchange: Exchange, reason: String) {
        guard !exchange.closed else { return }
        exchange.closed = true
        exchange.task?.cancel()
        exchange.connection.cancel()
        report(exchange, end: reason)
    }

    private func closeAll(reason: String) {
        for exchange in exchanges.values { close(exchange, reason: reason) }
    }

    private func report(_ exchange: Exchange, end: String) {
        let ms = Int(Date().timeIntervalSince(exchange.started) * 1000)
        let first = exchange.firstByteMs.map { "first byte \($0) ms" } ?? "no answer"
        log?("relay: item \(exchange.itemID) \(exchange.label) -> \(exchange.status), \(first), \(exchange.bytes) bytes in \(ms) ms, peak buffered \(exchange.maxPending >> 10) KiB, \(end)")
    }

    private static func reason(_ code: Int) -> String {
        switch code {
        case 200: return "OK"
        case 206: return "Partial Content"
        case 301, 302, 307, 308: return "Found"
        case 401: return "Unauthorized"
        case 404: return "Not Found"
        case 416: return "Range Not Satisfiable"
        default: return HTTPURLResponse.localizedString(forStatusCode: code).capitalized
        }
    }

    // MARK: - URLSessionDataDelegate (on `queue`)

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        var next = request
        if next.url?.host == origin?.host, let authorization {
            next.setValue(authorization, forHTTPHeaderField: "Authorization")  // the server redirects ?audio= requests
        }
        completionHandler(next)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        guard let exchange = exchanges[dataTask.taskIdentifier], !exchange.closed,
              let http = response as? HTTPURLResponse else {
            completionHandler(.cancel)
            return
        }
        exchange.status = http.statusCode
        exchange.firstByteMs = Int(Date().timeIntervalSince(exchange.started) * 1000)
        var head = "HTTP/1.1 \(http.statusCode) \(Self.reason(http.statusCode))\r\n"
        for name in ["Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Last-Modified", "ETag"] {
            if let value = http.value(forHTTPHeaderField: name) { head += "\(name): \(value)\r\n" }
        }
        head += "Connection: close\r\n\r\n"
        send(Data(head.utf8), exchange)
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        guard let exchange = exchanges[dataTask.taskIdentifier], !exchange.closed else { return }
        exchange.bytes += data.count
        send(data, exchange)
        if exchange.pending > Self.highWater, !exchange.suspended {
            exchange.suspended = true
            dataTask.suspend()
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard let exchange = exchanges.removeValue(forKey: task.taskIdentifier), !exchange.closed else { return }
        if let error {
            if exchange.firstByteMs == nil {
                let reply = "HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                send(Data(reply.utf8), exchange)
            }
            report(exchange, end: "failed: \(error.localizedDescription)")
        } else {
            report(exchange, end: "complete")
        }
        finish(exchange)
    }
}
#endif
