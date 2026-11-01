import XCTest

/// Siri Remote checks of the player's quick panel (hold the centre button) on the tvOS simulator (CI, design workflow
/// with ui_test=true). Needs the throwaway demo server on 127.0.0.1:8080 with something in the TV queue.
/// Every step prints a "UITEST:" line and saves a screenshot to $UI_SHOTS. Settings changed here are clicked back.
final class QuickPanelTests: XCTestCase {
    private let app = XCUIApplication()
    private let remote = XCUIRemote.shared

    override func setUp() {
        continueAfterFailure = true
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

    private func row(_ id: String) -> XCUIElement {
        app.descendants(matching: .any).matching(identifier: "quickPanel.\(id)").firstMatch
    }

    private func value(_ id: String) -> String {
        let element = row(id)
        return element.exists ? (element.value as? String ?? "<no value>") : "<missing>"
    }

    private func report(_ step: String) {
        let line = "\(step): app \(app.state == .runningForeground ? "foreground" : "NOT foreground (\(app.state.rawValue))"), "
            + "focus '\(focused)', equalizer '\(value("equalizer"))', fast seek '\(value("fastSeek"))', "
            + "version '\(value("version"))', prefer videos '\(value("preferVideos"))', love '\(value("love"))'"
        print("UITEST: \(line)")
        if let dir = ProcessInfo.processInfo.environment["UI_SHOTS"] {
            let name = step.replacingOccurrences(of: " ", with: "-").filter { $0.isLetter || $0.isNumber || $0 == "-" }
            try? XCUIScreen.main.screenshot().pngRepresentation.write(to: URL(fileURLWithPath: "\(dir)/\(name).png"))
        }
    }

    /// One launch: hold centre in the player, then the Equalizer row (a click goes to the next of Momentum 4 → Custom →
    /// Amplify → Off, four clicks come back), the Fast seek row below it (a click toggles, a second restores), the
    /// Version row ("—" for the queued song, which has no music video: a click does nothing), Prefer videos and Love (a
    /// click toggles, a second restores); every row is on screen; Back closes the panel and the app stays open.
    func testQuickPanelRows() {
        app.launchArguments = ["-serverAddress", "127.0.0.1:8080", "-appTheme", "aurora", "-demoRoute", "player"]
        app.launch()
        sleep(10)  // the player opens once the queue has loaded
        remote.press(.select, forDuration: 1.2)
        XCTAssertTrue(row("equalizer").waitForExistence(timeout: 5), "the panel should have an Equalizer row")
        XCTAssertTrue(row("fastSeek").exists, "the panel should have a Fast seek row")
        report("q1 panel open")
        XCTAssertTrue(focused.contains("Shuffle"), "the panel should open on Shuffle, has '\(focused)'")

        for _ in 0..<3 { press(.down, wait: 1) }
        XCTAssertTrue(focused.contains("Equalizer"), "three ▼ should reach Equalizer, has '\(focused)'")
        let cycle = ["Momentum 4", "Custom", "Amplify", "Off"]
        let eqStart = value("equalizer")
        press(.select, wait: 3)  // rebuilds the VLC player
        report("q2 equalizer clicked")
        let expected = cycle.firstIndex(of: eqStart).map { cycle[($0 + 1) % cycle.count] } ?? cycle[0]
        XCTAssertEqual(value("equalizer"), expected, "a click should go from '\(eqStart)' to '\(expected)'")
        for _ in 0..<3 { press(.select, wait: 3) }
        if cycle.contains(eqStart) {
            XCTAssertEqual(value("equalizer"), eqStart, "four clicks should come back to '\(eqStart)'")
        }

        press(.down, wait: 1)
        XCTAssertTrue(focused.contains("Fast seek"), "▼ should reach Fast seek, has '\(focused)'")
        let fastStart = value("fastSeek")
        press(.select, wait: 1)
        report("q3 fast seek clicked")
        XCTAssertEqual(value("fastSeek"), fastStart == "On" ? "Off" : "On", "a click should toggle '\(fastStart)'")
        press(.select, wait: 1)
        XCTAssertEqual(value("fastSeek"), fastStart, "a second click should restore '\(fastStart)'")

        let screen = app.windows.firstMatch.frame
        for id in ["shuffle", "version", "preferVideos", "love"] {
            let frame = row(id).frame
            XCTAssertTrue(row(id).exists && frame.minY >= screen.minY && frame.maxY <= screen.maxY,
                          "row \(id) should be on screen (\(frame) in \(screen))")
        }
        press(.down, wait: 1)
        XCTAssertTrue(focused.contains("Version"), "▼ should reach Version, has '\(focused)'")
        let versionStart = value("version")
        press(.select, wait: 3)
        report("q4 version clicked")
        if versionStart == "—" {
            XCTAssertEqual(value("version"), "—", "Version without other versions should do nothing")
        } else {
            XCTAssertNotEqual(value("version"), versionStart, "a click should play the next version")
        }

        press(.down, wait: 1)
        XCTAssertTrue(focused.contains("Prefer videos"), "▼ should reach Prefer videos, has '\(focused)'")
        let preferStart = value("preferVideos")
        press(.select, wait: 2)  // the queue owner answers with a new state
        report("q5 prefer videos clicked")
        XCTAssertEqual(value("preferVideos"), preferStart == "On" ? "Off" : "On", "a click should toggle '\(preferStart)'")
        press(.select, wait: 2)
        XCTAssertEqual(value("preferVideos"), preferStart, "a second click should restore '\(preferStart)'")

        press(.down, wait: 1)
        XCTAssertTrue(focused.contains("Love"), "▼ should reach Love, has '\(focused)'")
        let loveStart = value("love")
        press(.select, wait: 2)  // the queue owner answers with a new state (the item's heart)
        report("q6 love clicked")
        XCTAssertEqual(value("love"), loveStart == "On" ? "Off" : "On", "a click should toggle '\(loveStart)'")
        press(.select, wait: 2)
        XCTAssertEqual(value("love"), loveStart, "a second click should restore '\(loveStart)'")

        press(.menu)
        report("q7 after Back")
        XCTAssertFalse(row("equalizer").exists, "Back should close the panel")
        XCTAssertEqual(app.state, .runningForeground)
    }
}
