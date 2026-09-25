import SwiftUI

struct ItemDetailView: View {
    let itemID: String
    @EnvironmentObject private var store: LibraryStore
    @EnvironmentObject private var sync: SyncEngine
    @Environment(\.dismiss) private var dismiss

    @State private var newTag = ""
    @State private var note = ""
    @State private var confirmDelete = false
    @State private var showScreenshot = false

    /// Metadata shown in dedicated sections (or not worth showing) rather than the facts list.
    private static let hiddenKeys: Set<String> = [
        "screenshot_text", "sources", "confidence", "ingredients", "instructions", "imdb_rating", "rotten_tomatoes",
        "metacritic", "tmdb_rating", "stars", "rating", "rating_count", "description", "post_url", "imdb_votes", "tmdb_id",
        "page_description", "page_title", "github_full_name", "year",
    ]

    var body: some View {
        if let item = store.item(itemID) {
            content(item)
        } else {
            ContentUnavailableView("Item deleted", systemImage: "trash")
        }
    }

    private func content(_ item: Item) -> some View {
        let category = Category(rawValue: item.category ?? "")
        return List {
            Section {
                HStack(alignment: .top, spacing: 14) {
                    ItemImage(item: item)
                        .frame(width: 110, height: category?.isPortrait == true ? 165 : 80)
                        .clipShape(RoundedRectangle(cornerRadius: 10))
                    VStack(alignment: .leading, spacing: 6) {
                        Text(item.displayTitle).font(.title3.bold())
                        if let subtitle = item.subtitle { Text(subtitle).font(.subheadline).foregroundStyle(.secondary) }
                        Text(metaLine(item, category)).font(.caption).foregroundStyle(.secondary)
                    }
                }
                statusRow(item)
                scores(item)
                if let summary = item.summary { Text(summary) }
                if let url = item.canonicalUrl.flatMap(URL.init(string:)) {
                    Link(destination: url) { Label("Open source", systemImage: "arrow.up.right.square") }
                }
                ForEach(item.links, id: \.url) { link in
                    if let url = URL(string: link.url) { Link(link.label, destination: url) }
                }
            }

            let facts = item.metadata
                .filter { !Self.hiddenKeys.contains($0.key) && $0.value.string != nil }
                .sorted { $0.key < $1.key }
            if !facts.isEmpty {
                Section("Details") {
                    ForEach(facts, id: \.key) { key, value in
                        LabeledContent(key.replacingOccurrences(of: "_", with: " ").capitalized) {
                            Text(value.string ?? "").multilineTextAlignment(.trailing)
                        }
                    }
                }
            }

            if let ingredients = item.metadata["ingredients"]?.strings, !ingredients.isEmpty {
                Section("Ingredients") { ForEach(ingredients, id: \.self) { Text($0) } }
            }
            if let steps = item.metadata["instructions"]?.strings, !steps.isEmpty {
                Section("Instructions") {
                    ForEach(Array(steps.enumerated()), id: \.offset) { i, step in
                        Text("\(i + 1). \(step)")
                    }
                }
            }

            Section("Tags") {
                if !item.tags.isEmpty {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack {
                            ForEach(item.tags, id: \.self) { tag in
                                Chip(label: "#\(tag)  ✕") { edit(item) { $0.tags = item.tags.filter { $0 != tag } } }
                            }
                        }
                    }
                }
                TextField("Add tag", text: $newTag)
                    .textInputAutocapitalization(.never)
                    .onSubmit {
                        let added = newTag.split(separator: ",").map(String.init)
                        newTag = ""
                        if !added.isEmpty { edit(item) { $0.tags = item.tags + added } }
                    }
            }

            Section("Note") {
                TextField("Why did you save this?", text: $note, axis: .vertical)
                    .onSubmit { saveNote(item) }
                    .onAppear { note = item.note ?? "" }
                    .onDisappear { saveNote(item) }
            }

            Section {
                Picker("Category", selection: Binding(
                    get: { item.category ?? "other" },
                    set: { value in edit(item) { $0.category = value } }
                )) {
                    ForEach(Category.allCases) { Label($0.label, systemImage: $0.symbol).tag($0.rawValue) }
                }
                Button { showScreenshot = true } label: { Label("View screenshot", systemImage: "photo") }
                if !item.pendingUpload {
                    Button { store.reanalyze(item.id); sync.requestSync() } label: {
                        Label("Re-analyze", systemImage: "arrow.clockwise")
                    }
                }
                Button(role: .destructive) { confirmDelete = true } label: { Label("Delete", systemImage: "trash") }
            }

            if let text = item.meta("screenshot_text") {
                Section("Text in screenshot") { Text(text).font(.footnote).foregroundStyle(.secondary) }
            }
        }
        .navigationBarTitleDisplayMode(.inline)
        .confirmationDialog("Delete this item?", isPresented: $confirmDelete, titleVisibility: .visible) {
            Button("Delete", role: .destructive) {
                store.delete(item.id)
                sync.requestSync()
                dismiss()
            }
        }
        .sheet(isPresented: $showScreenshot) { ScreenshotSheet(item: item) }
    }

    @ViewBuilder
    private func statusRow(_ item: Item) -> some View {
        if item.status == "error" {
            Label(item.error ?? "Analysis failed", systemImage: "exclamationmark.triangle").foregroundStyle(.red).font(.footnote)
        } else if item.pendingUpload {
            Label("Saved on this phone. It will be uploaded and identified when your server is reachable.", systemImage: "icloud.and.arrow.up")
                .font(.footnote).foregroundStyle(.secondary)
        } else if item.status == "processing" {
            Label("Identifying… this usually takes under a minute.", systemImage: "sparkles").font(.footnote).foregroundStyle(.secondary)
        } else if store.hasPendingOps(for: item.id) {
            Label("Changes waiting to sync", systemImage: "arrow.triangle.2.circlepath").font(.footnote).foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private func scores(_ item: Item) -> some View {
        let scores: [(String, String)] = [
            ("IMDb", item.meta("imdb_rating")), ("Rotten Tomatoes", item.meta("rotten_tomatoes")),
            ("Metacritic", item.meta("metacritic")), ("TMDB", item.meta("tmdb_rating")),
            ("Stars", item.meta("stars")), ("Rating", item.meta("rating")),
        ].compactMap { label, value in value.map { (label, $0) } }
        if !scores.isEmpty {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack {
                    ForEach(scores, id: \.0) { label, value in
                        VStack(spacing: 2) {
                            Text(value).font(.headline)
                            Text(label).font(.caption2).foregroundStyle(.secondary)
                        }
                        .padding(.horizontal, 12).padding(.vertical, 6)
                        .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 10))
                    }
                }
            }
        }
    }

    private func metaLine(_ item: Item, _ category: Category?) -> String {
        [
            category?.label,
            item.meta("year"),
            item.sourcePlatform.map { "from \($0)" },
            item.createdDate?.formatted(date: .abbreviated, time: .omitted),
        ].compactMap { $0 }.joined(separator: " · ")
    }

    private func saveNote(_ item: Item) {
        let trimmed = note.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed != (item.note ?? ""), store.item(item.id) != nil else { return }
        edit(item) { $0.note = trimmed }
    }

    private func edit(_ item: Item, _ change: (inout ItemPatch) -> Void) {
        var patch = ItemPatch()
        change(&patch)
        store.update(item.id, patch)
        sync.requestSync()
    }
}

private struct ScreenshotSheet: View {
    let item: Item
    @State private var image: UIImage?
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                if let image {
                    Image(uiImage: image).resizable().scaledToFit()
                } else {
                    ProgressView().padding(.top, 80)
                }
            }
            .toolbar { Button("Done") { dismiss() } }
            .task { image = await ItemImage.screenshot(for: item) }
        }
    }
}
