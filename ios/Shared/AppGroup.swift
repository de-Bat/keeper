import Foundation

/// Storage shared between the app and the share extension.
///
/// The share extension never talks to the network: it drops screenshots into the
/// inbox, and the app imports them into its local store and upload queue.
enum AppGroup {
    /// Must match the App Group in both targets' entitlements (see project.yml).
    static let identifier = "group.com.example.magpie"

    static var container: URL {
        if let url = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: identifier) {
            return url
        }
        // Fallback for builds without the entitlement (e.g. previews); the extension won't work then.
        return FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
    }

    static var inbox: URL { directory("Inbox") }
    static var images: URL { directory("Images") }
    static var stateFile: URL { container.appendingPathComponent("state.json") }

    private static func directory(_ name: String) -> URL {
        let url = container.appendingPathComponent(name, isDirectory: true)
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }
}

/// Sidecar written next to each image in the inbox.
struct InboxEntry: Codable {
    var note: String?
    var createdAt: Date
}

enum ImageFormat {
    /// Detects PNG/JPEG/GIF/WebP by magic bytes. Other formats (HEIC...) return nil and are converted to JPEG.
    static func detect(_ data: Data) -> (ext: String, mime: String)? {
        let b = [UInt8](data.prefix(12))
        guard b.count >= 12 else { return nil }
        if b[0] == 0x89, b[1] == 0x50, b[2] == 0x4E, b[3] == 0x47 { return ("png", "image/png") }
        if b[0] == 0xFF, b[1] == 0xD8 { return ("jpg", "image/jpeg") }
        if b[0] == 0x47, b[1] == 0x49, b[2] == 0x46 { return ("gif", "image/gif") }
        if b[0] == 0x52, b[1] == 0x49, b[2] == 0x46, b[3] == 0x46, b[8] == 0x57, b[9] == 0x45, b[10] == 0x42, b[11] == 0x50 {
            return ("webp", "image/webp")
        }
        return nil
    }

    static func mime(forExtension ext: String) -> String {
        switch ext.lowercased() {
        case "png": return "image/png"
        case "gif": return "image/gif"
        case "webp": return "image/webp"
        default: return "image/jpeg"
        }
    }
}

extension JSONDecoder {
    static let inbox: JSONDecoder = {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .iso8601
        return d
    }()
}

extension JSONEncoder {
    static let inbox: JSONEncoder = {
        let e = JSONEncoder()
        e.dateEncodingStrategy = .iso8601
        return e
    }()
}
