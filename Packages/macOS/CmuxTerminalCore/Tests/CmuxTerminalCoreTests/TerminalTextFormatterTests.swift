import Foundation
import XCTest
@testable import CmuxTerminalCore

final class TerminalTextFormatterTests: XCTestCase {
    func testTailLinesPreservesSplitSuffixSemanticsWithoutFullSplit() {
        XCTAssertEqual(TerminalTextFormatter.tailLines("a\nb\nc", maxLines: 2), "b\nc")
        XCTAssertEqual(TerminalTextFormatter.tailLines("a\nb\n", maxLines: 2), "b\n")
        XCTAssertEqual(TerminalTextFormatter.tailLines("a", maxLines: 2), "a")
        XCTAssertEqual(TerminalTextFormatter.tailLines("a\nb", maxLines: 0), "")
    }

    func testPayloadTailsScrollbackBeforeEncoding() throws {
        let result = TerminalTextFormatter.payload(
            from: TerminalTextSnapshot(
                viewport: nil,
                screen: "old\nscreen",
                history: "one\ntwo\nthree",
                active: "four\nfive"
            ),
            includeScrollback: true,
            lineLimit: 3
        )
        let payload = try result.get()

        XCTAssertEqual(payload.text, "three\nfour\nfive")
        XCTAssertEqual(payload.base64, Data("three\nfour\nfive".utf8).base64EncodedString())
    }

    func testOutputReturnsPlainViewportWithoutEncodingRoundTrip() throws {
        let result = TerminalTextFormatter.output(
            from: TerminalTextSnapshot(
                viewport: "prompt λ\nresult ✓",
                screen: nil,
                history: nil,
                active: nil
            ),
            includeScrollback: false,
            lineLimit: 1
        )

        XCTAssertEqual(try result.get(), "result ✓")
    }

    func testBase64ResponsePreservesSocketBytes() {
        let response = TerminalTextFormatter.base64Response(
            from: TerminalTextSnapshot(
                viewport: "prompt λ\nresult ✓",
                screen: nil,
                history: nil,
                active: nil
            ),
            includeScrollback: false,
            lineLimit: nil
        )

        XCTAssertEqual(response, "OK cHJvbXB0IM67CnJlc3VsdCDinJM=")
    }

    func testOutputAndBase64ResponsePreserveEmptyText() throws {
        let snapshot = TerminalTextSnapshot(
            viewport: "",
            screen: nil,
            history: nil,
            active: nil
        )

        XCTAssertEqual(
            try TerminalTextFormatter.output(
                from: snapshot,
                includeScrollback: false,
                lineLimit: nil
            ).get(),
            ""
        )
        XCTAssertEqual(
            TerminalTextFormatter.base64Response(
                from: snapshot,
                includeScrollback: false,
                lineLimit: nil
            ),
            "OK "
        )
    }

    func testDecodeReadsBorrowedSelectionBytes() {
        let bytes = Array("selected λ".utf8)

        let decoded = bytes.withUnsafeBytes { buffer in
            TerminalTextFormatter.decode(buffer)
        }

        XCTAssertEqual(decoded, "selected λ")
    }

    func testDecodePreservesInvalidUTF8ReplacementSemantics() {
        let bytes: [UInt8] = [0x66, 0x80, 0x6f]

        let decoded = bytes.withUnsafeBytes { buffer in
            TerminalTextFormatter.decode(buffer)
        }

        XCTAssertEqual(decoded, "f\u{FFFD}o")
    }
}
