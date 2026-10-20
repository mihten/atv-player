import SwiftUI

/// Shown until the server is found: Bonjour search status plus a field to type the address.
struct ServerSearchView: View {
    @EnvironmentObject var locator: ServerLocator
    @Environment(\.appTheme) private var theme
    @State private var address = ServerLocator.manualAddress
    @State private var invalid = false

    var body: some View {
        VStack(spacing: 28) {
            if theme.isClassic {
                ProgressView()
            } else {
                SearchPulse()
            }
            Text("ATV Player").font(theme.isClassic ? .largeTitle.bold() : theme.display(largeTitle))
            Text(locator.status)
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 10) {
                Text("Or type the server address (e.g. 10.0.0.5 or 10.0.0.5:8080):")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                TextField("Server address", text: $address)
                    .textContentType(.URL)
                    .autocorrectionDisabled()
                    #if os(iOS)
                    .textInputAutocapitalization(.never)
                    .keyboardType(.URL)
                    .textFieldStyle(.roundedBorder)
                    #endif
                    .onSubmit(connect)
                Button("Connect", action: connect)
                    .disabled(address.trimmingCharacters(in: .whitespaces).isEmpty)
                if invalid {
                    Text("That is not an address.").foregroundStyle(.red)
                }
            }
            .frame(maxWidth: 700)
        }
        .padding(40)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background {
            if !theme.isClassic { ThemeBackdrop(intensity: 0.9) }
        }
    }

    private var largeTitle: CGFloat {
        #if os(tvOS)
        return 76
        #else
        return 38
        #endif
    }

    private func connect() {
        invalid = !locator.setManual(address)
    }
}

/// Three rings expanding from a dot: "looking for the server".
private struct SearchPulse: View {
    @Environment(\.appTheme) private var theme
    @EnvironmentObject private var artwork: ArtworkStore

    var body: some View {
        #if os(tvOS)
        let size: CGFloat = 220
        #else
        let size: CGFloat = 120
        #endif
        let accent = theme.accent(artwork.palette)
        TimelineView(.animation(minimumInterval: 1.0 / 30)) { context in
            let t = context.date.timeIntervalSinceReferenceDate
            ZStack {
                ForEach(0..<3, id: \.self) { i in
                    let phase = (t / 2.4 + Double(i) / 3).truncatingRemainder(dividingBy: 1)
                    Circle()
                        .stroke(accent.opacity(0.7 * (1 - phase)), lineWidth: 3)
                        .frame(width: size * phase, height: size * phase)
                }
                Image(systemName: "dot.radiowaves.left.and.right")
                    .font(.system(size: size * 0.2, weight: .semibold))
                    .foregroundStyle(accent)
            }
            .frame(width: size, height: size)
        }
    }
}
