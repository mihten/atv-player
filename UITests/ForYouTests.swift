import XCTest

/// Siri Remote checks of the auto-playlists ("For you") on the tvOS simulator (CI, design workflow with ui_test=true).
/// Needs the throwaway demo server on 127.0.0.1:8080 with something in the TV queue. Plays nothing, changes nothing.
/// Every step prints a "UITEST:" line and saves a screenshot to $UI_SHOTS.
final class ForYouTests: XCTestCase {
    private let app = XCUIApplication()
    private let remote = XCUIRemote.shared

    override func setUp() {
        continueAfterFailure = true
    }

    /// Home of the Aurora look (no -demoRoute).
    private func launchHome() {
        app.terminate()
        app.launchArguments = ["-serverAddress", "127.0.0.1:8080", "-appTheme", "aurora"]
        app.launch()
        sleep(7)
    }

    private func press(_ button: XCUIRemote.Button, wait: UInt32 = 2) {
        remote.press(button)
        sleep(wait)
    }

    private var focused: String {
        let element = app.descendants(matching: .any).element(matching: NSPredicate(format: "hasFocus == true"))
        guard element.exists else { return "<nothing>" }
        let label = element.label
        return label.isEmpty ? "<\(element.elementType.rawValue) without label>" : label
    }

    /// Card / row names of the auto-playlists (server AUTO_LISTS).
    private let autoNames = ["Loved", "Most played", "Recently added", "Not heard in a while", "On this day"]
    private var onAutoList: Bool { autoNames.contains { focused.contains($0) } }

    /// ▼ from the top of Home to the For you shelf, then ◀ to its first card (Loved).
    private func goToForYou() {
        for _ in 0..<4 where !onAutoList {
            press(.down)
        }
        for _ in 0..<4 where !focused.contains("Loved") {
            press(.left)
        }
    }

    /// The list screen of an auto-playlist with items: its "Play (N)" button.
    private var playButton: XCUIElement {
        app.buttons.matching(NSPredicate(format: "label BEGINSWITH %@", "Play (")).firstMatch
    }

    private func report(_ step: String) {
        let line = "\(step): app \(app.state == .runningForeground ? "foreground" : "NOT foreground (\(app.state.rawValue))"), "
            + "focus '\(focused)', play button \(playButton.exists)"
        print("UITEST: \(line)")
        if let dir = ProcessInfo.processInfo.environment["UI_SHOTS"] {
            let name = step.replacingOccurrences(of: " ", with: "-").filter { $0.isLetter || $0.isNumber || $0 == "-" }
            try? XCUIScreen.main.screenshot().pngRepresentation.write(to: URL(fileURLWithPath: "\(dir)/\(name).png"))
        }
    }

    /// Home: ▼ from the top reaches the "For you" shelf (Loved is its first card), ▶ reaches "Recently added"; a click
    /// pushes the list (focus on Play, ▼ Shuffle); Back returns to the same card on Home and the app stays open.
    func test1HomeShelf() {
        launchHome()
        report("1a home")
        goToForYou()
        report("1b for you shelf")
        XCTAssertTrue(focused.contains("Loved"), "▼ should reach the Loved card of For you, has '\(focused)'")
        for _ in 0..<4 where !focused.contains("Recently added") {
            press(.right)
        }
        report("1c recently added card")
        XCTAssertTrue(focused.contains("Recently added"), "▶ should reach Recently added, has '\(focused)'")
        press(.select, wait: 3)
        report("1d recently added list")
        XCTAssertTrue(playButton.exists, "the list screen should show Play")
        XCTAssertTrue(focused.contains("Play ("), "the first focus should be on Play, has '\(focused)'")
        press(.down)
        report("1e shuffle button")
        XCTAssertTrue(focused.contains("Shuffle"), "▼ from Play should reach Shuffle, has '\(focused)'")
        press(.menu)
        report("1f after Back")
        XCTAssertEqual(app.state, .runningForeground)
        XCTAssertFalse(playButton.exists, "Back should close the list screen")
        XCTAssertTrue(focused.contains("Recently added"), "Back should return to the card, has '\(focused)'")
    }

    /// Browse → Playlists: "For you" rows on top (Loved first). Loved may be empty (a screen with only its hint row and
    /// the Now Playing card; the hint row takes the focus); Back from it returns to Playlists, a second Back to Home,
    /// and the app stays open.
    func test2PlaylistsSection() {
        launchHome()
        goToForYou()
        press(.up)  // Browse, above the shelf; Playlists is its first tile
        for _ in 0..<4 where !focused.contains("Playlists") {
            press(.left)
        }
        report("2a playlists tile")
        XCTAssertTrue(focused.contains("Playlists"), "▼ should reach the Playlists tile of Browse, has '\(focused)'")
        press(.select, wait: 3)
        report("2b playlists screen")
        XCTAssertTrue(focused.contains("Loved"), "the first row of Playlists should be Loved, has '\(focused)'")
        press(.select, wait: 3)
        report("2c loved list")
        let empty = app.descendants(matching: .any)
            .matching(NSPredicate(format: "label BEGINSWITH %@", "Nothing loved yet")).firstMatch
        XCTAssertTrue(empty.exists || playButton.exists, "the Loved screen should open (its text or Play)")
        // Empty: the hint row itself takes the focus (with nothing playing there is no Now Playing card to go to).
        XCTAssertNotEqual(focused, "<nothing>", "the Loved screen should have a focused view")
        if empty.exists { print("UITEST: 2c empty Loved, hint row focused: \(focused.hasPrefix("Nothing loved yet"))") }
        press(.menu)
        report("2d back to playlists")
        XCTAssertEqual(app.state, .runningForeground)
        XCTAssertTrue(focused.contains("Loved"), "Back should return to the Loved row, has '\(focused)'")
        press(.menu)
        report("2e back to home")
        XCTAssertEqual(app.state, .runningForeground)
        XCTAssertTrue(focused.contains("Playlists"), "Back should return to the Playlists tile, has '\(focused)'")
    }
}
