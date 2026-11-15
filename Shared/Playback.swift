import Foundation

/// The ONLY way the apps start a list, folder or collection (playlist, For you list, folder, music videos, the Home
/// shuffle tiles): always shuffled; a picked entry (`start_id` / `start_index`) plays first. There is deliberately no
/// shuffle parameter (owner's rule; CI greps `Sources/` and `iOS/` for other `play_collection` sends). Repeat is always
/// "all" (server 0.30.0 and LocalQueue: no setting), shuffle can be turned off afterwards in the player.
extension ServerConnection {
    func startPlayback(_ arguments: [String: Any]) {
        var args = arguments
        args["shuffle"] = true
        command("play_collection", args)
    }

    /// Home tile Mix 1 / Mix 2: every folder and playlist of that mix, each file once, shuffled (server only).
    func startMix(_ number: Int) {
        startPlayback(["collection": "mix", "mix": number])
    }
}

/// Wording of the play buttons, shared by both apps so it cannot drift apart again. The item count goes in the row's
/// secondary text, not in the label.
enum PlayLabels {
    static let play = "Play"
    static let includingSubfolders = "Play (including subfolders)"
    static let onlyThisFolder = "Play (only this folder)"

    static func count(_ n: Int) -> String { n == 1 ? "1 item" : "\(n) items" }
}
