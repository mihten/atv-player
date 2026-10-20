import SwiftUI
import UIKit

/// Cover art from the server with a styled placeholder (gradient + symbol) and a soft fade-in.
struct ArtworkImage: View {
    let url: URL?
    var cornerRadius: CGFloat = 12
    var symbol = "music.note"
    var tint: Color = Color(white: 0.3)

    var body: some View {
        RemoteImage(url: url) { placeholder }
        .clipShape(RoundedRectangle(cornerRadius: cornerRadius, style: .continuous))
    }

    private var placeholder: some View {
        GeometryReader { geo in
            ZStack {
                LinearGradient(colors: [tint.opacity(0.9), tint.opacity(0.35)], startPoint: .topLeading,
                               endPoint: .bottomTrailing)
                Image(systemName: symbol)
                    .resizable()
                    .scaledToFit()
                    .frame(width: min(geo.size.width, geo.size.height) * 0.36)
                    .foregroundStyle(.white.opacity(0.55))
            }
        }
    }
}

extension MediaItem {
    /// nil when the server has no art for this item (saves a request that would 404).
    var artURLIfAny: URL? { art == false ? nil : artURL }
    var symbol: String { isVideo ? "film" : "music.note" }
    /// "FLAC · 2021" style facts for the player.
    var facts: [String] {
        var out: [String] = []
        if let codec, !codec.isEmpty { out.append(codec.uppercased()) }
        if lossless == true { out.append("Lossless") }
        if let year, year.count >= 4 { out.append(String(year.prefix(4))) }
        return out
    }
}

/// "Now playing" indicator: bars that dance while playing and rest when paused.
struct EqualizerBars: View {
    var animating: Bool
    var color: Color = .accentColor
    var bars = 4
    var width: CGFloat = 4
    var height: CGFloat = 18
    var spacing: CGFloat = 3

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 12, paused: !animating)) { context in
            let t = context.date.timeIntervalSinceReferenceDate
            HStack(alignment: .bottom, spacing: spacing) {
                ForEach(0..<bars, id: \.self) { i in
                    let level = animating ? 0.28 + 0.72 * abs(sin(t * (2.1 + Double(i) * 0.83) + Double(i) * 1.7)) : 0.22
                    Capsule()
                        .fill(color)
                        .frame(width: width, height: max(width, height * level))
                }
            }
            .frame(height: height, alignment: .bottom)
        }
    }
}

/// Rotation with spin-up and a gentle spin-down (a record slowing when paused). A class, not @State:
/// it is advanced while TimelineView renders and must not trigger new renders itself.
final class SpinState {
    private var angle = 0.0
    private var speed = 0.0
    private var last: Date?

    func angle(at date: Date, spinning: Bool) -> Double {
        let dt = last.map { min(max(date.timeIntervalSince($0), 0), 0.1) } ?? 0
        last = date
        let target = spinning ? 200.0 : 0  // 33⅓ rpm = 200°/s
        speed += (target - speed) * min(1, dt * 2.2)
        angle = (angle + speed * dt).truncatingRemainder(dividingBy: 360)
        return angle
    }
}

/// A vinyl record with the cover art as its label; spins while playing.
struct VinylRecord: View {
    let image: UIImage?
    var spinning: Bool
    var size: CGFloat
    var labelTint: Color = .orange
    @State private var spin = SpinState()
    @State private var coasting = false

