import Foundation
import Testing
@testable import SWECore

@Suite struct KeysSectionTests {
    @Test func aVariableTheCatalogueClaimsIsOnItsTab() {
        #expect(KeysSection.infra.contains(inferenceKey("VERCEL_TOKEN"), marked: []))
        #expect(KeysSection.infra.contains(inferenceKey("DIGITALOCEAN_TOKEN"), marked: []))
        #expect(KeysSection.services.contains(inferenceKey("FIRECRAWL_API_KEY"), marked: []))
        #expect(KeysSection.services.contains(inferenceKey("BROWSER_USE_API_KEY"), marked: []))
        // A services variable is not infra's, and the other way around.
        #expect(!KeysSection.infra.contains(inferenceKey("FIRECRAWL_API_KEY"), marked: []))
        #expect(!KeysSection.services.contains(inferenceKey("VERCEL_TOKEN"), marked: []))
    }

    @Test func anInferenceVariableStaysEvenUnderAServiceHeading() {
        let row = inferenceKey("VERCEL_GATEWAY_API_KEY", provider: "Vercel")
        for section in KeysSection.allCases {
            #expect(!section.contains(row, marked: []))
            // A mark can't override a catalogue variable either way.
            #expect(!section.contains(row, marked: ["VERCEL_GATEWAY_API_KEY"]))
            #expect(!section.contains(inferenceKey("OPENAI_API_KEY"), marked: ["OPENAI_API_KEY"]))
        }
    }

    /// A saved plan key claims its name ahead of the heading: Token
    /// Pass files VERCEL_TOKEN_PASS_KEY under "# Vercel" and must stay
    /// on Inference, or the plan row would read missing.
    @Test func aPlanKeyStaysInferenceEvenUnderAServiceHeading() {
        let row = inferenceKey("VERCEL_TOKEN_PASS_KEY", provider: "Vercel")
        for section in KeysSection.allCases {
            #expect(!section.contains(row, marked: []))
            #expect(!section.contains(row, marked: ["VERCEL_TOKEN_PASS_KEY"]))
            let items = section.items(keys: [row])
            #expect(!items.contains { $0.key?.name == "VERCEL_TOKEN_PASS_KEY" })
        }
    }

    @Test func aHeadingNamingAServiceIsOnThatServiceSTab() {
        #expect(KeysSection.infra.contains(inferenceKey("SOME_TOKEN", provider: "Vercel"), marked: []))
        #expect(KeysSection.infra.contains(inferenceKey("SOME_TOKEN", provider: "FLY_IO"), marked: []))
        #expect(KeysSection.services.contains(inferenceKey("SOME_TOKEN", provider: "Firecrawl"), marked: []))
        #expect(KeysSection.services.contains(inferenceKey("SOME_TOKEN", provider: "Browser Use"), marked: []))
        #expect(!KeysSection.infra.contains(inferenceKey("SOME_TOKEN", provider: "Firecrawl"), marked: []))
        #expect(!KeysSection.services.contains(inferenceKey("SOME_TOKEN", provider: "Vercel"), marked: []))
        #expect(!KeysSection.services.contains(inferenceKey("SOME_TOKEN", provider: "MyAPI"), marked: []))
    }

    /// `KeysSection.claimed(for:)` answers which tab an unknown variable's
    /// heading files it to, or nil when no tab's catalogue knows it.
    @Test func claimedReadsVariableThenHeading() {
        #expect(KeysSection.claimed(for: inferenceKey("VERCEL_TOKEN")) == .infra)
        #expect(KeysSection.claimed(for: inferenceKey("EXA_API_KEY")) == .services)
        #expect(KeysSection.claimed(for: inferenceKey("SOME_TOKEN", provider: "Netlify")) == .infra)
        #expect(KeysSection.claimed(for: inferenceKey("SOME_TOKEN", provider: "TinyFish")) == .services)
        #expect(KeysSection.claimed(for: inferenceKey("SOME_TOKEN", provider: "Mine")) == nil)
        // Inference providers and plans are claimed by neither tab.
        #expect(KeysSection.claimed(for: inferenceKey("OPENAI_API_KEY", provider: "Vercel")) == nil)
        #expect(KeysSection.claimed(forVariable: "OPENAI_API_KEY") == false)
        #expect(KeysSection.claimed(forVariable: "EXA_API_KEY") == true)
    }

    @Test func anUnclaimedNameNeedsAMark() {
        let row = inferenceKey("MYAPI_TOKEN", provider: "MyAPI")
        #expect(!KeysSection.services.contains(row, marked: []))
        #expect(KeysSection.services.contains(row, marked: ["MYAPI_TOKEN"]))
        // Marked on one tab, it shows only there.
        #expect(!KeysSection.infra.contains(row, marked: []))
    }

