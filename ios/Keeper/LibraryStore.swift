import Foundation
import UIKit

/// The on-device library. Everything the UI shows comes from here, so the app works fully
/// offline; changes are applied locally first and queued as `PendingOp`s for the server.
@MainActor
final class LibraryStore: ObservableObject {
    @Published private(set) var items: [Item] = []
    @Published private(set) var pending: [PendingOp] = []
    @Published var lastSync: String?

    private struct Snapshot: Codable {
        var items: [Item]
        var pending: [PendingOp]
        var lastSync: String?
    }

    init() {
        load()
    }

    // MARK: - queries

    func item(_ id: String) -> Item? { items.first { $0.id == id } }

    func filtered(query: String, category: Category?, tag: String?) -> [Item] {
        let words = query.folding(options: [.caseInsensitive, .diacriticInsensitive], locale: nil)
            .split(whereSeparator: { !$0.isLetter && !$0.isNumber }).map(String.init)
        return items.filter { item in
            if let category, item.category != category.rawValue { return false }
            if let tag, !item.tags.contains(tag) { return false }
            guard !words.isEmpty else { return true }
            let blob = item.searchBlob
            return words.allSatisfy { blob.contains($0) }
        }
    }

    var categoryCounts: [(Category, Int)] {
        let counts = Dictionary(grouping: items.compactMap { $0.category.flatMap(Category.init(rawValue:)) }, by: { $0 })
            .mapValues(\.count)
        return Category.allCases.compactMap { c in counts[c].map { (c, $0) } }
    }

    var tagCounts: [(String, Int)] {
        let counts = Dictionary(grouping: items.flatMap(\.tags), by: { $0 }).mapValues(\.count)
        return counts.sorted { $0.value == $1.value ? $0.key < $1.key : $0.value > $1.value }
    }

    func hasPendingOps(for id: String) -> Bool { pending.contains { $0.itemID == id } }

    // MARK: - local changes (work offline)

    /// Adds a screenshot. Returns the new item's id.
    @discardableResult
    func addScreenshot(_ data: Data, note: String? = nil, createdAt: Date = .now) -> String? {
        var imageData = data
        var ext: String
        if let format = ImageFormat.detect(data) {
            ext = format.ext
        } else if let jpeg = UIImage(data: data)?.jpegData(compressionQuality: 0.9) {
            imageData = jpeg  // HEIC and friends: the server accepts PNG/JPEG/WebP/GIF
            ext = "jpg"
        } else {
            return nil
        }
        let id = "ios-" + UUID().uuidString.lowercased()
        let filename = "\(id).\(ext)"
        do {
            try imageData.write(to: AppGroup.images.appendingPathComponent(filename), options: .atomic)
        } catch {
            return nil
        }
        let trimmedNote = note?.trimmingCharacters(in: .whitespacesAndNewlines)
        items.insert(Item(localID: id, localImage: filename, note: trimmedNote?.isEmpty == false ? trimmedNote : nil, createdAt: createdAt), at: 0)
        pending.append(PendingOp(itemID: id, kind: .upload))
        save()
        return id
    }

    /// Moves screenshots saved by the share extension into the library.
    func importInbox() {
        let fm = FileManager.default
        guard let files = try? fm.contentsOfDirectory(at: AppGroup.inbox, includingPropertiesForKeys: nil) else { return }
        for sidecar in files where sidecar.pathExtension == "json" {
            let base = sidecar.deletingPathExtension()
            guard let image = files.first(where: { $0.deletingPathExtension() == base && $0.pathExtension != "json" }),
                  let data = try? Data(contentsOf: image) else { continue }
            let entry = (try? Data(contentsOf: sidecar)).flatMap { try? JSONDecoder.inbox.decode(InboxEntry.self, from: $0) }
            if addScreenshot(data, note: entry?.note, createdAt: entry?.createdAt ?? .now) != nil {
                try? fm.removeItem(at: image)
                try? fm.removeItem(at: sidecar)
            }
        }
    }

    func update(_ id: String, _ patch: ItemPatch) {
        guard let i = items.firstIndex(where: { $0.id == id }) else { return }
        if let title = patch.title { items[i].title = title }
        if let note = patch.note { items[i].note = note }
        if let category = patch.category { items[i].category = category }
        if let tags = patch.tags { items[i].tags = Self.normalize(tags) }
        var patch = patch
        patch.tags = patch.tags.map(Self.normalize)
        pending.append(PendingOp(itemID: id, kind: .update(patch)))
        save()
    }

    func reanalyze(_ id: String) {
        guard let i = items.firstIndex(where: { $0.id == id }), !items[i].pendingUpload else { return }
        items[i].status = "processing"
        items[i].error = nil
        pending.append(PendingOp(itemID: id, kind: .reanalyze))
        save()
    }

    func delete(_ id: String) {
        guard let item = item(id) else { return }
        let neverUploaded = item.pendingUpload
        removeLocally(id)
        // Nothing to tell the server about an item it never received.
        if !neverUploaded { pending.append(PendingOp(itemID: id, kind: .delete)) }
        save()
    }

    // MARK: - applying server state (used by SyncEngine)

    func completeOp(_ op: PendingOp, result: Item? = nil) {
        pending.removeAll { $0.id == op.id }
        if let result { merge(result) }
        save()
    }

    /// The server says this item is gone for good (e.g. deleted from another device).
    func dropItem(_ id: String) {
        removeLocally(id)
        save()
    }

    func apply(_ delta: SyncResponse) {
        for id in delta.deleted where !hasPendingOps(for: id) {
            removeLocally(id)
        }
        for item in delta.items where !hasPendingOps(for: item.id) {
            merge(item)
        }
        lastSync = delta.serverTime
        save()
    }

    /// Forget everything cached and download the library again on next sync (keeps queued changes).
    func resetCache() {
        let keep = Set(pending.map(\.itemID))
        for item in items where !keep.contains(item.id) {
            if let file = item.localImage { try? FileManager.default.removeItem(at: AppGroup.images.appendingPathComponent(file)) }
        }
        items.removeAll { !keep.contains($0.id) }
        lastSync = nil
        save()
    }

    private func merge(_ server: Item) {
        var incoming = server
        if let i = items.firstIndex(where: { $0.id == server.id }) {
            incoming.localImage = items[i].localImage
            items[i] = incoming
        } else {
            items.append(incoming)
        }
        items.sort { $0.createdAt > $1.createdAt }
    }

    private func removeLocally(_ id: String) {
        if let file = item(id)?.localImage {
            try? FileManager.default.removeItem(at: AppGroup.images.appendingPathComponent(file))
        }
        items.removeAll { $0.id == id }
        pending.removeAll { $0.itemID == id && $0.kind != .delete }
    }

    private static func normalize(_ tags: [String]) -> [String] {
        let cleaned = tags.map {
            $0.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
                .replacingOccurrences(of: "#", with: "")
                .replacingOccurrences(of: " ", with: "-")
        }
        return Array(Set(cleaned.filter { !$0.isEmpty })).sorted()
    }

    // MARK: - persistence

    private func load() {
        guard let data = try? Data(contentsOf: AppGroup.stateFile),
              let snapshot = try? JSONDecoder().decode(Snapshot.self, from: data) else { return }
        items = snapshot.items
        pending = snapshot.pending
        lastSync = snapshot.lastSync
    }

    private func save() {
        let snapshot = Snapshot(items: items, pending: pending, lastSync: lastSync)
        guard let data = try? JSONEncoder().encode(snapshot) else { return }
        try? data.write(to: AppGroup.stateFile, options: .atomic)
    }
}
