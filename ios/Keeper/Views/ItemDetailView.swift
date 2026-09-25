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
    @State private var showCorrection = false

    /// Metadata shown in dedicated sections (or not worth showing) rather than the facts list.
    private static let hiddenKeys: Set<String> = [
        "screenshot_text", "sources", "confidence", "ingredients", "instructions", "imdb_rating", "rotten_tomatoes",
        "metacritic", "tmdb_rating", "stars", "rating", "rating_count", "description", "post_url", "imdb_votes", "tmdb_id",
        "page_description", "page_title", "github_full_name", "year", "ocr_text",
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
                confidenceSection(item)
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

            if let usage = item.usage, usage.runs > 0 {
                Section("Identification") {
                    LabeledContent("Cost", value: formatUSD(usage.costUsd))
                    if usage.webSearches > 0 { LabeledContent("Web searches", value: "\(usage.webSearches)") }
                    if let sources = item.metadata["sources"]?.strings, !sources.isEmpty {
                        LabeledContent("Via", value: sources.joined(separator: " → "))
                    }
                }
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
        .sheet(isPresented: $showCorrection) {
            CorrectionSheet(item: item) { correction in
                store.correct(item.id, correction)
                sync.requestSync()
            }
        }
    }

    @ViewBuilder
    private func statusRow(_ item: Item) -> some View {
        if item.status == "error" {
            Label(item.error ?? "Analysis failed", systemImage: "exclamationmark.triangle").foregroundStyle(.red).font(.footnote)
        } else if item.pendingUpload {
            Label("Saved on this phone. It will be uploaded and identified when your server is reachable.", systemImage: "icloud.and.arrow.up")
                .font(.footnote).foregroundStyle(.secondary)
        } else if item.batchPending {
            Label("Queued for Claude batch processing (half price). Usually done within an hour, at most 24 h.", systemImage: "hourglass")
                .font(.footnote).foregroundStyle(.secondary)
        } else if item.status == "processing" {
            Label("Identifying… this usually takes under a minute.", systemImage: "sparkles").font(.footnote).foregroundStyle(.secondary)
        } else if store.hasPendingOps(for: item.id) {
            Label("Changes waiting to sync", systemImage: "arrow.triangle.2.circlepath").font(.footnote).foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private func confidenceSection(_ item: Item) -> some View {
        if item.status == "ready" && !item.pendingUpload {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    if item.corrected {
                        Label("Corrected by you", systemImage: "checkmark.seal.fill").foregroundStyle(.green)
                    } else if let c = item.confidence {
                        Text("\(c)% sure").bold()
                        ProgressView(value: Double(c), total: 100)
                            .tint(c >= 85 ? .green : c >= 60 ? .yellow : .red)
                            .frame(width: 80)
                    }
                    Spacer()
                    Button(item.needsReview ? "Is this wrong?" : "Wrong? Fix it") { showCorrection = true }
                        .buttonStyle(.bordered).controlSize(.small)
                }
                .font(.subheadline)
                if !item.corrected, let reason = item.confidenceReason {
                    Text(reason).font(.footnote).foregroundStyle(.secondary)
                }
                if !item.corrected && !item.alternatives.isEmpty {
                    Text("Did you mean:").font(.footnote).foregroundStyle(.secondary)
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack {
                            ForEach(item.alternatives, id: \.self) { alt in
                                Chip(label: alt.year.map { "\(alt.title) (\($0))" } ?? alt.title,
                                     systemImage: Category(rawValue: alt.category ?? "")?.symbol) {
                                    store.correct(item.id, Correction(title: alt.title, category: alt.category,
                                                                      year: alt.year, canonicalUrl: alt.canonicalUrl))
                                    sync.requestSync()
                                }
                            }
                        }
                    }
                }
            }
            .padding(10)
            .background(item.needsReview ? Color.orange.opacity(0.12) : Color(.secondarySystemBackground),
                        in: RoundedRectangle(cornerRadius: 10))
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

/// "What is it really?" Fix the facts directly, or describe it and let Claude look again.
private struct CorrectionSheet: View {
    let item: Item
    let onSave: (Correction) -> Void
    @Environment(\.dismiss) private var dismiss

    @State private var title: String
    @State private var category: String
    @State private var year: String
    @State private var link: String
    @State private var hint = ""

    init(item: Item, onSave: @escaping (Correction) -> Void) {
        self.item = item
        self.onSave = onSave
        _title = State(initialValue: item.title ?? "")
        _category = State(initialValue: item.category ?? "other")
        _year = State(initialValue: item.meta("year") ?? "")
        _link = State(initialValue: item.canonicalUrl ?? "")
    }

    private var correction: Correction {
        func changed(_ new: String, _ old: String?) -> String? {
            let v = new.trimmingCharacters(in: .whitespacesAndNewlines)
            return v.isEmpty || v == (old ?? "") ? nil : v
        }
        let yearValue = Int(year.trimmingCharacters(in: .whitespaces))
        return Correction(
            title: changed(title, item.title),
            category: category == item.category ? nil : category,
            year: yearValue.flatMap { String($0) == item.meta("year") ? nil : $0 },
            canonicalUrl: changed(link, item.canonicalUrl),
            hint: changed(hint, nil)
        )
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Title", text: $title)
                    Picker("Type", selection: $category) {
                        ForEach(Category.allCases) { Label($0.label, systemImage: $0.symbol).tag($0.rawValue) }
                    }
                    TextField("Year", text: $year).keyboardType(.numberPad)
                    TextField("Link (IMDb, GitHub, recipe page…)", text: $link)
                        .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                } header: {
                    Text("What is it really?")
                } footer: {
                    Text("Keeper looks up posters, scores and details for what you enter.")
                }
                Section {
                    TextField("e.g. “It's the 2019 remake, not the original”", text: $hint, axis: .vertical)
                } header: {
                    Text("Or describe it")
                } footer: {
                    Text("Claude looks at the screenshot again with your description.")
                }
            }
            .navigationTitle("Fix identification")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        onSave(correction)
                        dismiss()
                    }
                    .disabled(correction.isEmpty)
                }
            }
        }
    }
}
