import Foundation

/// How tall a picker's popover list grows before it scrolls
/// (`MultiSelectMenu`). A list shorter than the cap keeps its own height,
/// so short menus (Pricing, Models) look as they always did; only a list
/// taller than the cap scrolls.
public enum PopoverList {
    /// Room for the AGENTS.md Compile tab's harness picker (an "All" row
    /// and thirteen harnesses, about 413pt at the standard text size) with
    /// margin, and still short of a 13-inch screen. The old 360pt cap cut
    /// the thirteenth harness off.
    public static let maxHeight: CGFloat = 520

    /// The list's height: `rows` rows of `rowHeight`, plus `chrome` (its
    /// padding and the divider under the "All" row), up to `maxHeight`.
    public static func height(rows: Int, rowHeight: CGFloat, chrome: CGFloat = 0) -> CGFloat {
        min(maxHeight, contentHeight(rows: rows, rowHeight: rowHeight, chrome: chrome))
    }

    /// Whether such a list scrolls.
    public static func scrolls(rows: Int, rowHeight: CGFloat, chrome: CGFloat = 0) -> Bool {
        contentHeight(rows: rows, rowHeight: rowHeight, chrome: chrome) > maxHeight
    }

    private static func contentHeight(rows: Int, rowHeight: CGFloat, chrome: CGFloat) -> CGFloat {
        CGFloat(max(rows, 0)) * rowHeight + chrome
    }
}
