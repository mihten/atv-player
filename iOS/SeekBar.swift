import SwiftUI

/// Progress bar for the iPhone player: tap anywhere to jump there, or drag.
///
/// It replaces SwiftUI's `Slider`, which on iOS only moves when its thumb is dragged. A plain tap on the track fired
/// "editing began / ended" WITHOUT moving the value, and the app then seeked to that stale value (up to 0.5 s old):
/// a small jump back plus a full player seek (and the audio break that comes with every seek).
///
/// Rules: a touch that starts on the thumb drags it (no jump on touch-down); a touch that starts elsewhere jumps to the
/// touch point and can be dragged on from there; a touch that ends within 1 s of where playback was when it began
/// is not a seek at all (`onEnded(nil)`).
struct SeekBar: View {
    @Binding var value: Double
    let range: Double
    var tint: Color = .accentColor
    var track: Color = Color.secondary.opacity(0.3)
    /// Finger down.
    var onBegan: () -> Void = {}
    /// Finger up: the seconds to seek to, or nil when the touch changed nothing.
    var onEnded: (Double?) -> Void

    @State private var dragging = false
    @State private var grab: CGFloat = 0
    @State private var startValue: Double = 0

    private let inset: CGFloat = 10

    var body: some View {
        GeometryReader { geo in
            let usable = max(geo.size.width - 2 * inset, 1)
            let span = max(range, 1)
            let frac = min(max(value / span, 0), 1)
            let thumbX = inset + usable * frac
            ZStack(alignment: .leading) {
                Capsule().fill(track)
                    .frame(width: usable, height: dragging ? 8 : 5)
                    .offset(x: inset)
                Capsule().fill(tint)
                    .frame(width: usable * frac, height: dragging ? 8 : 5)
                    .offset(x: inset)
                Circle().fill(tint)
                    .frame(width: dragging ? 22 : 14, height: dragging ? 22 : 14)
                    .offset(x: thumbX - (dragging ? 11 : 7))
            }
            .frame(width: geo.size.width, height: geo.size.height)
            .contentShape(Rectangle())
            .animation(.easeOut(duration: 0.12), value: dragging)
            .gesture(
                DragGesture(minimumDistance: 0, coordinateSpace: .local)
                    .onChanged { g in
                        if !dragging {
                            dragging = true
                            startValue = value
                            // On the thumb: keep the offset so it does not jump under the finger.
                            let off = g.startLocation.x - thumbX
                            grab = abs(off) <= 24 ? off : 0
                            onBegan()
                        }
                        value = seconds(at: g.location.x, usable: usable, span: span)
                    }
                    .onEnded { g in
                        let target = seconds(at: g.location.x, usable: usable, span: span)
                        dragging = false
                        value = target
                        onEnded(abs(target - startValue) >= 1 ? target : nil)
                    }
            )
        }
        .frame(height: 34)
        .accessibilityElement()
        .accessibilityLabel("Playback position")
        .accessibilityValue(formatTime(value))
        .accessibilityAdjustableAction { direction in
            let step: Double = 10
            let target = min(max(0, value + (direction == .increment ? step : -step)), range)
            onBegan()
            value = target
            onEnded(target)
        }
    }

    private func seconds(at x: CGFloat, usable: CGFloat, span: Double) -> Double {
        let f = min(max((x - grab - inset) / usable, 0), 1)
        return Double(f) * span
    }
}
