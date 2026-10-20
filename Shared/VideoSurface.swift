import SwiftUI
import UIKit

/// Hosts the engine's single VLC drawable so video keeps rendering across view rebuilds.
///
/// There is ONE video view, and a UIView has one superview. Two hosts on screen at once (the iPhone player
/// sheet and the full-screen cover above it) used to take it from each other on every SwiftUI update, so
/// after the next video loaded the picture sat in the covered host or flickered between them, and the
/// cover's own controls/tap layer stopped working. `active: false` makes a host leave it alone.
struct VideoSurface: UIViewRepresentable {
    let engine: PlayerEngine
    /// false = this host must not take the video view (another host on top of it shows the picture).
    var active = true

    func makeUIView(context: Context) -> UIView {
        let container = UIView()
        container.backgroundColor = .black
        // Never take touches: taps belong to the SwiftUI layer above (controls, show/hide).
        container.isUserInteractionEnabled = false
        attach(to: container)
        return container
    }

    func updateUIView(_ uiView: UIView, context: Context) {
        attach(to: uiView)
    }

    private func attach(to container: UIView) {
        guard active else { return }
        let view = engine.videoView
        guard view.superview !== container else { return }
        view.removeFromSuperview()
        view.frame = container.bounds
        view.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        container.addSubview(view)
    }
}
