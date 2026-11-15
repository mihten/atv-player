import SwiftUI

/// The badges of the playing item (made by the server, `MediaItem.shownBadges`) plus its version chip, as chips that wrap
/// onto a second line when they do not fit. iPhone player: after the artist, above the progress bar.
struct BadgeRow: View {
    let item: MediaItem
    let theme: AppTheme
    var size: CGFloat = 11
    /// Where the block of chips sits: leading under a leading title (Aurora, Noir, Vinyl), centered in Classic.
    var alignment: HorizontalAlignment = .leading

    var body: some View {
        let chips = item.facts
        if !chips.isEmpty {
            FlowLayout(spacing: 6) {
                ForEach(chips, id: \.self) { FactChip(text: $0, theme: theme, size: size) }
            }
            .frame(maxWidth: .infinity, alignment: Alignment(horizontal: alignment, vertical: .center))
        }
    }
}

/// Left to right, a new line when the next view does not fit.
struct FlowLayout: Layout {
    var spacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let maxWidth = proposal.width ?? .infinity
        var x: CGFloat = 0
        var y: CGFloat = 0
        var rowHeight: CGFloat = 0
        var width: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x > 0 && x + size.width > maxWidth {
                x = 0
                y += rowHeight + spacing
                rowHeight = 0
            }
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
            width = max(width, x - spacing)
        }
        return CGSize(width: width, height: y + rowHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX
        var y = bounds.minY
        var rowHeight: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x > bounds.minX && x + size.width > bounds.maxX {
                x = bounds.minX
                y += rowHeight + spacing
                rowHeight = 0
            }
            view.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
    }
}