    /// A mark can't put a key on two tabs: a variable a catalogue
    /// claims shows on that catalogue's tab alone, whatever an older
    /// mark filed it under before the catalogue knew the service.
    @Test func aCatalogueClaimBeatsAnotherTabsMark() {
        let row = inferenceKey("EXA_API_KEY")
        #expect(KeysSection.services.contains(row, marked: []))
        // infraKeys could hold EXA_API_KEY from before the Services
        // catalogue shipped; the Infra tab still must not show it.
        #expect(!KeysSection.infra.contains(row, marked: ["EXA_API_KEY"]))
        // The same for a heading that names the service.
        let headed = inferenceKey("SOME_TOKEN", provider: "Firecrawl")
        #expect(!KeysSection.infra.contains(headed, marked: ["SOME_TOKEN"]))
        #expect(KeysSection.services.contains(headed, marked: []))
    }

    @Test func itemsKeepFileOrderThenServicesWithNoKey() {
        let items = KeysSection.infra.items(keys: [
            inferenceKey("VERCEL_TOKEN", provider: "Deploy"),
            inferenceKey("CUSTOM_TOKEN", provider: "Mine"),
            inferenceKey("OPENAI_API_KEY"),
        ], marked: ["CUSTOM_TOKEN"])
        #expect(items.map(\.id).prefix(2).elementsEqual(["VERCEL_TOKEN", "CUSTOM_TOKEN"]))
        #expect(items[0].provider?.id == "vercel")
        #expect(items[0].title == "Vercel")
        // The custom row has no service; its heading is the title.
        #expect(items[1].provider == nil)
        #expect(items[1].title == "Mine")
        let missing = items.dropFirst(2)
        #expect(missing.count == APIKeyProviders.infra.count - 1)
        #expect(missing.allSatisfy { $0.key == nil && !$0.hasKey })
        #expect(!missing.contains { $0.id == "VERCEL_TOKEN" })
        // A key for a non-first variable still covers the service.
        let alt = KeysSection.infra.items(keys: [inferenceKey("DIGITALOCEAN_TOKEN")])
        #expect(!alt.contains { $0.id == "DIGITALOCEAN_ACCESS_TOKEN" && $0.key == nil })
    }

    @Test func servicesItemsListTheWholeCatalogue() {
        let items = KeysSection.services.items(keys: [inferenceKey("EXA_API_KEY")])
        #expect(items.map(\.id).prefix(1).elementsEqual(["EXA_API_KEY"]))
        let missing = items.dropFirst(1)
        #expect(missing.count == APIKeyProviders.services.count - 1)
        #expect(missing.map(\.provider?.id).contains("parallel"))
        #expect(missing.allSatisfy { $0.key == nil })
    }

    @Test func matchesReadsStateNameAndHeading() {
        let item = KeysSection.services.items(keys: [inferenceKey("EXA_API_KEY", provider: "Search")],
                                            marked: [])[0]
        let state: (KeysItem) -> InferenceKeyState = { $0.hasKey ? .valid : .noKey }
        #expect(KeysSection.matches(item, state: .valid, query: "", stateOf: state))
        #expect(!KeysSection.matches(item, state: .invalid, query: "", stateOf: state))
        #expect(KeysSection.matches(item, state: nil, query: "exa", stateOf: state))
        #expect(KeysSection.matches(item, state: nil, query: "exa_api_key", stateOf: state))
        #expect(KeysSection.matches(item, state: nil, query: "search", stateOf: state))
        #expect(!KeysSection.matches(item, state: nil, query: "firecrawl", stateOf: state))
    }

