import BackgroundTasks
import SwiftUI

@main
struct KeeperApp: App {
    @StateObject private var store: LibraryStore
    @StateObject private var sync: SyncEngine
    @Environment(\.scenePhase) private var scenePhase

    static let backgroundTaskID = (Bundle.main.bundleIdentifier ?? "keeper") + ".sync"

    init() {
        let store = LibraryStore()
        _store = StateObject(wrappedValue: store)
        _sync = StateObject(wrappedValue: SyncEngine(store: store))
    }

    var body: some Scene {
        WindowGroup {
            LibraryView()
                .environmentObject(store)
                .environmentObject(sync)
        }
        .onChange(of: scenePhase) { _, phase in
            switch phase {
            case .active: sync.requestSync()   // also picks up screenshots from the share extension
            case .background: scheduleBackgroundSync()
            default: break
            }
        }
        .backgroundTask(.appRefresh(Self.backgroundTaskID)) {
            await sync.sync()
            await scheduleBackgroundSync()
        }
    }

    /// Ask iOS to wake the app later so queued uploads go out even if it isn't opened.
    @MainActor
    private func scheduleBackgroundSync() {
        let request = BGAppRefreshTaskRequest(identifier: Self.backgroundTaskID)
        request.earliestBeginDate = Date(timeIntervalSinceNow: 15 * 60)
        try? BGTaskScheduler.shared.submit(request)
    }
}
