import AppKit
import SWECore
import SwiftUI

/// The model card on the Models page, laid out like Devin's model picker
/// card: the foundation model's name with the effort the user ran it at
/// in grey, who made it and which harnesses ran it, the headline
/// benchmarks and the user's tokens, then the cost scale and the price
/// tiles. Only the chart shows it, beside a hovered bar: the table already
/// has every figure in its columns, and Overview and Pricing are too
/// crowded for a popover.
struct ModelCard: View {
    var row: ModelsModel.Row
    /// The period "Your use" reads from (the Models page's own).
    var period: OverviewPeriod

    static let width: CGFloat = 300

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 8) {
                    BrandLogo(vendor: row.vendor, size: 18)
                    ModelName(name: title, qualifier: row.qualifier,
                              rawID: row.alias == nil ? nil : row.logged, font: Theme.font(size: 15))
                }
                Text(subtitle)
                    .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                    .lineLimit(2)
            }
            if let aa = row.aa {
                VStack(spacing: 8) {
                    line("Intelligence", aa.intelligence.map { "\(Int($0.rounded()))" }
                         ?? aa.pastIntelligence.map { "\(Int($0.value.rounded())) · \($0.tag)" })
                    // The card takes no hover itself, so an older score
                    // says what it is in place.
                    if aa.intelligence == nil, let past = aa.pastIntelligence {
                        Text(past.note)
                            .font(Theme.caption).foregroundStyle(Theme.textMuted)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    // The card takes no hover itself, so a missing score
                    // says why in place rather than in a tooltip.
                    line("Terminal-Bench 4.0", aa.coding.map { "\(Int($0.rounded()))%" } ?? "Not run by AA")
                    line("Speed", aa.speed.flatMap { $0 > 0 ? "\(Int($0.rounded())) t/s" : nil })
                    line("Your use", row.tokens > 0
                         ? "\(UsageFormat.tokens(row.tokens)) tokens \(periodLabel)" : nil)
                }
                Divider().overlay(Theme.border)
                cost(aa)
            } else {
                Text("Not on Artificial Analysis")
                    .font(Theme.bodySmall).foregroundStyle(Theme.textSecondary)
                if row.tokens > 0 {
                    line("Your use", "\(UsageFormat.tokens(row.tokens)) tokens \(periodLabel)")
                }
                // A LiteLLM-only search hit still has a price to show.
                if row.listPrice != nil {
                    Divider().overlay(Theme.border)
                    VStack(alignment: .leading, spacing: 12) {
                        Text("Cost").font(Theme.bodySmall).foregroundStyle(Theme.textPrimary)
                        priceTiles()
                    }
                }
            }
        }
        .padding(16)
        .frame(width: Self.width)
        .fixedSize(horizontal: false, vertical: true)
    }

    /// The foundation model's name, without AA's "(Max, Default
    /// Fallback)" tail: the effort shown is the one the user ran. An
    /// aliased id shows its real name, the id muted after it.
    private var title: String { row.displayName }

    /// "Anthropic · Claude Code, Devin": the vendor, then the harnesses
    /// that ran the model in the period.
    private var subtitle: String {
        ([row.vendor.name] + row.harnesses.map { UsageFormat.harness($0) })
            .joined(separator: " · ")
    }

    private var periodLabel: String {
        period.title.prefix(1).lowercased() + period.title.dropFirst()
    }

    /// A label on the left, its value in grey on the right ("—" when
    /// there is none), like the picker card's Effort row.
    private func line(_ label: String, _ value: String?) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label).font(Theme.bodySmall).foregroundStyle(Theme.textPrimary)
            Spacer(minLength: 12)
            Text(value ?? "—")
                .font(Theme.bodySmall).monospacedDigit()
                .foregroundStyle(value == nil ? Theme.textMuted : Theme.textSecondary)
                .lineLimit(1)
        }
    }

    /// "Cost": the list price the ledger charges (LiteLLM, or the user's
    /// own) as input, output and cache-read tiles, then Artificial
    /// Analysis's blended price with the green→orange→purple scale and
    /// the model's name over its dot, for comparison.
    @ViewBuilder private func cost(_ model: AALanguageModel) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Cost").font(Theme.bodySmall).foregroundStyle(Theme.textPrimary)
            priceTiles()
            line("AA blended", model.priceBlended.map { Self.price($0) + " / 1M" })
            if let rank = row.costPercentile {
                CostDotScale(name: title, rank: rank)
            }
        }
    }

    /// The list price as input, output and cache-read tiles. The card
    /// takes no hover, so the LiteLLM key they came from is left to the
    /// table's Cost column tip.
    private func priceTiles() -> some View {
        HStack(spacing: 6) {
            priceTile("Input", row.listPrice?.input, highlighted: true)
            priceTile("Output", row.listPrice?.output)
            priceTile("Cache read", row.listPrice?.cacheRead)
        }
    }

    private func priceTile(_ label: String, _ price: Double?,
                           highlighted: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label).font(Theme.font(size: 12, weight: .medium)).foregroundStyle(Theme.textSecondary)
            HStack(alignment: .firstTextBaseline, spacing: 3) {
                Text(price.map(Self.price) ?? "—")
                    .font(Theme.font(size: 13, weight: .medium)).monospacedDigit()
                    .foregroundStyle(price == nil ? Theme.textMuted : Theme.textPrimary)
                if price != nil {
                    Text("/ 1M").font(Theme.font(size: 11)).foregroundStyle(Theme.textMuted)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 8).padding(.vertical, 7)
        .background(highlighted ? Theme.subtle : .clear, in: .rect(cornerRadius: 8))
    }

    /// "$4", "$0.75", "$1.25": whole dollars lose their cents, and a
    /// paid price under a cent reads "<$0.01", never "$0.00".
    static func price(_ value: Double) -> String {
        if value > 0, value < 0.01 { return "<$0.01" }
        return value == value.rounded() ? "$\(Int(value))" : String(format: "$%.2f", value)
    }
}

