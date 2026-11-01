import Foundation

/// The ONLY way the apps start a list, folder or collection (playlist, For you list, folder, music videos, the Home
/// shuffle tiles): always shuffled with repeat all; a picked entry (`start_id` / `start_index`) plays first. There is
/// deliberately no shuffle parameter (owner's rule; CI greps `Sources/` and `iOS/` for other `play_collection` sends).
/// Shuffle and repeat can be changed afterwards in the player. LocalQueue / LocalAPI honour both in no-server mode.
extension ServerConnection {
    func startPlayback(_ arguments: [String: Any]) {
        var args = arguments
        args["shuffle"] = true
        command("play_collection", args)
        command("repeat", ["mode": "all"])
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
