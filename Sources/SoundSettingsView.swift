import SwiftUI
import TVVLCKit

/// Settings → Sound.
struct SoundSettingsView: View {
    @EnvironmentObject var engine: PlayerEngine
    @EnvironmentObject var connection: ServerConnection
    @Environment(\.settingsNav) private var nav
    @AppStorage(SoundSettings.presetKey) private var preset = "off"
    @AppStorage(SoundSettings.loudnessKey) private var loudness = SoundSettings.Loudness.raise.rawValue
    @AppStorage(SoundSettings.boostKey) private var boost = 0
    @AppStorage(AtvAudio.outputKey) private var soundOutput = AtvAudio.tvOutput
    @AppStorage(AtvAudio.lipSyncKey) private var lipSync = AtvAudio.defaultLipSync
    @AppStorage(SoundProfiles.activeNameKey) private var profileName = SoundProfiles.defaultName

    /// atv-audio can only be chosen while a sink is connected; a stored choice stays (and is shown) without one.
    private var outputOptions: [(label: String, value: String)] {
        if connection.state?.sink?.isConnected == true {
            return [("Apple TV", AtvAudio.tvOutput), ("atv-audio", AtvAudio.sinkOutput)]
        }
        var options: [(label: String, value: String)] = [("Apple TV", AtvAudio.tvOutput)]
        if soundOutput == AtvAudio.sinkOutput { options.append(("atv-audio (not connected)", AtvAudio.sinkOutput)) }
        return options
    }

    private var lipSyncChoice: Binding<Int> {
        Binding(get: { AtvAudio.clampLipSync(lipSync) }, set: { lipSync = $0 })
    }

    private var loudnessOn: Binding<Bool> {
        Binding(get: { loudness != SoundSettings.Loudness.off.rawValue },
                set: { loudness = SoundSettings.loudnessValue(on: $0) })
    }

    private var equalizerOptions: [(label: String, value: String)] { SoundSettings.presetChoices }

    var body: some View {
        SettingsPage {
            Section {
                ChoiceRow("Equalizer", selection: $preset, options: equalizerOptions)
                Button {
                    nav.push(.page(.equalizer))
                } label: {
                    HStack(spacing: 20) {
                        Text("Edit custom equalizer")
                        Spacer()
                        Text(preset == "custom" ? "In use" : "").foregroundStyle(.secondary)
                        Image(systemName: "chevron.right").font(.system(size: 24, weight: .semibold)).foregroundStyle(.secondary)
                    }
                }
            } header: {
                Text("Equalizer · \(profileName) profile")
            }
            Section {
                ChoiceRow("Boost", selection: $boost,
                          options: SoundSettings.boostRange.map { (SoundSettings.dbLabel(Float($0)), $0) })
            } header: {
                Text("Gain")
            } footer: {
                Text(SoundSettings.gainNote(loudnessGain: engine.levelGain))
                    .font(.system(size: 22)).foregroundStyle(.secondary)
            }
            Section {
                Toggle("Loudness", isOn: loudnessOn)
            } header: {
                Text("Loudness")
            } footer: {
                Text(SoundSettings.loudnessNote)
                    .font(.system(size: 22)).foregroundStyle(.secondary)
            }
            Section {
                ChoiceRow("Sound output", selection: $soundOutput, options: outputOptions)
                Text(AtvAudio.statusText(connection.state?.sink))
                    .font(.system(size: 24)).foregroundStyle(.secondary)
                if soundOutput == AtvAudio.sinkOutput {
                    ChoiceRow("Lip sync", selection: lipSyncChoice,
                              options: AtvAudio.lipSyncValues.map { (AtvAudio.lipSyncLabel($0), $0) })
                }
            } header: {
                Text("Sound output")
            } footer: {
                Text((soundOutput == AtvAudio.sinkOutput
                     ? "atv-audio plays the sound, the Apple TV shows the picture and stays silent (also while atv-audio is missing: playback pauses with a notice). Lip sync: how much earlier the headphones play (Bluetooth adds a delay): raise it if the sound comes late, lower it if it comes early."
                     : "atv-audio: a separate box plays the sound (e.g. aptX headphones), the Apple TV shows the picture. It can be chosen while it is connected.")
                     + " Equalizer, Boost and Loudness are kept separately for Apple TV and atv-audio.")
                    .font(.system(size: 22)).foregroundStyle(.secondary)
            }
        }
        .onChange(of: "\(preset)|\(loudness)|\(boost)") {
            engine.applySettings()
        }
        // Sent live to the sink: no player rebuild.
        .onChange(of: soundOutput) {
            engine.soundOutputChanged()
        }
        .onChange(of: lipSync) {
            engine.lipSyncChanged()
        }
    }
}

/// Graphic editor: ◀ ▶ choose a band, ▲ ▼ change it by 1 dB. "Load preset" (far left) fills the
/// bands from a built-in preset. ⏯ is NOT used here: it pauses/resumes playback everywhere in the
/// app (user feedback, build 18 used it for presets and it also toggled the music).
struct CustomEqualizerView: View {
    enum Field { case load, bands }
    @EnvironmentObject var engine: PlayerEngine
    @Environment(\.dismiss) private var dismiss
    @FocusState private var focus: Field?
    @State private var values: [Float] = []  // [preamp] + bands
    @State private var selected = 1
    @State private var changed = false
    @State private var showPresets = false
    @State private var loadedFrom: String?

    private let range: Float = 12
    private var labels: [String] {
        ["Pre"] + SoundSettings.frequencies.map { f in
            f >= 1000 ? String(format: f.truncatingRemainder(dividingBy: 1000) == 0 ? "%.0fk" : "%.1fk", f / 1000)
                : String(format: "%.0f", f)
        }
    }

