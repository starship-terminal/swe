import AppKit
import SWECore
import SwiftUI
import Testing
@testable import SWEApp

/// What the offscreen layout check reads off a detail page's probe: the
/// loads that have landed and where the rail and the editor pane sit.
@MainActor
protocol DetailLayoutProbe: AnyObject, Observable, Sendable {
    init()
    var loaded: Set<String> { get }
    var rail: CGRect { get }
    var pane: CGRect { get }
}

extension SkillDetailProbe: DetailLayoutProbe {}
extension PluginDetailProbe: DetailLayoutProbe {}

/// The layout check the skill and plugin details share. Each renders its
/// page offscreen inside the real `Page` at a roomy window, at the 900pt
/// minimum, and at the minimum with the sidebar at its widest (the
/// narrowest page the app makes, where the panes stack), and checks that
/// the rail keeps its width and the editor fills the height, or, stacked,
/// that both take the page's width and the editor keeps its least height.
@MainActor
enum DetailLayoutCheck {
    struct Window {
        var name: String
        var size: CGSize
        /// The app's sidebar: the page gets the rest of the window.
        var sidebar: CGFloat
    }

    static let windows = [
        Window(name: "1328", size: CGSize(width: 1328, height: 860), sidebar: SidebarWidth.standard),
        Window(name: "900", size: CGSize(width: 900, height: 860), sidebar: SidebarWidth.standard),
        Window(name: "narrowest", size: CGSize(width: 900, height: 600), sidebar: SidebarWidth.range.upperBound),
    ]

    /// The page the detail sits in: its title, subtitle and back chevron.
    struct PageChrome {
        var title: String
        var subtitle: String
        var back: String
    }

    /// Every scroll view under `view`.
    private static func scrollViews(in view: NSView) -> [NSScrollView] {
        view.subviews.flatMap { sub in ((sub as? NSScrollView).map { [$0] } ?? []) + scrollViews(in: sub) }
    }

    /// Renders what `make` builds in `page` at each window size, waits for
    /// its loads, runs `prepare`, then checks the panes and, with a
    /// `snapshots` folder, writes `<state>-<window>.png` there. With
    /// `legacyScrollers`, each window is checked a second time with every
    /// scroll view switched to always-visible scrollers, as CI's runners
    /// draw them: they take 17pt from a scrolling pane.
    static func run<Probe: DetailLayoutProbe, Content: View>(
        _ state: String, page chrome: PageChrome, snapshots: String?, wait: Set<String>,
        legacyScrollers: Bool = false, prepare: (PrimitiveDrafts) -> Void = { _ in },
        _ make: (Probe, PrimitiveDrafts) -> Content
    ) async throws {
        _ = NSApplication.shared
        for window in windows {
            let probe = Probe()
            let drafts = PrimitiveDrafts()
            let panel = CGSize(width: window.size.width - window.sidebar, height: window.size.height)
            let page = Page(title: chrome.title, subtitle: chrome.subtitle,
                            back: (chrome.back, {}), scrolls: false) { make(probe, drafts) }
                .frame(width: panel.width, height: panel.height)
                .background(Theme.panel)
            let host = NSHostingView(rootView: page)
            // Titled and ordered in, so SwiftUI runs the view's tasks; the
            // window still never reaches a screen.
            let shown = OffscreenWindow(size: panel, styleMask: [.titled])
            shown.appearance = NSAppearance(named: .aqua)
            shown.contentView = host
            shown.orderFront(nil)
            defer { shown.orderOut(nil) }
            // Parks on a gate the probe's changes open; SwiftUI keeps
            // laying out and running the view's tasks meanwhile.
            #expect(await observed {
                wait.isSubset(of: probe.loaded)
                    && drafts.documents.values.allSatisfy { $0.state != .loading }
                    && probe.pane.height > 0
            }, "\(state) at \(window.name): \(probe.loaded)")
            prepare(drafts)
            // A layout pass for what `prepare` changed.
            host.layoutSubtreeIfNeeded()

