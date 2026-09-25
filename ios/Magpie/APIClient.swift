import Foundation
import Security

/// Where the self-hosted server lives. The URL is in UserDefaults, the token in the Keychain.
enum ServerSettings {
    private static let urlKey = "serverURL"
    private static let tokenAccount = "magpie-api-token"

    static var serverURL: String {
        get { UserDefaults.standard.string(forKey: urlKey) ?? "" }
        set { UserDefaults.standard.set(newValue.trimmingCharacters(in: .whitespacesAndNewlines), forKey: urlKey) }
    }

    static var token: String {
        get { Keychain.read(tokenAccount) ?? "" }
        set {
            let value = newValue.trimmingCharacters(in: .whitespacesAndNewlines)
            value.isEmpty ? Keychain.delete(tokenAccount) : Keychain.write(tokenAccount, value)
        }
    }

    static var client: APIClient? {
        var raw = serverURL
        guard !raw.isEmpty else { return nil }
        if !raw.contains("://") { raw = "http://" + raw }
        guard let url = URL(string: raw) else { return nil }
        return APIClient(baseURL: url, token: token.isEmpty ? nil : token)
    }
}

enum Keychain {
    static func read(_ account: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword, kSecAttrAccount as String: account,
            kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var out: AnyObject?
        guard SecItemCopyMatching(query as CFDictionary, &out) == errSecSuccess, let data = out as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func write(_ account: String, _ value: String) {
        delete(account)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword, kSecAttrAccount as String: account,
            kSecValueData as String: Data(value.utf8),
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlock,
        ]
        SecItemAdd(query as CFDictionary, nil)
    }

    static func delete(_ account: String) {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrAccount as String: account]
        SecItemDelete(query as CFDictionary)
    }
}

enum APIError: LocalizedError {
    case http(Int, String)
    case transport(Error)
    case decoding(Error)

    var errorDescription: String? {
        switch self {
        case .http(let code, let detail): return "Server error \(code): \(detail)"
        case .transport(let e): return e.localizedDescription
        case .decoding(let e): return "Unexpected response: \(e.localizedDescription)"
        }
    }

    /// The request can never succeed as-is (bad input, item gone). Auth, timeouts and
    /// rate limits are not permanent — the user can fix them, or they pass.
    var isPermanent: Bool {
        if case .http(let code, _) = self { return (400..<500).contains(code) && ![401, 403, 408, 429].contains(code) }
        return false
    }
}

struct Health: Decodable {
    let ok: Bool
    let authRequired: Bool

    enum CodingKeys: String, CodingKey {
        case ok
        case authRequired = "auth_required"
    }
}

struct APIClient {
    let baseURL: URL
    let token: String?

    private static let session: URLSession = {
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 20
        config.waitsForConnectivity = false
        return URLSession(configuration: config)
    }()

    func health() async throws -> Health {
        try await send(request("api/health", timeout: 5))
    }

    /// Checks reachability *and* that the token is accepted.
    func verify() async throws {
        _ = try await health()
        let _: SyncResponse = try await send(request("api/sync?since=9999"))
    }

    func sync(since: String?) async throws -> SyncResponse {
        var path = "api/sync"
        if let since, let q = since.addingPercentEncoding(withAllowedCharacters: .alphanumerics) {
            path += "?since=\(q)"
        }
        return try await send(request(path))
    }

    func upload(item: Item, image: Data, filename: String) async throws -> Item {
        let boundary = "magpie-\(UUID().uuidString)"
        var body = Data()
        func field(_ name: String, _ value: String) {
            body.append("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(name)\"\r\n\r\n\(value)\r\n")
        }
        field("id", item.id)
        field("created_at", item.createdAt)
        if let note = item.note, !note.isEmpty { field("note", note) }
        if !item.tags.isEmpty { field("tags", item.tags.joined(separator: ",")) }
        let ext = (filename as NSString).pathExtension
        body.append("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\(filename)\"\r\n")
        body.append("Content-Type: \(ImageFormat.mime(forExtension: ext))\r\n\r\n")
        body.append(image)
        body.append("\r\n--\(boundary)--\r\n")

        var req = request("api/items", method: "POST", timeout: 120)
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        req.httpBody = body
        return try await send(req)
    }

