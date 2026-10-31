import UIKit

/// Interface orientations of the iPhone app. After the full-screen "portrait" button (or a song in full
/// screen) the interface is held in portrait while the phone is still sideways, until the device itself is
/// turned upright or a new video starts; then landscape is allowed again.
/// A temporary portrait-only mask (not just a one-off rotation request): without it iOS would turn the
/// interface straight back to landscape, because the device is still held sideways.
@MainActor
final class OrientationLock {
    static let shared = OrientationLock()

    private(set) var portraitOnly = false
    var log: @MainActor (String) -> Void = { _ in }
    private var observer: NSObjectProtocol?
    private var lastBlocked: UIDeviceOrientation = .unknown
    private var releasedSidewaysAt = Date.distantPast

    /// Landscape was just allowed again with the phone sideways: iOS is about to rotate the interface.
    var rotatingToLandscape: Bool { Date().timeIntervalSince(releasedSidewaysAt) < 1.5 }

    var mask: UIInterfaceOrientationMask { portraitOnly ? .portrait : .allButUpsideDown }

    /// Rotate the interface to portrait and keep it there until the phone is upright.
    func holdPortrait() {
        portraitOnly = true
        lastBlocked = .unknown
        startWatching()
        updateSupportedOrientations()
        for scene in windowScenes {
            scene.requestGeometryUpdate(.iOS(interfaceOrientations: .portrait)) { [weak self] error in
                Task { @MainActor in self?.log("orientation: portrait request failed: \(error.localizedDescription)") }
            }
        }
        log("orientation: portrait held until the phone is upright (device \(Self.name(UIDevice.current.orientation)))")
    }

    /// Allow landscape again; iOS then follows the device (a sideways phone turns the interface to landscape).
    func release(_ reason: String) {
        guard portraitOnly else { return }
        portraitOnly = false
        // read before stopWatching: without orientation notifications UIDevice reports .unknown
        if UIDevice.current.orientation.isLandscape { releasedSidewaysAt = Date() }
        stopWatching()
        updateSupportedOrientations()
        log("orientation: landscape allowed again (\(reason))")
    }

    private func startWatching() {
        guard observer == nil else { return }
        UIDevice.current.beginGeneratingDeviceOrientationNotifications()
        observer = NotificationCenter.default.addObserver(
            forName: UIDevice.orientationDidChangeNotification, object: nil, queue: .main
        ) { [weak self] _ in
            Task { @MainActor in self?.deviceTurned() }
        }
    }

    private func stopWatching() {
        guard let observer else { return }
        NotificationCenter.default.removeObserver(observer)
        self.observer = nil
        UIDevice.current.endGeneratingDeviceOrientationNotifications()
    }

    private func deviceTurned() {
        guard portraitOnly else { return }
        let orientation = UIDevice.current.orientation
        switch orientation {
        case .portrait:
            release("phone turned upright")
        case .landscapeLeft, .landscapeRight:
            guard orientation != lastBlocked else { return }
            lastBlocked = orientation
            log("full screen: blocked return to landscape (\(Self.name(orientation))), portrait held until the phone is upright")
        default:
            break  // face up / down, upside down: wait for a real portrait
        }
    }

    private var windowScenes: [UIWindowScene] {
        UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
    }

    /// The root controller and everything presented on it (sheet, full-screen cover) re-read the mask.
    private func updateSupportedOrientations() {
        for scene in windowScenes {
            for window in scene.windows {
                var controller = window.rootViewController
                while let current = controller {
                    current.setNeedsUpdateOfSupportedInterfaceOrientations()
                    controller = current.presentedViewController
                }
            }
        }
    }

    private static func name(_ orientation: UIDeviceOrientation) -> String {
        switch orientation {
        case .portrait: return "portrait"
        case .portraitUpsideDown: return "upside down"
        case .landscapeLeft: return "landscape left"
        case .landscapeRight: return "landscape right"
        case .faceUp: return "face up"
        case .faceDown: return "face down"
        default: return "unknown"
        }
    }
}

@MainActor
final class PhoneAppDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication,
                     supportedInterfaceOrientationsFor window: UIWindow?) -> UIInterfaceOrientationMask {
        OrientationLock.shared.mask
    }
}
