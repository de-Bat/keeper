import Foundation

/// Arbitrary JSON (the server's per-category `metadata` object).
enum JSONValue: Codable, Equatable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case array([JSONValue])
    case object([String: JSONValue])
    case null

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let v = try? c.decode(Bool.self) { self = .bool(v) }
        else if let v = try? c.decode(Double.self) { self = .number(v) }
        else if let v = try? c.decode(String.self) { self = .string(v) }
        else if let v = try? c.decode([JSONValue].self) { self = .array(v) }
        else { self = .object(try c.decode([String: JSONValue].self)) }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let v): try c.encode(v)
        case .number(let v): try c.encode(v)
        case .bool(let v): try c.encode(v)
        case .array(let v): try c.encode(v)
        case .object(let v): try c.encode(v)
        case .null: try c.encodeNil()
        }
    }

    var string: String? {
        switch self {
        case .string(let v): return v.isEmpty ? nil : v
        case .number(let v):
            guard v.rounded() == v, abs(v) < 1e15 else { return v.formatted() }
            return abs(v) < 10_000 ? String(Int(v)) : Int(v).formatted()  // years stay "2010"
        case .bool(let v): return v ? "Yes" : "No"
        case .array(let v):
            let parts = v.compactMap(\.string)
            return parts.isEmpty ? nil : parts.joined(separator: ", ")
        case .object, .null: return nil
        }
    }

    var strings: [String] {
        if case .array(let v) = self { return v.compactMap(\.string) }
        return string.map { [$0] } ?? []
    }

    /// All leaf strings, for local full-text search.
    var searchText: [String] {
        switch self {
        case .string(let v): return v.hasPrefix("http") ? [] : [v]
        case .array(let v): return v.flatMap(\.searchText)
        case .object(let v): return v.values.flatMap(\.searchText)
        default: return []
        }
    }
}

struct ItemLink: Codable, Equatable, Hashable {
    var label: String
    var url: String
}

/// Something else the model thinks the screenshot might be.
struct Alternative: Codable, Equatable, Hashable {
    var title: String
    var category: String?
    var year: Int?
    var canonicalUrl: String?
    var why: String?

    enum CodingKeys: String, CodingKey {
        case title, category, year, why
        case canonicalUrl = "canonical_url"
    }
}

/// "The model got it wrong": corrected facts, and/or a description for Claude to look again.
struct Correction: Codable, Equatable {
    var title: String?
    var category: String?
    var year: Int?
    var canonicalUrl: String?
    var hint: String?

    enum CodingKeys: String, CodingKey {
        case title, category, year, hint
        case canonicalUrl = "canonical_url"
    }

    var isEmpty: Bool { title == nil && category == nil && year == nil && canonicalUrl == nil && hint == nil }
    var hasFacts: Bool { title != nil || category != nil || year != nil || canonicalUrl != nil }
}

/// What identifying this item cost (measured on the server).
struct ItemUsage: Codable, Equatable {
    var costUsd: Double
    var runs: Int
    var webSearches: Int
    var via: [String]

    enum CodingKeys: String, CodingKey {
        case runs, via
        case costUsd = "cost_usd", webSearches = "web_searches"
    }
}

struct Item: Codable, Identifiable, Equatable {
    var id: String
    var createdAt: String
    var updatedAt: String
    var status: String                 // processing | ready | error | queued (local only)
    var error: String?
    var imageFile: String?             // file name on the server (/media/<imageFile>)
    var note: String?
    var category: String?
    var sourcePlatform: String?
    var title: String?
    var subtitle: String?
    var summary: String?
    var canonicalUrl: String?
    var imageUrl: String?
    var metadata: [String: JSONValue]
    var links: [ItemLink]
    var tags: [String]
    var confidence: Int?               // 0-100, how sure the model is (100 once corrected)
    var confidenceReason: String?
    var alternatives: [Alternative]
    var corrected: Bool
    var needsReview: Bool
    var usage: ItemUsage?
    var batchPending: Bool

    // Local-only state
    var localImage: String?            // file name in AppGroup.images
    var pendingUpload: Bool

    enum CodingKeys: String, CodingKey {
        case id, status, error, note, category, title, subtitle, summary, metadata, links, tags
        case createdAt = "created_at", updatedAt = "updated_at", imageFile = "image_file"
        case sourcePlatform = "source_platform", canonicalUrl = "canonical_url", imageUrl = "image_url"
        case localImage = "local_image", pendingUpload = "pending_upload"
        case confidence, alternatives, corrected
        case confidenceReason = "confidence_reason", needsReview = "needs_review"
        case usage, batchPending = "batch_pending"
    }

