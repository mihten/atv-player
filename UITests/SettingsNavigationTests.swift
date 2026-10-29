import XCTest

/// Siri Remote checks of the Apple TV Settings screen on the simulator (CI, design workflow with ui_test=true).
/// Needs the throwaway demo server on 127.0.0.1:8080 with something in the TV queue (Now Playing card).
/// Every step prints a "UITEST:" line (focus, visible page, app state) and saves a screenshot to $UI_SHOTS.
final class SettingsNavigationTests: XCTestCase {
    private let app = XCUIApplication()
    private let remote = XCUIRemote.shared

    override func setUp() {
        continueAfterFailure = true
    }

    private func launch(_ route: String) {
        app.terminate()
        app.launchArguments = ["-serverAddress", "127.0.0.1:8080", "-appTheme", "aurora", "-demoRoute", route]
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

    /// Settings is shown (its first page stays mounted under the others) and no page is open over it
    /// (every page above the first one has a title with this identifier; closed pages are removed).
    private var inSettings: Bool { app.staticTexts["About"].exists }
    private var onRoot: Bool { inSettings && !app.staticTexts["settings.pageTitle"].exists }

    private func report(_ step: String) {
        let line = "\(step): app \(app.state == .runningForeground ? "foreground" : "NOT foreground (\(app.state.rawValue))"), "
            + "in settings \(inSettings), root \(onRoot), focus '\(focused)'"
        print("UITEST: \(line)")
        if let dir = ProcessInfo.processInfo.environment["UI_SHOTS"] {
            let name = step.replacingOccurrences(of: " ", with: "-").filter { $0.isLetter || $0.isNumber || $0 == "-" }
            try? XCUIScreen.main.screenshot().pngRepresentation.write(to: URL(fileURLWithPath: "\(dir)/\(name).png"))
        }
    }

    /// Opening Settings focuses the first row; Back returns to the menu (the app stays open).
    func test1OpenFocusAndBack() {
        launch("settings")
        report("1a settings opened")
        XCTAssertTrue(focused.contains("Theme"), "first row should have the focus, has '\(focused)'")
        press(.menu)
        report("1b after Back")
        XCTAssertEqual(app.state, .runningForeground)
        XCTAssertFalse(inSettings, "Back should leave Settings")
    }

    /// Back with the focus on the Now Playing card returns to the menu (the app stays open).
    func test2BackFromNowPlayingCard() {
        launch("settings")
        press(.left)
        report("2a after left")
        XCTAssertTrue(focused.contains("Harbor Lights"), "left from the top row should reach the Now Playing card")
        press(.menu)
        report("2b after Back")
        XCTAssertEqual(app.state, .runningForeground)
        XCTAssertFalse(inSettings, "Back should leave Settings")
    }

    /// Page -> choice list -> pick -> Back -> Back: every Back goes one level up, the last one to the menu.
    func test3PagesAndChoices() {
        launch("settings")
        press(.select)
        report("3a theme page")
        XCTAssertFalse(onRoot)
        XCTAssertTrue(app.staticTexts["Look"].exists, "Theme page should show 'Look'")
        press(.select)
        report("3b look choices")
        XCTAssertTrue(focused.contains("Aurora"), "the choice list should start on the current value")
        press(.menu)
        report("3c back to theme page")
        XCTAssertTrue(app.staticTexts["Look"].exists)
        press(.menu)
        report("3d back to settings root")
        XCTAssertTrue(onRoot)
        XCTAssertEqual(app.state, .runningForeground)
        press(.menu)
        report("3e back to the menu")
        XCTAssertFalse(inSettings)
        XCTAssertEqual(app.state, .runningForeground)
    }

    /// On a page, Back with the focus on the Now Playing card closes the page (not Settings, not the app).
    func test4BackFromCardOnPage() {
        launch("sound")
        press(.left)
        report("4a sound page, after left")
        XCTAssertTrue(focused.contains("Harbor Lights"), "left should reach the Now Playing card")
        XCTAssertFalse(onRoot)
        press(.menu)
        report("4b after Back")
        XCTAssertTrue(onRoot, "Back on a page should return to the Settings root")
        XCTAssertEqual(app.state, .runningForeground)
    }
}
