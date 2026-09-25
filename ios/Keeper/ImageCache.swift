import CryptoKit
import SwiftUI
import UIKit

/// Disk cache for posters and server-hosted screenshots so the library looks right offline.
actor ImageCache {
    static let shared = ImageCache()

    private let directory: URL = {
        let url = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0].appendingPathComponent("images")
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }()
    private var memory: [String: UIImage] = [:]
    private var inFlight: [String: Task<UIImage?, Never>] = [:]

    func image(for request: URLRequest) async -> UIImage? {
        guard let url = request.url else { return nil }
        let key = Self.key(url)
        if let hit = memory[key] { return hit }
        let file = directory.appendingPathComponent(key)
        if let data = try? Data(contentsOf: file), let image = UIImage(data: data) {
            memory[key] = image
            return image
        }
        if let task = inFlight[key] { return await task.value }
        let task = Task<UIImage?, Never> {
            guard let (data, response) = try? await URLSession.shared.data(for: request),
                  (response as? HTTPURLResponse)?.statusCode == 200,
                  let image = UIImage(data: data) else { return nil }
            try? data.write(to: file, options: .atomic)
            return image
        }
        inFlight[key] = task
        let image = await task.value
        inFlight[key] = nil
        if let image { memory[key] = image }
        return image
    }

    nonisolated func prefetch(_ urls: [URL]) {
        Task.detached(priority: .background) {
            for url in urls { _ = await self.image(for: URLRequest(url: url)) }
        }
    }

    private static func key(_ url: URL) -> String {
        SHA256.hash(data: Data(url.absoluteString.utf8)).map { String(format: "%02x", $0) }.joined()
    }
}

/// Shows an item's best image: poster/preview if known, otherwise the screenshot itself.
struct ItemImage: View {
    let item: Item
    var preferScreenshot = false
    @State private var image: UIImage?

    var body: some View {
        // The overlay takes the rectangle's size, so a fill-scaled image can't grow the layout.
        Rectangle()
            .fill(Color(.secondarySystemBackground))
            .overlay {
                if let image {
                    Image(uiImage: image).resizable().scaledToFill()
                } else if item.isLink && item.category == nil {
                    VStack(spacing: 4) {
                        Image(systemName: "link").font(.title2)
                        Text(URL(string: item.sourceUrl ?? "")?.host ?? "").font(.caption).lineLimit(1)
                    }
                    .foregroundStyle(.secondary).padding(6)
                } else {
                    Image(systemName: Category(rawValue: item.category ?? "")?.symbol ?? (item.isLink ? "link" : "photo"))
                        .font(.largeTitle).foregroundStyle(.tertiary)
                }
            }
            .clipped()
            .task(id: "\(item.id)|\(item.imageUrl ?? "")|\(preferScreenshot)") { await load() }
    }

    private func load() async {
        if !preferScreenshot, let s = item.imageUrl, let url = URL(string: s),
           let poster = await ImageCache.shared.image(for: URLRequest(url: url)) {
            image = poster
            return
        }
        image = await ItemImage.screenshot(for: item)
    }

    static func screenshot(for item: Item) async -> UIImage? {
        if let file = item.localImage,
           let local = UIImage(contentsOfFile: AppGroup.images.appendingPathComponent(file).path) {
            return local
        }
        if let file = item.imageFile, !file.isEmpty, let api = ServerSettings.client {
            return await ImageCache.shared.image(for: api.mediaRequest(file))
        }
        return nil
    }
}