    init(localID: String, localImage: String, note: String?, createdAt: Date) {
        let ts = ISO8601DateFormatter().string(from: createdAt)
        id = localID
        self.createdAt = ts
        updatedAt = ts
        status = "queued"
        self.note = note
        self.localImage = localImage
        pendingUpload = true
        metadata = [:]
        links = []
        tags = []
        alternatives = []
        corrected = false
        needsReview = false
        batchPending = false
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        createdAt = try c.decodeIfPresent(String.self, forKey: .createdAt) ?? ""
        updatedAt = try c.decodeIfPresent(String.self, forKey: .updatedAt) ?? ""
        status = try c.decodeIfPresent(String.self, forKey: .status) ?? "ready"
        error = try c.decodeIfPresent(String.self, forKey: .error)
        imageFile = try c.decodeIfPresent(String.self, forKey: .imageFile)
        note = try c.decodeIfPresent(String.self, forKey: .note)
        category = try c.decodeIfPresent(String.self, forKey: .category)
        sourcePlatform = try c.decodeIfPresent(String.self, forKey: .sourcePlatform)
        title = try c.decodeIfPresent(String.self, forKey: .title)
        subtitle = try c.decodeIfPresent(String.self, forKey: .subtitle)
        summary = try c.decodeIfPresent(String.self, forKey: .summary)
        canonicalUrl = try c.decodeIfPresent(String.self, forKey: .canonicalUrl)
        imageUrl = try c.decodeIfPresent(String.self, forKey: .imageUrl)
        metadata = (try? c.decodeIfPresent([String: JSONValue].self, forKey: .metadata)) ?? [:]
        links = (try? c.decodeIfPresent([ItemLink].self, forKey: .links)) ?? []
        tags = try c.decodeIfPresent([String].self, forKey: .tags) ?? []
        confidence = try? c.decodeIfPresent(Int.self, forKey: .confidence)
        confidenceReason = try c.decodeIfPresent(String.self, forKey: .confidenceReason)
        alternatives = (try? c.decodeIfPresent([Alternative].self, forKey: .alternatives)) ?? []
        corrected = (try? c.decodeIfPresent(Bool.self, forKey: .corrected)) ?? false
        needsReview = (try? c.decodeIfPresent(Bool.self, forKey: .needsReview)) ?? false
        usage = try? c.decodeIfPresent(ItemUsage.self, forKey: .usage)
        batchPending = (try? c.decodeIfPresent(Bool.self, forKey: .batchPending)) ?? false
        localImage = try c.decodeIfPresent(String.self, forKey: .localImage)
        pendingUpload = try c.decodeIfPresent(Bool.self, forKey: .pendingUpload) ?? false
    }

    func meta(_ key: String) -> String? { metadata[key]?.string }

    var displayTitle: String {
        if let title, !title.isEmpty { return title }
        switch status {
        case "queued": return "Waiting to upload"
        case "processing": return "Analyzing…"
        case "error": return "Couldn't identify"
        default: return "Untitled"
        }
    }

    var createdDate: Date? {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f.date(from: createdAt) ?? ISO8601DateFormatter().date(from: createdAt)
    }

    /// Lowercased, diacritic-folded text used for offline search.
    var searchBlob: String {
        var parts = [title, subtitle, summary, note, category, sourcePlatform].compactMap { $0 }
        parts += tags
        parts += metadata.values.flatMap(\.searchText)
        return parts.joined(separator: " ").folding(options: [.caseInsensitive, .diacriticInsensitive], locale: nil)
    }
}

/// Edits sent with PATCH /api/items/{id}. Nil fields are omitted.
struct ItemPatch: Codable, Equatable {
    var title: String?
    var note: String?
    var category: String?
    var tags: [String]?
}

/// A change made on this device that the server hasn't seen yet. Replayed in order.
struct PendingOp: Codable, Identifiable, Equatable {
    enum Kind: Codable, Equatable {
        case upload
        case update(ItemPatch)
        case correct(Correction)
        case reanalyze
        case delete
    }

    var id = UUID()
    var itemID: String
    var kind: Kind
}

struct SyncResponse: Decodable {
    let serverTime: String
    let items: [Item]
    let deleted: [String]

    enum CodingKeys: String, CodingKey {
        case items, deleted
        case serverTime = "server_time"
    }
}

enum Category: String, CaseIterable, Identifiable {
    case movie, tv_show, github_repo, recipe, book, music, podcast, video, article, product, place, event, app, course, other

    var id: String { rawValue }

    var label: String {
        switch self {
        case .movie: return "Movie"
        case .tv_show: return "TV show"
        case .github_repo: return "GitHub"
        case .recipe: return "Recipe"
        case .book: return "Book"
        case .music: return "Music"
        case .podcast: return "Podcast"
        case .video: return "Video"
        case .article: return "Article"
        case .product: return "Product"
        case .place: return "Place"
        case .event: return "Event"
        case .app: return "App"
        case .course: return "Course"
        case .other: return "Other"
        }
    }

    var symbol: String {
        switch self {
        case .movie: return "film"
        case .tv_show: return "tv"
        case .github_repo: return "chevron.left.forwardslash.chevron.right"
        case .recipe: return "fork.knife"
        case .book: return "book"
        case .music: return "music.note"
        case .podcast: return "mic"
        case .video: return "play.rectangle"
        case .article: return "newspaper"
        case .product: return "bag"
        case .place: return "mappin.and.ellipse"
        case .event: return "calendar"
        case .app: return "apps.iphone"
        case .course: return "graduationcap"
        case .other: return "pin"
        }
    }

    /// Posters are portrait; everything else is a landscape preview.
    var isPortrait: Bool { self == .movie || self == .tv_show || self == .book }
}