    @MainActor @Test func dragOrderPersistsAndNewRowsGoLast() {
        let store = MemoryStore()
        let order = KeysOrder(store: store, section: .infra)
        let items = KeysSection.infra.items(keys: [
            inferenceKey("VERCEL_TOKEN"), inferenceKey("NEON_API_KEY"),
        ], providers: [])
        order.move("NEON_API_KEY", before: "VERCEL_TOKEN", in: items)
        #expect(order.sorted(items).map(\.id) == ["NEON_API_KEY", "VERCEL_TOKEN"])
        #expect(store.values["infraOrder"] == ["NEON_API_KEY", "VERCEL_TOKEN"])
        let more = KeysSection.infra.items(keys: [
            inferenceKey("VERCEL_TOKEN"), inferenceKey("NEON_API_KEY"), inferenceKey("TURSO_API_TOKEN"),
        ], providers: [])
        #expect(KeysOrder(store: store, section: .infra).sorted(more).map(\.id)
            == ["NEON_API_KEY", "VERCEL_TOKEN", "TURSO_API_TOKEN"])
        order.move("NEON_API_KEY", before: nil, in: more)
        #expect(order.sorted(more).map(\.id) == ["VERCEL_TOKEN", "TURSO_API_TOKEN", "NEON_API_KEY"])
    }

    /// The Services tab keeps its order under its own store key; the two
    /// tabs' orders never entangle.
    @MainActor @Test func servicesOrderUsesItsOwnKey() {
        let store = MemoryStore()
        let servicesOrder = KeysOrder(store: store, section: .services)
        let items = KeysSection.services.items(keys: [
            inferenceKey("EXA_API_KEY"), inferenceKey("FIRECRAWL_API_KEY"),
        ], providers: [])
        servicesOrder.move("EXA_API_KEY", before: "FIRECRAWL_API_KEY", in: items)
        #expect(store.values["servicesOrder"] == ["EXA_API_KEY", "FIRECRAWL_API_KEY"])
        #expect(store.values["infraOrder"] == nil)
    }

    /// A key saved under a sibling variable takes the catalogue row's
    /// dragged place instead of landing at the end as a new id.
    @MainActor @Test func aSiblingVariableKeepsTheDraggedPlace() {
        let store = MemoryStore()
        let order = KeysOrder(store: store, section: .infra)
        let rows = KeysSection.infra.items(keys: [inferenceKey("VERCEL_TOKEN"), inferenceKey("NEON_API_KEY")])
        order.move("DIGITALOCEAN_ACCESS_TOKEN", before: "VERCEL_TOKEN", in: rows)
        order.substitute("DIGITALOCEAN_ACCESS_TOKEN", with: "DIGITALOCEAN_TOKEN")
        #expect(store.values["infraOrder"]?.prefix(3)
            == ["DIGITALOCEAN_TOKEN", "VERCEL_TOKEN", "NEON_API_KEY"])
        let moved = KeysSection.infra.items(keys: [
            inferenceKey("VERCEL_TOKEN"), inferenceKey("DIGITALOCEAN_TOKEN"), inferenceKey("NEON_API_KEY"),
        ])
        #expect(order.sorted(moved).prefix(3).map(\.id)
            == ["DIGITALOCEAN_TOKEN", "VERCEL_TOKEN", "NEON_API_KEY"])
    }

    @MainActor @Test func marksPersistSortedPerTab() {
        let store = MemoryStore()
        let infraMarks = MarkedKeys(store: store, section: .infra)
        let servicesMarks = MarkedKeys(store: store, section: .services)
        infraMarks.mark("MYHOSTING_TOKEN")
        infraMarks.mark("ACME_TOKEN")
        servicesMarks.mark("MYAPI_TOKEN")
        #expect(infraMarks.names == ["MYHOSTING_TOKEN", "ACME_TOKEN"])
        #expect(servicesMarks.names == ["MYAPI_TOKEN"])
        #expect(store.values["infraKeys"] == ["ACME_TOKEN", "MYHOSTING_TOKEN"])
        #expect(store.values["servicesKeys"] == ["MYAPI_TOKEN"])
        #expect(MarkedKeys(store: store, section: .infra).names == ["MYHOSTING_TOKEN", "ACME_TOKEN"])
        infraMarks.unmark("ACME_TOKEN")
        #expect(infraMarks.names == ["MYHOSTING_TOKEN"])
    }
}

@Suite struct KeysCatalogueTests {
    @Test func everyServiceInOrder() {
        for section in KeysSection.allCases {
            let entries = section.catalogueEntries(keys: [])
            #expect(entries.count == section.providers.count)
            #expect(entries.map(\.name).elementsEqual(section.providers.map(\.name)))
            #expect(entries.allSatisfy { !$0.hasKey && $0.kind == .apiKey })
        }
    }

    @Test func aSavedKeyMarksItsEntry() {
        let entries = KeysSection.infra.catalogueEntries(
            keys: [inferenceKey("VERCEL_TOKEN"), inferenceKey("DIGITALOCEAN_TOKEN"),
                   inferenceKey("NEON_API_KEY", "")])
        #expect(entries.first { $0.id == "provider:vercel" }?.hasKey == true)
        // Any of a service's variables marks it, not only the first.
        #expect(entries.first { $0.id == "provider:digitalocean" }?.hasKey == true)
        // An empty value reads as no key, like the page's own rows.
        #expect(entries.first { $0.id == "provider:neon" }?.hasKey == false)
        // The table's own claim applies: a key under "# Vercel" fills
        // Vercel in the catalogue too, while an inference variable under
        // the same heading does not.
        let byHeading = KeysSection.infra.catalogueEntries(
            keys: [inferenceKey("DEPLOY_TOKEN", provider: "Vercel"),
                   inferenceKey("OPENAI_API_KEY", provider: "Cloudflare")])
        #expect(byHeading.first { $0.id == "provider:vercel" }?.hasKey == true)
        #expect(byHeading.first { $0.id == "provider:cloudflare" }?.hasKey == false)
        // Services read their own claims.
        let services = KeysSection.services.catalogueEntries(
            keys: [inferenceKey("FIRECRAWL_API_KEY"),
                   inferenceKey("SOME_KEY", provider: "Browser Use")])
        #expect(services.first { $0.id == "provider:firecrawl" }?.hasKey == true)
        #expect(services.first { $0.id == "provider:browser_use" }?.hasKey == true)
        #expect(services.first { $0.id == "provider:exa" }?.hasKey == false)
    }

