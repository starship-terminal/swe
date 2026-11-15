import Foundation
import Security
import Testing
@testable import SWECore

/// Temperatures and fans: the sensor fold, the SMC value codec, the root
/// helper's hold loop against a fake SMC, and the popover's model. Fakes
/// only; nothing here touches the real SMC.
@Suite struct FanTests {
    /// This test's own folder, removed with its contents when the test ends.
    let scratch: TempDir
    init() throws { scratch = try TempDir() }

    // MARK: Temperatures

    @Test func theDieSensorsAverageAndJunkSensorsDrop() {
        let reading = TemperatureReading.summarise([
            ("PMU tdie1", 50), ("PMU tdie2", 60), ("PMU tdie2", 70),
            ("PMU tdev1", -9202.9), ("PMU tcal", 51.8), ("NAND CH0 temp", 45),
            ("gas gauge battery", 34), ("gas gauge battery", 36),
        ])
        #expect(reading == TemperatureReading([.cpu: 60, .ssd: 45, .battery: 35]))
    }

    @Test func noKnownSensorMeansNoReading() {
        #expect(TemperatureReading.summarise([]) == nil)
        #expect(TemperatureReading.summarise([("PMU tdev1", -9000), ("PMU tcal", 50)]) == nil)
        #expect(TemperatureReading.summarise([("gas gauge battery", 30)])?[.cpu] == nil)
    }

    @Test func hidIsReadOnlyForWhatTheSMCLeftOut() {
        var asked = 0
        let hid = { () -> TemperatureReading? in
            asked += 1
            return TemperatureReading([.cpu: 70, .ssd: 40, .battery: 30])
        }
        // The SMC answered all three: the HID sensors are never read.
        let full = TemperatureReading([.cpu: 60, .gpu: 55, .ssd: 45, .battery: 35])
        #expect(MacTemperatureSampler.combine(full, hid: hid) == full)
        #expect(asked == 0)

        // No SSD key answered: HID is read and fills only the gap.
        let partial = TemperatureReading([.cpu: 60, .battery: 35])
        #expect(MacTemperatureSampler.combine(partial, hid: hid)
                == TemperatureReading([.cpu: 60, .ssd: 40, .battery: 35]))
        #expect(asked == 1)

