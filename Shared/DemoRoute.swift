import Foundation

/// Screenshot automation only (CI simulator runs, tools/design_shots.sh): the launch argument
/// `-demoRoute <screen>` opens that screen right away. Launch arguments live in UserDefaults' volatile
/// argument domain, so nothing is stored and a normal launch on a device never has a route.
enum DemoRoute {  // v0.13.0 design preview
    static let key = "demoRoute"

    static var screen: String? {
        UserDefaults.standard.volatileDomain(forName: UserDefaults.argumentDomain)[key] as? String
    }

    static func `is`(_ name: String) -> Bool { screen == name }

    /// Run `action` once the app has something loaded (the server sends the queue on connect).
    @MainActor
    static func whenLoaded(_ engine: PlayerEngine, after seconds: Double = 2.5, _ action: @escaping () -> Void) {
        Task { @MainActor in
            for _ in 0..<40 {
                try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
                if engine.current != nil { action(); return }
            }
        }
    }
}
