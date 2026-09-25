import SwiftUI
import UIKit
import UniformTypeIdentifiers

/// "Share → Magpie" from Photos, the screenshot editor, Safari, Instagram, etc.
///
/// Extensions have tight memory and time limits and may run while the phone is offline,
/// so this only saves the images (plus an optional note) into the shared inbox.
/// The app imports and uploads them the next time it runs.
final class ShareViewController: UIViewController {
    private var images: [Data] = []

    override func viewDidLoad() {
        super.viewDidLoad()
        Task { @MainActor in
            images = await loadImages()
            let root = ShareView(count: images.count, onSave: { [weak self] note in self?.save(note: note) },
                                 onCancel: { [weak self] in self?.finish() })
            let host = UIHostingController(rootView: root)
            addChild(host)
            host.view.frame = view.bounds
            host.view.autoresizingMask = [.flexibleWidth, .flexibleHeight]
            view.addSubview(host.view)
            host.didMove(toParent: self)
        }
    }

    private func loadImages() async -> [Data] {
        let providers = (extensionContext?.inputItems as? [NSExtensionItem] ?? [])
            .flatMap { $0.attachments ?? [] }
            .filter { $0.hasItemConformingToTypeIdentifier(UTType.image.identifier) }
        var result: [Data] = []
        for provider in providers {
            if let data = await Self.imageData(from: provider) { result.append(data) }
        }
        return result
    }

    private static func imageData(from provider: NSItemProvider) async -> Data? {
        // Most sources hand over the encoded file; the screenshot editor may hand over a UIImage.
        let data: Data? = await withCheckedContinuation { cont in
            provider.loadDataRepresentation(forTypeIdentifier: UTType.image.identifier) { data, _ in cont.resume(returning: data) }
        }
        if let data, ImageFormat.detect(data) != nil || UIImage(data: data) != nil { return data }
        return await withCheckedContinuation { cont in
            provider.loadItem(forTypeIdentifier: UTType.image.identifier) { item, _ in
                switch item {
                case let image as UIImage: cont.resume(returning: image.pngData())
                case let url as URL: cont.resume(returning: try? Data(contentsOf: url))
                case let data as Data: cont.resume(returning: data)
                default: cont.resume(returning: nil)
                }
            }
        }
    }

    private func save(note: String) {
        let trimmed = note.trimmingCharacters(in: .whitespacesAndNewlines)
        for data in images {
            let name = UUID().uuidString
            let ext = ImageFormat.detect(data)?.ext ?? "img"   // the app converts unknown formats
            let entry = InboxEntry(note: trimmed.isEmpty ? nil : trimmed, createdAt: .now)
            do {
                try data.write(to: AppGroup.inbox.appendingPathComponent("\(name).\(ext)"), options: .atomic)
                // Sidecar last: the app only imports images whose sidecar exists.
                try JSONEncoder.inbox.encode(entry).write(to: AppGroup.inbox.appendingPathComponent("\(name).json"), options: .atomic)
            } catch {
                continue
            }
        }
        finish()
    }

    private func finish() {
        extensionContext?.completeRequest(returningItems: nil)
    }
}

private struct ShareView: View {
    let count: Int
    let onSave: (String) -> Void
    let onCancel: () -> Void
    @State private var note = ""

    var body: some View {
        NavigationStack {
            Form {
                if count == 0 {
                    Text("No image found to save. Magpie saves screenshots and photos.")
                } else {
                    Section {
                        Label(count == 1 ? "1 screenshot" : "\(count) screenshots", systemImage: "photo.on.rectangle")
                        TextField("Note (optional) — e.g. “Dana recommended”", text: $note, axis: .vertical)
                    } footer: {
                        Text("Magpie will identify what this is and look up the details when it syncs with your server.")
                    }
                }
            }
            .navigationTitle("Save to Magpie")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel", action: onCancel) }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") { onSave(note) }.disabled(count == 0)
                }
            }
        }
    }
}
