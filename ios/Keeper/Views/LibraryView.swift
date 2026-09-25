import PhotosUI
import SwiftUI

struct LibraryView: View {
    @EnvironmentObject private var store: LibraryStore
    @EnvironmentObject private var sync: SyncEngine

    @State private var query = ""
    @State private var category: Category?
    @State private var tag: String?
    @State private var needsReview = false
    @State private var picked: [PhotosPickerItem] = []
    @State private var showSettings = false
    @State private var message: String?

    private var results: [Item] { store.filtered(query: query, category: category, tag: tag, needsReview: needsReview) }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    SyncBanner(onOpenSettings: { showSettings = true })
                    filters
                    if results.isEmpty {
                        emptyState
                    } else {
                        LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 12)], spacing: 12) {
                            ForEach(results) { item in
                                NavigationLink(value: item.id) { ItemCard(item: item) }
                                    .buttonStyle(.plain)
                            }
                        }
                    }
                }
                .padding(.horizontal)
            }
            .navigationTitle("Keeper")
            .navigationDestination(for: String.self) { ItemDetailView(itemID: $0) }
            .searchable(text: $query, prompt: "Titles, tags, text in screenshots…")
            .refreshable { await sync.sync() }
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button { showSettings = true } label: { SyncStatusIcon() }
                }
                ToolbarItemGroup(placement: .topBarTrailing) {
                    Button { pasteFromClipboard() } label: { Image(systemName: "doc.on.clipboard") }
                        .accessibilityLabel("Paste a screenshot or link")
                    PhotosPicker(selection: $picked, maxSelectionCount: 20, matching: .images) {
                        Image(systemName: "plus")
                    }
                    .accessibilityLabel("Add screenshots")
                }
            }
            .sheet(isPresented: $showSettings) { SettingsView() }
            .onChange(of: picked) { _, newValue in addPicked(newValue) }
            .overlay(alignment: .bottom) {
                if let message {
                    Text(message)
                        .padding(.horizontal, 16).padding(.vertical, 10)
                        .background(.thinMaterial, in: Capsule())
                        .padding(.bottom, 24)
                        .transition(.move(edge: .bottom).combined(with: .opacity))
                }
            }
        }
    }

    private var filters: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                if store.needsReviewCount > 0 {
                    Chip(label: "Needs review \(store.needsReviewCount)", systemImage: "exclamationmark.triangle", active: needsReview) {
                        needsReview.toggle()
                    }
                }
                ForEach(store.categoryCounts, id: \.0) { c, count in
                    Chip(label: "\(c.label) \(count)", systemImage: c.symbol, active: category == c) {
                        category = category == c ? nil : c
                    }
                }
                if !store.tagCounts.isEmpty { Divider().frame(height: 20) }
                ForEach(store.tagCounts.prefix(30), id: \.0) { t, _ in
                    Chip(label: "#\(t)", active: tag == t) { tag = tag == t ? nil : t }
                }
            }
        }
    }

    private var emptyState: some View {
        ContentUnavailableView {
            Label(store.items.isEmpty ? "Nothing saved yet" : "No matches", systemImage: "pin")
        } description: {
            Text(store.items.isEmpty
                 ? "Share a screenshot to Keeper from Photos or right after taking it, tap + to pick one, or copy a link and tap the clipboard."
                 : "Try a different search or filter.")
        }
        .padding(.top, 60)
    }

    private func addPicked(_ selection: [PhotosPickerItem]) {
        guard !selection.isEmpty else { return }
        Task {
            var added = 0
            for pick in selection {
                if let data = try? await pick.loadTransferable(type: Data.self), store.addScreenshot(data) != nil {
                    added += 1
                }
            }
            picked = []
            flash(added == 1 ? "Screenshot saved" : "\(added) screenshots saved")
            sync.requestSync()
        }
    }

    /// Paste a screenshot, or a link copied from Safari, X, Instagram ("Copy link"), etc.
    private func pasteFromClipboard() {
        let board = UIPasteboard.general
        if board.hasImages, let data = board.image?.pngData() {
            store.addScreenshot(data)
            flash("Screenshot saved")
        } else if let url = board.url ?? board.string.flatMap(Self.link(from:)) {
            store.addLink(url)
            flash("Link saved")
        } else {
            flash("Copy a screenshot or a link first")
            return
        }
        sync.requestSync()
    }

    private static func link(from text: String) -> URL? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.contains(" "), trimmed.contains(".") else { return nil }
        let url = URL(string: trimmed.contains("://") ? trimmed : "https://" + trimmed)
        return url?.scheme?.hasPrefix("http") == true && url?.host != nil ? url : nil
    }

    private func flash(_ text: String) {
        withAnimation { message = text }
        Task {
            try? await Task.sleep(for: .seconds(2.5))
            withAnimation { if message == text { message = nil } }
        }
    }
}

struct ItemCard: View {
    let item: Item

