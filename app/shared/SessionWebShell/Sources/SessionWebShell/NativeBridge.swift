import Foundation
import WebKit

/// A call the session page made on `window.sweNative`. Decode only for
/// now: what each does in the app comes with the embeds (G17, G18).
public enum BridgeMessage: Equatable, Sendable {
    /// The page lost its connection and wants the app to check the
    /// network and nudge it (the client supervisor's native wake).
    case wake
    /// Open another session, by its session key.
    case open(key: String)
    /// Leave the session view (the native back button's job).
    case back
}

public enum BridgeError: Error, Equatable, Sendable {
    case notAnObject
    case badVersion
    case unknownType(String)
    case badKey
    /// From a frame other than the pinned hub's main frame.
    case untrustedFrame
}

public enum NativeBridge {
    /// The message handler name: `window.webkit.messageHandlers.sweNative`.
    public static let handlerName = "sweNative"
    public static let version = 1
    static let maxKeyLength = 256

    /// Installed at document start in the page world. Defines a frozen
    /// `window.sweNative` whose methods post `{v, type, key?}`; it carries
    /// no state and no secret.
    public static let userScript = """
    (() => {
      const post = (body) => {
        try { window.webkit.messageHandlers.\(handlerName).postMessage(Object.assign({ v: \(version) }, body)); }
        catch (_) {}
      };
      Object.defineProperty(window, 'sweNative', {
        value: Object.freeze({
          wake() { post({ type: 'wake' }); },
          open(key) { post({ type: 'open', key: String(key) }); },
          back() { post({ type: 'back' }); },
        }),
        writable: false, configurable: false,
      });
    })();
    """

    /// Decodes a posted body (`WKScriptMessage.body`: a dictionary from JS).
    public static func decode(_ body: Any) throws(BridgeError) -> BridgeMessage {
        guard let object = body as? [String: Any] else { throw .notAnObject }
        guard let version = (object["v"] as? NSNumber)?.intValue, version == Self.version else {
            throw .badVersion
        }
        let type = object["type"] as? String ?? ""
        switch type {
        case "wake": return .wake
        case "back": return .back
        case "open":
            guard let key = object["key"] as? String, isValidKey(key) else { throw .badKey }
            return .open(key: key)
        default:
            throw .unknownType(type)
        }
    }

    /// Only the page's main frame on the pinned hub may call the bridge.
    public static func isTrusted(scheme: String, host: String, port: Int, isMainFrame: Bool,
                                 endpoint: HubEndpoint) -> Bool {
        isMainFrame && scheme.lowercased() == HubEndpoint.scheme
            && host.lowercased() == endpoint.id && port == 0
    }

    static func isValidKey(_ key: String) -> Bool {
        guard (1...maxKeyLength).contains(key.count) else { return false }
        return key.unicodeScalars.allSatisfy { !CharacterSet.controlCharacters.contains($0) }
            && key.trimmingCharacters(in: .whitespaces) == key
    }
}

/// Receives `sweNative` posts, checks the frame, decodes, and hands the
/// result to `onMessage`.
@MainActor
public final class BridgeReceiver: NSObject, WKScriptMessageHandler {
    public let endpoint: HubEndpoint
    public var onMessage: (Result<BridgeMessage, BridgeError>) -> Void

    public init(endpoint: HubEndpoint, onMessage: @escaping (Result<BridgeMessage, BridgeError>) -> Void) {
        self.endpoint = endpoint
        self.onMessage = onMessage
    }

    public func userContentController(_ userContentController: WKUserContentController,
                                      didReceive message: WKScriptMessage) {
        let origin = message.frameInfo.securityOrigin
        guard NativeBridge.isTrusted(scheme: origin.protocol, host: origin.host, port: origin.port,
                                     isMainFrame: message.frameInfo.isMainFrame, endpoint: endpoint)
        else {
            onMessage(.failure(.untrustedFrame))
            return
        }
        do {
            onMessage(.success(try NativeBridge.decode(message.body)))
        } catch {
            onMessage(.failure(error))
        }
    }
}