    /// The sheet's mark column: every row's logo names a bundled
    /// harness-*.svg, as the inference catalogue's own test pins.
    @Test func everyEntryResolvesABundledMark() {
        for section in KeysSection.allCases {
            for entry in section.catalogueEntries(keys: []) {
                let logo = entry.logo
                #expect(logo != nil, "\(entry.id) has no bundled mark")
                #expect(logo.flatMap { HarnessIcons.icon(for: $0) } != nil,
                        "\(entry.id) names \(logo ?? "?") with no harness-*.svg")
            }
        }
    }
}

@Suite struct KeysProbeTests {
    /// Each non-inference service's first variable either has a probe
    /// with an auth-reading host, or an explicit no-check reason; a
    /// silent nil on the probe table would land as the generic message
    /// instead.
    @Test func everyServiceStatesItsCheck() {
        let reasoned: [KeysSection: Set<String>] = [
            .infra: ["fly", "railway", "heroku"],
            .services: ["brave", "langfuse"],
        ]
        for section in KeysSection.allCases {
            for provider in section.providers {
                let name = provider.envVars[0]
                let probed = APIKeyProbe.probe(forName: name) != nil
                let excused = reasoned[section]?.contains(provider.id) ?? false
                #expect(probed || excused,
                        "\(provider.id) has no probe and no stated no-check reason")
                if excused {
                    #expect(!APIKeyProbe.noCheckReason(forName: name).contains("doesn't know how"),
                            "\(provider.id) should name why there's no check")
                }
            }
        }
    }

    @Test func infraProbesAreBearerGets() {
        let probe = APIKeyProbe.probe(forName: "VERCEL_TOKEN")
        #expect(probe?.auth == .bearer)
        #expect(probe?.method == "GET")
        #expect(probe?.hosts == [URL(string: "https://api.vercel.com/v2/user")!])
        // Both DigitalOcean spellings share one probe.
        #expect(APIKeyProbe.probe(forName: "DIGITALOCEAN_TOKEN")
            == APIKeyProbe.probe(forName: "DIGITALOCEAN_ACCESS_TOKEN"))
    }

    /// The services probes: the cheapest auth-reading endpoint under
    /// the header the service actually takes, each verified to refuse a
    /// fake key. Parallel and Exa run auth middleware before routing a
    /// request, so a valid key 404s every GET after it passes: they
    /// check on a POST whose empty body a real key answers with a
    /// request error.
    @Test func servicesProbesMatchTheDocumentedAuth() {
        #expect(APIKeyProbe.probe(forName: "PARALLEL_API_KEY")
            == APIKeyProbe(hosts: [URL(string: "https://api.parallel.ai/v1/tasks/runs")!],
                           method: "POST", body: Data("{}".utf8), auth: .header("x-api-key"),
                           worksOnBadRequest: true))
        #expect(APIKeyProbe.probe(forName: "FIRECRAWL_API_KEY")?.auth == .bearer)
        #expect(APIKeyProbe.probe(forName: "TINYFISH_API_KEY")?.auth == .header("X-API-Key"))
        #expect(APIKeyProbe.probe(forName: "BROWSER_USE_API_KEY")?.auth == .header("X-Browser-Use-API-Key"))
        #expect(APIKeyProbe.probe(forName: "EXA_API_KEY")
            == APIKeyProbe(hosts: [URL(string: "https://api.exa.ai/search")!],
                           method: "POST", body: Data("{}".utf8), auth: .header("x-api-key"),
                           worksOnBadRequest: true))
        #expect(APIKeyProbe.probe(forName: "AGENTMAIL_API_KEY")
            == APIKeyProbe(hosts: [URL(string: "https://api.agentmail.to/v0/inboxes")!],
                           method: "GET", body: nil, auth: .bearer,
                           worksOnBadRequest: false))
    }
}
