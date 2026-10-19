import SwiftUI
import UIKit

/// One colour as plain components, so palettes are Equatable and can be computed off the main thread.
struct RGB: Equatable, Hashable, Sendable {
    var r: Double, g: Double, b: Double

    var color: Color { Color(red: r, green: g, blue: b) }

    var hsb: (h: Double, s: Double, v: Double) {
        let maxV = max(r, g, b), minV = min(r, g, b), d = maxV - minV
        var h = 0.0
        if d > 0 {
            if maxV == r { h = ((g - b) / d).truncatingRemainder(dividingBy: 6) }
            else if maxV == g { h = (b - r) / d + 2 }
            else { h = (r - g) / d + 4 }
            h /= 6
            if h < 0 { h += 1 }
        }
        return (h, maxV == 0 ? 0 : d / maxV, maxV)
    }

    init(r: Double, g: Double, b: Double) {
        self.r = r; self.g = g; self.b = b
    }

    init(h: Double, s: Double, v: Double) {
        let h6 = (h - floor(h)) * 6, i = Int(h6) % 6, f = h6 - floor(h6)
        let p = v * (1 - s), q = v * (1 - s * f), t = v * (1 - s * (1 - f))
        switch i {
        case 0: (r, g, b) = (v, t, p)
        case 1: (r, g, b) = (q, v, p)
        case 2: (r, g, b) = (p, v, t)
        case 3: (r, g, b) = (p, q, v)
        case 4: (r, g, b) = (t, p, v)
        default: (r, g, b) = (v, p, q)
        }
    }

    /// Same hue, chosen saturation (at least) and brightness.
    func saturated(_ s: Double, brightness v: Double) -> RGB {
        let c = hsb
        return RGB(h: c.h, s: max(c.s, s), v: v)
    }

    func with(brightness v: Double, saturation s: Double? = nil) -> RGB {
        let c = hsb
        return RGB(h: c.h, s: s ?? c.s, v: v)
    }
}

/// Colours taken from the cover art: background tones for the moving gradient plus one bright accent.
struct Palette: Hashable, Sendable {
    var base: RGB          // very dark tone for the background
    var tones: [RGB]       // 4 mid/dark saturated tones for the gradient
    var vibrant: RGB       // bright accent (progress bars, highlights)

    static let fallback = Palette.from(hue: 0.72)

    /// Artwork-free palette: a deterministic hue (per item id) so songs without art still differ.
    static func from(hue: Double) -> Palette {
        Palette(
            base: RGB(h: hue, s: 0.6, v: 0.10),
            tones: [RGB(h: hue, s: 0.75, v: 0.45), RGB(h: hue + 0.08, s: 0.7, v: 0.35),
                    RGB(h: hue - 0.1, s: 0.65, v: 0.4), RGB(h: hue + 0.5, s: 0.5, v: 0.25)],
            vibrant: RGB(h: hue, s: 0.65, v: 1)
        )
    }

    static func forItem(_ id: Int) -> Palette {
        from(hue: (Double(id) * 0.618_033_988).truncatingRemainder(dividingBy: 1))
    }

