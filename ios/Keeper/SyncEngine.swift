import Foundation
import Network

/// Pushes queued local changes to the self-hosted server and pulls down what changed there.
///
/// Sync runs when the app starts or returns to the foreground, when the network comes back,
/// after every local change, on pull-to-refresh, and from a background refresh task.
/// If the server can't be reached, nothing is lost: changes stay queued until the next attempt.
@MainActor
final class SyncEngine: ObservableObject {
    enum Status: Equatable {
        case notConfigured
        case idle
        case syncing
        case offline(String)
        case failed(String)
    }

    @Published private(set) var status: Status = .idle
    @Published private(set) var lastSuccess: Date?

    private let store: LibraryStore
    private let monitor = NWPathMonitor()
    private var running = false
    private var rerun = false
    private var followUp: Task<Void, Never>?

    init(store: LibraryStore) {
        self.store = store
        monitor.pathUpdateHandler = { [weak self] path in
            guard path.status == .satisfied else { return }
            Task { @MainActor in self?.requestSync() }
        }
        monitor.start(queue: DispatchQueue(label: "keeper.network"))
    }

    /// Fire-and-forget sync, coalescing overlapping requests.
    func requestSync() {
        Task { await sync() }
    }

    func sync() async {
        store.importInbox()
        if running {
            rerun = true
            return
        }
        guard let api = ServerSettings.client else {
            status = .notConfigured
            return
        }
        running = true
        status = .syncing
        defer { running = false }

        repeat {
            rerun = false
            do {
                _ = try await api.health()
            } catch {
                status = .offline(error.localizedDescription)
                return
            }
            do {
                try await pushPending(api)
                let delta = try await api.sync(since: store.lastSync)
                store.apply(delta)
                lastSuccess = .now
                status = .idle
            } catch let error as APIError {
                if case .transport = error { status = .offline(error.localizedDescription) } else { status = .failed(error.localizedDescription) }
                return
            } catch {
                status = .failed(error.localizedDescription)
                return
            }
        } while rerun

        scheduleFollowUpIfProcessing()
        ImageCache.shared.prefetch(store.items.compactMap { $0.imageUrl.flatMap(URL.init(string:)) })
    }

    /// Replays queued ops in order. Stops at the first retryable failure so ordering is preserved.
    private func pushPending(_ api: APIClient) async throws {
        while let op = store.pending.first {
            do {
                switch op.kind {
                case .upload where store.item(op.itemID)?.isLink == true:
                    let saved = try await api.captureLink(item: store.item(op.itemID)!)
                    if saved.id != op.itemID {
                        store.replaceLocal(op.itemID, with: saved)
                    } else {
                        store.completeOp(op, result: saved)
                    }
                case .upload:
                    guard let item = store.item(op.itemID), let file = item.localImage,
                          let data = try? Data(contentsOf: AppGroup.images.appendingPathComponent(file)) else {
                        store.completeOp(op)  // the screenshot is gone; nothing to upload
                        continue
                    }
                    store.completeOp(op, result: try await api.upload(item: item, image: data, filename: file))
                case .update(let patch):
                    store.completeOp(op, result: try await api.update(id: op.itemID, patch: patch))
                case .correct(let correction):
                    store.completeOp(op, result: try await api.correct(id: op.itemID, correction: correction))
                case .reanalyze:
                    store.completeOp(op, result: try await api.reanalyze(id: op.itemID))
                case .delete:
                    try await api.delete(id: op.itemID)
                    store.completeOp(op)
                }
            } catch let error as APIError where error.isPermanent {
                if case .http(410, _) = error {
                    store.dropItem(op.itemID)  // deleted on another device while this one was offline
                }
                store.completeOp(op)  // retrying would fail forever; drop it
            }
        }
    }

    /// While the server is still analyzing new screenshots, check back shortly.
    private func scheduleFollowUpIfProcessing() {
        followUp?.cancel()
        guard store.items.contains(where: { $0.status == "processing" }) else { return }
        followUp = Task { [weak self] in
            try? await Task.sleep(for: .seconds(4))
            guard !Task.isCancelled else { return }
            await self?.sync()
        }
    }
}
