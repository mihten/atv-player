import SwiftUI

/// Covers the video while `engine.pictureSettling`: the picture track came back mid-stream and stays blocky until the
/// next keyframe. Shows the item's artwork on black and fades out when the engine lifts it. Never takes touches.
struct PictureCover: View {
    @EnvironmentObject var engine: PlayerEngine

    var body: some View {
        ZStack {
            if engine.pictureSettling, let item = engine.current {
                ZStack {
                    Color.black
                    RemoteImage(url: item.art == true ? item.artURL : nil) { Color.black }
                        .opacity(0.85)
                }
                .clipped()
                .transition(.opacity)
            }
        }
        .animation(.easeOut(duration: 0.4), value: engine.pictureSettling)
        .allowsHitTesting(false)
    }
}
