import SwiftUI

/// The engine's notice on the iPhone (ATV-Server: "The headphones went to the Apple TV — playback paused", the sink
/// missing, the sound output changed during playback). Stays until Play or a tap on it.
struct PhoneNoticeBanner: View {
    @EnvironmentObject var engine: PlayerEngine

    var body: some View {
        ZStack {
            if let text = engine.notice {
                Button {
                    engine.acknowledgeNotice("tap")
                } label: {
                    Text(text)
                        .font(.footnote.weight(.semibold))
                        .multilineTextAlignment(.center)
                        .padding(.horizontal, 16)
                        .padding(.vertical, 10)
                        .background(.ultraThinMaterial, in: Capsule())
                }
                .buttonStyle(.plain)
                .padding(.horizontal, 20)
                .padding(.top, 6)
                .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
        .animation(.easeOut(duration: 0.3), value: engine.notice)
    }
}