    /// Hue histogram over a 32×32 thumbnail, weighted by saturation × brightness (vivid colours win).
    static func extract(from image: UIImage) -> Palette? {
        guard let cg = image.cgImage else { return nil }
        let side = 32
        var pixels = [UInt8](repeating: 0, count: side * side * 4)
        let drawn = pixels.withUnsafeMutableBytes { buffer -> Bool in
            guard let ctx = CGContext(data: buffer.baseAddress, width: side, height: side, bitsPerComponent: 8,
                                      bytesPerRow: side * 4, space: CGColorSpaceCreateDeviceRGB(),
                                      bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return false }
            ctx.interpolationQuality = .medium
            ctx.draw(cg, in: CGRect(x: 0, y: 0, width: side, height: side))
            return true
        }
        guard drawn else { return nil }

        struct Bin { var r = 0.0, g = 0.0, b = 0.0, weight = 0.0 }
        var bins = [Bin](repeating: Bin(), count: 12)
        var dark = Bin()
        var total = 0.0
        for i in stride(from: 0, to: pixels.count, by: 4) {
            let px = RGB(r: Double(pixels[i]) / 255, g: Double(pixels[i + 1]) / 255, b: Double(pixels[i + 2]) / 255)
            let c = px.hsb
            if c.v < 0.5 {  // shadows: the depth colour of the background
                let w = 0.2 + c.s
                dark.r += px.r * w; dark.g += px.g * w; dark.b += px.b * w; dark.weight += w
            }
            guard c.s > 0.18, c.v > 0.14 else { continue }
            let w = c.s * c.v * c.v
            let k = min(11, Int(c.h * 12))
            bins[k].r += px.r * w; bins[k].g += px.g * w; bins[k].b += px.b * w
            bins[k].weight += w
            total += w
        }
        let ranked = bins.filter { $0.weight > 0 && $0.weight > total * 0.04 }
            .sorted { $0.weight > $1.weight }
            .map { RGB(r: $0.r / $0.weight, g: $0.g / $0.weight, b: $0.b / $0.weight) }
        let shadow = dark.weight > 0 ? RGB(r: dark.r / dark.weight, g: dark.g / dark.weight, b: dark.b / dark.weight) : nil
        guard let top = ranked.first else {
            // grey / black-and-white art: a cool neutral palette
            let h = shadow?.hsb.h ?? 0.6
            return Palette(base: RGB(h: h, s: 0.15, v: 0.08),
                           tones: [RGB(h: h, s: 0.12, v: 0.32), RGB(h: h, s: 0.1, v: 0.22),
                                   RGB(h: h + 0.5, s: 0.1, v: 0.18), RGB(h: h, s: 0.05, v: 0.4)],
                           vibrant: RGB(h: 0.58, s: 0.25, v: 0.95))
        }
        let t = top.hsb
        let depth: RGB = shadow.map { s in
            let c = s.hsb
            return RGB(h: c.h, s: min(0.8, max(0.35, c.s * 1.4)), v: 0.3)
        } ?? RGB(h: t.h, s: t.s * 0.8, v: 0.28)
        let lift = { (c: RGB) -> RGB in c.with(brightness: min(0.62, max(0.38, c.hsb.v * 0.7))) }
        let t0 = lift(top)
        let t1 = ranked.count > 1 ? lift(ranked[1]) : RGB(h: t.h, s: t.s, v: 0.3)
        let t3 = ranked.count > 2 ? lift(ranked[2])
            : RGB(r: (t0.r + depth.r) / 2, g: (t0.g + depth.g) / 2, b: (t0.b + depth.b) / 2)
        let d = depth.hsb
        return Palette(
            base: RGB(h: d.h, s: min(0.7, d.s), v: 0.09),
            tones: [t0, t1, depth, t3],
            vibrant: top.saturated(0.55, brightness: 1)
        )
    }
}

/// Cover art + palette of the item that is loaded now (and a small cache for going back and forth).
/// The server sends Cache-Control: max-age, so URLSession's cache also serves repeat downloads.
@MainActor
final class ArtworkStore: ObservableObject {
    @Published private(set) var itemID: Int?
    @Published private(set) var image: UIImage?
    @Published private(set) var palette: Palette = .fallback
    private var cache: [Int: (UIImage?, Palette)] = [:]
    private var order: [Int] = []

    func follow(_ item: MediaItem?) {
        guard let item, item.id != itemID else { return }
        itemID = item.id
        if let hit = cache[item.id] {
            image = hit.0
            palette = hit.1
            return
        }
        guard item.art == true else {
            store(item.id, nil, .forItem(item.id))
            return
        }
        let url = item.artURL, id = item.id
        Task {
            var result: (UIImage?, Palette) = (nil, .forItem(id))
            if let (data, response) = try? await URLSession.shared.data(from: url),
               (response as? HTTPURLResponse)?.statusCode == 200,
               let decoded = UIImage(data: data) {
                let palette = await Task.detached(priority: .userInitiated) {
                    Palette.extract(from: decoded)
                }.value
                result = (decoded, palette ?? .forItem(id))
            }
            store(id, result.0, result.1)
        }
    }

    private func store(_ id: Int, _ img: UIImage?, _ pal: Palette) {
        cache[id] = (img, pal)
        order.append(id)
        if order.count > 40 { cache[order.removeFirst()] = nil }
        guard itemID == id else { return }
        withAnimation(.easeInOut(duration: 1.2)) {
            image = img
            palette = pal
        }
    }
}