    /// Save a shared link. The server may answer with an existing item if the link was saved before.
    func captureLink(item: Item) async throws -> Item {
        let boundary = "magpie-\(UUID().uuidString)"
        var body = Data()
        func field(_ name: String, _ value: String) {
            body.append("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(name)\"\r\n\r\n\(value)\r\n")
        }
        field("url", item.sourceUrl ?? "")
        field("id", item.id)
        field("created_at", item.createdAt)
        if let note = item.note, !note.isEmpty { field("note", note) }
        if !item.tags.isEmpty { field("tags", item.tags.joined(separator: ",")) }
        body.append("--\(boundary)--\r\n")
        var req = request("api/items", method: "POST", timeout: 60)
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        req.httpBody = body
        return try await send(req)
    }

    func update(id: String, patch: ItemPatch) async throws -> Item {
        var req = request("api/items/\(id)", method: "PATCH")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONEncoder().encode(patch)
        return try await send(req)
    }

    func correct(id: String, correction: Correction) async throws -> Item {
        var req = request("api/items/\(id)/correct", method: "POST")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONEncoder().encode(correction)
        return try await send(req)
    }

    func usage(days: Int = 30) async throws -> UsageReport {
        try await send(request("api/usage?days=\(days)"))
    }

    func reanalyze(id: String) async throws -> Item {
        try await send(request("api/items/\(id)/reanalyze", method: "POST"))
    }

    func delete(id: String) async throws {
        do {
            _ = try await raw(request("api/items/\(id)", method: "DELETE"))
        } catch APIError.http(404, _) {
            // already gone
        }
    }

    /// Authenticated GET for server-hosted screenshots (/media/...).
    func mediaRequest(_ file: String) -> URLRequest {
        request("media/\(file)")
    }

    // MARK: - plumbing

    func request(_ path: String, method: String = "GET", timeout: TimeInterval = 20) -> URLRequest {
        var req = URLRequest(url: URL(string: path, relativeTo: baseURL.appendingPathComponent(""))!.absoluteURL)
        req.httpMethod = method
        req.timeoutInterval = timeout
        if let token { req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        return req
    }

    private func raw(_ req: URLRequest) async throws -> Data {
        let data: Data, response: URLResponse
        do {
            (data, response) = try await Self.session.data(for: req)
        } catch {
            throw APIError.transport(error)
        }
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(code) else {
            let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"] as? String
            throw APIError.http(code, detail ?? HTTPURLResponse.localizedString(forStatusCode: code))
        }
        return data
    }

    private func send<T: Decodable>(_ req: URLRequest) async throws -> T {
        let data = try await raw(req)
        do {
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            throw APIError.decoding(error)
        }
    }
}

private extension Data {
    mutating func append(_ string: String) { append(Data(string.utf8)) }
}

/// GET /api/usage: measured cost of identifying screenshots.
struct UsageReport: Decodable {
    struct Totals: Decodable {
        let screenshots: Int
        let costUsd: Double
        let webSearches: Int
        enum CodingKeys: String, CodingKey {
            case screenshots
            case costUsd = "cost_usd", webSearches = "web_searches"
        }
    }
    struct Analyzer: Decodable, Identifiable {
        let analyzer: String
        let mode: String?
        let model: String?
        let runs: Int
        let costUsd: Double?
        let avgCostUsd: Double?
        var id: String { "\(analyzer)|\(mode ?? "")|\(model ?? "")" }
        enum CodingKeys: String, CodingKey {
            case analyzer, mode, model, runs
            case costUsd = "cost_usd", avgCostUsd = "avg_cost_usd"
        }
    }
    let periodDays: Int
    let totals: Totals
    let perScreenshotUsd: Double
    let projected30dUsd: Double
    let claudeShare: Double
    let byAnalyzer: [Analyzer]

    enum CodingKeys: String, CodingKey {
        case totals
        case periodDays = "period_days", perScreenshotUsd = "per_screenshot_usd"
        case projected30dUsd = "projected_30d_usd", claudeShare = "claude_share", byAnalyzer = "by_analyzer"
    }
}

func formatUSD(_ value: Double) -> String {
    value == 0 ? "$0" : value < 0.01 ? String(format: "$%.4f", value) : String(format: value < 1 ? "$%.3f" : "$%.2f", value)
}
