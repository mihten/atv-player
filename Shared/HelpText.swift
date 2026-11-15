import Foundation

/// The explanations that used to sit under settings and menus: Settings → About → Help, once, in both apps. A setting
/// keeps at most one short line; anything longer lives here.
enum HelpText {
    struct Topic: Identifiable {
        let title: String
        let text: String
        var id: String { title }
    }

    static let mixes = Topic(title: "Mixes", text: "Mix 1 and Mix 2 are the two small tiles on Home. Each holds folders and playlists; a tile shuffles all of them, each file once. Folders can include their subfolders. They are kept on the server, so the iPhone, the Apple TV and the web page show the same two. Without the server (no-server or Offline mode) there are no mixes.")

    static let badges = Topic(title: "Badges", text: "The small labels under the title come from the server and are the same in every app and on the web page: video codec, picture size, HDR, frame rate, the audio format with bit depth and sample rate (or the bit rate of lossy files) and surround sound. Without the server there are none.")

    static let themeEffects = Topic(title: "Theme effects", text: "Aurora moves its background and Vinyl spins the record while a song plays. Noir and Classic have no effects, so the switch is grayed out there.")

    static let fastSeek = Topic(title: "Fast seek", text: "Fast seek jumps to the nearest keyframe, so a jump can land a few seconds off.")

    static let preferVideos = Topic(title: "Prefer videos", text: "Songs in the queue play as one of their music videos when they have one. You can still pick another version, or the audio-only one, while a song plays.")

    static let secondLanguage = Topic(title: "Second language", text: "A second line at the top of a music video that has lyrics in that language (timed lyrics only). The main language stays at the bottom.")

    static let atvServer = Topic(title: "ATV-Server sound output", text: "ATV-Server plays the sound, the Apple TV shows the picture and stays silent (also while ATV-Server is missing: playback pauses with a notice). Lip sync: how much earlier the headphones play (Bluetooth adds a delay): raise it if the sound comes late, lower it if it comes early. Codec: the switch pauses playback for a few seconds. The TV Player can use the same headphones: the last one to press Play gets them. Equalizer, Boost and Loudness are kept separately for Apple TV and ATV-Server; choosing Apple TV turns the Equalizer and Boost off, choosing ATV-Server turns the Equalizer on.")

    static let library = Topic(title: "Library", text: "Media server: songs, videos, queues and playlists come from the ATV Player server. No server: plays files from this iPhone and an SMB share, without playlists, lyrics search, loudness levelling or the web remote. Offline: plays only what you downloaded; switch back to Media server when you are home.")

    static let smb = Topic(title: "SMB share and this iPhone", text: "An SMB share is a shared folder on a NAS or computer, e.g. nas/music; leave the user name empty for guest access. Files you copy into Files › On My iPhone › ATV Player show up under Folders › On this iPhone after Scan again. Scan again reads the SMB share and this iPhone's own folder together; the counts below the button show them apart.")

    static let soundProfiles = Topic(title: "Sound profiles", text: "Equalizer, Boost and Loudness belong to the output playing now. Every Bluetooth device and CarPlay keep their own (CarPlay wins over the car's Bluetooth); the speaker, wired headphones and AirPlay share the iPhone profile. A new device starts with the iPhone settings.")

    static let connection = Topic(title: "Connected through", text: "Shows the network the device is on and the address in use. A typed home address is always used, also on mobile data; leave it empty to find the server automatically and to use the internet address when away.")

    static let awayFromHome = Topic(title: "Away from home", text: "Audio only plays music videos as sound away from home and uses far less data.")

    /// iPhone: Settings → About → Help.
    static var iPhone: [Topic] {
        [library, smb, mixes, badges, themeEffects, soundProfiles, connection, awayFromHome, fastSeek, preferVideos]
    }

    /// Apple TV: Settings → About → Help.
    static var appleTV: [Topic] {
        [mixes, badges, themeEffects, atvServer, secondLanguage, preferVideos, fastSeek]
    }
}
