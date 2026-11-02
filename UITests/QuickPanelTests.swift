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
            + "focus '\(focused)', equalizer '\(value("equalizer"))', loudness '\(value("loudness"))', "
            + "boost '\(value("boost"))', fast seek '\(value("fastSeek"))', "
            + "version '\(value("version"))', prefer videos '\(value("preferVideos"))', love '\(value("love"))'"
        print("UITEST: \(line)")
        if let dir = ProcessInfo.processInfo.environment["UI_SHOTS"] {
            let name = step.replacingOccurrences(of: " ", with: "-").filter { $0.isLetter || $0.isNumber || $0 == "-" }
            try? XCUIScreen.main.screenshot().pngRepresentation.write(to: URL(fileURLWithPath: "\(dir)/\(name).png"))
        }
    }

    private func list(_ id: String) -> XCUIElement {
        app.descendants(matching: .any).matching(identifier: "quickPanel.\(id).list").firstMatch
    }

    /// One launch: hold centre in the player; the panel opens on Love. Row kinds (task 18): toggles (Love, Shuffle,
    /// Loudness, Fast seek, Prefer videos: a click flips, a second restores), ◀▶ values (Repeat, Lyrics / subtitles:
    /// stop at the ends, a click does nothing), level bars (Boost ◀▶ 1 dB), inline lists (Equalizer: click opens a list
    /// under the row on the current value, Back closes only the list, ▼ + click picks the next preset; Version "—" for
    /// a song without a video: not openable). No Volume row with the Apple TV's own sound (the demo default). Every row
    /// is on screen; Back closes the panel and the app stays open.
    func testQuickPanelRows() {
        app.launchArguments = ["-serverAddress", "127.0.0.1:8080", "-appTheme", "aurora", "-demoRoute", "player"]
        app.launch()
        sleep(10)  // the player opens once the queue has loaded
        remote.press(.select, forDuration: 1.2)
        XCTAssertTrue(row("equalizer").waitForExistence(timeout: 5), "the panel should have an Equalizer row")
        XCTAssertTrue(row("loudness").exists, "the panel should have a Loudness row")
        XCTAssertTrue(row("boost").exists, "the panel should have a Boost row")
        XCTAssertFalse(row("limiter").exists, "the Limiter row is gone (the limiter is always on)")
        XCTAssertTrue(row("fastSeek").exists, "the panel should have a Fast seek row")
        XCTAssertFalse(row("volume").exists, "no Volume row while the Apple TV plays its own sound")
        report("q1 panel open")
        XCTAssertTrue(focused.contains("Love"), "the panel should open on Love, has '\(focused)'")

        let screen = app.windows.firstMatch.frame
        let order = ["love", "shuffle", "repeatMode", "subtitles", "equalizer", "loudness", "boost", "fastSeek",
                     "preferVideos", "version"]
        var lastY = -CGFloat.infinity
        for id in order {
            let frame = row(id).frame
            XCTAssertTrue(row(id).exists && frame.minY >= screen.minY && frame.maxY <= screen.maxY,
                          "row \(id) should be on screen (\(frame) in \(screen))")
            XCTAssertGreaterThan(frame.minY, lastY, "row \(id) should be below the previous row (order \(order))")
            lastY = frame.minY
        }

        let loveStart = value("love")
        press(.select, wait: 2)  // the queue owner answers with a new state (the item's heart)
        report("q2 love clicked")
        XCTAssertEqual(value("love"), loveStart == "On" ? "Off" : "On", "a click should toggle '\(loveStart)'")
        press(.select, wait: 2)
        XCTAssertEqual(value("love"), loveStart, "a second click should restore '\(loveStart)'")

        press(.down, wait: 1)
        XCTAssertTrue(focused.contains("Shuffle"), "▼ from Love should reach Shuffle, has '\(focused)'")

        // Repeat: ◀▶ value, stops at the ends, a click does nothing
        press(.down, wait: 1)
        XCTAssertTrue(focused.contains("Repeat"), "▼ should reach Repeat, has '\(focused)'")
        let repeats = ["All", "One", "Off"]
        let repeatStart = value("repeatMode")
        press(.select, wait: 2)
        XCTAssertEqual(value("repeatMode"), repeatStart, "a click on Repeat should do nothing")
        for _ in 0..<3 { press(.left, wait: 2) }
        report("q3 repeat at the left end")
        XCTAssertEqual(value("repeatMode"), "All", "◀ should stop at All")
        press(.right, wait: 2)
        XCTAssertEqual(value("repeatMode"), "One", "▶ from All should give One")
        for _ in 0..<3 { press(.right, wait: 2) }
        XCTAssertEqual(value("repeatMode"), "Off", "▶ should stop at Off")
        if let at = repeats.firstIndex(of: repeatStart) {
            for _ in 0..<(repeats.count - 1 - at) { press(.left, wait: 2) }
            XCTAssertEqual(value("repeatMode"), repeatStart, "◀ should come back to '\(repeatStart)'")
        }

        // Lyrics / subtitles: ◀▶ value, stops at the ends
        press(.down, wait: 1)
        XCTAssertTrue(focused.contains("Lyrics"), "▼ should reach Lyrics / subtitles, has '\(focused)'")
        let subs = ["File default", "English", "Croatian", "Off"]
        let subStart = value("subtitles")
        for _ in 0..<4 { press(.left, wait: 1) }
        XCTAssertEqual(value("subtitles"), "File default", "◀ should stop at File default")
        if let at = subs.firstIndex(of: subStart) {
            for _ in 0..<at { press(.right, wait: 1) }
            XCTAssertEqual(value("subtitles"), subStart, "▶ should come back to '\(subStart)'")
        }

        // Equalizer: inline list
        press(.down, wait: 1)
        XCTAssertTrue(focused.contains("Equalizer"), "▼ should reach Equalizer, has '\(focused)'")
        let eqStart = value("equalizer")
        press(.select, wait: 2)
        report("q4 equalizer list open")
        XCTAssertTrue(list("equalizer").exists, "a click should open the equalizer list")
        XCTAssertTrue(focused.contains(eqStart), "the list should open on the current preset '\(eqStart)', has '\(focused)'")
        press(.menu, wait: 2)
        XCTAssertFalse(list("equalizer").exists, "Back should close the list")
        XCTAssertTrue(row("equalizer").exists, "... and only the list (the panel stays)")
        XCTAssertTrue(focused.contains("Equalizer"), "... focus back on Equalizer, has '\(focused)'")
        press(.select, wait: 2)
        press(.down, wait: 1)
        let picked = focused
        press(.select, wait: 3)  // new curve on the running player
        report("q5 equalizer picked")
        XCTAssertFalse(list("equalizer").exists, "a click on an entry should close the list")
        if picked != eqStart, !picked.hasPrefix("<") {
            XCTAssertEqual(value("equalizer"), picked, "the picked preset should be in use")
        }
        press(.select, wait: 2)
        press(.up, wait: 1)
        press(.select, wait: 3)
        XCTAssertEqual(value("equalizer"), eqStart, "▲ + click should restore '\(eqStart)'")

        press(.down, wait: 1)
        XCTAssertTrue(focused.contains("Loudness"), "▼ should reach Loudness, has '\(focused)'")

        // Boost: level bar, ◀▶ in 1 dB steps, stops at the ends
        press(.down, wait: 1)
        XCTAssertTrue(focused.contains("Boost"), "▼ should reach Boost, has '\(focused)'")
        let boostStart = value("boost")
        press(.right, wait: 2)
        report("q6 boost right")
        if boostStart != "+8 dB" {
            XCTAssertNotEqual(value("boost"), boostStart, "▶ should raise Boost by 1 dB from '\(boostStart)'")
            press(.left, wait: 2)
        }
        XCTAssertEqual(value("boost"), boostStart, "◀ should come back to '\(boostStart)'")
        if boostStart == "0 dB" {
            press(.left, wait: 2)
            XCTAssertEqual(value("boost"), "0 dB", "◀ should stop at 0 dB")
            press(.right, wait: 2)
            XCTAssertEqual(value("boost"), "+1 dB", "▶ from 0 dB should give +1 dB")
            press(.left, wait: 2)
        }

        press(.down, wait: 1)
        XCTAssertTrue(focused.contains("Fast seek"), "▼ should reach Fast seek, has '\(focused)'")
        let fastStart = value("fastSeek")
        press(.select, wait: 1)
        report("q7 fast seek clicked")
        XCTAssertEqual(value("fastSeek"), fastStart == "On" ? "Off" : "On", "a click should toggle '\(fastStart)'")
        press(.select, wait: 1)
        XCTAssertEqual(value("fastSeek"), fastStart, "a second click should restore '\(fastStart)'")

        press(.down, wait: 1)
        XCTAssertTrue(focused.contains("Prefer videos"), "▼ should reach Prefer videos, has '\(focused)'")
        let preferStart = value("preferVideos")
        press(.select, wait: 2)  // the queue owner answers with a new state
        report("q8 prefer videos clicked")
        XCTAssertEqual(value("preferVideos"), preferStart == "On" ? "Off" : "On", "a click should toggle '\(preferStart)'")
        press(.select, wait: 2)
        XCTAssertEqual(value("preferVideos"), preferStart, "a second click should restore '\(preferStart)'")

        press(.down, wait: 1)
        XCTAssertTrue(focused.contains("Version"), "▼ should reach Version (last row), has '\(focused)'")
        let versionStart = value("version")
        press(.select, wait: 2)
        report("q9 version clicked")
        if versionStart == "—" {
            XCTAssertFalse(list("version").exists, "Version without other versions should not open a list")
        } else {
            XCTAssertTrue(list("version").exists, "a click should open the version list")
            press(.menu, wait: 2)
            XCTAssertFalse(list("version").exists, "Back should close the version list")
            XCTAssertEqual(value("version"), versionStart, "closing the list should not switch the version")
        }

        press(.menu)
        report("q10 after Back")
        XCTAssertFalse(row("equalizer").exists, "Back should close the panel")
        XCTAssertEqual(app.state, .runningForeground)
    }
}
