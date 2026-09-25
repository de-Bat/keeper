import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var store: LibraryStore
    @EnvironmentObject private var sync: SyncEngine
    @Environment(\.dismiss) private var dismiss

    @State private var serverURL = ServerSettings.serverURL
    @State private var token = ServerSettings.token
    @State private var testResult: (ok: Bool, message: String)?
    @State private var testing = false
    @State private var confirmReset = false
    @State private var usage: UsageReport?
    @State private var usageError: String?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("http://192.168.1.20:8000", text: $serverURL)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    SecureField("API token (KEEPER_API_TOKEN)", text: $token)
                    Button {
                        Task { await test() }
                    } label: {
                        HStack {
                            Text("Test connection")
                            if testing { Spacer(); ProgressView() }
                        }
                    }
                    .disabled(serverURL.isEmpty || testing)
                    if let testResult {
                        Label(testResult.message, systemImage: testResult.ok ? "checkmark.circle.fill" : "xmark.octagon.fill")
                            .foregroundStyle(testResult.ok ? .green : .red)
                            .font(.footnote)
                    }
                } header: {
                    Text("Your Keeper server")
                } footer: {
                    Text("The self-hosted server that identifies screenshots and stores your library. Use its LAN, Tailscale or public address.")
                }

                Section("Sync") {
                    LabeledContent("Status", value: statusText)
                    LabeledContent("Waiting to sync", value: "\(store.pending.count)")
                    LabeledContent("Items on this phone", value: "\(store.items.count)")
                    if let last = sync.lastSuccess {
                        LabeledContent("Last synced", value: last.formatted(date: .omitted, time: .shortened))
                    }
                    Button("Sync now") { save(); sync.requestSync() }
                        .disabled(serverURL.isEmpty)
                }

                Section {
                    if let usage {
                        LabeledContent("Total", value: formatUSD(usage.totals.costUsd))
                        LabeledContent("Per screenshot", value: formatUSD(usage.perScreenshotUsd))
                        LabeledContent("Screenshots", value: "\(usage.totals.screenshots)")
                        LabeledContent("Projected / 30 days", value: formatUSD(usage.projected30dUsd))
                        LabeledContent("Sent to Claude", value: "\(Int((usage.claudeShare * 100).rounded()))%")
                        ForEach(usage.byAnalyzer) { a in
                            LabeledContent("\(a.analyzer) · \(a.mode ?? "")", value: "\(a.runs) × \(formatUSD(a.avgCostUsd ?? 0))")
                                .font(.footnote)
                        }
                    } else if let usageError {
                        Text(usageError).font(.footnote).foregroundStyle(.secondary)
                    } else {
                        ProgressView()
                    }
                } header: {
                    Text("Usage & cost · last 30 days")
                } footer: {
                    Text("Measured on your server from real token and search usage, at list prices.")
                }

                Section {
                    Button("Re-download library", role: .destructive) { confirmReset = true }
                } footer: {
                    Text("Clears the offline copy and downloads everything from the server again. Changes waiting to sync are kept.")
                }
            }
            .task { await loadUsage() }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                Button("Done") {
                    save()
                    sync.requestSync()
                    dismiss()
                }
            }
            .confirmationDialog("Re-download the whole library?", isPresented: $confirmReset, titleVisibility: .visible) {
                Button("Re-download", role: .destructive) {
                    store.resetCache()
                    sync.requestSync()
                }
            }
        }
    }

    private var statusText: String {
        switch sync.status {
        case .notConfigured: return "Not set up"
        case .idle: return "Up to date"
        case .syncing: return "Syncing…"
        case .offline: return "Server unreachable"
        case .failed(let message): return message
        }
    }

    private func loadUsage() async {
        guard let api = ServerSettings.client else { usageError = "Connect a server to see usage."; return }
        do { usage = try await api.usage() } catch { usageError = "Unavailable offline." }
    }

    private func save() {
        ServerSettings.serverURL = serverURL
        ServerSettings.token = token
    }

    private func test() async {
        save()
        testing = true
        defer { testing = false }
        guard let api = ServerSettings.client else {
            testResult = (false, "That doesn't look like a URL")
            return
        }
        do {
            try await api.verify()
            testResult = (true, "Connected")
            sync.requestSync()
        } catch APIError.http(401, _) {
            testResult = (false, "Server reachable, but the API token was rejected")
        } catch {
            testResult = (false, error.localizedDescription)
        }
    }
}
