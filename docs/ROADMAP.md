# Roadmap

Magpie captures **screenshots** and **links** today. The two items below complete the goal of *share anything — a URL, a screenshot, a selection, a note — and get back a typed, enriched, auto-filed item*. Each is written as a spec ready to be picked up.

## 1. Text selections and notes as items

**Today:** text can only be attached to a screenshot or a link as its *note*.

**Goal:** a shared text selection ("You have to watch *Past Lives*, and read Celine Song's interview"), or a note typed into Magpie, becomes an item of its own that is identified, enriched and filed like the others.

### Design

- **Capture.** `POST /api/items` accepts a third input: `text` (form field, up to ~20k characters), alongside `file` and `url`. Store it as `kind = "text"`, keep the text in a new `text` column, and use `image_file = ""`, as link items do.
- **Links inside the text.** If the text is little more than a URL, treat it as a link capture. If it contains URLs, keep them as candidates: the first recognized one (GitHub, IMDb, TMDB, a recipe page…) can identify the item without a model, like link capture does today.
- **Identification.** Reuse the link path (`Pipeline.process_url`, `AnalyzerRouter.analyze(image=None, page_hints=…)`):
  - the text goes to the model as `<shared_text>…</shared_text>`, with web search enabled, since a selection is usually a *mention* of something rather than the thing itself;
  - in hybrid mode the local model tries first;
  - in `ocr` mode the rules in `ocr.extract_signals` run on the text (they take any text) and `links.generic` becomes a "note" card.
- **Notes that aren't about anything.** When the model finds no specific subject ("buy milk"), keep it as category `note` (a new category) with the text as its summary. Confidence isn't meaningful here, so don't flag these for review.
- **Search.** The text goes into the full-text index (store it like `ocr_text`).
- **UI.**
  - Web/PWA: a "Write a note" option next to "Save link"; pasting non-URL text outside an input creates a text item.
  - iOS: a "New note" sheet, and pasting text from the clipboard.
  - Detail view: shows the text in full; the card shows its first line.
- **Tests:** a text mentioning a film becomes a movie card with a poster; a text with a GitHub URL becomes a repo card without a model call; plain notes are stored as `note`; offline notes sync.

**Effort:** small to medium. The pipeline, analyzer and offline sync already handle model calls without an image.

## 2. Share paths for links and text

**Today:**
- the iOS Share Extension accepts **images only**;
- the PWA's Web Share Target accepts image files, and maps shared text to the *note* of an image;
- on iPhone, web apps can't receive shares at all (Apple doesn't support Web Share Target).

**Goal:** "Share → Magpie" works for whatever you're looking at: a page in Safari, a post in X or Instagram ("Share → Magpie" or "Copy link"), a text selection.

### iOS Share Extension (native app)

- **Activation rule.** Extend `NSExtensionActivationRule` in `ios/project.yml`: add `NSExtensionActivationSupportsWebURLWithMaxCount: 1` and `NSExtensionActivationSupportsText: true`, keeping the image rule.
- **Loading.** In `ShareViewController`, load `UTType.url` and `UTType.plainText` items as well as `UTType.image`.
  - Safari offers the page URL (and a title); X and Instagram offer the post URL. Some apps offer the URL as plain text, so detect a lone URL in the text.
  - An item can carry both a screenshot and a URL (e.g. Markup "Share" of a web page). Prefer the URL, since it's cheaper and more precise, and attach the image only when there's no URL.
- **Inbox format.** Write `<uuid>.json` sidecars with `{ "url": …, "text": …, "note": …, "createdAt": … }` and no image file. `LibraryStore.importInbox()` reads a sidecar without an image as a link (`addLink`) or a text item (from the roadmap item above).
- **Share sheet UI.** Show a preview (the page title for links, the first lines for text) plus the note field.
- **Safari "Copy link" fallback.** Already works today through the clipboard button in the app.

### PWA (Android and desktop Chrome/Edge)

- **Manifest.** In `share_target`, add `"url": "url"` and `"title": "title"` to `params`, and keep `"text": "note"` for images. `share_target.params.text` must also create text items when there are no files.
- **Service worker.** `receiveShare` in `sw.js` stashes `{url, title, text}` in the `magpie-share-inbox` cache, as it does for files. Many Android apps put the URL in `text`, so detect a lone URL there. `importShared()` in `app.js` calls `addLink` / `addText` accordingly.
- **iOS PWA.** Not possible (no Web Share Target in Safari). Document the options instead: the native app's Share Extension, or copying the link and pasting it into the PWA's "Save link" field.

### Also worth considering

- **A bookmarklet / browser extension** for desktop: `javascript:fetch('<server>/api/items', {method:'POST', body: new URLSearchParams({url: location.href})})`. It needs the API token, so a small extension with a settings page is cleaner.
- **An iOS Shortcut** ("Save to Magpie") that posts the shared URL or text to the API. No code in the app; it just needs documentation.

**Effort:**
- iOS Share Extension: small (~150 lines of Swift plus plist keys).
- PWA share target: small.
- Both depend on the text-item design above for selections.