    var body: some View {
        ZStack {
            TimelineView(.animation(minimumInterval: 1.0 / 30, paused: !(spinning || coasting))) { context in
                disc.rotationEffect(.degrees(spin.angle(at: context.date, spinning: spinning)))
            }
            // the light reflection stays put while the record turns
            Circle()
                .fill(AngularGradient(colors: [.clear, .white.opacity(0.13), .clear, .clear,
                                               .white.opacity(0.09), .clear, .clear],
                                      center: .center, angle: .degrees(-30)))
                .padding(size * 0.02)
                .blendMode(.screen)
        }
        .frame(width: size, height: size)
        .shadow(color: .black.opacity(0.6), radius: size * 0.05, y: size * 0.02)
        .onChange(of: spinning) { _, now in
            guard !now else { return }
            coasting = true
            DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) { coasting = false }
        }
    }

    private var disc: some View {
        ZStack {
            Circle().fill(Color(white: 0.05))
            Canvas { ctx, sz in
                let c = CGPoint(x: sz.width / 2, y: sz.height / 2)
                var r = sz.width * 0.485
                var i = 0
                while r > sz.width * 0.2 {
                    let rect = CGRect(x: c.x - r, y: c.y - r, width: 2 * r, height: 2 * r)
                    ctx.stroke(Path(ellipseIn: rect), with: .color(.white.opacity(i % 7 == 0 ? 0.08 : 0.035)),
                               lineWidth: max(0.5, sz.width * 0.0016))
                    r -= sz.width * 0.0095
                    i += 1
                }
            }
            label
                .frame(width: size * 0.36, height: size * 0.36)
                .clipShape(Circle())
                .overlay(Circle().stroke(.black.opacity(0.35), lineWidth: size * 0.004))
            Circle().fill(Color(white: 0.02)).frame(width: size * 0.028, height: size * 0.028)
        }
    }

    @ViewBuilder private var label: some View {
        if let image {
            Image(uiImage: image).resizable().scaledToFill()
        } else {
            ZStack {
                labelTint
                Image(systemName: "music.note").font(.system(size: size * 0.08)).foregroundStyle(.white.opacity(0.7))
            }
        }
    }
}

/// Tonearm that swings onto the record while playing and lifts off when paused. Pivot = top centre.
struct Tonearm: View {
    var engaged: Bool
    var length: CGFloat

    var body: some View {
        ZStack(alignment: .top) {
            Capsule()
                .fill(LinearGradient(colors: [Color(white: 0.85), Color(white: 0.55)], startPoint: .leading, endPoint: .trailing))
                .frame(width: length * 0.045, height: length)
            RoundedRectangle(cornerRadius: length * 0.02)
                .fill(LinearGradient(colors: [Color(white: 0.75), Color(white: 0.45)], startPoint: .top, endPoint: .bottom))
                .frame(width: length * 0.11, height: length * 0.17)
                .offset(y: length * 0.9)
            Circle()
                .fill(RadialGradient(colors: [Color(white: 0.7), Color(white: 0.25)], center: .topLeading,
                                     startRadius: 0, endRadius: length * 0.16))
                .frame(width: length * 0.2, height: length * 0.2)
                .overlay(Circle().fill(Color(white: 0.8)).frame(width: length * 0.07, height: length * 0.07))
                .offset(y: -length * 0.09)
        }
        .shadow(color: .black.opacity(0.5), radius: length * 0.03, x: length * 0.02, y: length * 0.03)
        .rotationEffect(.degrees(engaged ? 26 : 4), anchor: UnitPoint(x: 0.5, y: 0.0))
        .animation(.spring(response: 1.1, dampingFraction: 0.8), value: engaged)
    }
}

/// Thin progress line with an optional glow (Aurora) and a knob.
struct ProgressLine: View {
    var value: Double
    var color: Color
    var height: CGFloat = 6
    var glow = false
    var knob = false
    var track: Color = .white.opacity(0.18)

    var body: some View {
        GeometryReader { geo in
            let w = geo.size.width * min(max(value, 0), 1)
            ZStack(alignment: .leading) {
                Capsule().fill(track)
                Capsule()
                    .fill(color)
                    .frame(width: max(height, w))
                    .shadow(color: glow ? color.opacity(0.8) : .clear, radius: glow ? height * 1.5 : 0)
                if knob {
                    Circle()
                        .fill(color)
                        .frame(width: height * 2.4, height: height * 2.4)
                        .shadow(color: .black.opacity(0.4), radius: 4)
                        .offset(x: w - height * 1.2)
                }
            }
            .frame(height: height)
            .frame(maxHeight: .infinity)
        }
        .frame(height: knob ? height * 2.4 : height)
    }
}