        // No SMC at all: HID is the whole reading, and nothing means nil.
        #expect(MacTemperatureSampler.combine(nil, hid: hid)?[.cpu] == 70)
        #expect(MacTemperatureSampler.combine(nil, hid: { nil }) == nil)
    }

    @Test func chipNamesParse() {
        #expect(ThermalKeys.chip("Apple M4 Pro")! == (4, "pro"))
        #expect(ThermalKeys.chip("Apple M1")! == (1, "base"))
        #expect(ThermalKeys.chip("Apple M3 Ultra")! == (3, "ultra"))
        #expect(ThermalKeys.chip("Apple A18 Pro")! == (18, "a18pro"))
        #expect(ThermalKeys.chip("Intel(R) Core(TM) i9-9880H CPU @ 2.30GHz") == nil)
        #expect(ThermalKeys.chip("") == nil)
    }

    @Test func eachChipGetsItsOwnGPUKeys() {
        let pro = ThermalKeys.keys(forBrand: "Apple M4 Pro")
        #expect(pro[.gpu]?.contains("Tg1U") == true && pro[.gpu]?.contains("Tg0G") == false)
        let base = ThermalKeys.keys(forBrand: "Apple M4")
        #expect(base[.gpu]?.contains("Tg0G") == true && base[.gpu]?.contains("Tg1U") == false)
        #expect(ThermalKeys.keys(forBrand: "Apple M2")[.gpu] == ["Tg0f", "Tg0j"])
        // An unknown chip: the shared keys only, so HID fills the CPU.
        let unknown = ThermalKeys.keys(forBrand: "Apple M9 Max")
        #expect(unknown[.cpu] == nil && unknown[.gpu] == nil)
        #expect(unknown[.ssd] == ["TH0x"] && unknown[.display] == ["TL0P"])
    }

    @Test func smcKeysFoldPerComponentAndSkipJunk() {
        let smc = FakeSMC()
        smc.put("Tp01", 70); smc.put("Tp05", 80); smc.put("Te05", 60)
        smc.put("Tg1U", 55); smc.put("Tg0K", 65)
        smc.put("TH0x", 40)
        // An unused sensor answers about 8 °C; it must not drag a mean down.
        smc.put("TaLP", 8.3); smc.put("TaRF", 8.3)
        let reading = ThermalKeys.read(ThermalKeys.keys(forBrand: "Apple M4 Pro"), from: smc)
        #expect(reading == TemperatureReading([.cpu: 70, .gpu: 60, .ssd: 40]))
        #expect(ThermalKeys.read(ThermalKeys.keys(forBrand: "Apple M4 Pro"), from: FakeSMC()) == nil)
    }

    @Test func aFanCountsAsUpOnlyClearOfItsIdleDrift() {
        func fan(_ actual: Double) -> FanReading { FanReading(index: 0, actual: actual, minimum: 2317, maximum: 7826) }
        // An M4 Pro's second fan idles near 2,500 against a 2,317 floor.
        #expect(FanSummary.from([fan(2317), fan(2509)]) == FanSummary(rpm: 2509, up: false))
        #expect(FanSummary.from([fan(2317), fan(2700)]) == FanSummary(rpm: 2700, up: true))
        // Spun down is not up; no fans is no summary.
        #expect(FanSummary.from([fan(0)])?.up == false)
        #expect(FanSummary.from([]) == nil)
    }

    @Test func smcReadingsWinAndHIDFillsTheGaps() {
        let smc = TemperatureReading([.cpu: 70, .gpu: 60])
        let hid = TemperatureReading([.cpu: 55, .battery: 33])
        #expect(smc.filling(from: hid) == TemperatureReading([.cpu: 70, .gpu: 60, .battery: 33]))
        #expect(smc.filling(from: nil) == smc)
    }

    @Test func everyComponentHasItsOwnTitle() {
        let titles = ThermalComponent.allCases.map(\.title)
        #expect(Set(titles).count == titles.count)
        #expect(ThermalComponent.cpu.title == "CPU" && ThermalComponent.airflow.title == "Airflow")
    }

    @Test func chipsTheKeyMapDoesNotKnowParseToNil() {
        #expect(ThermalKeys.chip("Apple") == nil)
        #expect(ThermalKeys.chip("Apple Mx Pro") == nil)
        // Only the A18 Pro has a key list; other A-series chips are unknown.
        #expect(ThermalKeys.chip("Apple A18") == nil)
        #expect(ThermalKeys.chip("Apple A17 Pro") == nil)
    }

    @Test func everyKnownGenerationGetsItsOwnCPUAndGPUKeys() {
        let brands = ["Apple M1", "Apple M2", "Apple M3", "Apple M4", "Apple M5", "Apple M6", "Apple A18 Pro"]
        let maps = brands.map { ThermalKeys.keys(forBrand: $0) }
        for (brand, keys) in zip(brands, maps) {
            #expect(keys[.cpu]?.isEmpty == false, "\(brand)")
            #expect(keys[.gpu]?.isEmpty == false, "\(brand)")
            // The shared keys ride along on every chip.
            #expect(keys[.ssd] == ["TH0x"] && keys[.battery]?.count == 3, "\(brand)")
        }
        // The same key can be another core on another chip, so no two
        // generations share a CPU list.
        let cpu = maps.compactMap { $0[.cpu] }
        #expect(Set(cpu).count == brands.count)
        // Memory keys exist for the M1 and M4 lines only.
        #expect(maps.map { $0[.memory] != nil } == [true, false, false, true, false, false, false])
    }

    @Test func aSamplerTakesTheSMCAndFansAndLeavesHIDAlone() {
        let smc = FakeSMC.twoFans()
        smc.put("Tp01", 70); smc.put("TH0x", 40); smc.put("TB0T", 30)
        let hid = Thermometer(TemperatureReading([.cpu: 99, .ssd: 99, .battery: 99]))
        let session = SMCSession(open: { smc })
        let sampler = MacTemperatureSampler(brand: "Apple M4 Pro", hid: hid, smc: session)
        let reading = sampler.read()
        #expect(reading?.values == [.cpu: 70, .ssd: 40, .battery: 30])
        #expect(reading?.fans == FanSummary(rpm: 2509, up: false))
        #expect(hid.reads == 0)
        _ = sampler.read()
        #expect(session.opened == 1)
    }

    @Test func fansAloneKeepTheConnectionAndHIDGivesTheTemperatures() {
        let smc = FakeSMC.twoFans()
        let hid = Thermometer(TemperatureReading([.cpu: 55]))
        let session = SMCSession(open: { smc })
        let sampler = MacTemperatureSampler(brand: "Apple M4 Pro", hid: hid, smc: session)
        #expect(sampler.read() == TemperatureReading([.cpu: 55], fans: FanSummary(rpm: 2509, up: false)))
        _ = sampler.read()
        #expect(session.opened == 1)
    }

    @Test func aSilentSMCIsReopenedNextTickAndHIDStandsIn() {
        let session = SMCSession(open: { FakeSMC() })
        let hid = Thermometer(TemperatureReading([.cpu: 55]))
        let sampler = MacTemperatureSampler(brand: "Apple M4 Pro", hid: hid, smc: session)
        #expect(sampler.read() == TemperatureReading([.cpu: 55]))
        #expect(sampler.read() == TemperatureReading([.cpu: 55]))
        // Nothing back drops the connection, so each tick opens a new one.
        #expect(session.opened == 2)
        hid.reading = nil
        #expect(sampler.read() == nil)
    }

    @Test func noSMCAtAllLeavesHIDAsTheWholeReading() {
        let session = SMCSession(open: { nil })
        let hid = Thermometer(TemperatureReading([.battery: 31]))
        let sampler = MacTemperatureSampler(brand: "Intel(R) Core(TM) i9-9880H CPU @ 2.30GHz", hid: hid, smc: session)
        #expect(sampler.read() == TemperatureReading([.battery: 31]))
        #expect(session.opened == 0 && hid.reads == 1)
    }

    // MARK: SMC values

    @Test func smcNumbersDecodeByType() {
        #expect(SMCValue(type: "flt ", bytes: SMCValue.float(2317)).number == 2317)
        #expect(SMCValue(type: "ui8 ", bytes: [2]).number == 2)
        #expect(SMCValue(type: "ui16", bytes: [0x01, 0x02]).number == 258)
        #expect(SMCValue(type: "ui32", bytes: [0, 0, 1, 0]).number == 256)
        #expect(SMCValue(type: "flt ", bytes: [1]).number == nil)
        #expect(SMCValue(type: "ch8*", bytes: [65]).number == nil)
    }

    @Test func fourCharacterCodesRoundTrip() {
        #expect(AppleSMC.fourCC("F0Ac") == 0x4630_4163)
        #expect(AppleSMC.string(0x4630_4163) == "F0Ac")
        #expect(AppleSMC.fourCC("TOO LONG") == nil)
    }

    @Test func theDriverStructIsItsOwnEightyBytes() {
        // The driver refuses a struct of any other size, which would read
        // as every key missing rather than as an error.
        #expect(AppleSMC.paramSize == 80)
    }

    @Test func aRefusedWriteSaysWhy() {
        #expect(SMCWriteResult.sizeMismatch(type: "ui8 ", size: 1, given: 4).description
                == "the key is 'ui8 ' with 1 bytes, 4 given")
        // kIOReturnNotPrivileged prints as its unsigned hex, not a negative.
        #expect(SMCWriteResult.failed(kern: -536_870_206, result: 0x84).description
                == "refused (kern 0xe00002c2, SMC result 0x84)")
        #expect(SMCWriteResult.failed(kern: 0, result: 1).description == "refused (kern 0x0, SMC result 0x1)")
    }

    // MARK: Fan readings

    @Test func aLevelMapsOntoTheFansOwnRange() {
        let fan = FanReading(index: 0, actual: 2400, minimum: 2000, maximum: 6000)
        #expect(fan.rpm(atLevel: 0) == 2000)
        #expect(fan.rpm(atLevel: 0.5) == 4000)
        #expect(fan.rpm(atLevel: 1) == 6000)
        // Never below the floor, never above the top.
        #expect(fan.rpm(atLevel: -1) == 2000)
        #expect(fan.rpm(atLevel: 7) == 6000)
    }

    @Test func fansReadFromTheSMC() {
        let smc = FakeSMC.twoFans()
        let fans = FanKeys.read(smc)
        #expect(fans.map(\.index) == [0, 1])
        #expect(fans[0] == FanReading(index: 0, actual: 2319, minimum: 2317, maximum: 7826,
                                      target: 2317, mode: 0))
        #expect(FanKeys.read(FakeSMC()).isEmpty)
    }

    @Test func m5SpellsTheModeKeyInLowerCase() {
        let smc = FakeSMC.twoFans()
        smc.rename("F0Md", to: "F0md")
        #expect(FanKeys.mode(0, in: smc) == "F0md")
        #expect(FanKeys.mode(1, in: smc) == "F1Md")
        #expect(FanKeys.read(smc)[0].mode == 0)
    }

    // MARK: The hold loop

    private func ask(_ level: Double, owner: Int32 = 77, at seconds: Double = 1) -> FanControlFile {
        FanControlFile(level: level, owner: owner, requested: Date(timeIntervalSince1970: seconds))
    }

    /// A loop that reads one request per pass, then one empty pass, then
    /// stops.
    private func hold(_ smc: FakeSMC, _ requests: [FanControlFile?],
                      ownerAlive: @escaping (Int32) -> Bool = { _ in true },
                      underPressure: Bool = false) -> FanHold {
        let counter = Box(0)
        var hold = FanHold(
            smc: smc,
            request: { counter.value < requests.count ? requests[counter.value] : nil },
            ownerAlive: ownerAlive,
            underPressure: { underPressure },
            shouldStop: { counter.value > requests.count },
            sleep: { seconds in if seconds >= 1 { counter.value += 1 } })
        hold.unlockTimeout = 1
        return hold
    }

    @Test func aMacThatTakesTheModeWriteHoldsAtTheLevelThenLetsGo() {
        let smc = FakeSMC.twoFans(gate: false)
        var targets: [Double] = []
        var manualWhileHolding = false
        smc.onWrite = { key, bytes in
            if key == "F0Tg" { targets.append(SMCValue(type: "flt ", bytes: bytes).number ?? 0) }
            if key == "F1Tg" { manualWhileHolding = smc.number("F0Md") == 1 && smc.number("F1Md") == 1 }
        }
        hold(smc, [ask(0.5), ask(0.5)]).run()
        // Halfway between 2317 and 7826, written once and not rewritten.
        #expect(targets == [5071.5])
        #expect(manualWhileHolding)
        // The request went away: both fans automatic again.
        #expect(smc.number("F0Md") == 0 && smc.number("F1Md") == 0)
    }

    @Test func m4NeedsFtstAndRetriesUntilThermalmonitordLetsGo() {
        let smc = FakeSMC.twoFans()
        // The mode write fails until the gate is open and a few retries
        // have passed, as thermalmonitord takes a few seconds to let go.
        smc.refuseModeWrites = 5
        var sawManual = false
        smc.onWrite = { key, _ in if key == "F1Tg" { sawManual = smc.number("F0Md") == 1 } }
        hold(smc, [ask(0.5)]).run()
        #expect(sawManual)
        // Gate closed again once the fans are released.
        #expect(smc.number("Ftst") == 0)
        #expect(smc.number("F0Md") == 0)
    }

    @Test func aRequestTheFansRefuseIsNotRetriedUntilANewOne() {
        let smc = FakeSMC.twoFans()
        smc.refuseModeWrites = .max
        var gateOpens = 0
        smc.onWrite = { key, bytes in if key == "Ftst" && bytes == [1] { gateOpens += 1 } }
        hold(smc, [ask(0.5), ask(0.5), ask(0.5)]).run()
        #expect(gateOpens == 1)
        #expect(smc.number("Ftst") == 0)
        // Pressing Set speed again stamps a new request, which is tried.
        gateOpens = 0
        hold(smc, [ask(0.5, at: 1), ask(0.5, at: 1), ask(0.5, at: 2)]).run()
        #expect(gateOpens == 2)
    }

    @Test func noRequestOrAGoneOwnerNeverTouchesTheFans() {
        let smc = FakeSMC.twoFans(gate: false)
        var writes: [String] = []
        smc.onWrite = { key, _ in writes.append(key) }
        hold(smc, [nil, nil]).run()
        hold(smc, [ask(1), ask(1)], ownerAlive: { _ in false }).run()
        #expect(writes.isEmpty)
    }

    @Test func theOwnerQuittingHandsTheFansBack() {
        let smc = FakeSMC.twoFans(gate: false)
        let alive = Box(true)
        var modeAfterQuit: Double?
        let counter = Box(0)
        smc.onWrite = { key, _ in if key == "F0Md" { modeAfterQuit = smc.number("F0Md") } }
        hold(smc, [ask(1), ask(1), ask(1), ask(1)], ownerAlive: { _ in
            counter.value += 1
            if counter.value > 2 { alive.value = false }
            return alive.value
        }).run()
        #expect(modeAfterQuit == 0)
        #expect(smc.number("F0Md") == 0)
    }

    @Test func thermalPressureRunsTheFansFlatOut() {
        let smc = FakeSMC.twoFans(gate: false)
        var target: Double?
        smc.onWrite = { key, bytes in if key == "F0Tg" { target = SMCValue(type: "flt ", bytes: bytes).number } }
        hold(smc, [ask(0)], underPressure: true).run()
        #expect(target == 7826)
    }

    @Test func aReleaseThatDidNotStickIsRetried() {
        let smc = FakeSMC.twoFans(gate: false)
        smc.refuseReleaseWrites = 1
        var releaseWrites = 0
        smc.onWrite = { key, bytes in if key == "F0Md" && bytes == [0] { releaseWrites += 1 } }
        hold(smc, [ask(1), nil, nil]).run()
        #expect(releaseWrites == 1)
        #expect(smc.number("F0Md") == 0 && smc.number("F1Md") == 0)
    }

    @Test func automaticWhileUnlockingStopsTheRetries() {
        let smc = FakeSMC.twoFans()
        smc.refuseModeWrites = .max
        let reads = Box(0)
        var targets = 0
        smc.onWrite = { key, _ in if key.hasSuffix("Tg") { targets += 1 } }
        let counter = Box(0)
        let request = ask(1)
        var hold = FanHold(
            smc: smc,
            // The request is there for the pass, then withdrawn after a
            // few retries' worth of reads.
            request: { reads.value += 1; return counter.value == 0 && reads.value < 4 ? request : nil },
            ownerAlive: { _ in true }, underPressure: { false },
            shouldStop: { counter.value > 1 },
            sleep: { seconds in if seconds >= 1 { counter.value += 1 } })
        hold.unlockTimeout = 60
        hold.run()
        // Far fewer than the 600 retries the timeout would allow.
        #expect(reads.value < 10)
        #expect(targets == 0)
        #expect(smc.number("Ftst") == 0)
    }

    @Test func noFansMeansNothingToHold() {
        let smc = FakeSMC()
        hold(smc, [ask(1)]).run()
        #expect(FanKeys.read(smc).isEmpty)
    }

    @Test func aRefusalSaysWhyOnceAndIsLogged() {
        let smc = FakeSMC.twoFans(gate: false)
        smc.refuseModeWrites = .max
        var reports: [FanHelperStatus] = []
        var lines: [String] = []
        var hold = hold(smc, [ask(0.5), ask(0.5), ask(0.5)])
        hold.report = { reports.append($0) }
        hold.log = { lines.append($0) }
        hold.run()
        #expect(reports.map(\.state) == [.refused])
        #expect(reports.first?.answers(ask(0.5)) == true)
        #expect(reports.first?.reason
            == "The SMC refused manual mode for Fan 1 (F0Md: refused), and this Mac has no Ftst gate to open.")
        #expect(lines.contains("Fan 1: F0Md type 'ui8 ' size 1 bytes 00 = 0.0; write 1 -> refused"))
        #expect(lines.contains("Fan 1: Ftst absent, no gate to open"))
    }

    @Test func aGateThatDoesNotHelpSaysSo() {
        let smc = FakeSMC.twoFans()
        smc.refuseModeWrites = .max
        var reports: [FanHelperStatus] = []
        var hold = hold(smc, [ask(0.5)])
        hold.report = { reports.append($0) }
        hold.run()
        #expect(reports.first?.reason == "The SMC refused manual mode for Fan 1 (F0Md: refused), even with Ftst open.")
    }

    @Test func aGateThatWouldNotOpenSaysSo() {
        let smc = FakeSMC.twoFans()
        smc.refuseModeWrites = .max
        smc.refuseGateWrites = true
        var reports: [FanHelperStatus] = []
        var hold = hold(smc, [ask(0.5)])
        hold.report = { reports.append($0) }
        hold.run()
        #expect(reports.first?.reason == "The SMC refused manual mode for Fan 1 (F0Md: refused), "
            + "and opening the Ftst gate failed (refused).")
    }

    @Test func everyPassBeatsForTheWatchdog() {
        var beats = 0
        var hold = hold(FakeSMC.twoFans(gate: false), [ask(0.5)])
        hold.beat = { beats += 1 }
        hold.run()
        // One pass takes the request, one empty one hands it back: a
        // pass that never ends is a beat that never comes.
        #expect(beats == 2)
    }

    @Test func aHeldRequestIsReportedOnce() {
        var reports: [FanHelperStatus] = []
        var hold = hold(FakeSMC.twoFans(gate: false), [ask(0.5), ask(0.5), ask(0.7, at: 2)])
        hold.report = { reports.append($0) }
        hold.run()
        #expect(reports.map(\.state) == [.held, .held])
        #expect(reports.last?.answers(ask(0.7, at: 2)) == true)
    }

    @Test func aRequestFromNoRunningSWEIsReported() {
        var reports: [FanHelperStatus] = []
        var hold = hold(FakeSMC.twoFans(gate: false), [ask(1), ask(1)], ownerAlive: { _ in false })
        hold.report = { reports.append($0) }
        hold.run()
        #expect(reports.map(\.state) == [.refused])
        #expect(reports.first?.reason?.contains("pid 77") == true)
    }

    @Test func aWiderModeKeyIsWrittenAtItsOwnSize() {
        let smc = FakeSMC.twoFans(gate: false)
        smc.replace("F0Md", SMCValue(type: "ui16", bytes: [0, 0]))
        var modeWrites: [[UInt8]] = []
        smc.onWrite = { key, bytes in if key == "F0Md" { modeWrites.append(bytes) } }
        hold(smc, [ask(0.5)]).run()
        // Manual as a big-endian 1, then back to 0, never a lone byte.
        #expect(modeWrites == [[0, 1], [0, 0]])
    }

    @Test func aModeKeyOfAnUnknownTypeIsRefusedByName() {
        let smc = FakeSMC.twoFans(gate: false)
        smc.replace("F0Md", SMCValue(type: "{xyz", bytes: [0, 0, 0]))
        var reports: [FanHelperStatus] = []
        var hold = hold(smc, [ask(0.5)])
        hold.report = { reports.append($0) }
        hold.run()
        #expect(reports.first?.reason
            == "Fan 1's mode key F0Md is '{xyz' with 3 bytes, which the fan helper doesn't know how to write.")
    }

    @Test func valuesEncodeInTheKeysOwnTypeAndSize() {
        #expect(SMCValue(type: "ui8 ", bytes: [0]).encoding(1) == [1])
        #expect(SMCValue(type: "flag", bytes: [0]).encoding(1) == [1])
        #expect(SMCValue(type: "ui16", bytes: [0, 0]).encoding(1) == [0, 1])
        #expect(SMCValue(type: "ui32", bytes: [0, 0, 0, 0]).encoding(1) == [0, 0, 0, 1])
        #expect(SMCValue(type: "si16", bytes: [0, 0]).encoding(1) == nil)
        #expect(SMCValue(type: "ui8 ", bytes: [0]).encoding(256) == nil)
        // Whatever is written reads back as the same number.
        for type in ["ui8 ", "ui16", "ui32", "flt "] {
            let key = SMCValue(type: type, bytes: Array(repeating: 0, count: type == "ui8 " ? 1 : type == "ui16" ? 2 : 4))
            for value: UInt32 in [0, 1, 3] {
                #expect(key.encoding(value).flatMap { SMCValue(type: type, bytes: $0).number } == Double(value))
            }
        }
    }

    @Test func theSurveyListsEveryFanKeyAndTheGate() {
        let smc = FakeSMC.twoFans()
        smc.rename("F0Md", to: "F0md")
        let lines = FanKeys.survey(smc)
        #expect(lines.count == 1 + 2 * 6 + 1)
        #expect(lines.first == "FNum: type 'ui8 ' size 1 bytes 02 = 2.0")
        #expect(lines.contains("F0Md: absent"))
        #expect(lines.contains("F0md: type 'ui8 ' size 1 bytes 00 = 0.0"))
        #expect(lines.last == "Ftst: type 'ui8 ' size 1 bytes 00 = 0.0")
        #expect(FanKeys.survey(FakeSMC()) == ["FNum: absent", "Ftst: absent"])
    }

    @Test func theHelperStatusRoundTripsAndAnswersOnlyItsRequest() throws {
        let url = scratch.url.appending(path: "fan-status-\(UUID().uuidString).json")
        defer { try? FileManager.default.removeItem(at: url) }
        #expect(FanHelperStatus.read(at: url) == nil)
        let request = ask(0.5, at: 1_800_000_000.123)
        let status = FanHelperStatus(state: .refused, reason: "no", request: request,
                                     at: Date(timeIntervalSince1970: 5))
        try status.write(to: url)
        #expect(FanHelperStatus.read(at: url) == status)
        let mode = try FileManager.default.attributesOfItem(atPath: url.path)[.posixPermissions] as? Int
        #expect(mode == 0o644)
        #expect(status.answers(request))
        #expect(!status.answers(ask(0.5, at: 2)))
        #expect(!status.answers(ask(0.5, owner: 78, at: 1_800_000_000.123)))
        #expect(!status.answers(nil))
        #expect(!FanHelperStatus(state: .ready).answers(request))
    }

    /// The time limit catches a kill that never comes; the 60 s sleep
    /// would outlast it.
    @Test(.timeLimit(.minutes(1))) func aToolThatHangsIsKilledAndCountsAsNoAnswer() async {
        let (yes, no, hung, missing) = await offMain {
            (FanControl.exitStatus(of: "/usr/bin/true", [], within: 5),
             FanControl.exitStatus(of: "/usr/bin/false", [], within: 5),
             FanControl.exitStatus(of: "/bin/sleep", ["60"], within: 0.2),
             FanControl.exitStatus(of: "/nonexistent/\(UUID().uuidString)", [], within: 1))
        }
        #expect(yes == 0)
        #expect(no == 1)
        #expect(hung == nil)
        #expect(missing == nil)
    }

    // MARK: The helper's arguments and install

    @Test func helperArgumentsParse() {
        #expect(FanHelper.parse(["SWE", "--fan-daemon", "--control", "/tmp/c.json"])?.path == "/tmp/c.json")
        #expect(FanHelper.parse(["SWE", "--fan-daemon", "--control", "relative.json"]) == nil)
        #expect(FanHelper.parse(["SWE", "--fan-daemon"]) == nil)
        #expect(FanHelper.parse(["SWE", "--control", "/tmp/c.json"]) == nil)
        #expect(FanHelper.runIfRequested(["SWE", "-swePath", "/x"]) == nil)
        #expect(FanHelper.runIfRequested(["SWE", "--fan-daemon"]) == 2)
    }

    @Test func theLaunchdJobRunsTheRootOwnedCopyAndStaysUp() throws {
        let data = FanHelper.plist(control: URL(fileURLWithPath: "/Users/me/.swe/fan/control.json"),
                                   owner: .team("ABCDE12345", identifier: "com.cwenlong.swe"))
        let job = try #require(try PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any])
        #expect(job["Label"] as? String == "com.cwenlong.swe.fan")
        #expect(job["ProgramArguments"] as? [String] == [
            "/Library/PrivilegedHelperTools/com.cwenlong.swe.fan", "--fan-daemon",
            "--control", "/Users/me/.swe/fan/control.json", "--owner-team", "ABCDE12345",
            "--owner-identifier", "com.cwenlong.swe",
            "--helper-version", String(FanHelper.version)])
        #expect(job["KeepAlive"] as? Bool == true)
        #expect(job["RunAtLoad"] as? Bool == true)
    }

    @Test func theInstallScriptChecksTheCopyAndWritesTheJobAsRoot() throws {
        let command = AdministratorInstaller.installCommand(
            executable: "/Users/o'neil/Apps/SWE.app/Contents/MacOS/SWE", sha256: "abc123", owner: Self.adHoc,
            control: URL(fileURLWithPath: "/Users/o'neil/.swe/fan/control.json"))
        #expect(command.contains(#"/usr/bin/install -m 755 -o root -g wheel '/Users/o'\''neil/Apps/SWE.app/Contents/MacOS/SWE' '/Library/PrivilegedHelperTools/com.cwenlong.swe.fan.new'"#))
        // The hash is checked on the root-owned copy, before re-signing
        // changes its bytes.
        let hash = try #require(command.range(of: "/usr/bin/shasum -a 256 '/Library/PrivilegedHelperTools/"))
        let sign = try #require(command.range(of: "/usr/bin/codesign --force --sign -"))
        #expect(hash.lowerBound < sign.lowerBound)
        #expect(command.contains("= 'abc123' ]"))
        // No file from the user's side becomes the job: root writes it.
        #expect(command.contains("/usr/bin/printf '%s' '<?xml"))
        #expect(command.contains(#"<string>/Users/o'\''neil/.swe/fan/control.json</string>"#))
        #expect(command.contains("> '/Library/LaunchDaemons/com.cwenlong.swe.fan.plist'"))
        #expect(!command.contains("\n"))
        #expect(command.hasSuffix("/bin/launchctl bootstrap system '/Library/LaunchDaemons/com.cwenlong.swe.fan.plist'"))
        #expect(AdministratorInstaller.uninstallCommand.contains("/bin/rm -f '/Library/LaunchDaemons/"))
        #expect(AdministratorInstaller.script(#"echo "a\b" 'c'"#, prompt: #"Say "hi""#)
            == #"do shell script "echo \"a\\b\" 'c'" with prompt "Say \"hi\"" with administrator privileges"#)
    }

    /// A team-signed app checks the helper's signature on the root-owned
    /// copy, after the hash and before the ad-hoc re-sign erases it: a
    /// helper patched in the user-writable bundle never runs as root.
    @Test func aSignedAppChecksTheHelpersTeamBeforeRunningItAsRoot() throws {
        let command = AdministratorInstaller.installCommand(
            executable: "/Apps/SWE.app/Contents/MacOS/SWEFanHelper", sha256: "abc123", owner: .team("ABCDE12345", identifier: "com.cwenlong.swe"),
            control: URL(fileURLWithPath: "/Users/me/.swe/fan/control.json"))
        let hash = try #require(command.range(of: "/usr/bin/shasum -a 256 "))
        let verify = try #require(command.range(
            of: #"/usr/bin/codesign --verify --strict -R '=identifier "SWEFanHelper" and anchor apple generic"#
                + #" and certificate leaf[subject.OU] = "ABCDE12345"' '/Library/PrivilegedHelperTools/com.cwenlong.swe.fan.new'"#))
        let sign = try #require(command.range(of: "/usr/bin/codesign --force --sign -"))
        let bootout = try #require(command.range(of: "/bin/launchctl bootout"))
        let move = try #require(command.range(of: "/bin/mv -f '/Library/PrivilegedHelperTools/com.cwenlong.swe.fan.new'"
            + " '/Library/PrivilegedHelperTools/com.cwenlong.swe.fan'"))
        let job = try #require(command.range(of: "/usr/bin/printf '%s'"))
        #expect(hash.lowerBound < verify.lowerBound)
        #expect(verify.lowerBound < sign.lowerBound)
        // The live executable an old KeepAlive job could relaunch changes
        // only after every check, and only once that job is unloaded.
        #expect(sign.lowerBound < bootout.lowerBound)
        #expect(bootout.lowerBound < move.lowerBound)
        #expect(move.lowerBound < job.lowerBound)
        // Nothing but the move writes the live path.
        #expect(command.components(separatedBy: "'/Library/PrivilegedHelperTools/com.cwenlong.swe.fan'").count == 2)
        #expect(!command.contains("\n"))
        // An ad-hoc build has no team to check against and keeps the hash.
        let adHoc = AdministratorInstaller.installCommand(
            executable: "/Apps/SWE.app/Contents/MacOS/SWEFanHelper", sha256: "abc123", owner: Self.adHoc,
            control: URL(fileURLWithPath: "/Users/me/.swe/fan/control.json"))
        #expect(!adHoc.contains("--verify"))
        #expect(adHoc.contains("/usr/bin/shasum -a 256 "))
        // Either way the job root writes pins whose requests the helper takes.
        #expect(command.contains("<string>--owner-team</string><string>ABCDE12345</string>"
            + "<string>--owner-identifier</string><string>com.cwenlong.swe</string>"))
        #expect(adHoc.contains("<string>--owner-cdhash</string><string>\(Self.adHocHash)</string>"))
    }

    /// The check itself, run: a helper that isn't the team's (here an
    /// ad-hoc signed copy) is deleted and the script stops with a reason.
    @Test func aHelperWithoutTheTeamsSignatureIsRefusedAndDeleted() async throws {
        let tool = scratch.url.appending(path: "it's a helper").path
        try FileManager.default.copyItem(atPath: "/usr/bin/true", toPath: tool)
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/sh")
        process.arguments = ["-c", AdministratorInstaller.signatureCheck(tool: tool, team: "ABCDE12345")
            + " && echo installed"]
        let out = Pipe(), err = Pipe()
        process.standardOutput = out
        process.standardError = err
        try await runToExit(process)
        #expect(process.terminationStatus == 1)
        #expect(String(decoding: out.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self).isEmpty)
        #expect(String(decoding: err.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
            .contains("not signed by the team that signed SWE"))
        #expect(!FileManager.default.fileExists(atPath: tool))
    }

    @Test func theHelperRequirementCompiles() throws {
        var requirement: SecRequirement?
        let text = String(AdministratorInstaller.helperRequirement(team: "ABCDE12345").dropFirst())
        #expect(SecRequirementCreateWithString(text as CFString, [], &requirement) == errSecSuccess)
        #expect(requirement != nil)
    }

    @Test func onlyRealTeamIdentifiersReachTheScript() async {
        #expect(AdministratorInstaller.isTeamIdentifier("ABCDE12345"))
        for odd in ["", "abcde12345", "ABCDE1234", "ABCDE123456", "ABCDE\"1234", "ABCDE 1234"] {
            #expect(!AdministratorInstaller.isTeamIdentifier(odd), "\(odd)")
        }
        // Refused before any prompt: neither the hash nor osascript runs.
        let odd = AdministratorInstaller(owner: { .team("AB'; rm -rf", identifier: "com.cwenlong.swe") },
                                         runScript: Self.noScript)
        await #expect(throws: FanHelperInstallError.self) {
            try await odd.install(executable: "/nonexistent", control: URL(fileURLWithPath: "/tmp/c.json"))
        }
        let unreadable = AdministratorInstaller(owner: { throw FanHelperInstallError.failed("no signature") },
                                                runScript: Self.noScript)
        await #expect(throws: FanHelperInstallError.failed("no signature")) {
            try await unreadable.install(executable: "/nonexistent", control: URL(fileURLWithPath: "/tmp/c.json"))
        }
        let oddHash = AdministratorInstaller(owner: { .cdhash("not-a-hash") }, runScript: Self.noScript)
        await #expect(throws: FanHelperInstallError.self) {
            try await oddHash.install(executable: "/nonexistent", control: URL(fileURLWithPath: "/tmp/c.json"))
        }
    }

    // MARK: - The helper's entry

    /// The root helper's start-up checks, in order: its flag, a control
    /// path, a valid owner pin, then root. Each refusal exits 2 before
    /// anything opens the SMC.
    @Test func theHelpersEntryRefusesBadArgumentsAndNonRoot() throws {
        let control = "/Users/me/.swe/fan-control.json"
        let hash = String(repeating: "ab", count: 20)
        let good = ["SWEFanHelper", FanHelper.flag, "--control", control, "--owner-cdhash", hash]
        if case .notHelper = FanHelper.start(["SWE"], uid: 0) {} else { Issue.record("no flag still started") }
        if case .notHelper = FanHelper.start(["SWE", "--control", control], uid: 0) {} else {
            Issue.record("a control path without the flag started the helper")
        }
        if case .survey = FanHelper.start(["SWE", FanHelper.surveyFlag], uid: 501) {} else {
            Issue.record("the survey needs no root")
        }
        func refusal(_ arguments: [String], uid: uid_t) -> String?? {
            if case .refuse(let why) = FanHelper.start(arguments, uid: uid) { return .some(why) }
            return nil
        }
        // No control path, or a relative one: refused without a word.
        #expect(refusal(["SWEFanHelper", FanHelper.flag, "--owner-cdhash", hash], uid: 0) == .some(nil))
        #expect(refusal(["SWEFanHelper", FanHelper.flag, "--control", "fan.json", "--owner-cdhash", hash], uid: 0)
                    == .some(nil))
        // No pin, a malformed one, or two: nobody's requests are taken.
        let noOwner = "no valid owner signature in the arguments"
        #expect(refusal(Array(good.prefix(4)), uid: 0) == .some(noOwner))
        #expect(refusal(Array(good.prefix(5)) + ["XYZ"], uid: 0) == .some(noOwner))
        #expect(refusal(good + ["--owner-cdhash", hash], uid: 0) == .some(noOwner))
        // The pin is checked before root, so a bad job says what is wrong.
        #expect(refusal(Array(good.prefix(4)), uid: 501) == .some(noOwner))
        #expect(refusal(good, uid: 501) == .some("needs root"))
        guard case .serve(let url, let pin, _) = FanHelper.start(good, uid: 0) else {
            Issue.record("good arguments as root didn't serve")
            return
        }
        #expect(url.path == control)
        #expect(pin == .cdhash(hash))
    }

    /// What `serve` saw: reports, log lines, beats and the stop wiring.
    final class HelperRun: @unchecked Sendable {
        var reports: [FanHelperStatus.State] = []
        var log: [String] = []
        var beats = 0
        var stop: (@Sendable () -> Void)?
        var undone = 0
        var passes = 0
    }

    private func runtime(_ run: HelperRun, smc: (any SMCAccess)?,
                         stopAfter passes: Int = 2, during: @escaping () -> Void = {}) -> FanHelper.Runtime {
        FanHelper.Runtime(
            openSMC: { smc },
            report: { run.reports.append($0.state) },
            onStop: { stop in
                run.stop = stop
                return { run.undone += 1 }
            },
            underPressure: { false },
            sleep: { seconds in
                guard seconds >= 1 else { return }
                run.passes += 1
                during()
                // The job's SIGTERM, as the signal source would deliver it.
                if run.passes == passes { run.stop?() }
            },
            beat: { run.beats += 1 },
            log: { run.log.append($0) })
    }

    /// No SMC: the helper says so in its status file and exits 1 for
    /// launchd to retry, without wiring a stop it would never use.
    @Test func theHelperReportsAnSMCItCannotOpenAndExits() {
        let run = HelperRun()
        let status = FanHelper.serve(control: scratch.path("control.json"), ownerAlive: { _ in true },
                                     runtime: runtime(run, smc: nil))
        #expect(status == 1)
        #expect(run.reports == [.error])
        #expect(run.log == ["couldn't open the AppleSMC"])
        #expect(run.stop == nil)
    }

    /// The daemon end to end on a fake SMC: ready, then the control
    /// file's request held with both fans manual, then the stop signal
    /// hands the fans back and serve returns 0 with the signal undone.
    @Test func theHelperHoldsTheControlFilesRequestUntilStopped() throws {
        let control = scratch.path("control.json")
        try FanControlFile(level: 0.5, owner: 77, requested: Date(timeIntervalSince1970: 1)).write(to: control)
        let smc = FakeSMC.twoFans(gate: false)
        let run = HelperRun()
        var manualWhileHeld = false
        let status = FanHelper.serve(
            control: control, ownerAlive: { $0 == 77 },
            runtime: runtime(run, smc: smc) {
                if run.passes == 1 { manualWhileHeld = smc.number("F0Md") == 1 && smc.number("F1Md") == 1 }
            })
        #expect(status == 0)
        #expect(run.reports == [.ready, .held])
        #expect(manualWhileHeld)
        // Stopped: both fans automatic again.
        #expect(smc.number("F0Md") == 0 && smc.number("F1Md") == 0)
        #expect(run.passes == 2)
        #expect(run.undone == 1)
        #expect(run.log.last == "stopped")
        // The watchdog's beats: start-up's three, then one a pass.
        #expect(run.beats >= 5)
    }

    /// A request whose owner isn't a running, pinned SWE is refused with
    /// the reason, and the fans are never taken.
    @Test func theHelperIgnoresARequestFromAnUnpinnedOwner() throws {
        let control = scratch.path("control.json")
        try FanControlFile(level: 1, owner: 4242, requested: Date(timeIntervalSince1970: 1)).write(to: control)
        let smc = FakeSMC.twoFans(gate: false)
        var modeWrites = 0
        smc.onWrite = { key, _ in if key.hasSuffix("Md") { modeWrites += 1 } }
        let run = HelperRun()
        let status = FanHelper.serve(control: control, ownerAlive: { _ in false },
                                     runtime: runtime(run, smc: smc))
        #expect(status == 0)
        #expect(run.reports == [.ready, .refused])
        #expect(modeWrites == 0)
    }

    /// The installed job is read only when its executable is there and
    /// its plist is a dictionary: a half-finished install or a damaged
    /// plist reads as no job, never as a crash or someone else's args.
    @Test func aJobsArgumentsNeedItsExecutableAndAReadablePlist() throws {
        let tool = scratch.path("helper"), plist = scratch.path("job.plist")
        let arguments = FanHelper.programArguments(control: scratch.path("control.json"),
                                                   owner: .cdhash(String(repeating: "ab", count: 20)))
        try PropertyListSerialization.data(fromPropertyList: ["ProgramArguments": arguments], format: .xml, options: 0)
            .write(to: plist)
        // No executable yet.
        #expect(FanControl.jobArguments(executable: tool.path, plist: plist.path) == nil)
        try Data().write(to: tool)
        // There but not executable.
        #expect(FanControl.jobArguments(executable: tool.path, plist: plist.path) == nil)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: tool.path)
        #expect(FanControl.jobArguments(executable: tool.path, plist: plist.path) == arguments)
        // A missing plist, one that isn't a plist, and one that is an array.
        #expect(FanControl.jobArguments(executable: tool.path, plist: scratch.path("none.plist").path) == nil)
        try Data("not a plist".utf8).write(to: plist)
        #expect(FanControl.jobArguments(executable: tool.path, plist: plist.path) == nil)
        try PropertyListSerialization.data(fromPropertyList: arguments, format: .xml, options: 0).write(to: plist)
        #expect(FanControl.jobArguments(executable: tool.path, plist: plist.path) == nil)
    }

    /// A script runner for installers that must never reach osascript.
    static let noScript: @Sendable (String) async -> (status: Int32, stderr: String) = { _ in
        Issue.record("osascript ran")
        return (0, "")
    }

    /// The scripts an installer ran, answering each with `reply`.
    final class Scripts: @unchecked Sendable {
        private let lock = NSLock()
        private var ran: [String] = []
        let reply: (status: Int32, stderr: String)
        init(_ status: Int32, _ stderr: String) { reply = (status, stderr) }
        var all: [String] { lock.withLock { ran } }
        func run(_ script: String) -> (status: Int32, stderr: String) {
            lock.withLock { ran.append(script) }
            return reply
        }
    }

    /// Uninstall runs one administrator script: the removal command
    /// under its own prompt. osascript's answer maps to the error the
    /// popover shows: -128 anywhere in stderr is the user's Cancel, any
    /// other failure says what osascript said, or a plain line when it
    /// said nothing.
    @Test func uninstallRunsOneScriptAndMapsItsAnswer() async throws {
        let ok = Scripts(0, "")
        try await AdministratorInstaller(runScript: { ok.run($0) }).uninstall()
        #expect(ok.all == [AdministratorInstaller.script(
            AdministratorInstaller.uninstallCommand, prompt: "SWE wants to remove its fan helper.")])

        let cases: [(Int32, String, FanHelperInstallError)] = [
            (1, "0:91: execution error: User canceled. (-128)\n", .cancelled),
            // A cancel wins even on a zero status.
            (0, "(-128)", .cancelled),
            (1, "  0:52: execution error: launchctl: no such job (1)\n", .failed("0:52: execution error: launchctl: no such job (1)")),
            (1, " \n", .failed("The fan helper didn't install.")),
            (-1, "The file “osascript” doesn’t exist.", .failed("The file “osascript” doesn’t exist.")),
        ]
        for (status, stderr, expected) in cases {
            let scripts = Scripts(status, stderr)
            await #expect(throws: expected, "\(status) \(stderr)") {
                try await AdministratorInstaller(runScript: { scripts.run($0) }).uninstall()
            }
            #expect(scripts.all.count == 1)
        }
    }

    /// The real runner, on scripts that need no privileges: a clean exit
    /// answers 0, and an AppleScript error answers osascript's failing
    /// status with the error's text on stderr, which is what `run` maps.
    @Test func theOsascriptRunnerAnswersStatusAndStderr() async {
        let fine = await AdministratorInstaller.osascript("return 1")
        #expect(fine.status == 0)
        #expect(fine.stderr.isEmpty)
        let failed = await AdministratorInstaller.osascript("error \"no such job\" number 7")
        #expect(failed.status != 0)
        #expect(failed.stderr.contains("no such job"))
        #expect(failed.stderr.contains("(7)"))
        let cancelled = await AdministratorInstaller.osascript("error number -128")
        #expect(cancelled.stderr.contains("-128"))
    }

    /// The test runner's own signature reads without error, as a team or
    /// a cdhash, and makes a pin the helper accepts.
    @Test func theRunningCodesPinReads() throws {
        let pin = try FanOwnerPin.current()
        #expect(pin.isValid)
        #expect(FanOwnerPin.parse(["helper"] + pin.arguments) == pin)
    }

    /// The job names the helper's version, so an install from before it
    /// (a copy of SWE's executable, which crashed in dyld) or an older
    /// helper reads as outdated and gets reinstalled.
    @Test func theJobCarriesTheHelperVersion() throws {
        let control = URL(fileURLWithPath: "/Users/me/.swe/fan/control.json")
        let job = try PropertyListSerialization.propertyList(from: FanHelper.plist(control: control, owner: Self.adHoc),
                                                             format: nil) as? [String: Any]
        let arguments = try #require(job?["ProgramArguments"] as? [String])
        #expect(arguments == [FanHelper.installedExecutable, FanHelper.flag, "--control", control.path,
                              "--owner-cdhash", Self.adHocHash, "--helper-version", String(FanHelper.version)])
        #expect(FanHelper.version(in: arguments) == FanHelper.version)
        #expect(FanOwnerPin.parse(arguments) == Self.adHoc)
        #expect(arguments != [FanHelper.installedExecutable, FanHelper.flag, "--control", control.path])
        // The helper itself still finds its control file past the version.
        #expect(FanHelper.parse(arguments) == control)
    }

    @Test func theBundledHelperIsPreferredOverTheApp() throws {
        let dir = scratch.url.appending(path: "helper-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let app = dir.appending(path: "SWE").path
        #expect(FanHelper.bundledExecutable(appExecutable: app) == app)
        let helper = dir.appending(path: FanHelper.bundledName).path
        FileManager.default.createFile(atPath: helper, contents: Data(), attributes: [.posixPermissions: 0o755])
        #expect(FanHelper.bundledExecutable(appExecutable: app) == helper)
    }

    @Test func theOneLineJobIsTheSameJob() throws {
        let control = URL(fileURLWithPath: "/Users/me/.swe/fan/control.json")
        let line = FanHelper.plistLine(control: control, owner: .team("ABCDE12345", identifier: "com.cwenlong.swe"))
        #expect(!line.contains("\n"))
        let parsed = try PropertyListSerialization.propertyList(from: Data(line.utf8), format: nil) as? NSDictionary
        let original = try PropertyListSerialization.propertyList(from: FanHelper.plist(control: control,
                                                                                        owner: .team("ABCDE12345", identifier: "com.cwenlong.swe")),
                                                                  format: nil) as? NSDictionary
        #expect(parsed == original)
    }

    @Test func theHashIsSHA256OfTheFile() throws {
        let url = scratch.url.appending(path: "hash-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: url) }
        try Data("abc".utf8).write(to: url)
        #expect(AdministratorInstaller.sha256(of: url.path)
            == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        #expect(AdministratorInstaller.sha256(of: "/nonexistent/\(UUID().uuidString)") == nil)
    }

    // MARK: The owner's signature

    static let adHocHash = "0123456789abcdef0123456789abcdef01234567"
    static let adHoc = FanOwnerPin.cdhash(adHocHash)

    @Test func theOwnerPinParsesOnlyOneRealValue() throws {
        let signed = ["h", "--owner-team", "ABCDE12345", "--owner-identifier", "com.cwenlong.swe"]
        #expect(FanOwnerPin.parse(signed) == .team("ABCDE12345", identifier: "com.cwenlong.swe"))
        #expect(FanOwnerPin.parse(["h", "--fan-daemon", "--owner-cdhash", Self.adHocHash]) == Self.adHoc)
        // None, two, a missing value, or anything that isn't a team id or
        // a lowercase 40-digit cdhash: the helper won't start.
        // A team without its identifier, or an identifier that could
        // break out of the requirement's quotes, too.
        for odd in [["h"], ["h", "--owner-team"], ["h", "--owner-team", "ABCDE12345"],
                    ["h", "--owner-team", "abcde12345", "--owner-identifier", "com.cwenlong.swe"],
                    ["h", "--owner-team", #"ABCDE"1234"#, "--owner-identifier", "com.cwenlong.swe"],
                    ["h", "--owner-team", "ABCDE12345", "--owner-identifier", #"x" or anchor apple"#],
                    ["h", "--owner-team", "ABCDE12345", "--owner-identifier", ""],
                    ["h", "--owner-identifier", "com.cwenlong.swe"],
                    signed + ["--owner-identifier", "com.cwenlong.swe"], ["h", "--owner-cdhash", String(Self.adHocHash.dropLast())],
                    ["h", "--owner-cdhash", Self.adHocHash.uppercased()],
                    ["h", "--owner-cdhash", String(Self.adHocHash.dropLast()) + "g"],
                    signed + ["--owner-cdhash", Self.adHocHash]] {
            #expect(FanOwnerPin.parse(odd) == nil, "\(odd)")
        }
        #expect(FanOwnerPin.team("ABCDE12345", identifier: "com.cwenlong.swe").team == "ABCDE12345")
        #expect(Self.adHoc.team == nil)
        #expect(FanOwnerPin.team("ABCDE12345", identifier: "com.cwenlong.swe").requirement
            == #"identifier "com.cwenlong.swe" and anchor apple generic and certificate leaf[subject.OU] = "ABCDE12345""#)
        #expect(Self.adHoc.requirement == #"cdhash H"\#(Self.adHocHash)""#)
        // Both requirements compile.
        for pin in [FanOwnerPin.team("ABCDE12345", identifier: "com.cwenlong.swe"), Self.adHoc] {
            var requirement: SecRequirement?
            #expect(SecRequirementCreateWithString(pin.requirement as CFString, [], &requirement) == errSecSuccess)
            #expect(FanOwnerCheck(pin: pin) != nil)
        }
        #expect(FanOwnerCheck(pin: .team("nope", identifier: "com.cwenlong.swe")) == nil)
    }

    /// A job from before the pin (or a hand-written one) starts nothing:
    /// the helper refuses before it even checks for root.
    @Test func theHelperWontStartWithoutAnOwnerPin() {
        #expect(FanHelper.runIfRequested(["helper", "--fan-daemon", "--control", "/tmp/c.json",
                                          "--helper-version", "3"]) == 2)
    }

    /// The real check, run on real processes: the test runner meets its
    /// own pin; a program named SWE meets only a pin for its own code,
    /// never the app's, whatever it is called; a gone pid meets nothing.
    ///
    /// The impostor is a copy of /bin/sleep signed ad hoc. A bare copy
    /// keeps Apple's platform signature, and macOS kills a platform
    /// binary run from outside the system volume a few milliseconds
    /// after it starts: the checks below then raced that kill and failed
    /// now and then on a gone process (1 run in 3 under load).
    @Test func theOwnerIsCheckedBySignatureNotName() async throws {
        let check = try #require(FanOwnerCheck(pin: try FanOwnerPin.current()))
        #expect(check.isOwner(getpid()))
        #expect(!check.isOwner(Int32.max))
        #expect(!check.isOwner(0))
        #expect(!check.isOwner(1))

        let impostor = scratch.url.appending(path: "SWE")
        try FileManager.default.copyItem(atPath: "/bin/sleep", toPath: impostor.path)
        let sign = Process()
        sign.executableURL = URL(fileURLWithPath: "/usr/bin/codesign")
        sign.arguments = ["--force", "--sign", "-", impostor.path]
        sign.standardError = FileHandle.nullDevice
        try await runToExit(sign)
        try #require(sign.terminationStatus == 0, "couldn't sign the impostor ad hoc")
        let process = Process()
        process.executableURL = impostor
        process.arguments = ["30"]
        // The impostor has to be the signed copy itself, so it cannot be
        // run through /bin/sh like the stubs elsewhere; its exit is
        // bounded instead, and killed if it never comes. A check that
        // throws before the end still stops it.
        let child = try ChildProcess(starting: process)
        defer { process.terminate() }
        let pid = process.processIdentifier
        #expect(!check.isOwner(pid))
        #expect(!(FanOwnerCheck(pin: .team("ABCDE12345", identifier: "com.cwenlong.swe"))?.isOwner(pid) ?? true))
        // Its own code's hash does pass: the check reads the running code.
        let token = try #require(FanOwnerCheck.auditToken(of: pid))
        var code: SecCode?, staticCode: SecStaticCode?, info: CFDictionary?
        let audit = withUnsafeBytes(of: token) { Data($0) }
        #expect(SecCodeCopyGuestWithAttributes(nil, [kSecGuestAttributeAudit: audit] as CFDictionary, [], &code) == errSecSuccess)
        #expect(SecCodeCopyStaticCode(try #require(code), [], &staticCode) == errSecSuccess)
        #expect(SecCodeCopySigningInformation(try #require(staticCode),
                                              SecCSFlags(rawValue: kSecCSSigningInformation), &info) == errSecSuccess)
        let unique = try #require((info as? [String: Any])?[kSecCodeInfoUnique as String] as? Data)
        let own = FanOwnerPin.cdhash(unique.map { String(format: "%02x", $0) }.joined())
        #expect(FanOwnerCheck(pin: own)?.isOwner(pid) == true)
        // Every answer above was about a live process, not a gone one.
        #expect(process.isRunning)
        process.terminate()
        await child.waitForExit()
    }

    /// The signature is checked once per process, not on every call: the
    /// answer is kept for the pid's version, and a reused pid (a new
    /// version) is checked afresh.
    @Test func theOwnersAnswerIsKeptForThatProcessOnly() throws {
        let version = Box<UInt32>(1)
        let verdict = Box(true)
        let calls = Box(0)
        let check = try #require(FanOwnerCheck(
            pin: .team("ABCDE12345", identifier: "com.cwenlong.swe"),
            token: { pid in audit_token_t(val: (0, 0, 0, 0, 0, UInt32(bitPattern: pid), 0, version.value)) },
            satisfies: { _, _ in calls.value += 1; return verdict.value }))
        #expect(check.isOwner(500))
        #expect(check.isOwner(500))
        #expect(calls.value == 1)
        // Same pid, another process: a fresh check, which now fails.
        version.value = 2
        verdict.value = false
        #expect(!check.isOwner(500))
        #expect(!check.isOwner(500))
        #expect(calls.value == 2)
        // A token for another pid than asked never passes.
        let mismatched = try #require(FanOwnerCheck(
            pin: .team("ABCDE12345", identifier: "com.cwenlong.swe"),
            token: { _ in audit_token_t(val: (0, 0, 0, 0, 0, 42, 0, 1)) },
            satisfies: { _, _ in true }))
        #expect(!mismatched.isOwner(500))
    }

    // MARK: Telling an old helper from a new one

    @Test func theInstalledJobSaysCurrentOutdatedOrMissing() {
        let control = URL(fileURLWithPath: "/Users/me/.swe/fan/control.json")
        let pin = FanOwnerPin.team("ABCDE12345", identifier: "com.cwenlong.swe")
        let current = FanHelper.programArguments(control: control, owner: pin)
        func state(_ arguments: [String], owner: FanOwnerPin? = pin, loaded: Bool = true) -> FanHelperState {
            FanControl.helperState(arguments: arguments, control: control, owner: owner) { loaded }
        }
        #expect(state(current) == .current)
        #expect(state(current, loaded: false) == .missing)
        // Version 3 (no pin) and the first helpers (no version at all).
        let v3 = [FanHelper.installedExecutable, FanHelper.flag, "--control", control.path, "--helper-version", "3"]
        #expect(state(v3) == .outdated(version: 3))
        #expect(state(v3, loaded: false) == .outdated(version: 3))
        #expect(state(Array(v3.dropLast(2))) == .outdated(version: 0))
        // Another account's job, another build's pin, or an unreadable
        // pin of our own: not ours to offer an update for.
        #expect(state(FanHelper.programArguments(control: URL(fileURLWithPath: "/Users/other/.swe/fan/control.json"),
                                                 owner: pin)) == .missing)
        #expect(state(FanHelper.programArguments(control: control, owner: Self.adHoc)) == .missing)
        #expect(state(current, owner: nil) == .missing)
        #expect(state(Array(current.dropLast()) + ["x"]) == .missing)
        #expect(state(["/bin/sh", "-c", "true"]) == .missing)
    }

    @Test func aQuiverHelperCountsAsOutdatedNotMissing() throws {
        // Quiver.app's daemon still names the old control root; the
        // data move leaves ~/.quiver a link to ~/.swe, so resolving the
        // two paths lands on this control file. It is replaced on the
        // next install, so it is outdated — not absent, which would
        // leave a saved fan rule silently dead.
        let dir = try TempDir()
        defer { withExtendedLifetime(dir) {} }
        let swe = dir.url.appending(path: ".swe")
        try FileManager.default.createDirectory(at: swe, withIntermediateDirectories: true)
        try FileManager.default.createSymbolicLink(
            at: dir.url.appending(path: ".quiver"), withDestinationURL: swe)
        let control = swe.appending(path: "fan/control.json")
        let pin = FanOwnerPin.team("ABCDE12345", identifier: "com.cwenlong.swe")
        func state(installed: [String]? = nil, legacy: [String]?) -> FanHelperState {
            FanControl.helperState(installed: installed, legacy: legacy,
                                   control: control, owner: pin) { true }
        }
        let quiverJob = [FanHelper.legacyInstalledExecutable, FanHelper.flag,
                         "--control", dir.url.appending(path: ".quiver/fan/control.json").path,
                         "--helper-version", "3"]
        #expect(state(legacy: quiverJob) == .outdated(version: 3))
        // No --control parse or a foreign account's file is not ours.
        #expect(state(legacy: Array(quiverJob.dropLast(2))) == .outdated(version: 0))
        #expect(state(legacy: [FanHelper.legacyInstalledExecutable, FanHelper.flag,
                               "--control", "/Users/other/.swe/fan/control.json",
                               "--helper-version", "3"]) == .missing)
        #expect(state(legacy: ["/bin/sh", "-c", "true"]) == .missing)
        #expect(state(legacy: nil) == .missing)
        // The new job, when present, decides on its own.
        let current = FanHelper.programArguments(control: control, owner: pin)
        #expect(state(installed: current, legacy: quiverJob) == .current)
    }

    /// The helper says its version in the status file; one from before it
    /// did reads as nil.
    @Test func theStatusCarriesTheHelpersVersion() throws {
        let status = FanHelperStatus(state: .ready)
        let decoded = try JSONDecoder().decode(FanHelperStatus.self, from: JSONEncoder().encode(status))
        #expect(decoded.version == FanHelper.version)
        let old = try JSONDecoder().decode(FanHelperStatus.self, from: Data(#"{"state":"ready","at":1}"#.utf8))
        #expect(old.version == nil)
    }

    // MARK: The launch offer

    @MainActor @Test func anOutdatedHelperIsOfferedOnceAndNeverSteered() async {
        let smc = FakeSMC.twoFans()
        let installer = FakeInstaller()
        installer.outdated.value = true
        let (fans, _) = control(smc, installer: installer)
        #expect(await fans.shouldOfferHelperUpdate())
        #expect(fans.helperOutdated)
        #expect(!fans.helperInstalled)
        // Not now: no prompt, nothing installed, and no offer next launch.
        fans.declineHelperUpdate()
        #expect(installer.installs == 0)
        #expect(!(await fans.shouldOfferHelperUpdate()))
        let defaults = TestDefaults("fan")
        defaults.set(FanHelper.version, forKey: FanControl.updateDeclinedKey)
        let later = FanControl(control: scratch.url.appending(path: "later/control.json"), reader: { FanKeys.read(smc) },
                               status: { nil }, installer: installer, inspect: { _ in installer.state },
                               executable: "/Apps/SWE", owner: 77, defaults: defaults)
        #expect(!(await later.shouldOfferHelperUpdate()))
        #expect(later.helperOutdated)
        // The rule never steers the old helper: the fans stay with macOS.
        later.thresholdEnabled = true
        await later.checkTemperature(99)
        #expect(!later.heldByThreshold)
        #expect(later.phase == .automatic)
        #expect(installer.installs == 0)
        // An offer turned down for an older version is made again.
        defaults.set(FanHelper.version - 1, forKey: FanControl.updateDeclinedKey)
        #expect(await later.shouldOfferHelperUpdate())
    }

    /// The offer reads the job itself: a refresh that already took the
    /// one-shot install check (another launch-time refresh) can't hide it.
    @MainActor @Test func theOfferLooksEvenWhenARefreshAlreadyChecked() async {
        let smc = FakeSMC.twoFans()
        let installer = FakeInstaller()
        let (fans, _) = control(smc, installer: installer)
        await fans.refresh()
        #expect(!fans.helperOutdated)
        installer.outdated.value = true
        #expect(await fans.shouldOfferHelperUpdate())
        #expect(fans.helperOutdated)
    }

    @MainActor @Test func updatingReplacesTheHelperWithOnePrompt() async {
        let smc = FakeSMC.twoFans()
        let installer = FakeInstaller()
        installer.outdated.value = true
        let (fans, url) = control(smc, installer: installer)
        #expect(await fans.shouldOfferHelperUpdate())
        #expect(await fans.updateHelper() == nil)
        #expect(installer.installs == 1)
        #expect(installer.lastControl == url)
        #expect(fans.helperInstalled)
        #expect(!fans.helperOutdated)
        // Nothing is held: the user sets a speed as usual, with no prompt.
        #expect(fans.phase == .automatic)
        #expect(FanControlFile.request(at: url) == nil)
        await fans.refresh()
        #expect(!fans.helperOutdated)
        #expect(!(await fans.shouldOfferHelperUpdate()))
        await fans.applyManual()
        #expect(installer.installs == 1)
    }

    @MainActor @Test func aCancelledOrFailedUpdateLeavesTheOldHelperUnused() async {
        let smc = FakeSMC.twoFans()
        let installer = FakeInstaller()
        installer.outdated.value = true
        let (fans, _) = control(smc, installer: installer)
        await fans.refresh()
        installer.error = .cancelled
        #expect(await fans.updateHelper() == nil)
        #expect(fans.helperOutdated && !fans.helperInstalled)
        #expect(fans.phase == .automatic)
        installer.error = .failed("The fan helper is not signed by the team that signed SWE.")
        #expect(await fans.updateHelper() == "The fan helper is not signed by the team that signed SWE.")
        #expect(fans.helperOutdated && !fans.helperInstalled)
        #expect(fans.phase == .failed("The fan helper is not signed by the team that signed SWE."))
        // Set speed is the same install, so it is an update too.
        installer.error = nil
        await fans.applyManual()
        #expect(installer.installs == 3)
        #expect(fans.helperInstalled && !fans.helperOutdated)
    }


    @Test func theControlFileRoundTripsAndAnythingOddMeansAutomatic() throws {
        let folder = scratch.url.appending(path: "fan-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: folder) }
        let url = folder.appending(path: "fan/control.json")
        #expect(FanControlFile.request(at: url) == nil)
        try ask(0.25).write(to: url)
        #expect(FanControlFile.request(at: url) == ask(0.25))
        try ask(4).write(to: url)
        #expect(FanControlFile.request(at: url)?.level == 1)
        try FanControlFile(level: 0.5).write(to: url)
        #expect(FanControlFile.request(at: url) == nil)
        try FanControlFile(level: nil).write(to: url)
        #expect(FanControlFile.request(at: url) == nil)
        try Data("not json".utf8).write(to: url)
        #expect(FanControlFile.request(at: url) == nil)
    }

    @Test func theRootHelperNeverReadsBigFilesLinksOrPipes() throws {
        let folder = scratch.url.appending(path: "fan-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let good = folder.appending(path: "good.json")
        try ask(0.5).write(to: good)
        // Oversized, even when it starts with a valid request.
        let big = folder.appending(path: "big.json")
        var data = try JSONEncoder().encode(ask(0.5))
        data.append(Data(repeating: 0x20, count: 5000))
        try data.write(to: big)
        #expect(FanControlFile.request(at: big) == nil)
        // A link to a good file is still refused.
        let link = folder.appending(path: "link.json")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: good)
        #expect(FanControlFile.request(at: link) == nil)
        // A FIFO answers at once instead of blocking the loop.
        let fifo = folder.appending(path: "fifo.json")
        #expect(mkfifo(fifo.path, 0o600) == 0)
        #expect(FanControlFile.request(at: fifo) == nil)
        #expect(FanControlFile.request(at: good) == ask(0.5))
    }

    // MARK: The popover's model

    @MainActor
    private func control(_ smc: FakeSMC, installer: FakeInstaller,
                         clock: Box<Date> = Box(Date(timeIntervalSince1970: 1_800_000_000)),
                         status: Box<FanHelperStatus?> = Box(nil),
                         sleeper: Sleeper = Sleeper()) -> (FanControl, URL) {
        let url = scratch.url
            .appending(path: "fan-\(UUID().uuidString)/control.json")
        let control = FanControl(control: url, reader: { FanKeys.read(smc) }, status: { status.value },
                                 installer: installer,
                                 inspect: { _ in installer.checks += 1; return installer.state },
                                 executable: "/Apps/SWE", owner: 77, clock: { clock.value },
                                 sleep: { await sleeper.sleep($0) },
                                 defaults: TestDefaults("fan"))
        return (control, url)
    }

    @MainActor @Test func theFirstSpeedInstallsTheHelperThenOnlyTheFileChanges() async {
        let smc = FakeSMC.twoFans()
        let installer = FakeInstaller()
        let (fans, url) = control(smc, installer: installer)
        #expect(!fans.helperInstalled)
        fans.level = 0.75
        await fans.applyManual()
        #expect(installer.installs == 1)
        #expect(installer.lastControl == url)
        #expect(fans.helperInstalled)
        #expect(FanControlFile.request(at: url)?.level == 0.75)
        #expect(FanControlFile.request(at: url)?.owner == 77)
        #expect(fans.phase == .starting)
        // The helper took the fans.
        smc.set("F0Md", 1); smc.set("F1Md", 1)
        await fans.refresh()
        #expect(fans.phase == .manual)
        // A new level reaches the helper through the file alone.
        fans.level = 1
        await fans.applyManual()
        #expect(installer.installs == 1)
        #expect(FanControlFile.request(at: url)?.level == 1)
        #expect(fans.phase == .manual)
        fans.restoreAutomatic()
        #expect(FanControlFile.request(at: url) == nil)
        #expect(fans.phase == .automatic)
    }

    @MainActor @Test func cancellingThePasswordPromptLeavesTheFansAutomatic() async {
        let installer = FakeInstaller()
        installer.error = .cancelled
        let (fans, url) = control(FakeSMC.twoFans(), installer: installer)
        await fans.applyManual()
        #expect(fans.phase == .automatic)
        #expect(!fans.helperInstalled)
        #expect(FanControlFile.request(at: url) == nil)
    }

    @MainActor @Test func aFailedInstallSaysWhy() async {
        let installer = FakeInstaller()
        installer.error = .failed("launchctl said no")
        let (fans, url) = control(FakeSMC.twoFans(), installer: installer)
        await fans.applyManual()
        #expect(fans.phase == .failed("launchctl said no"))
        #expect(FanControlFile.request(at: url) == nil)
    }

    @MainActor @Test func startingTooLongGivesUp() async {
        let clock = Box(Date(timeIntervalSince1970: 1_800_000_000))
        let installer = FakeInstaller()
        installer.installed.value = true
        let (fans, url) = control(FakeSMC.twoFans(), installer: installer, clock: clock)
        await fans.refresh()
        await fans.applyManual()
        #expect(installer.installs == 0)
        #expect(fans.phase == .starting)
        clock.value = clock.value.addingTimeInterval(FanControl.startTimeout + 1)
        await fans.refresh()
        #expect(fans.phase == .failed(FanControl.notTaken))
        #expect(FanControlFile.request(at: url) == nil)
        // The helper may be gone or half installed: look again, so the
        // next Set speed can reinstall it.
        let checks = installer.checks
        await fans.refresh()
        #expect(installer.checks == checks + 1)
        await fans.refresh()
        #expect(installer.checks == checks + 1)
    }

    @MainActor @Test(skippedAsRoot) func automaticIsOnlyClaimedOnceTheRequestIsGone() async throws {
        let installer = FakeInstaller()
        installer.installed.value = true
        let (fans, url) = control(FakeSMC.twoFans(), installer: installer)
        await fans.refresh()
        await fans.applyManual()
        let folder = url.deletingLastPathComponent()
        try FileManager.default.setAttributes([.posixPermissions: 0o500], ofItemAtPath: folder.path)
        defer { try? FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: folder.path) }
        fans.restoreAutomatic()
        guard case .failed(let message) = fans.phase else {
            Issue.record("expected a failure, got \(fans.phase)")
            return
        }
        #expect(message.contains("stay manual"))
        #expect(FanControlFile.request(at: url) != nil)
    }

    @MainActor @Test func fansTakenBackWhileHoldingWaitForTheHelperAgain() async {
        let smc = FakeSMC.twoFans()
        let installer = FakeInstaller()
        installer.installed.value = true
        let (fans, url) = control(smc, installer: installer)
        await fans.refresh()
        await fans.applyManual()
        smc.set("F0Md", 1); smc.set("F1Md", 1)
        await fans.refresh()
        #expect(fans.phase == .manual)
        // After sleep macOS has them; the request stays for the helper.
        smc.set("F0Md", 3); smc.set("F1Md", 3)
        await fans.refresh()
        #expect(fans.phase == .starting)
        #expect(fans.isHolding)
        #expect(FanControlFile.request(at: url) != nil)
    }

    @MainActor @Test func aRefreshDuringRemovalDoesNotLeaveItLookingInstalled() async {
        let installer = FakeInstaller()
        installer.installed.value = true
        let (fans, _) = control(FakeSMC.twoFans(), installer: installer)
        await fans.refresh()
        #expect(fans.helperInstalled)
        // The popover's own refresh lands while the password prompt is up.
        installer.duringUninstall = { await fans.refresh() }
        await fans.removeHelper()
        #expect(!fans.helperInstalled)
    }

    // MARK: The threshold

    @MainActor private func installedControl(_ smc: FakeSMC = FakeSMC.twoFans()) async -> (FanControl, URL, FakeInstaller) {
        let installer = FakeInstaller()
        installer.installed.value = true
        let (fans, url) = control(smc, installer: installer)
        await fans.refresh()
        return (fans, url, installer)
    }

    @MainActor @Test func theThresholdHoldsAboveTheLineAndLetsGoFiveBelow() async {
        let (fans, url, _) = await installedControl()
        fans.thresholdEnabled = true
        fans.threshold = 80
        fans.level = 0.6
        await fans.checkTemperature(79)
        #expect(FanControlFile.request(at: url) == nil)
        await fans.checkTemperature(80)
        #expect(fans.heldByThreshold)
        #expect(FanControlFile.request(at: url)?.level == 0.6)
        // Hovering just under the line keeps the fans up.
        await fans.checkTemperature(77)
        #expect(fans.heldByThreshold)
        await fans.checkTemperature(75)
        #expect(!fans.heldByThreshold)
        #expect(fans.phase == .automatic)
        #expect(FanControlFile.request(at: url) == nil)
    }

    @MainActor @Test func aSpeedTheUserSetWinsOverTheThreshold() async {
        let (fans, url, _) = await installedControl()
        fans.thresholdEnabled = true
        fans.level = 1
        await fans.applyManual()
        await fans.checkTemperature(95)
        await fans.checkTemperature(40)
        // Neither the hot nor the cool reading touched the user's speed.
        #expect(!fans.heldByThreshold)
        #expect(fans.isHolding)
        #expect(FanControlFile.request(at: url)?.level == 1)
        // And taking over a threshold hold makes it the user's.
        fans.restoreAutomatic()
        await fans.checkTemperature(90)
        #expect(fans.heldByThreshold)
        await fans.applyManual()
        #expect(!fans.heldByThreshold)
        await fans.checkTemperature(40)
        #expect(fans.isHolding)
    }

    @MainActor @Test func theThresholdNeverPromptsFromTheBackground() async {
        let installer = FakeInstaller()
        let (fans, url) = control(FakeSMC.twoFans(), installer: installer)
        fans.thresholdEnabled = true
        await fans.checkTemperature(99)
        #expect(installer.installs == 0)
        #expect(FanControlFile.request(at: url) == nil)
        #expect(!fans.heldByThreshold)
    }

    @MainActor @Test func switchingTheThresholdOnInstallsTheHelperOnce() async {
        let installer = FakeInstaller()
        installer.error = .cancelled
        let (fans, url) = control(FakeSMC.twoFans(), installer: installer)
        await fans.enableThreshold()
        #expect(!fans.thresholdEnabled)
        installer.error = nil
        await fans.enableThreshold()
        #expect(fans.thresholdEnabled)
        #expect(installer.installs == 2)
        // Switching it on sets no speed by itself.
        #expect(FanControlFile.request(at: url) == nil)
    }

    /// Off picked while the install prompt is still up wins: the enable
    /// finishing afterwards must not switch the rule back on.
    @MainActor @Test func switchingTheThresholdOffDuringTheInstallPromptWins() async {
        let installer = FakeInstaller()
        let (fans, _) = control(FakeSMC.twoFans(), installer: installer)
        installer.duringInstall = { fans.thresholdEnabled = false }
        await fans.enableThreshold()
        #expect(installer.installs == 1)
        #expect(!fans.thresholdEnabled)
        // A later enable, with nothing in its way, still switches it on.
        installer.duringInstall = nil
        await fans.enableThreshold()
        #expect(fans.thresholdEnabled)
    }

    @MainActor @Test func switchingTheThresholdOffWhileHoldingHandsTheFansBack() async {
        let (fans, url, _) = await installedControl()
        fans.thresholdEnabled = true
        await fans.checkTemperature(90)
        #expect(fans.heldByThreshold)
        fans.thresholdEnabled = false
        #expect(!fans.heldByThreshold)
        #expect(fans.phase == .automatic)
        #expect(FanControlFile.request(at: url) == nil)
    }

    /// Parks the fan read on its background thread until released. The
    /// read is synchronous, so it holds on a semaphore rather than a gate;
    /// the bound only keeps a regression from hanging the suite.
    final class ReadGate: Sendable {
        private let isArmed = Box(false)
        private let parks = CountGate()
        private let release = DispatchSemaphore(value: 0)
        var armed: Bool {
            get { isArmed.value }
            set { isArmed.value = newValue }
        }
        /// Called by the reader: parks an armed read until `open()`.
        func hold() {
            guard armed else { return }
            Task { await parks.bump() }
            // A timed-out hold must not pass as a release.
            if release.wait(timeout: .now() + .seconds(Int(gateTimeout.components.seconds))) == .timedOut {
                preconditionFailure("a held fan read was never released")
            }
        }
        /// True once an armed read is parked, false after `gateTimeout`.
        func parked() async -> Bool { await parks.reach(1) }
        func open() { release.signal() }
    }

    @MainActor @Test func switchingOffMidTakeoverLeavesNothingHeld() async {
        let smc = FakeSMC.twoFans()
        let gate = ReadGate()
        let installer = FakeInstaller()
        installer.installed.value = true
        let url = scratch.url.appending(path: "fan-\(UUID().uuidString)/control.json")
        let fans = FanControl(control: url,
                              reader: { gate.hold(); return FanKeys.read(smc) },
                              status: { nil }, installer: installer, inspect: { _ in .current }, executable: "/Apps/SWE", owner: 77,
                              defaults: TestDefaults("fan"))
        await fans.refresh()
        fans.thresholdEnabled = true
        gate.armed = true
        let takeover = Task { await fans.checkTemperature(90) }
        #expect(await gate.parked(), "the fan read never started")
        // The takeover is waiting on its fan read: switch the rule off now.
        fans.thresholdEnabled = false
        gate.armed = false
        gate.open()
        await takeover.value
        #expect(!fans.heldByThreshold)
        #expect(fans.phase == .automatic)
        #expect(FanControlFile.request(at: url) == nil)
    }

    @MainActor @Test func theThresholdSpeedReachesTheFansAtOnce() async {
        let (fans, url, _) = await installedControl()
        fans.thresholdEnabled = true
        fans.level = 0.4
        await fans.checkTemperature(90)
        #expect(FanControlFile.request(at: url)?.level == 0.4)
        fans.setThresholdLevel(0.9)
        #expect(FanControlFile.request(at: url)?.level == 0.9)
        // The Manual slider waits for Update speed, even mid-hold.
        fans.level = 0.3
        #expect(FanControlFile.request(at: url)?.level == 0.9)
        await fans.applyManual()
        #expect(FanControlFile.request(at: url)?.level == 0.3)
        // And once the speed is the user's, the threshold slider does not
        // reach it either.
        fans.setThresholdLevel(0.2)
        #expect(FanControlFile.request(at: url)?.level == 0.3)
    }

    @MainActor @Test func aSuccessfulRetryLetsTheRuleAct() async {
        let installer = FakeInstaller()
        installer.error = .failed("launchctl said no")
        let (fans, url) = control(FakeSMC.twoFans(), installer: installer)
        await fans.enableThreshold()
        #expect(fans.phase == .failed("launchctl said no"))
        installer.error = nil
        await fans.enableThreshold()
        #expect(fans.phase == .automatic)
        await fans.checkTemperature(90)
        #expect(fans.heldByThreshold)
        #expect(FanControlFile.request(at: url) != nil)
    }

    @MainActor @Test func aTakeoverTheHelperNeverFinishesTimesOutInTheBackground() async {
        let clock = Box(Date(timeIntervalSince1970: 1_800_000_000))
        let installer = FakeInstaller()
        installer.installed.value = true
        let (fans, url) = control(FakeSMC.twoFans(), installer: installer, clock: clock)
        await fans.refresh()
        fans.thresholdEnabled = true
        await fans.checkTemperature(90)
        #expect(fans.phase == .starting)
        // No page, no popover: only the ticks read the fans.
        clock.value = clock.value.addingTimeInterval(FanControl.startTimeout + 1)
        await fans.checkTemperature(90)
        #expect(fans.phase == .failed(FanControl.notTaken))
        #expect(!fans.heldByThreshold)
        #expect(FanControlFile.request(at: url) == nil)
    }

    @MainActor @Test func aFailedTakeoverRetriesAfterAWait() async {
        let clock = Box(Date(timeIntervalSince1970: 1_800_000_000))
        let installer = FakeInstaller()
        installer.installed.value = true
        let (fans, url) = control(FakeSMC.twoFans(), installer: installer, clock: clock)
        await fans.refresh()
        fans.thresholdEnabled = true
        await fans.checkTemperature(90)
        #expect(fans.phase == .starting)
        clock.value = clock.value.addingTimeInterval(FanControl.startTimeout + 1)
        await fans.checkTemperature(90)
        #expect(fans.phase == .failed(FanControl.notTaken))
        #expect(FanControlFile.request(at: url) == nil)
        // Inside the backoff the failure stands.
        clock.value = clock.value.addingTimeInterval(FanControl.retryAfter - 1)
        await fans.checkTemperature(90)
        #expect(FanControlFile.request(at: url) == nil)
        #expect(fans.phase == .failed(FanControl.notTaken))
        // Past it the rule asks again instead of staying off until the
        // app happens to be poked.
        clock.value = clock.value.addingTimeInterval(2)
        await fans.checkTemperature(90)
        #expect(FanControlFile.request(at: url) != nil)
        #expect(fans.phase == .starting)
        #expect(fans.heldByThreshold)
    }

    @MainActor @Test func startingGivesUpOnItsOwnClockWhileAReadHangs() async {
        let smc = FakeSMC.twoFans()
        let gate = ReadGate()
        let sleeper = Sleeper()
        let installer = FakeInstaller()
        installer.installed.value = true
        let url = scratch.url.appending(path: "fan-\(UUID().uuidString)/control.json")
        let fans = FanControl(control: url,
                              reader: { gate.hold(); return FanKeys.read(smc) },
                              status: { nil }, installer: installer, inspect: { _ in .current },
                              executable: "/Apps/SWE", owner: 77, sleep: { await sleeper.sleep($0) },
                              defaults: TestDefaults("fan"))
        await fans.refresh()
        gate.armed = true
        let takeover = Task { await fans.applyManual() }
        #expect(await gate.parked(), "the fan read never started")
        #expect(fans.phase == .starting)
        // The read never comes back, and no refresh settles anything: the
        // deadline set when Starting began ends it anyway.
        let deadline = fans.deadline
        sleeper.fire()
        await deadline?.value
        #expect(fans.phase == .failed(FanControl.notTaken))
        #expect(FanControlFile.request(at: url) == nil)
        gate.armed = false
        gate.open()
        await takeover.value
        #expect(fans.phase == .failed(FanControl.notTaken))
    }

    @MainActor @Test func aDeadlineFromAnEarlierStartChangesNothing() async {
        let smc = FakeSMC.twoFans()
        let sleeper = Sleeper()
        let installer = FakeInstaller()
        installer.installed.value = true
        let (fans, url) = control(smc, installer: installer, sleeper: sleeper)
        await fans.refresh()
        await fans.applyManual()
        let first = fans.deadline
        #expect(first != nil)
        smc.set("F0Md", 1); smc.set("F1Md", 1)
        await fans.refresh()
        #expect(fans.phase == .manual)
        #expect(fans.deadline == nil)
        sleeper.fire()
        await first?.value
        #expect(fans.phase == .manual)
        #expect(FanControlFile.request(at: url) != nil)
    }

    @MainActor @Test func aRefusalShowsTheHelpersReasonAtOnce() async throws {
        let status = Box<FanHelperStatus?>(nil)
        let installer = FakeInstaller()
        installer.installed.value = true
        let (fans, url) = control(FakeSMC.twoFans(), installer: installer, status: status)
        await fans.refresh()
        await fans.applyManual()
        #expect(fans.phase == .starting)
        // An answer to an earlier request is not this one's.
        status.value = FanHelperStatus(state: .refused, reason: "old", request: ask(0.5, at: 5))
        await fans.refresh()
        #expect(fans.phase == .starting)
        let request = try #require(FanControlFile.request(at: url))
        let reason = "The SMC refused manual mode for Fan 1 (F0md: refused), and this Mac has no Ftst gate to open."
        status.value = FanHelperStatus(state: .refused, reason: reason, request: request)
        await fans.refresh()
        #expect(fans.phase == .failed(reason))
        #expect(FanControlFile.request(at: url) == nil)
    }

    @MainActor @Test func aHelperThatCannotRunSaysWhy() async {
        let status = Box<FanHelperStatus?>(nil)
        let installer = FakeInstaller()
        installer.installed.value = true
        let (fans, _) = control(FakeSMC.twoFans(), installer: installer, status: status)
        // An error from an earlier run, still in the file when the request
        // is made, says nothing about the helper now.
        status.value = FanHelperStatus(state: .error, reason: "old run",
                                       at: Date(timeIntervalSince1970: 1_799_999_990))
        await fans.refresh()
        await fans.applyManual()
        #expect(fans.phase == .starting)
        await fans.refresh()
        #expect(fans.phase == .starting)
        // The restarted helper failing after the request does.
        status.value = FanHelperStatus(state: .error, reason: "The fan helper couldn't open the SMC.",
                                       at: Date(timeIntervalSince1970: 1_800_000_005))
        await fans.refresh()
        #expect(fans.phase == .failed("The fan helper couldn't open the SMC."))
    }

    @MainActor @Test func manualModeThatNeverReadsBackTimesOutSayingSo() async throws {
        let clock = Box(Date(timeIntervalSince1970: 1_800_000_000))
        let status = Box<FanHelperStatus?>(nil)
        let installer = FakeInstaller()
        installer.installed.value = true
        let (fans, url) = control(FakeSMC.twoFans(), installer: installer, clock: clock, status: status)
        await fans.refresh()
        await fans.applyManual()
        // The helper says it holds them; the fans still read automatic.
        status.value = FanHelperStatus(state: .held, request: try #require(FanControlFile.request(at: url)))
        await fans.refresh()
        #expect(fans.phase == .starting)
        clock.value = clock.value.addingTimeInterval(FanControl.startTimeout + 1)
        await fans.refresh()
        #expect(fans.phase == .failed(FanControl.didNotStick))
    }

    @MainActor @Test func theThresholdIsClampedAndRemembered() async {
        let defaults = TestDefaults("fan")
        let url = scratch.url.appending(path: "fan-\(UUID().uuidString)/control.json")
        let fans = FanControl(control: url, reader: { [] }, status: { nil }, installer: FakeInstaller(),
                              inspect: { _ in .missing }, defaults: defaults)
        #expect(fans.threshold == 80 && !fans.thresholdEnabled)
        fans.threshold = 140
        #expect(fans.threshold == 100)
        fans.threshold = 72
        // Unpicked, the off temperature follows the line, so a restart
        // finds the same value it left rather than a shifted one.
        #expect(fans.releaseBelow == 67)
        let mid = FanControl(control: url, reader: { [] }, status: { nil }, installer: FakeInstaller(),
                             inspect: { _ in .missing }, defaults: defaults)
        #expect(mid.releaseBelow == 67)
        fans.thresholdEnabled = true
        fans.releaseBelow = 60
        let again = FanControl(control: url, reader: { [] }, status: { nil }, installer: FakeInstaller(),
                               inspect: { _ in .missing }, defaults: defaults)
        #expect(again.threshold == 72 && again.thresholdEnabled)
        #expect(again.releaseBelow == 60)
    }

    @MainActor @Test func theOffTemperatureDecidesWhereTheFansLetGo() async {
        let (fans, url, _) = await installedControl()
        fans.thresholdEnabled = true
        fans.threshold = 80
        fans.releaseBelow = 60
        await fans.checkTemperature(85)
        #expect(fans.heldByThreshold)
        // Under the line but over the off temperature, the fans stay held.
        await fans.checkTemperature(75)
        #expect(fans.heldByThreshold)
        await fans.checkTemperature(60)
        #expect(!fans.heldByThreshold)
        #expect(fans.phase == .automatic)
        #expect(FanControlFile.request(at: url) == nil)
    }

    @MainActor @Test func theOffTemperatureStaysUnderTheOnLine() async {
        let defaults = TestDefaults("fan")
        let url = scratch.url.appending(path: "fan-\(UUID().uuidString)/control.json")
        let fans = FanControl(control: url, reader: { [] }, status: { nil }, installer: FakeInstaller(),
                              inspect: { _ in .missing }, defaults: defaults)
        #expect(fans.releaseBelow == 75)
        // Unpicked, it follows the line at the default gap.
        fans.threshold = 90
        #expect(fans.releaseBelow == 85)
        // Picked, it cannot reach or pass the on line.
        fans.releaseBelow = 95
        #expect(fans.releaseBelow == 89)
        fans.releaseBelow = 60
        #expect(fans.releaseBelow == 60)
        // Moving the line under a pick drags it down with a degree to spare.
        fans.threshold = 55
        #expect(fans.releaseBelow == 54)
        // Nor under its floor.
        fans.releaseBelow = 10
        #expect(fans.releaseBelow == 30)
    }

    @MainActor @Test func removingTheHelperHandsTheFansBackFirst() async {
        let smc = FakeSMC.twoFans()
        let installer = FakeInstaller()
        installer.installed.value = true
        let (fans, url) = control(smc, installer: installer)
        await fans.refresh()
        await fans.applyManual()
        await fans.removeHelper()
        #expect(installer.uninstalls == 1)
        #expect(FanControlFile.request(at: url) == nil)
        #expect(fans.phase == .automatic)
        #expect(!fans.helperInstalled)
    }

    // MARK: Handing the fans back when something fails

    @MainActor @Test func aSpeedThatCantBeWrittenSaysWhereAndAsksForNoPassword() async throws {
        // A file where the control folder should be: no write can land,
        // even as root, so the request never exists and nothing prompts.
        let blocker = scratch.url.appending(path: "blocker-\(UUID().uuidString)")
        try Data().write(to: blocker)
        let url = blocker.appending(path: "control.json")
        let installer = FakeInstaller()
        let fans = FanControl(control: url, reader: { FanKeys.read(FakeSMC.twoFans()) }, status: { nil },
                              installer: installer, inspect: { _ in installer.state },
                              executable: "/Apps/SWE", owner: 77, defaults: TestDefaults("fan"))
        await fans.applyManual()
        guard case .failed(let message) = fans.phase else {
            Issue.record("phase is \(fans.phase), not failed")
            return
        }
        #expect(message.hasPrefix("Couldn't write \(url.path)"))
        #expect(installer.installs == 0)
        #expect(!fans.isHolding)
    }

    @MainActor @Test func aFailedRemovalStillHandsTheFansBack() async {
        let installer = FakeInstaller()
        installer.installed.value = true
        // The Starting deadline parks here; let it go when the test ends.
        let sleeper = Sleeper()
        defer { sleeper.fire() }
        let (fans, url) = control(FakeSMC.twoFans(), installer: installer, sleeper: sleeper)
        await fans.refresh()
        await fans.applyManual()
        #expect(FanControlFile.request(at: url) != nil)
        installer.error = .failed("launchctl bootout failed")
        await fans.removeHelper()
        // The request is gone before the prompt, so the helper that stays
        // behind sets the fans to automatic; the failure is still said.
        #expect(installer.uninstalls == 1)
        #expect(FanControlFile.request(at: url) == nil)
        #expect(fans.phase == .failed("launchctl bootout failed"))
        #expect(fans.helperInstalled)
        #expect(!fans.busy)
    }

    @MainActor @Test func aCancelledRemovalHandsTheFansBackAndKeepsTheHelper() async {
        let installer = FakeInstaller()
        installer.installed.value = true
        // The Starting deadline parks here; let it go when the test ends.
        let sleeper = Sleeper()
        defer { sleeper.fire() }
        let (fans, url) = control(FakeSMC.twoFans(), installer: installer, sleeper: sleeper)
        await fans.refresh()
        await fans.applyManual()
        installer.error = .cancelled
        await fans.removeHelper()
        #expect(FanControlFile.request(at: url) == nil)
        #expect(fans.phase == .automatic)
        #expect(fans.helperInstalled)
    }

    @MainActor @Test func switchingTheRuleOffMidTakeoverGivesTheFansBack() async {
        // The rule's takeover parks in its fan read; the user switches the
        // rule off meanwhile. The fans must end automatic, not held by a
        // rule that is off.
        let smc = FakeSMC.twoFans()
        let installer = FakeInstaller()
        installer.installed.value = true
        let blocking = Box(false)
        let arrived = CountGate()
        let release = DispatchSemaphore(value: 0)
        let sleeper = Sleeper()
        defer { sleeper.fire() }
        let url = scratch.url.appending(path: "fan-\(UUID().uuidString)/control.json")
        let fans = FanControl(control: url,
                              reader: {
                                  if blocking.value {
                                      Task { await arrived.bump() }
                                      _ = release.wait(timeout: .now() + 60)
                                  }
                                  return FanKeys.read(smc)
                              },
                              status: { nil }, installer: installer, inspect: { _ in installer.state },
                              executable: "/Apps/SWE", owner: 77,
                              clock: { Date(timeIntervalSince1970: 1_800_000_000) },
                              // The Starting deadline never fires, so only the
                              // switch can end the hold.
                              sleep: { await sleeper.sleep($0) },
                              defaults: TestDefaults("fan"))
        await fans.refresh()
        fans.thresholdEnabled = true
        fans.threshold = 80
        blocking.value = true
        let takeover = Task { await fans.checkTemperature(95) }
        #expect(await arrived.reach(1))
        #expect(fans.heldByThreshold)
        blocking.value = false
        fans.thresholdEnabled = false
        release.signal()
        await takeover.value
        #expect(!fans.heldByThreshold)
        #expect(fans.phase == .automatic)
        #expect(FanControlFile.request(at: url) == nil)
    }

    @Test func theInstallerRefusesBeforeAnyPromptWhenAPathOrTheCopyIsOdd() async throws {
        // Both refusals land before the hash and osascript: a nonexistent
        // executable can never be read, so no prompt can appear here.
        let pin = FanOwnerPin.cdhash(String(repeating: "ab", count: 20))
        #expect(pin.isValid)
        let installer = AdministratorInstaller(owner: { pin }, replacing: { false }, runScript: Self.noScript)
        let missing = "/nonexistent/\(UUID().uuidString)/SWE"
        await #expect(throws: FanHelperInstallError.failed("SWE's paths can't hold a line break for the fan helper.")) {
            try await installer.install(executable: missing, control: URL(fileURLWithPath: "/tmp/a\nb/control.json"))
        }
        await #expect(throws: FanHelperInstallError.failed("SWE's paths can't hold a line break for the fan helper.")) {
            try await installer.install(executable: "/Apps/S\nWE", control: URL(fileURLWithPath: "/tmp/control.json"))
        }
        await #expect(throws: FanHelperInstallError.failed("Couldn't read \(missing).")) {
            try await installer.install(executable: missing, control: URL(fileURLWithPath: "/tmp/control.json"))
        }
    }

    // MARK: - Fakes

    /// A HID stand-in that counts its reads.
    final class Thermometer: TemperatureSampling, @unchecked Sendable {
        var reading: TemperatureReading?
        private(set) var reads = 0
        init(_ reading: TemperatureReading?) { self.reading = reading }
        func read() -> TemperatureReading? {
            reads += 1
            return reading
        }
    }


    /// An SMC in a dictionary. Mode writes can be refused a number of times,
    /// and only once Ftst is 1 when the Mac has the gate, like an M4.
    final class FakeSMC: SMCAccess, @unchecked Sendable {
        private var values: [String: SMCValue] = [:]
        var refuseModeWrites = 0
        /// Mode writes back to automatic to refuse, like a busy SMC.
        var refuseReleaseWrites = 0
        /// Ftst writes to refuse, like a gate the SMC won't open.
        var refuseGateWrites = false
        var onWrite: (String, [UInt8]) -> Void = { _, _ in }

        static func twoFans(gate: Bool = true) -> FakeSMC {
            let smc = FakeSMC()
            smc.values["FNum"] = SMCValue(type: "ui8 ", bytes: [2])
            for fan in 0..<2 {
                smc.values["F\(fan)Ac"] = SMCValue(type: "flt ", bytes: SMCValue.float(fan == 0 ? 2319 : 2509))
                smc.values["F\(fan)Mn"] = SMCValue(type: "flt ", bytes: SMCValue.float(2317))
                smc.values["F\(fan)Mx"] = SMCValue(type: "flt ", bytes: SMCValue.float(7826))
                smc.values["F\(fan)Tg"] = SMCValue(type: "flt ", bytes: SMCValue.float(2317))
                smc.values["F\(fan)Md"] = SMCValue(type: "ui8 ", bytes: [0])
            }
            if gate { smc.values["Ftst"] = SMCValue(type: "ui8 ", bytes: [0]) }
            return smc
        }

        func number(_ key: String) -> Double? { values[key]?.number }
        func put(_ key: String, _ celsius: Double) { values[key] = SMCValue(type: "flt ", bytes: SMCValue.float(celsius)) }
        func set(_ key: String, _ value: UInt8) { values[key]?.bytes = [value] }

        func replace(_ key: String, _ value: SMCValue) { values[key] = value }

        func rename(_ key: String, to other: String) {
            values[other] = values.removeValue(forKey: key)
        }

        func read(_ key: String) -> SMCValue? { values[key] }

        @discardableResult
        func write(_ key: String, _ bytes: [UInt8]) -> Bool {
            guard let old = values[key], old.bytes.count == bytes.count else { return false }
            if key == "Ftst" && refuseGateWrites { return false }
            // Going manual is gated; going back to automatic never is.
            if key.hasPrefix("F"), key.lowercased().hasSuffix("md"), bytes == [1] {
                let gated = values["Ftst"] != nil
                if gated && values["Ftst"]?.bytes != [1] && refuseModeWrites > 0 { return false }
                if refuseModeWrites > 0 {
                    refuseModeWrites -= 1
                    return false
                }
            }
            if key.hasPrefix("F"), key.lowercased().hasSuffix("md"), bytes == [0], refuseReleaseWrites > 0 {
                refuseReleaseWrites -= 1
                return false
            }
            values[key]?.bytes = bytes
            onWrite(key, bytes)
            return true
        }
    }

    final class FakeInstaller: FanHelperInstalling, @unchecked Sendable {
        let installed = Box(false)
        /// An older helper is installed, until an install replaces it.
        let outdated = Box(false)
        var checks = 0
        var state: FanHelperState {
            installed.value ? .current : outdated.value ? .outdated(version: 3) : .missing
        }
        var installs = 0
        var uninstalls = 0
        var lastControl: URL?
        var error: FanHelperInstallError?

        /// Runs while the install "prompt" is up.
        var duringInstall: (@MainActor () async -> Void)?

        func install(executable: String, control: URL) async throws {
            installs += 1
            lastControl = control
            await duringInstall?()
            if let error { throw error }
            installed.value = true
            outdated.value = false
        }

        /// Runs while the removal "prompt" is up.
        var duringUninstall: (@MainActor () async -> Void)?

        func uninstall() async throws {
            uninstalls += 1
            await duringUninstall?()
            if let error { throw error }
            installed.value = false
        }
    }

    /// Stands in for the Starting deadline's sleep: every sleep waits
    /// until `fire`, and any sleep after it returns at once.
    final class Sleeper: @unchecked Sendable {
        private let lock = NSLock()
        private var waiting: [CheckedContinuation<Void, Never>] = []
        private var fired = false

        func sleep(_ seconds: TimeInterval) async {
            await withCheckedContinuation { continuation in
                let now = lock.withLock {
                    if !fired { waiting.append(continuation) }
                    return fired
                }
                if now { continuation.resume() }
            }
        }

        func fire() {
            let all = lock.withLock {
                fired = true
                defer { waiting = [] }
                return waiting
            }
            all.forEach { $0.resume() }
        }
    }
}