    private var category: Category? { Category(rawValue: item.category ?? "") }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ItemImage(item: item)
                .aspectRatio(category?.isPortrait == true ? 2 / 3 : 16 / 10, contentMode: .fit)
                .overlay(alignment: .topLeading) { badge.padding(6) }
                .overlay(alignment: .topTrailing) {
                    if item.needsReview, let c = item.confidence {
                        Label("\(c)%", systemImage: "questionmark.circle.fill").badgeStyle(.orange).padding(6)
                            .accessibilityLabel("Not sure: \(c) percent confident")
                    }
                }
                .overlay {
                    if item.status == "processing" || item.status == "queued" {
                        Color.black.opacity(0.35)
                        VStack(spacing: 6) {
                            if item.status == "processing" { ProgressView().tint(.white) } else { Image(systemName: "icloud.and.arrow.up") }
                            Text(item.status == "processing" ? "Analyzing…" : "Waiting to upload").font(.caption.bold())
                        }
                        .foregroundStyle(.white)
                    }
                }
            VStack(alignment: .leading, spacing: 4) {
                Text(item.displayTitle).font(.subheadline.weight(.semibold)).lineLimit(2)
                if let subtitle = item.subtitle {
                    Text(subtitle).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                }
                let facts = cardFacts
                if !facts.isEmpty {
                    Text(facts.joined(separator: " · ")).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            .padding(10)
        }
        .background(Color(.systemBackground))
        .clipShape(RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color(.separator), lineWidth: 0.5))
    }

    @ViewBuilder private var badge: some View {
        if item.status == "error" {
            Label("Failed", systemImage: "exclamationmark.triangle.fill").badgeStyle(.red)
        } else if let category {
            Label(category.label, systemImage: category.symbol).badgeStyle(.black.opacity(0.6))
        }
    }

    private var cardFacts: [String] {
        var out: [String] = []
        if let r = item.meta("imdb_rating") ?? item.meta("tmdb_rating") { out.append("★ " + r.replacingOccurrences(of: "/10", with: "")) }
        if let rt = item.meta("rotten_tomatoes") { out.append("🍅 " + rt) }
        if let stars = item.meta("stars") { out.append("★ " + stars) }
        if let lang = item.meta("programming_language") { out.append(lang) }
        if let time = item.meta("total_time") { out.append(time) }
        if let year = item.meta("year"), category?.isPortrait == true { out.append(year) }
        return Array(out.prefix(3))
    }
}

struct Chip: View {
    let label: String
    var systemImage: String?
    var active = false
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 4) {
                if let systemImage { Image(systemName: systemImage) }
                Text(label)
            }
            .font(.footnote)
            .padding(.horizontal, 10).padding(.vertical, 5)
            .background(active ? Color.accentColor.opacity(0.18) : Color(.secondarySystemBackground), in: Capsule())
            .overlay(Capsule().stroke(active ? Color.accentColor : .clear))
        }
        .buttonStyle(.plain)
    }
}

private extension View {
    func badgeStyle(_ color: Color) -> some View {
        font(.caption2.bold())
            .labelStyle(.titleAndIcon)
            .padding(.horizontal, 7).padding(.vertical, 3)
            .background(color, in: Capsule())
            .foregroundStyle(.white)
    }
}

struct SyncStatusIcon: View {
    @EnvironmentObject private var store: LibraryStore
    @EnvironmentObject private var sync: SyncEngine

    var body: some View {
        switch sync.status {
        case .syncing: ProgressView()
        case .offline: Image(systemName: "icloud.slash")
        case .failed: Image(systemName: "exclamationmark.icloud")
        case .notConfigured: Image(systemName: "gearshape")
        case .idle:
            Image(systemName: store.pending.isEmpty ? "checkmark.icloud" : "arrow.triangle.2.circlepath.icloud")
        }
    }
}

/// Explains why things aren't syncing, only when it matters.
struct SyncBanner: View {
    @EnvironmentObject private var store: LibraryStore
    @EnvironmentObject private var sync: SyncEngine
    let onOpenSettings: () -> Void

    var body: some View {
        switch sync.status {
        case .notConfigured:
            banner("Connect your Keeper server to identify screenshots. Everything you add is kept on this phone until then.",
                   icon: "server.rack", action: "Set up")
        case .offline where !store.pending.isEmpty:
            banner("Offline — \(store.pending.count) change\(store.pending.count == 1 ? "" : "s") will sync when your server is reachable.",
                   icon: "icloud.slash", action: nil)
        case .failed(let message):
            banner("Sync failed: \(message)", icon: "exclamationmark.icloud", action: "Settings")
        default:
            EmptyView()
        }
    }

    private func banner(_ text: String, icon: String, action: String?) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: icon)
            Text(text).font(.footnote).frame(maxWidth: .infinity, alignment: .leading)
            if let action { Button(action, action: onOpenSettings).font(.footnote.bold()) }
        }
        .padding(12)
        .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 12))
    }
}
