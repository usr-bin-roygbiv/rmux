import Foundation

public struct TerminalTextSnapshot: Equatable, Sendable {
    public var viewport: String?
    public var screen: String?
    public var history: String?
    public var active: String?

    public init(
        viewport: String?,
        screen: String?,
        history: String?,
        active: String?
    ) {
        self.viewport = viewport
        self.screen = screen
        self.history = history
        self.active = active
    }
}

public struct TerminalTextPayload: Equatable, Sendable {
    public let text: String
    public let base64: String

    public init(text: String, base64: String) {
        self.text = text
        self.base64 = base64
    }
}

public struct TerminalTextFormattingError: Error, Equatable, Sendable {
    public let message: String

    public init(message: String) {
        self.message = message
    }
}

public enum TerminalTextFormatter {
    public static func decode(_ bytes: UnsafeRawBufferPointer) -> String {
        // The caller owns the borrowed buffer. String consumes it synchronously
        // and returns owned storage, so no intermediate Data copy is needed.
        String(decoding: bytes, as: UTF8.self)
    }

    public static func output(
        from snapshot: TerminalTextSnapshot,
        includeScrollback: Bool,
        lineLimit: Int?
    ) -> Result<String, TerminalTextFormattingError> {
        let output: String
        if includeScrollback {
            var candidates: [String] = []
            if let screen = snapshot.screen {
                candidates.append(lineLimit.map { tailLines(screen, maxLines: $0) } ?? screen)
            }
            if snapshot.history != nil || snapshot.active != nil {
                var merged = lineLimit.map {
                    tailLines(snapshot.history ?? "", maxLines: $0)
                } ?? (snapshot.history ?? "")
                if let active = snapshot.active {
                    if !merged.isEmpty, !merged.hasSuffix("\n"), !active.isEmpty {
                        merged.append("\n")
                    }
                    merged.append(lineLimit.map { tailLines(active, maxLines: $0) } ?? active)
                }
                candidates.append(lineLimit.map { tailLines(merged, maxLines: $0) } ?? merged)
            }

            guard let best = candidates.max(by: { lhs, rhs in
                let left = candidateScore(lhs)
                let right = candidateScore(rhs)
                if left.lines != right.lines {
                    return left.lines < right.lines
                }
                return left.bytes < right.bytes
            }) else {
                return .failure(TerminalTextFormattingError(message: "Failed to read terminal text"))
            }
            output = best
        } else {
            guard var viewport = snapshot.viewport else {
                return .failure(TerminalTextFormattingError(message: "Failed to read terminal text"))
            }
            if let lineLimit {
                viewport = tailLines(viewport, maxLines: lineLimit)
            }
            output = viewport
        }

        return .success(output)
    }

    public static func payload(
        from snapshot: TerminalTextSnapshot,
        includeScrollback: Bool,
        lineLimit: Int?
    ) -> Result<TerminalTextPayload, TerminalTextFormattingError> {
        switch output(
            from: snapshot,
            includeScrollback: includeScrollback,
            lineLimit: lineLimit
        ) {
        case .success(let output):
            let base64 = Data(output.utf8).base64EncodedString()
            return .success(TerminalTextPayload(text: output, base64: base64))
        case .failure(let error):
            return .failure(error)
        }
    }

    public static func base64Response(
        from snapshot: TerminalTextSnapshot,
        includeScrollback: Bool,
        lineLimit: Int?
    ) -> String {
        switch payload(
            from: snapshot,
            includeScrollback: includeScrollback,
            lineLimit: lineLimit
        ) {
        case .success(let payload):
            return "OK \(payload.base64)"
        case .failure(let error):
            return "ERROR: \(error.message)"
        }
    }

    public static func tailLines(_ text: String, maxLines: Int) -> String {
        guard maxLines > 0 else { return "" }
        var newlineCount = 0
        var index = text.endIndex
        while index > text.startIndex {
            let previous = text.index(before: index)
            if text[previous] == "\n" {
                newlineCount += 1
                if newlineCount == maxLines {
                    return String(text[index...])
                }
            }
            index = previous
        }
        return text
    }

    private static func candidateScore(_ text: String) -> (lines: Int, bytes: Int) {
        if text.isEmpty { return (0, 0) }
        var newlineCount = 0
        var byteCount = 0
        for byte in text.utf8 {
            byteCount += 1
            if byte == 0x0A {
                newlineCount += 1
            }
        }
        return (newlineCount + 1, byteCount)
    }
}