    var body: some View {
        Group {
            if showPresets {
                // Inside the editor (not a pushed page), so the edit survives choosing a preset.
                PresetPickerList { tag in
                    if let (pre, bands) = SoundSettings.presetValues(tag) {
                        values = [pre.rounded()] + bands.map { $0.rounded() }
                        changed = true
                        loadedFrom = SoundSettings.presetName(tag)
                    }
                    showPresets = false
                }
            } else {
                editor
            }
        }
        // Back in the preset list returns to the bands; in the bands the Settings screen closes the page (saves).
        .onExitCommand(perform: presetsBack)
        .onChange(of: showPresets) {
            if !showPresets { DispatchQueue.main.async { focus = .bands } }
        }
        .onAppear {
            focus = .bands
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { if focus == nil { focus = .bands } }
            guard values.isEmpty else { return }
            var pre: Float, bands: [Float]
            if UserDefaults.standard.string(forKey: SoundSettings.customKey) == nil,
               let fromPreset = SoundSettings.presetValues(SoundSettings.preset) {
                (pre, bands) = fromPreset
            } else {
                (pre, bands) = SoundSettings.customValues()
            }
            values = [pre.rounded()] + bands.map { $0.rounded() }
        }
        .onDisappear {
            guard changed, !values.isEmpty else { return }
            SoundSettings.saveCustom(preamp: values[0], bands: Array(values.dropFirst()))
            UserDefaults.standard.set("custom", forKey: SoundSettings.presetKey)
            engine.applySettings()
        }
    }

    private var presetsBack: (() -> Void)? {
        showPresets ? { showPresets = false } : nil
    }

    private var editor: some View {
        VStack(spacing: 24) {
            Text("◀ ▶ band    ▲ ▼ ±1 dB    ◀ from Pre = presets    Back = save")
                .font(.system(size: 22))
                .foregroundStyle(.secondary)
            HStack(alignment: .center, spacing: 20) {
                Button {
                    showPresets = true
                } label: {
                    VStack(spacing: 10) {
                        Image(systemName: "slider.vertical.3").font(.system(size: 34))
                        Text("Load\npreset").font(.system(size: 20, weight: .semibold)).multilineTextAlignment(.center)
                    }
                    .foregroundStyle(.white)
                    .frame(width: 100, height: 140)
                }
                .focused($focus, equals: .load)
                // tvOS moves focus to a focusable neighbour BEFORE onMoveCommand sees the press, so an
                // always-focusable button here stole every ◀ (build 19). Only focusable while "Pre"
                // (leftmost) is selected or it already has focus; disabled = unreachable (seen on the
                // TV in build 11), so ◀ on other bands reaches onMoveCommand again.
                // Also disabled while nothing has the focus yet (the page is opening), so tvOS starts on the bands.
                .disabled(focus != .load && selected != 0)
                HStack(alignment: .bottom, spacing: 10) {
                    ForEach(values.indices, id: \.self) { i in
                        BandColumn(label: i < labels.count ? labels[i] : "", value: values[i], range: range,
                                   selected: i == selected, focused: focus == .bands)
                    }
                }
                .padding(20)
                .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 20))
                .focusable()
                .focusEffectDisabled()
                .focused($focus, equals: .bands)
                .onMoveCommand { direction in
                    guard !values.isEmpty else { return }
                    switch direction {
                    case .left:
                        if selected == 0 { focus = .load } else { selected -= 1 }  // past "Pre": the button
                    case .right: selected = min(values.count - 1, selected + 1)
                    case .up: values[selected] = min(range, values[selected] + 1); changed = true
                    case .down: values[selected] = max(-range, values[selected] - 1); changed = true
                    default: break
                    }
                }
            }
            Text(loadedFrom.map { "Started from \($0)" } ?? " ")
                .font(.system(size: 20))
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// Built-in curves to start the custom equalizer from: the app's own presets, then VLC's (Flat first).
/// Shown inside the equalizer editor, laid out like Settings → Sound.
struct PresetPickerList: View {
    let pick: (String) -> Void

    var body: some View {
        List {
            Section {
                ForEach(SoundSettings.appPresets, id: \.id) { p in
                    Button(p.name) { pick("a:\(p.id)") }
                }
            } header: {
                Text("ATV Player")
            }
            Section {
                ForEach(SoundSettings.presets, id: \.index) { p in
                    Button(p.name) { pick("p:\(p.index)") }
                }
            } header: {
                Text("VLC")
            }
        }
        .font(.system(size: 30))
    }
}

struct BandColumn: View {
    let label: String
    let value: Float
    let range: Float
    let selected: Bool
    let focused: Bool

    var body: some View {
        let height: CGFloat = 300
        let half = height / 2
        let barHeight = CGFloat(abs(value) / range) * half
        VStack(spacing: 12) {
            Text(value > 0 ? "+\(Int(value))" : "\(Int(value))")
                .font(.system(size: 20, weight: selected ? .bold : .regular))
            ZStack {
                RoundedRectangle(cornerRadius: 6).fill(.white.opacity(0.08)).frame(width: 40, height: height)
                Rectangle().fill(.white.opacity(0.35)).frame(width: 40, height: 2)
                RoundedRectangle(cornerRadius: 6)
                    .fill(selected && focused ? Color.accentColor : .white.opacity(0.6))
                    .frame(width: 40, height: max(barHeight, 3))
                    .offset(y: value >= 0 ? -barHeight / 2 : barHeight / 2)
            }
            .frame(height: height)
            Text(label)
                .font(.system(size: 18, weight: selected ? .bold : .regular))
        }
        .padding(.horizontal, 4)
        .padding(.vertical, 10)
        .background(selected ? Color.white.opacity(focused ? 0.18 : 0.08) : .clear,
                    in: RoundedRectangle(cornerRadius: 12))
    }
}