/// The cost scale: a gradient track with a white dot centred on it at
/// the model's rank, and the model's name over the dot, nudged in so it
/// never runs past either end.
struct CostDotScale: View {
    var name: String
    var rank: Double

    private static let track: CGFloat = 8
    private static let dot: CGFloat = 10
    private static var labelFont: NSFont { Theme.nsFont(size: 11, weight: .semibold) }

    var body: some View {
        VStack(spacing: 5) {
            GeometryReader { geo in
                let width = min((name as NSString).size(withAttributes: [.font: Self.labelFont])
                    .width.rounded(.up), geo.size.width)
                Text(name)
                    .font(Theme.font(size: 11, weight: .semibold))
                    .foregroundStyle(Theme.textPrimary)
                    .lineLimit(1).truncationMode(.middle)
                    .frame(width: width)
                    .position(x: min(max(rank * geo.size.width, width / 2),
                                     geo.size.width - width / 2),
                              y: geo.size.height / 2)
            }
            .frame(height: 14)
            GeometryReader { geo in
                Capsule()
                    .fill(LinearGradient(colors: [Color(hex: "#2fa35a"), Color(hex: "#e8913a"),
                                                  Color(hex: "#a46ae0")],
                                         startPoint: .leading, endPoint: .trailing))
                    .frame(height: Self.track)
                    .frame(maxHeight: .infinity)
                    .overlay(alignment: .leading) {
                        Circle().fill(.white).frame(width: Self.dot, height: Self.dot)
                            .shadow(color: .black.opacity(0.25), radius: 1, y: 0.5)
                            .offset(x: min(max(rank * geo.size.width - Self.dot / 2, 0),
                                           geo.size.width - Self.dot))
                    }
            }
            .frame(height: Self.dot)
        }
    }
}

/// A model's name with the effort or mode the user ran it at in grey
/// after it: "Claude Opus 5.5 High · Fast". Used by the card and the
/// Models table so the qualifier reads the same everywhere.
struct ModelName: View {
    var name: String
    var qualifier: String?
    /// The logged id an alias renamed ("MODEL_PRIVATE_11"), shown muted.
    var rawID: String? = nil
    var font: Font = Theme.bodySmall

    var body: some View {
        let tail = Text(qualifier.map { " \($0)" } ?? "").foregroundStyle(Theme.textSecondary)
        let raw = Text(rawID.map { " \($0)" } ?? "").foregroundStyle(Theme.textMuted)
        Text("\(Text(name).foregroundStyle(Theme.textPrimary))\(tail)\(raw)")
            .font(font)
            .lineLimit(1).truncationMode(.middle)
    }
}
