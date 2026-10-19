import SwiftUI

/// Shown until the server is found: Bonjour search status plus a field to type the address.
struct ServerSearchView: View {
    @EnvironmentObject var locator: ServerLocator
    @State private var address = ServerLocator.manualAddress
    @State private var invalid = false

    var body: some View {
        VStack(spacing: 28) {
            ProgressView()
            Text("ATV Player").font(.largeTitle.bold())
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
    }

    private func connect() {
        invalid = !locator.setManual(address)
    }
}