            let content = panel.width - 80
            let stacked = SkillDetailLayout.stacks(available: Double(content))
            let here = "\(state) at \(window.name)"
            assertLayout(probe, panel: panel, shown: shown, stacked: stacked, here: here)
            try snapshot(host, "\(state)-\(window.name)", in: snapshots)

            if legacyScrollers {
                // Every scroll view to legacy, then the notice AppKit posts
                // when the system setting changes: SwiftUI's scroll views
                // lay their content out again for it.
                for scrollView in scrollViews(in: host) { scrollView.scrollerStyle = .legacy }
                NotificationCenter.default.post(name: NSScroller.preferredScrollerStyleDidChangeNotification, object: nil)
                host.layoutSubtreeIfNeeded()
                if stacked {
                    // The page scrolls, so its scroller takes its width.
                    #expect(await observed { probe.rail.width < content - 10 }, "\(here), legacy: \(probe.rail)")
                }
                assertLayout(probe, panel: panel, shown: shown, stacked: stacked, here: here + ", legacy")
                try snapshot(host, "\(state)-\(window.name)-legacy", in: snapshots)
            }
        }
    }

    /// `host` as `<name>.png` in the snapshot folder, when one is set.
    private static func snapshot(_ host: NSView, _ name: String, in dir: String?) throws {
        guard let dir, let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { return }
        host.cacheDisplay(in: host.bounds, to: rep)
        let png = try #require(rep.representation(using: .png, properties: [:]))
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        try png.write(to: URL(fileURLWithPath: dir).appending(path: name + ".png"))
    }

    /// Nothing is wider than the page, and the editor never drops under
    /// 380pt beside the rail. Stacked, rail and editor share the width the
    /// page's scroll view gives them, all of it with overlay scrollers and
    /// all but a legacy scroller's width with those.
    private static func assertLayout(_ probe: some DetailLayoutProbe, panel: CGSize, shown: NSWindow,
                                     stacked: Bool, here: String) {
        let content = panel.width - 80
        let scroller = NSScroller.scrollerWidth(for: .regular, scrollerStyle: .legacy)
        #expect(probe.rail.minX >= 40 - 1 && probe.rail.maxX <= panel.width - 40 + 1, "\(here): rail \(probe.rail)")
        #expect(probe.pane.maxX <= panel.width - 40 + 1, "\(here): pane \(probe.pane)")
        if stacked {
            #expect(abs(probe.rail.width - probe.pane.width) < 1, "\(here): \(probe.rail) \(probe.pane)")
            #expect(probe.pane.width <= content + 1 && probe.pane.width >= content - scroller - 1,
                    "\(here): pane \(probe.pane)")
            #expect(probe.pane.height >= CGFloat(SkillDetailLayout.stackedEditorHeight) - 1, "\(here): pane \(probe.pane)")
            #expect(probe.pane.minY > probe.rail.maxY, "\(here): \(probe.rail) \(probe.pane)")
        } else {
            // The rail keeps its width and the editor fills the page's
            // height beside it, out to the page's edge.
            let rail = SkillDetailLayout.railWidth(available: Double(content))
            #expect(abs(probe.rail.width - CGFloat(rail)) < 1, "\(here): rail \(probe.rail)")
            #expect(abs(probe.pane.maxX - (panel.width - 40)) < 1, "\(here): pane \(probe.pane)")
            #expect(probe.pane.width >= CGFloat(SkillDetailLayout.stackedBelow) - 1, "\(here): pane \(probe.pane)")
            #expect(abs(probe.rail.height - probe.pane.height) < 1, "\(here): \(probe.rail) \(probe.pane)")
            // Under the page's header, down to its bottom padding. Global
            // frames count from the window's top, title bar included.
            let titleBar = shown.frame.height - panel.height
            #expect(abs(probe.pane.maxY - titleBar - (panel.height - 36)) < 1, "\(here): pane \(probe.pane)")
            #expect(probe.pane.height > panel.height * 0.7, "\(here): pane \(probe.pane)")
        }
    }
}
